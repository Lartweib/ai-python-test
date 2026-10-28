"""Shared deterministic test doubles: fake time, scripted upstream and app harness."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx

# Swallow log output during tests; assertLogs still captures records.
logging.getLogger().addHandler(logging.NullHandler())

from settings import Settings  # noqa: E402


class FakeClock:
    """Monotonic clock whose time only moves when a fake sleep advances it."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)


def test_settings(**overrides):
    base = Settings(
        workers=2,
        queue_capacity=8,
        record_capacity=32,
        job_deadline_seconds=60,
        unadmitted_ttl_seconds=60,
        shutdown_grace_seconds=0.5,
        sweep_interval_seconds=60,
    )
    return replace(base, **overrides)


def ai_body(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def canonical_json(to="ana@test.com", message="Hola Ana", type_="email"):
    return json.dumps({"to": to, "message": message, "type": type_})


class ScriptedUpstream:
    """HTTPX MockTransport handler that replays scripted AI/notify responses.

    Each script entry is an ``httpx.Response``, an exception instance to raise,
    or a callable ``(request) -> Response``. When a script runs out, the default
    response factory is used.
    """

    def __init__(self, ai=None, notify=None, ai_default=None, notify_default=None):
        self.ai_script = list(ai or [])
        self.notify_script = list(notify or [])
        self.ai_default = ai_default or (lambda request: httpx.Response(200, json=ai_body(canonical_json())))
        self.notify_default = notify_default or (
            lambda request: httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})
        )
        self.requests = []
        self.ai_gate = None  # optional asyncio.Event that holds AI responses

    @property
    def ai_requests(self):
        return [r for r in self.requests if r.url.path == "/v1/ai/extract"]

    @property
    def notify_requests(self):
        return [r for r in self.requests if r.url.path == "/v1/notify"]

    def transport(self):
        return httpx.MockTransport(self.handle)

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/ai/extract":
            if self.ai_gate is not None:
                await self.ai_gate.wait()
            return await self._next(self.ai_script, self.ai_default, request)
        if request.url.path == "/v1/notify":
            return await self._next(self.notify_script, self.notify_default, request)
        return httpx.Response(404, json={"detail": "Not Found"})

    @staticmethod
    async def _next(script, default, request):
        item = script.pop(0) if script else default
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request)
            if asyncio.iscoroutine(item):
                item = await item
        return item


@asynccontextmanager
async def running_app(settings=None, upstream=None, clock=None, rand=None):
    """Start an isolated app instance (with lifespan) and yield (app, client)."""
    from main import create_app

    clock = clock or FakeClock()
    upstream = upstream or ScriptedUpstream()
    app = create_app(
        settings=settings or test_settings(),
        transport=upstream.transport(),
        clock=clock,
        sleep=clock.sleep,
        rand=rand or (lambda: 0.0),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
            yield app, client


async def wait_for(predicate, timeout=5.0, interval=0.005):
    """Poll an (a)sync predicate until truthy or fail after a real-time bound."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if loop.time() > end:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(interval)
