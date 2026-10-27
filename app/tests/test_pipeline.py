import asyncio
import json
import unittest

import httpx

from support import FakeClock, ScriptedUpstream, ai_body, canonical_json, test_settings, wait_for
from models import FailureCode, Status
from pipeline import GateDeadline, NotificationGate, Pipeline
from provider_client import ProviderClient, build_http_client
from store import RequestStore

DELIVERED = httpx.Response(200, json={"status": "delivered", "provider_id": "p-7"})


class NotificationGateTest(unittest.IsolatedAsyncioTestCase):
    def make_gate(self, **kw):
        self.clock = FakeClock()
        params = dict(window=10.1, limit=50, min_interval=0.21, cooldown=10.1)
        params.update(kw)
        return NotificationGate(clock=self.clock, sleep=self.clock.sleep, **params)

    async def test_first_acquire_is_immediate(self):
        gate = self.make_gate()
        await gate.acquire(deadline=self.clock.now + 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(gate.reservations), 1)

    async def test_minimum_spacing_between_dispatches(self):
        gate = self.make_gate()
        times = []
        for _ in range(5):
            await gate.acquire(deadline=self.clock.now + 100)
            times.append(self.clock.now)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.21 - 1e-9 for g in gaps), gaps)

    async def test_sliding_window_never_exceeds_limit_under_concurrency(self):
        gate = self.make_gate(min_interval=0.0)
        dispatched = []

        async def worker():
            for _ in range(15):
                await gate.acquire(deadline=self.clock.now + 1000)
                dispatched.append(self.clock.now)

        await asyncio.gather(*(worker() for _ in range(10)))
        self.assertEqual(len(dispatched), 150)
        dispatched.sort()
        for i, start in enumerate(dispatched):
            in_window = [t for t in dispatched[i:] if t - start < 10.1]
            self.assertLessEqual(len(in_window), 50)

    async def test_default_pacing_respects_provider_limit(self):
        gate = self.make_gate()
        dispatched = []
        for _ in range(120):
            await gate.acquire(deadline=self.clock.now + 1000)
            dispatched.append(self.clock.now)
        for i, start in enumerate(dispatched):
            self.assertLessEqual(len([t for t in dispatched[i:] if t - start < 10.0]), 50)

    async def test_cooldown_is_shared_and_never_shorter_than_fallback(self):
        gate = self.make_gate()
        await gate.acquire(deadline=self.clock.now + 100)
        hit_at = self.clock.now
        gate.penalize(retry_after=1.0)  # shorter guidance never shortens fallback
        await gate.acquire(deadline=self.clock.now + 100)
        self.assertGreaterEqual(self.clock.now - hit_at, 10.1)

    async def test_longer_retry_after_is_honoured(self):
        gate = self.make_gate()
        start = self.clock.now
        gate.penalize(retry_after=30.0)
        await gate.acquire(deadline=self.clock.now + 100)
        self.assertGreaterEqual(self.clock.now - start, 30.0)

    async def test_wait_beyond_deadline_fails_without_reservation(self):
        gate = self.make_gate()
        gate.penalize(retry_after=30.0)
        with self.assertRaises(GateDeadline):
            await gate.acquire(deadline=self.clock.now + 5)
        self.assertEqual(len(gate.reservations), 0)


class PipelineCase(unittest.IsolatedAsyncioTestCase):
    async def start_pipeline(self, upstream, **overrides):
        self.clock = FakeClock()
        self.settings = test_settings(**overrides)
        self.store = RequestStore(
            capacity=self.settings.record_capacity,
            unadmitted_ttl=self.settings.unadmitted_ttl_seconds,
            job_deadline=self.settings.job_deadline_seconds,
            clock=self.clock,
        )
        self.queue = asyncio.Queue(maxsize=self.settings.queue_capacity)
        self.http = build_http_client(self.settings, transport=upstream.transport())
        self.pipeline = Pipeline(
            store=self.store,
            queue=self.queue,
            client=ProviderClient(self.http, self.settings),
            settings=self.settings,
            clock=self.clock,
            sleep=self.clock.sleep,
            rand=lambda: 0.5,
        )
        self.pipeline.start()

        async def cleanup():
            await self.pipeline.stop()
            await self.http.aclose()

        self.addAsyncCleanup(cleanup)
        self.upstream = upstream

    async def run_one(self, user_input="Manda un mail a ana@test.com diciendo Hola Ana"):
        snap = self.store.create(user_input)
        self.store.admit(snap.id, self.queue)
        await wait_for(lambda: self.store.get(snap.id).status.terminal)
        return self.store.get(snap.id)


class HappyPathTest(PipelineCase):
    async def test_extract_then_notify_marks_sent_with_receipt(self):
        await self.start_pipeline(ScriptedUpstream(notify=[DELIVERED]))
        done = await self.run_one()
        self.assertEqual(done.status, Status.SENT)
        self.assertEqual(done.provider_id, "p-7")
        self.assertEqual((done.ai_attempts, done.notify_attempts), (1, 1))
        self.assertEqual(
            json.loads(self.upstream.notify_requests[0].content),
            {"to": "ana@test.com", "message": "Hola Ana", "type": "email"},
        )

    async def test_job_transitions_through_processing(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        await self.start_pipeline(upstream)
        snap = self.store.create("x")
        self.store.admit(snap.id, self.queue)
        await wait_for(lambda: self.store.get(snap.id).status is Status.PROCESSING)
        upstream.ai_gate.set()
        await wait_for(lambda: self.store.get(snap.id).status is Status.SENT)


class ExtractionRetryTest(PipelineCase):
    async def test_transient_ai_failure_then_success(self):
        await self.start_pipeline(ScriptedUpstream(ai=[httpx.Response(503)]))
        done = await self.run_one()
        self.assertEqual(done.status, Status.SENT)
        self.assertEqual(done.ai_attempts, 2)
        self.assertIn(0.25 + 0.5 * 0.25, self.clock.sleeps)  # base + jitter

    async def test_two_retryable_failures_exhaust_shared_budget(self):
        upstream = ScriptedUpstream(ai=[httpx.ConnectError("x"), httpx.Response(200, json=ai_body("{to: 'broken"))])
        await self.start_pipeline(upstream)
        done = await self.run_one()
        self.assertEqual(done.status, Status.FAILED)
        self.assertEqual(len(upstream.ai_requests), 2)
        self.assertEqual(len(upstream.notify_requests), 0)

    async def test_syntax_failure_retries_with_feedback_and_same_input(self):
        text = "Manda un mail a ana@test.com: Hola"
        upstream = ScriptedUpstream(ai=[httpx.Response(200, json=ai_body('{"to": "a@b.co", "message": "x"y", "type": "email"}'))])
        await self.start_pipeline(upstream)
        done = await self.run_one(text)
        self.assertEqual(done.status, Status.SENT)
        first, second = (json.loads(r.content)["messages"] for r in upstream.ai_requests)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 3)
        self.assertEqual(second[-1], {"role": "user", "content": text})

    async def test_terminal_extraction_outcomes_do_not_retry(self):
        cases = {
            "refusal": (httpx.Response(200, json=ai_body("Refused: Content analysis flagged sensitive information.")), FailureCode.EXTRACTION_REFUSED),
            "missing": (httpx.Response(200, json=ai_body('{"to": "ana@test.com", "message": "m"}')), FailureCode.EXTRACTION_INVALID),
            "auth": (httpx.Response(401, json={"detail": "Invalid API Key"}), FailureCode.AI_REJECTED),
            "mismatch": (httpx.Response(200, json=ai_body(canonical_json(type_="sms"))), FailureCode.EXTRACTION_INVALID),
        }
        for name, (response, code) in cases.items():
            with self.subTest(case=name):
                upstream = ScriptedUpstream(ai=[response])
                await self.start_pipeline(upstream)
                done = await self.run_one()
                self.assertEqual((done.status, done.failure_code, done.ai_attempts), (Status.FAILED, code, 1))
                self.assertEqual(len(upstream.notify_requests), 0)
                await self.pipeline.stop()


class DeliveryRetryTest(PipelineCase):
    async def test_429_then_delivered_waits_cooldown_without_new_extraction(self):
        upstream = ScriptedUpstream(notify=[httpx.Response(429, json={"detail": "Rate limit exceeded"}), DELIVERED])
        await self.start_pipeline(upstream)
        done = await self.run_one()
        self.assertEqual((done.status, done.notify_attempts, done.ai_attempts), (Status.SENT, 2, 1))
        self.assertEqual(len(upstream.ai_requests), 1)
        self.assertTrue(any(s >= 10.1 for s in self.clock.sleeps), self.clock.sleeps)

    async def test_presend_failures_exhaust_after_three_attempts(self):
        upstream = ScriptedUpstream(notify=[httpx.ConnectError("x")] * 5)
        await self.start_pipeline(upstream)
        done = await self.run_one()
        self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.NOTIFICATION_UNAVAILABLE))
        self.assertEqual(len(upstream.notify_requests), 3)
        self.assertEqual(len(upstream.ai_requests), 1)

    async def test_uncertain_outcomes_are_never_resent(self):
        for response in (httpx.Response(500), httpx.ReadTimeout("x"), httpx.Response(200, json={"status": "delivered"})):
            with self.subTest(response=response):
                upstream = ScriptedUpstream(notify=[response, DELIVERED])
                await self.start_pipeline(upstream)
                done = await self.run_one()
                self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.DELIVERY_UNKNOWN))
                self.assertEqual(len(upstream.notify_requests), 1)
                await self.pipeline.stop()

    async def test_permanent_rejection_is_not_retried(self):
        upstream = ScriptedUpstream(notify=[httpx.Response(422, json={"detail": []}), DELIVERED])
        await self.start_pipeline(upstream)
        done = await self.run_one()
        self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.NOTIFICATION_REJECTED))
        self.assertEqual(len(upstream.notify_requests), 1)

    async def test_retry_after_beyond_deadline_fails_job(self):
        upstream = ScriptedUpstream(notify=[httpx.Response(429, headers={"Retry-After": "120"}), DELIVERED])
        await self.start_pipeline(upstream, job_deadline_seconds=30)
        done = await self.run_one()
        self.assertEqual(done.status, Status.FAILED)
        self.assertEqual(len(upstream.notify_requests), 1)


class DeadlineAndIsolationTest(PipelineCase):
    async def test_expired_queued_job_is_skipped_by_worker(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        await self.start_pipeline(upstream, workers=1, job_deadline_seconds=5)
        blocker = self.store.create("block")
        self.store.admit(blocker.id, self.queue)
        await wait_for(lambda: self.store.get(blocker.id).status is Status.PROCESSING)
        waiting = self.store.create("wait")
        self.store.admit(waiting.id, self.queue)
        self.clock.advance(6)
        self.store.expire()
        upstream.ai_gate.set()
        await wait_for(lambda: self.queue.qsize() == 0 and self.store.get(blocker.id).status.terminal)
        await asyncio.wait_for(self.queue.join(), 1)
        self.assertEqual(self.store.get(waiting.id).failure_code, FailureCode.DEADLINE_EXCEEDED)
        self.assertEqual(len(upstream.ai_requests), 1)

    async def test_real_time_deadline_during_extraction_fails_job(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()  # never released
        await self.start_pipeline(upstream, job_deadline_seconds=0.05)
        done = await self.run_one()
        self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.DEADLINE_EXCEEDED))

    async def test_deadline_during_notification_is_delivery_unknown(self):
        async def stall(request):
            await asyncio.sleep(5)
            return DELIVERED

        await self.start_pipeline(ScriptedUpstream(notify=[stall]), job_deadline_seconds=0.1)
        done = await self.run_one()
        self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.DELIVERY_UNKNOWN))

    async def test_unexpected_error_fails_only_that_job(self):
        calls = {"n": 0}

        def explode_once(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return httpx.Response(200, json=ai_body(canonical_json()))

        await self.start_pipeline(ScriptedUpstream(ai=[explode_once]), workers=1)
        with self.assertLogs("notify", level="ERROR"):
            bad = await self.run_one()
        good = await self.run_one()
        self.assertEqual((bad.status, bad.failure_code), (Status.FAILED, FailureCode.INTERNAL_ERROR))
        self.assertEqual(good.status, Status.SENT)


class LifecycleTest(PipelineCase):
    async def test_graceful_stop_drains_queued_work(self):
        await self.start_pipeline(ScriptedUpstream(), workers=1, shutdown_grace_seconds=2)
        ids = []
        for i in range(3):
            snap = self.store.create(str(i))
            self.store.admit(snap.id, self.queue)
            ids.append(snap.id)
        await self.pipeline.stop()
        self.assertEqual([self.store.get(i).status for i in ids], [Status.SENT] * 3)

    async def test_forced_stop_accounts_for_all_outstanding_work(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()  # stalled upstream
        await self.start_pipeline(upstream, workers=1, shutdown_grace_seconds=0.05)
        running = self.store.create("run")
        self.store.admit(running.id, self.queue)
        await wait_for(lambda: self.store.get(running.id).status is Status.PROCESSING)
        queued = self.store.create("queued")
        self.store.admit(queued.id, self.queue)
        idle = self.store.create("idle")

        await asyncio.wait_for(self.pipeline.stop(), 2)

        self.assertTrue(self.store.closed)
        self.assertEqual(self.store.get(running.id).failure_code, FailureCode.SHUTDOWN)
        self.assertEqual(self.store.get(queued.id).failure_code, FailureCode.SHUTDOWN)
        self.assertEqual(self.store.get(idle.id).failure_code, FailureCode.SHUTDOWN)
        self.assertTrue(self.queue.empty())
        await asyncio.wait_for(self.queue.join(), 0.5)  # task_done balanced
        self.assertTrue(all(t.done() for t in self.pipeline.tasks))

    async def test_stop_is_idempotent(self):
        await self.start_pipeline(ScriptedUpstream())
        await self.pipeline.stop()
        await self.pipeline.stop()

    async def test_sweeper_expires_abandoned_requests(self):
        await self.start_pipeline(ScriptedUpstream(), sweep_interval_seconds=0.01, unadmitted_ttl_seconds=5)
        idle = self.store.create("idle")
        self.clock.advance(6)
        await wait_for(lambda: self.store.get(idle.id).status is Status.FAILED)
        self.assertEqual(self.store.get(idle.id).failure_code, FailureCode.UNADMITTED_EXPIRED)

    async def test_sweeper_logs_state_summary_while_work_is_outstanding(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        await self.start_pipeline(upstream, sweep_interval_seconds=0.01)
        snap = self.store.create("x")
        self.store.admit(snap.id, self.queue)
        with self.assertLogs("notify", level="INFO") as logs:
            await wait_for(lambda: any('"state_summary"' in line for line in logs.output))
        summary = json.loads(next(r.getMessage() for r in logs.records if '"state_summary"' in r.getMessage()))
        self.assertEqual(summary["counts"]["processing"], 1)
        self.assertIn("queue_depth", summary)
        upstream.ai_gate.set()

    async def test_fatal_worker_exit_closes_admission_and_fails_work(self):
        await self.start_pipeline(ScriptedUpstream(), workers=1)
        snap = self.store.create("x")

        class Fatal(BaseException):
            pass

        async def fatal_job(request_id):
            raise Fatal()

        self.pipeline._run_job = fatal_job
        with self.assertLogs("notify", level="CRITICAL"):
            self.store.admit(snap.id, self.queue)
            await wait_for(lambda: self.store.closed)
        self.assertEqual(self.store.get(snap.id).status, Status.FAILED)


class LoggingTest(PipelineCase):
    async def test_logs_are_structured_and_sanitized(self):
        secret_input = "Manda un mail a ana@test.com diciendo PRIVATE-TEXT"
        upstream = ScriptedUpstream(ai=[httpx.Response(200, json=ai_body(canonical_json(message="PRIVATE-TEXT")))])
        await self.start_pipeline(upstream)
        with self.assertLogs("notify", level="INFO") as logs:
            done = await self.run_one(secret_input)
        output = "\n".join(logs.output)
        for secret in ("PRIVATE-TEXT", "ana@test.com", "test-dev-2026"):
            self.assertNotIn(secret, output)
        finished = [json.loads(r.getMessage()) for r in logs.records if '"job_finished"' in r.getMessage()]
        self.assertEqual(finished[0]["request_id"], done.id)
        self.assertEqual(finished[0]["status"], "sent")
        self.assertEqual(finished[0]["ai_attempts"], 1)
        self.assertIn("duration_ms", finished[0])


if __name__ == "__main__":
    unittest.main()
