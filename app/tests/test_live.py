"""Opt-in live end-to-end test against a running stack (real Uvicorn + real mock provider).

Skipped unless ``LIVE_BASE_URL`` is set, e.g.:

    docker compose up -d --build provider app
    LIVE_BASE_URL=http://localhost:5000 PYTHONPATH=app python -m unittest discover -s app/tests -p test_live.py -v
"""

import os
import unittest

import httpx

import support  # noqa: F401
from smoke import DEFAULT_INPUTS, run_smoke

BASE_URL = os.environ.get("LIVE_BASE_URL")
TIMEOUT = float(os.environ.get("LIVE_TIMEOUT_SECONDS", "120"))


@unittest.skipUnless(BASE_URL, "set LIVE_BASE_URL to run against a live stack")
class LiveStackTest(unittest.TestCase):
    def test_requests_reach_a_terminal_state_through_the_real_provider(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            report = run_smoke(client, DEFAULT_INPUTS, TIMEOUT)
        self.assertEqual(report.contract_errors, [], report.to_json())
        self.assertEqual(report.unresolved, [], report.to_json())
        # The mock refuses ~1 in 6 replies on purpose, so require some, not all, to be sent.
        self.assertGreater(report.counts.get("sent", 0), 0, report.to_json())

    def test_unknown_id_and_invalid_body_follow_the_contract(self):
        with httpx.Client(base_url=BASE_URL, timeout=10) as client:
            self.assertEqual(client.get("/v1/requests/does-not-exist").status_code, 404)
            self.assertEqual(client.post("/v1/requests", json={"user_input": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
