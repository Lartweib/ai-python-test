import unittest
from dataclasses import FrozenInstanceError

import support  # noqa: F401  (configures test logging)
from models import FailureCode, Notification, RequestSnapshot, Status
from settings import Settings, SettingsError


class SettingsTest(unittest.TestCase):
    def test_defaults_match_design(self):
        s = Settings()
        self.assertEqual(s.provider_base_url, "http://localhost:3001")
        self.assertEqual(s.provider_api_key, "test-dev-2026")
        self.assertEqual(s.workers, 32)
        self.assertEqual(s.queue_capacity, 4096)
        self.assertEqual(s.record_capacity, 10000)
        self.assertEqual(s.job_deadline_seconds, 1800)
        self.assertEqual(s.unadmitted_ttl_seconds, 1800)
        self.assertEqual(s.shutdown_grace_seconds, 10)
        self.assertEqual(s.input_max_chars, 4096)

    def test_from_env_parses_overrides(self):
        s = Settings.from_env(
            {
                "PROVIDER_BASE_URL": "http://provider:3001",
                "PROVIDER_API_KEY": "k",
                "NOTIFY_WORKERS": "4",
                "NOTIFY_QUEUE_CAPACITY": "10",
                "NOTIFY_RECORD_CAPACITY": "20",
                "NOTIFY_JOB_DEADLINE_SECONDS": "12.5",
                "NOTIFY_UNADMITTED_TTL_SECONDS": "30",
                "NOTIFY_SHUTDOWN_GRACE_SECONDS": "1",
                "NOTIFY_INPUT_MAX_CHARS": "100",
            }
        )
        self.assertEqual(s.provider_base_url, "http://provider:3001")
        self.assertEqual(s.provider_api_key, "k")
        self.assertEqual((s.workers, s.queue_capacity, s.record_capacity), (4, 10, 20))
        self.assertEqual(s.job_deadline_seconds, 12.5)
        self.assertEqual(s.input_max_chars, 100)

    def test_from_env_without_values_uses_defaults(self):
        self.assertEqual(Settings.from_env({}), Settings())

    def test_rejects_invalid_values(self):
        invalid = [
            {"workers": 0},
            {"queue_capacity": -1},
            {"record_capacity": 0},
            {"job_deadline_seconds": 0},
            {"unadmitted_ttl_seconds": -5},
            {"shutdown_grace_seconds": -1},
            {"input_max_chars": 0},
            {"provider_base_url": ""},
            # admitted jobs alone must fit in the record store
            {"workers": 10, "queue_capacity": 10, "record_capacity": 15},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(SettingsError):
                Settings(**overrides)

    def test_rejects_unparsable_env(self):
        for env in ({"NOTIFY_WORKERS": "many"}, {"NOTIFY_WORKERS": "1.5"}, {"NOTIFY_JOB_DEADLINE_SECONDS": "x"}):
            with self.subTest(env=env), self.assertRaises(SettingsError):
                Settings.from_env(env)

    def test_settings_are_immutable_and_instances_isolated(self):
        a = Settings(workers=1, queue_capacity=1, record_capacity=4)
        b = Settings()
        with self.assertRaises(FrozenInstanceError):
            a.workers = 5  # type: ignore[misc]
        self.assertEqual(b.workers, 32)

    def test_repr_hides_api_key(self):
        self.assertNotIn("test-dev-2026", repr(Settings()))


class ModelsTest(unittest.TestCase):
    def test_public_statuses_are_exactly_the_readme_contract(self):
        self.assertEqual([s.value for s in Status], ["queued", "processing", "sent", "failed"])
        self.assertTrue(Status.SENT.terminal and Status.FAILED.terminal)
        self.assertFalse(Status.QUEUED.terminal or Status.PROCESSING.terminal)

    def test_notification_payload_has_exactly_three_fields(self):
        n = Notification(to="a@b.co", message="Hi", type="email")
        self.assertEqual(n.as_payload(), {"to": "a@b.co", "message": "Hi", "type": "email"})
        with self.assertRaises(FrozenInstanceError):
            n.to = "x"  # type: ignore[misc]

    def test_snapshot_public_view_excludes_internal_fields(self):
        snap = RequestSnapshot(
            id="abc",
            user_input="secret text",
            status=Status.FAILED,
            admitted=True,
            created_at=1.0,
            failure_code=FailureCode.DELIVERY_UNKNOWN,
            provider_id="p-1",
        )
        self.assertEqual(snap.public(), {"id": "abc", "status": "failed"})


if __name__ == "__main__":
    unittest.main()
