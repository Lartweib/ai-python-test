import io
import itertools
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import httpx

import support  # noqa: F401
import smoke
from smoke import run_smoke


class FakeService:
    """Minimal stand-in for the app with scriptable status progressions."""

    def __init__(self, progression=("queued", "processing", "sent"), create_status=201, process_status=202):
        self.progression = progression
        self.create_status = create_status
        self.process_status = process_status
        self.polls = {}
        self.counter = itertools.count(1)

    def handler(self, request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/requests":
            rid = f"r{next(self.counter)}"
            self.polls[rid] = 0
            return httpx.Response(self.create_status, json={"id": rid})
        if request.method == "POST" and path.endswith("/process"):
            rid = path.split("/")[3]
            return httpx.Response(self.process_status, json={"id": rid, "status": "queued"})
        rid = path.split("/")[3]
        idx = min(self.polls[rid], len(self.progression) - 1)
        self.polls[rid] += 1
        return httpx.Response(200, json={"id": rid, "status": self.progression[idx]})


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(service, inputs=("a", "b"), timeout=10.0):
    t = FakeTime()
    with httpx.Client(transport=httpx.MockTransport(service.handler), base_url="http://app") as client:
        return run_smoke(client, list(inputs), timeout=timeout, poll_interval=0.5, clock=t.clock, sleep=t.sleep)


class SmokeTest(unittest.TestCase):
    def test_all_sent_passes(self):
        report = run(FakeService())
        self.assertTrue(report.ok)
        self.assertEqual(report.counts, {"sent": 2})
        self.assertEqual(report.contract_errors, [])
        self.assertEqual(report.unresolved, [])

    def test_failed_outcomes_are_reported_but_not_contract_errors(self):
        report = run(FakeService(progression=("processing", "failed")))
        self.assertTrue(report.ok)
        self.assertEqual(report.counts, {"failed": 2})

    def test_unresolved_deadline_fails(self):
        report = run(FakeService(progression=("queued",)), timeout=3.0)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.unresolved), 2)

    def test_wrong_status_codes_are_contract_errors(self):
        for service in (FakeService(create_status=200), FakeService(process_status=500)):
            with self.subTest(create=service.create_status, process=service.process_status):
                report = run(service)
                self.assertFalse(report.ok)
                self.assertTrue(report.contract_errors)

    def test_invalid_status_value_is_contract_error(self):
        report = run(FakeService(progression=("delivered",)))
        self.assertFalse(report.ok)

    def test_report_serializes_summary(self):
        summary = json.loads(run(FakeService()).to_json())
        self.assertEqual(summary["requests"], 2)
        self.assertIn("max_seconds_to_terminal", summary)


class MainTest(unittest.TestCase):
    def run_main(self, service, *argv):
        seen, real_client = {}, httpx.Client

        def client(base_url, timeout):
            seen["base_url"] = base_url
            return real_client(transport=httpx.MockTransport(service.handler), base_url=base_url, timeout=timeout)

        out = io.StringIO()
        with mock.patch.object(smoke.httpx, "Client", client), redirect_stdout(out):
            code = smoke.main(list(argv))
        return code, json.loads(out.getvalue()), seen["base_url"]

    def test_exits_zero_and_prints_report_when_all_requests_resolve(self):
        code, summary, base_url = self.run_main(FakeService(progression=("sent",)), "--base-url", "http://svc:1")
        self.assertEqual(code, 0)
        self.assertEqual(base_url, "http://svc:1")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["requests"], len(smoke.DEFAULT_INPUTS))

    def test_exits_nonzero_when_requests_are_unresolved(self):
        code, summary, _ = self.run_main(FakeService(progression=("queued",)), "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertFalse(summary["ok"])
        self.assertEqual(len(summary["unresolved"]), len(smoke.DEFAULT_INPUTS))


if __name__ == "__main__":
    unittest.main()
