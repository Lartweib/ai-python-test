"""Bounded worker pool: extraction -> guardrails -> paced notification.

Workers own a whole job. A shared ``NotificationGate`` paces every notify
attempt across workers, so waiting for notification capacity naturally applies
backpressure to extraction.
"""

import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from extraction import RETRY_FEEDBACK, ExtractionError, extract_notification
from models import FailureCode, Notification, Status
from provider_client import AIError, NotifyError, NotifyOutcome, ProviderClient
from settings import Settings
from store import RequestStore

logger = logging.getLogger("notify")

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Emit one JSON log line. Callers pass IDs, counters and fixed codes only."""
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


class GateDeadline(Exception):
    """The next notification slot opens after the job's deadline."""


class NotificationGate:
    """Rolling-window limiter with even pacing and a shared 429 cooldown.

    ``acquire`` never awaits while deciding, so its check-and-reserve step is
    atomic on the event loop; after every sleep it re-evaluates from scratch.
    Reservations are never refunded: the provider counts failed attempts too.
    """

    def __init__(self, clock: Clock, sleep: Sleep, window: float, limit: int, min_interval: float, cooldown: float):
        self._clock = clock
        self._sleep = sleep
        self._window = window
        self._limit = limit
        self._min_interval = min_interval
        self._cooldown = cooldown
        self._cooldown_until = float("-inf")
        self.reservations: deque[float] = deque()

    def _wait_time(self, now: float) -> float:
        while self.reservations and now - self.reservations[0] >= self._window:
            self.reservations.popleft()
        wait = self._cooldown_until - now
        if self.reservations:
            wait = max(wait, self.reservations[-1] + self._min_interval - now)
        if len(self.reservations) >= self._limit:
            wait = max(wait, self.reservations[0] + self._window - now)
        return wait

    async def acquire(self, deadline: float) -> None:
        while True:
            now = self._clock()
            wait = self._wait_time(now)
            if wait <= 0:
                self.reservations.append(now)
                return
            if now + wait > deadline:
                raise GateDeadline()
            await self._sleep(wait)

    def penalize(self, retry_after: float | None = None) -> None:
        """Pause every worker after a 429; provider guidance may only lengthen the pause."""
        delay = max(self._cooldown, retry_after or 0.0)
        self._cooldown_until = max(self._cooldown_until, self._clock() + delay)


class _JobFailure(Exception):
    def __init__(self, code: FailureCode):
        super().__init__(code.value)
        self.code = code


@dataclass
class _JobState:
    ai_attempts: int = 0
    notify_attempts: int = 0
    transmitting: bool = False  # a notify request may have reached the provider


class Pipeline:
    def __init__(
        self,
        store: RequestStore,
        queue: asyncio.Queue,
        client: ProviderClient,
        settings: Settings,
        clock: Clock,
        sleep: Sleep,
        rand: Callable[[], float] = random.random,
    ):
        self._store = store
        self._queue = queue
        self._client = client
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self.gate = NotificationGate(
            clock=clock,
            sleep=sleep,
            window=settings.notify_window_seconds,
            limit=settings.notify_window_limit,
            min_interval=settings.notify_min_interval_seconds,
            cooldown=settings.notify_cooldown_seconds,
        )
        self.tasks: list[asyncio.Task] = []
        self._stopping = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        for n in range(self._settings.workers):
            task = asyncio.create_task(self._worker(), name=f"notify-worker-{n}")
            task.add_done_callback(self._on_worker_done)
            self.tasks.append(task)
        self.tasks.append(asyncio.create_task(self._sweeper(), name="notify-sweeper"))
        log_event("pipeline_started", workers=self._settings.workers, queue_capacity=self._queue.maxsize)

    async def stop(self) -> None:
        """Close admission, drain for the grace period, then cancel and account for the rest."""
        if self._stopping:
            return
        self._stopping = True
        self._store.close()
        log_event("pipeline_draining", queue_depth=self._queue.qsize(), counts=self._store.counts())
        try:
            async with asyncio.timeout(self._settings.shutdown_grace_seconds):
                await self._queue.join()
        except TimeoutError:
            pass
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        failed = self._store.fail_outstanding()
        log_event("pipeline_stopped", failed_on_shutdown=len(failed), counts=self._store.counts())

    def _on_worker_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or self._stopping:
            return
        # A worker must never exit on its own; refuse new work rather than run degraded.
        exc = task.exception()
        log_event("worker_crashed", logging.CRITICAL, worker=task.get_name(), error=type(exc).__name__)
        self._store.close()
        self._store.fail_outstanding()

    async def _worker(self) -> None:
        while True:
            request_id = await self._queue.get()
            try:
                await self._run_job(request_id)
            except Exception as exc:  # defensive: _run_job already isolates job errors
                log_event("worker_error", logging.ERROR, request_id=request_id, error=type(exc).__name__)
            finally:
                self._queue.task_done()

    async def _sweeper(self) -> None:
        # Real-time interval; expiry itself uses the injected clock.
        while True:
            await asyncio.sleep(self._settings.sweep_interval_seconds)
            expired = self._store.expire()
            if expired:
                log_event("requests_expired", count=len(expired), queue_depth=self._queue.qsize())
            counts = self._store.counts()
            if counts["processing"] or self._queue.qsize():
                # Backlog/drain evidence: the HTTP status checks alone cannot show it.
                log_event("state_summary", counts=counts, queue_depth=self._queue.qsize())

    # -- job execution -------------------------------------------------

    async def _run_job(self, request_id: str) -> None:
        snapshot = self._store.start(request_id)
        if snapshot is None:
            return  # expired or already terminal: a tombstone in the queue
        started = self._clock()
        state = _JobState()
        status, code, provider_id = Status.FAILED, None, None
        try:
            remaining = snapshot.deadline - started
            if remaining <= 0:
                raise _JobFailure(FailureCode.DEADLINE_EXCEEDED)
            async with asyncio.timeout(remaining):
                notification = await self._extract(snapshot.user_input, state)
                provider_id = await self._deliver(notification, state, snapshot.deadline)
            status = Status.SENT
        except _JobFailure as failure:
            code = failure.code
        except TimeoutError:
            code = FailureCode.DELIVERY_UNKNOWN if state.transmitting else FailureCode.DEADLINE_EXCEEDED
        except asyncio.CancelledError:
            code = FailureCode.DELIVERY_UNKNOWN if state.transmitting else FailureCode.SHUTDOWN
            self._finish(request_id, status, code, provider_id, state, started)
            raise
        except Exception as exc:
            code = FailureCode.INTERNAL_ERROR
            log_event("job_internal_error", logging.ERROR, request_id=request_id, error=type(exc).__name__)
        self._finish(request_id, status, code, provider_id, state, started)

    def _finish(self, request_id, status, code, provider_id, state: _JobState, started: float) -> None:
        self._store.finish(
            request_id,
            status,
            failure_code=code,
            provider_id=provider_id,
            ai_attempts=state.ai_attempts,
            notify_attempts=state.notify_attempts,
        )
        log_event(
            "job_finished",
            request_id=request_id,
            status=status.value,
            failure_code=code.value if code else None,
            ai_attempts=state.ai_attempts,
            notify_attempts=state.notify_attempts,
            duration_ms=round((self._clock() - started) * 1000, 1),
            queue_depth=self._queue.qsize(),
        )

    async def _backoff(self) -> None:
        s = self._settings
        await self._sleep(s.retry_base_delay_seconds + self._rand() * s.retry_jitter_seconds)

    async def _extract(self, user_input: str, state: _JobState) -> Notification:
        """At most ``ai_max_attempts`` calls shared by transport, envelope and syntax failures."""
        feedback = None
        max_attempts = self._settings.ai_max_attempts
        while True:
            state.ai_attempts += 1
            last = state.ai_attempts >= max_attempts
            try:
                content = await self._client.extract(user_input, feedback)
                return extract_notification(content)
            except AIError as err:
                log_event("ai_attempt_failed", attempt=state.ai_attempts, reason=err.reason, retryable=err.retryable)
                if not err.retryable or last:
                    raise _JobFailure(err.code) from None
                await self._backoff()
            except ExtractionError as err:
                log_event("extraction_rejected", attempt=state.ai_attempts, code=err.code.value, reason=err.reason)
                if not err.retryable or last:
                    raise _JobFailure(err.code) from None
                feedback = RETRY_FEEDBACK

    async def _deliver(self, notification: Notification, state: _JobState, deadline: float) -> str:
        """Resend only after a definitive 429 or a proven pre-send failure; never re-extract."""
        max_attempts = self._settings.notify_max_attempts
        while True:
            try:
                await self.gate.acquire(deadline)
            except GateDeadline:
                code = FailureCode.NOTIFICATION_UNAVAILABLE if state.notify_attempts else FailureCode.DEADLINE_EXCEEDED
                raise _JobFailure(code) from None
            state.notify_attempts += 1
            state.transmitting = True
            try:
                return await self._client.notify(notification)
            except NotifyError as err:
                log_event("notify_attempt_failed", attempt=state.notify_attempts, outcome=err.outcome.value, reason=err.reason)
                if err.outcome is NotifyOutcome.UNKNOWN:
                    raise _JobFailure(FailureCode.DELIVERY_UNKNOWN) from None
                state.transmitting = False
                if err.outcome is NotifyOutcome.REJECTED:
                    raise _JobFailure(FailureCode.NOTIFICATION_REJECTED) from None
                if state.notify_attempts >= max_attempts:
                    raise _JobFailure(FailureCode.NOTIFICATION_UNAVAILABLE) from None
                if err.outcome is NotifyOutcome.RATE_LIMITED:
                    self.gate.penalize(err.retry_after)
                else:
                    await self._backoff()
