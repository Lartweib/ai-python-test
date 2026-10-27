import asyncio
import unittest
import uuid

from support import FakeClock
from models import FailureCode, Status
from store import AdmitResult, QueueFull, RequestNotFound, RequestStore, ShuttingDown, StoreFull


def make_store(capacity=10, ttl=100.0, deadline=50.0, clock=None):
    clock = clock or FakeClock()
    return RequestStore(capacity=capacity, unadmitted_ttl=ttl, job_deadline=deadline, clock=clock), clock


class CreateAndGetTest(unittest.TestCase):
    def test_create_returns_queued_unadmitted_snapshot_with_uuid4(self):
        store, clock = make_store()
        snap = store.create("Manda un mail a feda@test.com diciendo hola")
        self.assertEqual(uuid.UUID(snap.id).version, 4)
        self.assertEqual(snap.status, Status.QUEUED)
        self.assertFalse(snap.admitted)
        self.assertEqual(snap.created_at, clock.now)
        self.assertEqual(store.get(snap.id), snap)

    def test_equal_input_creates_distinct_requests(self):
        store, _ = make_store()
        a, b = store.create("same"), store.create("same")
        self.assertNotEqual(a.id, b.id)

    def test_input_is_preserved_exactly(self):
        store, _ = make_store()
        text = "  Señal: MAYÚSCULAS \"quotes\" \\ {braces}\n "
        self.assertEqual(store.get(store.create(text).id).user_input, text)

    def test_unknown_id_returns_none(self):
        store, _ = make_store()
        self.assertIsNone(store.get("nope"))

    def test_snapshots_are_not_live_references(self):
        store, _ = make_store()
        snap = store.create("x")
        store.admit(snap.id, asyncio.Queue())
        self.assertFalse(snap.admitted)
        self.assertTrue(store.get(snap.id).admitted)


class AdmissionTest(unittest.TestCase):
    def test_first_admission_enqueues_once_and_sets_deadline(self):
        store, clock = make_store(deadline=50.0)
        queue = asyncio.Queue()
        snap = store.create("x")
        result, admitted = store.admit(snap.id, queue)
        self.assertIs(result, AdmitResult.ACCEPTED)
        self.assertEqual(admitted.status, Status.QUEUED)
        self.assertTrue(admitted.admitted)
        self.assertEqual(admitted.deadline, clock.now + 50.0)
        self.assertEqual(queue.qsize(), 1)

    def test_repeated_admission_does_not_enqueue_again(self):
        store, _ = make_store()
        queue = asyncio.Queue()
        snap = store.create("x")
        store.admit(snap.id, queue)
        for _ in range(5):
            result, _snap = store.admit(snap.id, queue)
            self.assertIs(result, AdmitResult.ALREADY_ACTIVE)
        self.assertEqual(queue.qsize(), 1)

    def test_already_admitted_work_succeeds_even_if_queue_is_full(self):
        store, _ = make_store()
        queue = asyncio.Queue(maxsize=1)
        snap = store.create("x")
        store.admit(snap.id, queue)
        result, _ = store.admit(snap.id, queue)
        self.assertIs(result, AdmitResult.ALREADY_ACTIVE)

    def test_processing_is_reported_as_already_active(self):
        store, _ = make_store()
        queue = asyncio.Queue()
        snap = store.create("x")
        store.admit(snap.id, queue)
        store.start(snap.id)
        result, current = store.admit(snap.id, queue)
        self.assertIs(result, AdmitResult.ALREADY_ACTIVE)
        self.assertEqual(current.status, Status.PROCESSING)

    def test_terminal_request_is_never_readmitted(self):
        store, _ = make_store()
        queue = asyncio.Queue()
        snap = store.create("x")
        store.admit(snap.id, queue)
        queue.get_nowait()
        store.start(snap.id)
        store.finish(snap.id, Status.SENT, provider_id="p-1")
        result, current = store.admit(snap.id, queue)
        self.assertIs(result, AdmitResult.TERMINAL)
        self.assertEqual(current.status, Status.SENT)
        self.assertTrue(queue.empty())

    def test_full_queue_rejects_and_leaves_request_unadmitted(self):
        store, _ = make_store()
        queue = asyncio.Queue(maxsize=1)
        first, second = store.create("a"), store.create("b")
        store.admit(first.id, queue)
        with self.assertRaises(QueueFull):
            store.admit(second.id, queue)
        self.assertFalse(store.get(second.id).admitted)
        self.assertEqual(store.get(second.id).status, Status.QUEUED)
        queue.get_nowait()
        result, _ = store.admit(second.id, queue)
        self.assertIs(result, AdmitResult.ACCEPTED)

    def test_unknown_id_raises_not_found(self):
        store, _ = make_store()
        with self.assertRaises(RequestNotFound):
            store.admit("missing", asyncio.Queue())

    def test_closed_admission_rejects_new_work_but_reports_existing(self):
        store, _ = make_store()
        queue = asyncio.Queue()
        active, idle = store.create("a"), store.create("b")
        store.admit(active.id, queue)
        store.close()
        with self.assertRaises(ShuttingDown):
            store.admit(idle.id, queue)
        with self.assertRaises(ShuttingDown):
            store.create("c")
        self.assertIs(store.admit(active.id, queue)[0], AdmitResult.ALREADY_ACTIVE)


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = make_store()
        self.queue = asyncio.Queue()
        self.snap = self.store.create("x")

    def test_start_requires_admission(self):
        self.assertIsNone(self.store.start(self.snap.id))
        self.store.admit(self.snap.id, self.queue)
        started = self.store.start(self.snap.id)
        self.assertEqual(started.status, Status.PROCESSING)
        self.assertIsNone(self.store.start(self.snap.id))

    def test_finish_records_outcome_and_metadata(self):
        self.store.admit(self.snap.id, self.queue)
        self.store.start(self.snap.id)
        self.assertTrue(
            self.store.finish(self.snap.id, Status.SENT, provider_id="p-9", ai_attempts=1, notify_attempts=2)
        )
        done = self.store.get(self.snap.id)
        self.assertEqual((done.status, done.provider_id, done.ai_attempts, done.notify_attempts), (Status.SENT, "p-9", 1, 2))
        self.assertEqual(done.finished_at, self.clock.now)
        self.assertTrue(done.admitted)

    def test_terminal_outcomes_are_immutable(self):
        self.store.admit(self.snap.id, self.queue)
        self.store.start(self.snap.id)
        self.store.finish(self.snap.id, Status.FAILED, failure_code=FailureCode.DEADLINE_EXCEEDED)
        self.assertFalse(self.store.finish(self.snap.id, Status.SENT, provider_id="late"))
        self.assertIsNone(self.store.start(self.snap.id))
        done = self.store.get(self.snap.id)
        self.assertEqual((done.status, done.failure_code), (Status.FAILED, FailureCode.DEADLINE_EXCEEDED))

    def test_finish_rejects_nonterminal_status(self):
        with self.assertRaises(ValueError):
            self.store.finish(self.snap.id, Status.PROCESSING)

    def test_counts_by_status(self):
        self.store.create("y")
        self.store.admit(self.snap.id, self.queue)
        self.store.start(self.snap.id)
        self.assertEqual(self.store.counts(), {"queued": 1, "processing": 1, "sent": 0, "failed": 0})


class RetentionTest(unittest.TestCase):
    def test_full_store_evicts_oldest_terminal_record(self):
        store, _ = make_store(capacity=3)
        queue = asyncio.Queue()
        ids = [store.create(str(i)).id for i in range(3)]
        for rid in ids[:2]:
            store.admit(rid, queue)
            store.start(rid)
            store.finish(rid, Status.SENT)
        new = store.create("new")
        self.assertIsNone(store.get(ids[0]))
        self.assertIsNotNone(store.get(ids[1]))
        self.assertIsNotNone(store.get(new.id))

    def test_full_store_without_terminal_records_rejects_creation(self):
        store, _ = make_store(capacity=2)
        queue = asyncio.Queue()
        a = store.create("a")
        store.create("b")
        store.admit(a.id, queue)
        with self.assertRaises(StoreFull):
            store.create("c")
        self.assertEqual(len(store), 2)

    def test_expire_fails_unadmitted_and_overdue_queued_but_not_processing(self):
        store, clock = make_store(ttl=100.0, deadline=50.0)
        queue = asyncio.Queue()
        idle = store.create("idle")
        waiting = store.create("waiting")
        running = store.create("running")
        store.admit(waiting.id, queue)
        store.admit(running.id, queue)
        store.start(running.id)

        clock.advance(49)
        self.assertEqual(store.expire(), [])
        clock.advance(2)
        self.assertEqual(store.expire(), [waiting.id])
        self.assertEqual(store.get(waiting.id).failure_code, FailureCode.DEADLINE_EXCEEDED)
        self.assertEqual(store.get(running.id).status, Status.PROCESSING)

        clock.advance(50)
        self.assertEqual(store.expire(), [idle.id])
        self.assertEqual(store.get(idle.id).failure_code, FailureCode.UNADMITTED_EXPIRED)
        # Expired queued IDs are harmless tombstones for workers.
        self.assertIsNone(store.start(waiting.id))

    def test_creation_under_pressure_expires_then_evicts_but_keeps_active_work(self):
        store, clock = make_store(capacity=3, ttl=10.0)
        queue = asyncio.Queue()
        active = store.create("active")
        store.admit(active.id, queue)
        stale = store.create("stale")
        fresh = store.create("fresh")
        clock.advance(11)
        new = store.create("new")  # stale expires -> terminal -> evicted
        self.assertIsNone(store.get(stale.id))
        self.assertIsNotNone(store.get(active.id))
        self.assertEqual(store.get(fresh.id).status, Status.FAILED)  # also past TTL, retained
        self.assertEqual(store.get(new.id).status, Status.QUEUED)
        self.assertEqual(store.get(active.id).status, Status.QUEUED)

    def test_fail_outstanding_accounts_for_every_nonterminal_record(self):
        store, _ = make_store()
        queue = asyncio.Queue()
        idle, waiting, running, done = (store.create(x) for x in "abcd")
        for snap in (waiting, running, done):
            store.admit(snap.id, queue)
        store.start(running.id)
        store.start(done.id)
        store.finish(done.id, Status.SENT)
        failed = store.fail_outstanding()
        self.assertEqual(set(failed), {idle.id, waiting.id, running.id})
        self.assertEqual(store.get(waiting.id).failure_code, FailureCode.SHUTDOWN)
        self.assertEqual(store.get(running.id).failure_code, FailureCode.DELIVERY_UNKNOWN)
        self.assertEqual(store.get(done.id).status, Status.SENT)


if __name__ == "__main__":
    unittest.main()
