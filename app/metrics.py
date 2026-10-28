"""Prometheus text exposition (format 0.0.4) without a client library.

Values are read from the store, queue and gate at scrape time. Label values
are fixed enum codes, so they never need escaping and never carry user data.
"""

import asyncio

from models import FailureCode, Status
from pipeline import NotificationGate
from store import RequestStore

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _family(lines: list[str], name: str, kind: str, help_text: str, samples) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for suffix, labels, value in samples:
        label_text = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""
        number = f"{value:g}" if isinstance(value, float) else str(value)
        lines.append(f"{name}{suffix}{label_text} {number}")


def render(store: RequestStore, queue: asyncio.Queue, gate: NotificationGate, workers: int) -> str:
    stats = store.stats
    counts = store.counts()
    outcomes = [(Status.SENT.value, "")] + [(Status.FAILED.value, code.value) for code in FailureCode]
    lines: list[str] = []

    def single(name, kind, help_text, value):
        _family(lines, name, kind, help_text, [("", (), value)])

    single("notify_up", "gauge", "1 while the service accepts new work, 0 when stopping or degraded.", 0 if store.closed else 1)
    _family(lines, "notify_requests", "gauge", "Requests currently retained, by public status.",
            [("", (("status", s.value),), counts[s.value]) for s in Status])
    single("notify_queue_depth", "gauge", "Admitted request ids waiting for a worker.", queue.qsize())
    single("notify_queue_capacity", "gauge", "Maximum queue depth before process calls get 503.", queue.maxsize)
    single("notify_workers", "gauge", "Configured worker tasks.", workers)
    single("notify_requests_created_total", "counter", "Requests created.", stats.created)
    single("notify_idempotent_replays_total", "counter", "Create calls answered from an existing Idempotency-Key.", stats.replayed)
    single("notify_requests_admitted_total", "counter", "Requests admitted for processing.", stats.admitted)
    single("notify_requests_evicted_total", "counter", "Terminal records evicted to make room.", stats.evicted)
    _family(lines, "notify_requests_finished_total", "counter", "Requests that reached a terminal status, by failure code.",
            [("", (("status", s), ("failure_code", c)), stats.outcomes.get((s, c), 0)) for s, c in outcomes])
    single("notify_ai_attempts_total", "counter", "AI extraction calls made by finished jobs.", stats.ai_attempts)
    single("notify_delivery_attempts_total", "counter", "Notification calls made by finished jobs.", stats.notify_attempts)
    single("notify_provider_dispatches_total", "counter", "Notification slots granted by the rate gate.", gate.dispatched_total)
    single("notify_provider_rate_limited_total", "counter", "Provider 429 responses that paused all workers.", gate.rate_limited_total)
    _family(lines, "notify_time_to_terminal_seconds", "summary", "Time from admission to a terminal status.",
            [("_sum", (), float(stats.terminal_seconds_sum)), ("_count", (), stats.terminal_seconds_count)])
    return "\n".join(lines) + "\n"
