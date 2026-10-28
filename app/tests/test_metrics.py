import asyncio
import re
import unittest

from support import FakeClock
from metrics import CONTENT_TYPE, render
from models import FailureCode, Status
from pipeline import NotificationGate
from store import RequestStore

SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (-?[0-9][0-9.e+-]*)$")


def parse(text):
    """Return ({series: value}, {family: type}) and assert the exposition format is well formed."""
    samples, types = {}, {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert name not in types, f"duplicate TYPE for {name}"
            types[name] = kind
        elif line.startswith("# HELP ") or not line:
            continue
        else:
            match = SAMPLE.match(line)
            assert match, f"malformed sample line: {line!r}"
            samples[match.group(1) + (match.group(2) or "")] = float(match.group(3))
    return samples, types


class MetricsRenderTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = RequestStore(capacity=10, unadmitted_ttl=100, job_deadline=50, clock=self.clock)
        self.queue = asyncio.Queue(maxsize=8)
        self.gate = NotificationGate(self.clock, self.clock.sleep, window=10.1, limit=50, min_interval=0.21, cooldown=10.1)

    def render(self):
        return parse(render(self.store, self.queue, self.gate, workers=4))

    def test_empty_service_exposes_every_family_with_zero_values(self):
        samples, types = self.render()
        self.assertEqual(samples["notify_up"], 1)
        self.assertEqual(samples["notify_workers"], 4)
        self.assertEqual(samples["notify_queue_capacity"], 8)
        for status in Status:
            self.assertEqual(samples[f'notify_requests{{status="{status.value}"}}'], 0)
        # Every possible outcome is pre-declared so rates work from the first scrape.
        self.assertEqual(samples['notify_requests_finished_total{status="sent",failure_code=""}'], 0)
        for code in FailureCode:
            self.assertEqual(samples[f'notify_requests_finished_total{{status="failed",failure_code="{code.value}"}}'], 0)
        self.assertEqual(types["notify_requests_finished_total"], "counter")
        self.assertEqual(types["notify_time_to_terminal_seconds"], "summary")
        for name, kind in types.items():
            self.assertEqual(name.endswith("_total"), kind == "counter", name)

    def test_values_follow_store_queue_and_gate_state(self):
        store, queue = self.store, self.queue
        sent, _ = store.create_idempotent("a", "k")
        store.create_idempotent("a", "k")
        store.admit(sent.id, queue)
        store.start(sent.id)
        self.clock.advance(3.0)
        store.finish(sent.id, Status.SENT, ai_attempts=1, notify_attempts=2)
        waiting = store.create("b")
        store.admit(waiting.id, queue)
        store.create("c")
        self.gate.penalize()
        self.gate.dispatched_total = 2

        samples, _ = self.render()
        self.assertEqual(samples['notify_requests{status="sent"}'], 1)
        self.assertEqual(samples['notify_requests{status="queued"}'], 2)
        self.assertEqual(samples["notify_queue_depth"], 2)  # the finished id is still a tombstone in the queue
        self.assertEqual(samples["notify_requests_created_total"], 3)
        self.assertEqual(samples["notify_idempotent_replays_total"], 1)
        self.assertEqual(samples["notify_requests_admitted_total"], 2)
        self.assertEqual(samples['notify_requests_finished_total{status="sent",failure_code=""}'], 1)
        self.assertEqual(samples["notify_ai_attempts_total"], 1)
        self.assertEqual(samples["notify_delivery_attempts_total"], 2)
        self.assertEqual(samples["notify_provider_dispatches_total"], 2)
        self.assertEqual(samples["notify_provider_rate_limited_total"], 1)
        self.assertEqual(samples["notify_time_to_terminal_seconds_count"], 1)
        self.assertEqual(samples["notify_time_to_terminal_seconds_sum"], 3.0)

    def test_up_is_zero_once_admission_is_closed(self):
        self.store.close()
        self.assertEqual(self.render()[0]["notify_up"], 0)

    def test_content_type_is_prometheus_text(self):
        self.assertEqual(CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")
        self.assertTrue(render(self.store, self.queue, self.gate, workers=1).endswith("\n"))


if __name__ == "__main__":
    unittest.main()
