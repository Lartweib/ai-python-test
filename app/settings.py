"""Validated, environment-backed configuration.

Settings are parsed once at startup. Tests build ``Settings(...)`` directly with
tiny capacities and time values instead of mutating the environment.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Mapping

# Parser and request safety limits (deliberately not configurable).
MAX_RESPONSE_BYTES = 65536
MAX_CONTENT_BYTES = 16384
MAX_NESTING_DEPTH = 8
MAX_RECIPIENT_CHARS = 320
MAX_MESSAGE_CHARS = 4096
HTTP_MAX_CONNECTIONS = 40


class SettingsError(ValueError):
    """Raised when configuration is invalid; the service must not start."""


@dataclass(frozen=True)
class Settings:
    provider_base_url: str = "http://localhost:3001"
    provider_api_key: str = field(default="test-dev-2026", repr=False)

    workers: int = 32
    queue_capacity: int = 4096
    record_capacity: int = 10000
    job_deadline_seconds: float = 1800
    unadmitted_ttl_seconds: float = 1800
    shutdown_grace_seconds: float = 10
    input_max_chars: int = 4096
    sweep_interval_seconds: float = 5

    # Upstream timeouts: inactivity timeouts plus a bound on each whole attempt.
    connect_timeout_seconds: float = 2
    ai_read_timeout_seconds: float = 10
    notify_read_timeout_seconds: float = 5
    ai_attempt_timeout_seconds: float = 15
    notify_attempt_timeout_seconds: float = 10

    # Retry budgets and backoff.
    ai_max_attempts: int = 2
    notify_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.25
    retry_jitter_seconds: float = 0.25

    # Local notification pacing (provider allows 50 per sliding 10 s).
    notify_window_seconds: float = 10.1
    notify_window_limit: int = 50
    notify_min_interval_seconds: float = 0.21
    notify_cooldown_seconds: float = 10.1

    def __post_init__(self):
        if not self.provider_base_url:
            raise SettingsError("provider_base_url must not be empty")
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if f.name in ("shutdown_grace_seconds", "retry_base_delay_seconds", "retry_jitter_seconds"):
                if value < 0:
                    raise SettingsError(f"{f.name} must be >= 0")
            elif value <= 0:
                raise SettingsError(f"{f.name} must be > 0")
        if self.workers + self.queue_capacity > self.record_capacity:
            raise SettingsError("record_capacity must hold every executing and queued job")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        names = {
            "provider_base_url": "PROVIDER_BASE_URL",
            "provider_api_key": "PROVIDER_API_KEY",
            "workers": "NOTIFY_WORKERS",
            "queue_capacity": "NOTIFY_QUEUE_CAPACITY",
            "record_capacity": "NOTIFY_RECORD_CAPACITY",
            "job_deadline_seconds": "NOTIFY_JOB_DEADLINE_SECONDS",
            "unadmitted_ttl_seconds": "NOTIFY_UNADMITTED_TTL_SECONDS",
            "shutdown_grace_seconds": "NOTIFY_SHUTDOWN_GRACE_SECONDS",
            "input_max_chars": "NOTIFY_INPUT_MAX_CHARS",
        }
        types = {f.name: f.type for f in fields(cls)}
        values = {}
        for attr, var in names.items():
            raw = env.get(var)
            if raw is None:
                continue
            try:
                values[attr] = types[attr](raw)
            except ValueError:
                raise SettingsError(f"{var} has an invalid value") from None
        return cls(**values)
