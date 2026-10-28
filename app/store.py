"""In-memory request records, atomic admission and conditional state transitions.

Every method is synchronous and never awaits, so on a single event loop each
call is atomic: no other coroutine can observe or modify a half-applied
transition. This gives the guarantees of a store lock without the risk of
awaiting one from a cancelled worker.
"""

import asyncio
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable

from models import FailureCode, RequestSnapshot, Status


class AdmitResult(Enum):
    ACCEPTED = "accepted"
    ALREADY_ACTIVE = "already_active"
    TERMINAL = "terminal"


class RequestNotFound(LookupError):
    pass


class StoreFull(RuntimeError):
    pass


class QueueFull(RuntimeError):
    pass


class ShuttingDown(RuntimeError):
    pass


class IdempotencyMismatch(ValueError):
    """An idempotency key was reused with a different input."""


@dataclass
class StoreStats:
    """Monotonic counters; they survive eviction, unlike the records themselves."""

    created: int = 0
    replayed: int = 0
    admitted: int = 0
    evicted: int = 0
    ai_attempts: int = 0
    notify_attempts: int = 0
    outcomes: Counter = field(default_factory=Counter)  # (status, failure code or "") -> count
    terminal_seconds_sum: float = 0.0  # admission -> terminal, admitted work only
    terminal_seconds_count: int = 0


@dataclass
class _Record:
    snapshot: RequestSnapshot
    idempotency_key: str | None = None

    def update(self, **changes) -> RequestSnapshot:
        self.snapshot = replace(self.snapshot, **changes)
        return self.snapshot


class RequestStore:
    def __init__(self, capacity: int, unadmitted_ttl: float, job_deadline: float, clock: Callable[[], float]):
        self._capacity = capacity
        self._unadmitted_ttl = unadmitted_ttl
        self._job_deadline = job_deadline
        self._clock = clock
        self._records: dict[str, _Record] = {}
        self._terminal: OrderedDict[str, None] = OrderedDict()  # finish order, oldest first
        self._keys: dict[str, str] = {}  # idempotency key -> request id, bounded by the records
        self._closed = False
        self.stats = StoreStats()

    def __len__(self) -> int:
        return len(self._records)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Stop accepting new requests and new admissions."""
        self._closed = True

    def create(self, user_input: str) -> RequestSnapshot:
        if self._closed:
            raise ShuttingDown()
        if len(self._records) >= self._capacity:
            self.expire()
            if not self._terminal:
                raise StoreFull()
            oldest, _ = self._terminal.popitem(last=False)
            evicted = self._records.pop(oldest)
            self._keys.pop(evicted.idempotency_key, None)
            self.stats.evicted += 1
        snapshot = RequestSnapshot(
            id=str(uuid.uuid4()),
            user_input=user_input,
            status=Status.QUEUED,
            admitted=False,
            created_at=self._clock(),
        )
        self._records[snapshot.id] = _Record(snapshot)
        self.stats.created += 1
        return snapshot

    def create_idempotent(self, user_input: str, key: str) -> tuple[RequestSnapshot, bool]:
        """Create once per key; a repeat with the same input returns the existing record (``True``)."""
        request_id = self._keys.get(key)
        if request_id is not None:
            snapshot = self._records[request_id].snapshot
            if snapshot.user_input != user_input:
                raise IdempotencyMismatch()
            self.stats.replayed += 1
            return snapshot, True
        snapshot = self.create(user_input)
        self._records[snapshot.id].idempotency_key = key
        self._keys[key] = snapshot.id
        return snapshot, False

    def get(self, request_id: str) -> RequestSnapshot | None:
        record = self._records.get(request_id)
        return record.snapshot if record else None

    def admit(self, request_id: str, queue: asyncio.Queue) -> tuple[AdmitResult, RequestSnapshot]:
        record = self._records.get(request_id)
        if record is None:
            raise RequestNotFound(request_id)
        snap = record.snapshot
        if snap.status.terminal:
            return AdmitResult.TERMINAL, snap
        if snap.admitted:
            return AdmitResult.ALREADY_ACTIVE, snap
        if self._closed:
            raise ShuttingDown()
        try:
            queue.put_nowait(request_id)
        except asyncio.QueueFull:
            raise QueueFull() from None
        now = self._clock()
        self.stats.admitted += 1
        return AdmitResult.ACCEPTED, record.update(admitted=True, admitted_at=now, deadline=now + self._job_deadline)

    def start(self, request_id: str) -> RequestSnapshot | None:
        """Move admitted queued work to processing; ``None`` means skip this ID."""
        record = self._records.get(request_id)
        if record is None or record.snapshot.status is not Status.QUEUED or not record.snapshot.admitted:
            return None
        return record.update(status=Status.PROCESSING)

    def finish(
        self,
        request_id: str,
        status: Status,
        *,
        failure_code: FailureCode | None = None,
        provider_id: str | None = None,
        ai_attempts: int | None = None,
        notify_attempts: int | None = None,
    ) -> bool:
        """Record a terminal outcome unless the request already has one."""
        if not status.terminal:
            raise ValueError("finish requires a terminal status")
        record = self._records.get(request_id)
        if record is None or record.snapshot.status.terminal:
            return False
        changes = {"status": status, "finished_at": self._clock(), "failure_code": failure_code, "provider_id": provider_id}
        if ai_attempts is not None:
            changes["ai_attempts"] = ai_attempts
        if notify_attempts is not None:
            changes["notify_attempts"] = notify_attempts
        snap = record.update(**changes)
        self._terminal[request_id] = None
        self._count_outcome(snap)
        return True

    def _count_outcome(self, snap: RequestSnapshot) -> None:
        stats = self.stats
        stats.outcomes[(snap.status.value, snap.failure_code.value if snap.failure_code else "")] += 1
        stats.ai_attempts += snap.ai_attempts
        stats.notify_attempts += snap.notify_attempts
        if snap.admitted_at is not None:
            stats.terminal_seconds_sum += snap.finished_at - snap.admitted_at
            stats.terminal_seconds_count += 1

    def expire(self) -> list[str]:
        """Fail abandoned unadmitted records and admitted work still queued past its deadline."""
        now = self._clock()
        expired = []
        for request_id, record in list(self._records.items()):
            snap = record.snapshot
            if snap.status is not Status.QUEUED:
                continue
            if not snap.admitted and now - snap.created_at >= self._unadmitted_ttl:
                self.finish(request_id, Status.FAILED, failure_code=FailureCode.UNADMITTED_EXPIRED)
                expired.append(request_id)
            elif snap.admitted and snap.deadline is not None and now >= snap.deadline:
                self.finish(request_id, Status.FAILED, failure_code=FailureCode.DEADLINE_EXCEEDED)
                expired.append(request_id)
        return expired

    def fail_outstanding(self) -> list[str]:
        """Shutdown accounting: every nonterminal record becomes failed."""
        failed = []
        for request_id, record in list(self._records.items()):
            status = record.snapshot.status
            if status.terminal:
                continue
            # A processing job may already have transmitted its notification.
            code = FailureCode.DELIVERY_UNKNOWN if status is Status.PROCESSING else FailureCode.SHUTDOWN
            self.finish(request_id, Status.FAILED, failure_code=code)
            failed.append(request_id)
        return failed

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in Status}
        for record in self._records.values():
            counts[record.snapshot.status.value] += 1
        return counts
