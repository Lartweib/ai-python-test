import asyncio
import unittest
from pathlib import Path

from support import ScriptedUpstream, running_app, test_settings, wait_for


class ImportTest(unittest.TestCase):
    def test_module_level_app_exists_without_starting_resources(self):
        import main
        from fastapi import FastAPI

        self.assertIsInstance(main.app, FastAPI)
        self.assertFalse(hasattr(main.app.state, "pipeline"))
        paths = {(route.path, tuple(sorted(route.methods))) for route in main.app.routes if hasattr(route, "methods")}
        self.assertIn(("/v1/requests", ("POST",)), paths)
        self.assertIn(("/v1/requests/{request_id}/process", ("POST",)), paths)
        self.assertIn(("/v1/requests/{request_id}", ("GET",)), paths)
        self.assertIn(("/health", ("GET",)), paths)
        self.assertIn(("/metrics", ("GET",)), paths)

    def test_entry_point_declares_the_readme_instance_literally(self):
        # The README requires `app = FastAPI()` in app/main.py; reviewers may look for it verbatim.
        import main

        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^app = FastAPI\(")


class HealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_ok_while_accepting_work_without_calling_upstream(self):
        upstream = ScriptedUpstream()
        async with running_app(upstream=upstream) as (app, client):
            res = await client.get("/health")
            self.assertEqual((res.status_code, res.json()), (200, {"status": "ok"}))
            self.assertEqual(upstream.requests, [])

    async def test_health_is_503_once_admission_is_closed(self):
        async with running_app() as (app, client):
            app.state.store.close()
            res = await client.get("/health")
            self.assertEqual((res.status_code, res.json()), (503, {"status": "unavailable"}))


class CreateTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_returns_201_with_only_id_and_no_upstream_call(self):
        upstream = ScriptedUpstream()
        async with running_app(upstream=upstream) as (app, client):
            res = await client.post("/v1/requests", json={"user_input": "Manda un mail a feda@test.com diciendo hola"})
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertEqual(set(body), {"id"})
            self.assertIsInstance(body["id"], str)
            status = await client.get(f"/v1/requests/{body['id']}")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json(), {"id": body["id"], "status": "queued"})
            await asyncio.sleep(0.05)
            self.assertEqual(upstream.requests, [])

    async def test_equal_bodies_create_distinct_requests(self):
        async with running_app() as (app, client):
            a = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            b = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            self.assertNotEqual(a, b)

    async def test_invalid_bodies_return_sanitized_422_and_create_nothing(self):
        secret = "SECRET-INPUT-123"
        bodies = [
            {"json": {}},
            {"json": {"user_input": None}},
            {"json": {"user_input": 42}},
            {"json": {"user_input": ["a"]}},
            {"json": {"user_input": {"a": secret}}},
            {"json": {"user_input": True}},
            {"json": {"user_input": ""}},
            {"json": {"user_input": "   \n\t"}},
            {"json": {"user_input": secret * 10}},  # over the configured limit
            {"json": {"user_input": "ok", secret: "extra"}},
            {"json": [secret]},
            {"content": b'{"user_input": "' + secret.encode() + b'"', "headers": {"content-type": "application/json"}},
            {"content": b"", "headers": {"content-type": "application/json"}},
        ]
        upstream = ScriptedUpstream()
        async with running_app(settings=test_settings(input_max_chars=50), upstream=upstream) as (app, client):
            for kwargs in bodies:
                with self.subTest(body=kwargs):
                    res = await client.post("/v1/requests", **kwargs)
                    self.assertEqual(res.status_code, 422)
                    self.assertNotIn(secret, res.text)
                    self.assertIn("detail", res.json())
            self.assertEqual(len(app.state.store), 0)
            self.assertEqual(upstream.requests, [])

    async def test_input_at_limit_is_accepted_and_preserved(self):
        text = "  Ñ" * 16 + "ab"  # 50 chars, leading whitespace kept
        async with running_app(settings=test_settings(input_max_chars=50)) as (app, client):
            res = await client.post("/v1/requests", json={"user_input": text})
            self.assertEqual(res.status_code, 201)
            self.assertEqual(app.state.store.get(res.json()["id"]).user_input, text)

    async def test_store_full_returns_503(self):
        settings = test_settings(workers=1, queue_capacity=1, record_capacity=2)
        async with running_app(settings=settings) as (app, client):
            for _ in range(2):
                self.assertEqual((await client.post("/v1/requests", json={"user_input": "x"})).status_code, 201)
            res = await client.post("/v1/requests", json={"user_input": "x"})
            self.assertEqual(res.status_code, 503)
            self.assertEqual(res.json(), {"detail": "store_full"})


class MetricsEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_reflect_completed_work_without_calling_upstream(self):
        upstream = ScriptedUpstream()
        async with running_app(upstream=upstream) as (app, client):
            rid = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            await client.post(f"/v1/requests/{rid}/process")
            await wait_for(lambda: app.state.store.get(rid).status.terminal)
            calls = len(upstream.requests)
            res = await client.get("/metrics")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.headers["content-type"], "text/plain; version=0.0.4; charset=utf-8")
            self.assertIn('notify_requests_finished_total{status="sent",failure_code=""} 1\n', res.text)
            self.assertIn("notify_provider_dispatches_total 1\n", res.text)
            self.assertIn("notify_workers 2\n", res.text)
            self.assertEqual(len(upstream.requests), calls)

    async def test_metrics_never_expose_request_content(self):
        async with running_app() as (app, client):
            await client.post("/v1/requests", json={"user_input": "secret ana@test.com"}, headers={"Idempotency-Key": "k-1"})
            text = (await client.get("/metrics")).text
            self.assertNotIn("ana@test.com", text)
            self.assertNotIn("k-1", text)


class IdempotencyKeyTest(unittest.IsolatedAsyncioTestCase):
    async def create(self, client, text="x", key="order-42"):
        return await client.post("/v1/requests", json={"user_input": text}, headers={"Idempotency-Key": key})

    async def test_repeat_with_same_key_and_body_replays_the_original_id(self):
        async with running_app() as (app, client):
            first, again = await self.create(client), await self.create(client)
            self.assertEqual((first.status_code, again.status_code), (201, 201))
            self.assertEqual(again.json(), first.json())
            self.assertNotIn("idempotent-replayed", first.headers)
            self.assertEqual(again.headers["idempotent-replayed"], "true")
            self.assertEqual(len(app.state.store), 1)

    async def test_replay_after_processing_does_not_trigger_new_work(self):
        upstream = ScriptedUpstream()
        async with running_app(upstream=upstream) as (app, client):
            rid = (await self.create(client)).json()["id"]
            await client.post(f"/v1/requests/{rid}/process")
            await wait_for(lambda: app.state.store.get(rid).status.terminal)
            self.assertEqual((await self.create(client)).json(), {"id": rid})
            self.assertEqual(len(upstream.ai_requests), 1)

    async def test_same_key_with_different_body_is_rejected(self):
        async with running_app() as (app, client):
            await self.create(client, text="x")
            res = await self.create(client, text="y")
            self.assertEqual((res.status_code, res.json()), (422, {"detail": "idempotency_key_reused"}))
            self.assertEqual(len(app.state.store), 1)

    async def test_malformed_keys_are_rejected_without_creating(self):
        async with running_app() as (app, client):
            for key in ("", " lead", "has space", "k" * 256, "clé"):
                with self.subTest(key=key):
                    res = await client.post(
                        "/v1/requests", json={"user_input": "x"}, headers={"Idempotency-Key": key.encode()}
                    )
                    self.assertEqual((res.status_code, res.json()), (400, {"detail": "invalid_idempotency_key"}))
            self.assertEqual(len(app.state.store), 0)

    async def test_longest_allowed_key_is_accepted(self):
        async with running_app() as (app, client):
            self.assertEqual((await self.create(client, key="k" * 255)).status_code, 201)


class UnknownIdTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_ids_return_404(self):
        upstream = ScriptedUpstream()
        async with running_app(upstream=upstream) as (app, client):
            for rid in ("nope", "00000000-0000-4000-8000-000000000000"):
                with self.subTest(rid=rid):
                    get = await client.get(f"/v1/requests/{rid}")
                    proc = await client.post(f"/v1/requests/{rid}/process")
                    self.assertEqual((get.status_code, proc.status_code), (404, 404))
                    self.assertEqual(get.json(), {"detail": "request_not_found"})
                    self.assertEqual(proc.json(), {"detail": "request_not_found"})
            self.assertEqual(upstream.requests, [])


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_process_returns_202_before_ai_completes(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        async with running_app(upstream=upstream) as (app, client):
            rid = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            res = await asyncio.wait_for(client.post(f"/v1/requests/{rid}/process"), 1)
            self.assertEqual(res.status_code, 202)
            self.assertEqual(res.json()["id"], rid)
            self.assertIn(res.json()["status"], ("queued", "processing"))
            await wait_for(lambda: len(upstream.ai_requests) == 1)
            self.assertEqual((await client.get(f"/v1/requests/{rid}")).json()["status"], "processing")
            upstream.ai_gate.set()
            await wait_for(lambda: app.state.store.get(rid).status.terminal)
            self.assertEqual((await client.get(f"/v1/requests/{rid}")).json(), {"id": rid, "status": "sent"})

    async def test_concurrent_duplicates_admit_once_and_terminal_returns_200(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        async with running_app(upstream=upstream) as (app, client):
            rid = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            responses = await asyncio.gather(*(client.post(f"/v1/requests/{rid}/process") for _ in range(20)))
            self.assertTrue(all(r.status_code == 202 for r in responses))
            upstream.ai_gate.set()
            await wait_for(lambda: app.state.store.get(rid).status.terminal)
            again = await client.post(f"/v1/requests/{rid}/process")
            self.assertEqual((again.status_code, again.json()), (200, {"id": rid, "status": "sent"}))
            await asyncio.sleep(0.05)
            self.assertEqual((len(upstream.ai_requests), len(upstream.notify_requests)), (1, 1))

    async def test_failed_request_is_not_reprocessed(self):
        import httpx
        from support import ai_body

        upstream = ScriptedUpstream(ai=[httpx.Response(200, json=ai_body("Refused: Content analysis flagged sensitive information."))])
        async with running_app(upstream=upstream) as (app, client):
            rid = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            await client.post(f"/v1/requests/{rid}/process")
            await wait_for(lambda: app.state.store.get(rid).status.terminal)
            again = await client.post(f"/v1/requests/{rid}/process")
            self.assertEqual((again.status_code, again.json()["status"]), (200, "failed"))
            self.assertEqual(len(upstream.ai_requests), 1)

    async def test_queue_full_returns_503_and_later_admission_succeeds(self):
        upstream = ScriptedUpstream()
        upstream.ai_gate = asyncio.Event()
        settings = test_settings(workers=1, queue_capacity=1, record_capacity=10)
        async with running_app(settings=settings, upstream=upstream) as (app, client):
            ids = [(await client.post("/v1/requests", json={"user_input": str(i)})).json()["id"] for i in range(3)]
            self.assertEqual((await client.post(f"/v1/requests/{ids[0]}/process")).status_code, 202)
            await wait_for(lambda: len(upstream.ai_requests) == 1)  # worker busy
            self.assertEqual((await client.post(f"/v1/requests/{ids[1]}/process")).status_code, 202)  # fills queue
            full = await client.post(f"/v1/requests/{ids[2]}/process")
            self.assertEqual((full.status_code, full.json()), (503, {"detail": "queue_full"}))
            self.assertEqual((await client.get(f"/v1/requests/{ids[2]}")).json()["status"], "queued")
            # duplicate of admitted work is not affected by saturation
            self.assertEqual((await client.post(f"/v1/requests/{ids[1]}/process")).status_code, 202)
            upstream.ai_gate.set()
            await wait_for(lambda: app.state.store.get(ids[1]).status.terminal)
            self.assertEqual((await client.post(f"/v1/requests/{ids[2]}/process")).status_code, 202)
            await wait_for(lambda: app.state.store.get(ids[2]).status.terminal)
            self.assertEqual(len(upstream.ai_requests), 3)

    async def test_shutdown_rejects_new_work_with_503(self):
        async with running_app() as (app, client):
            rid = (await client.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            app.state.store.close()
            create = await client.post("/v1/requests", json={"user_input": "x"})
            proc = await client.post(f"/v1/requests/{rid}/process")
            self.assertEqual((create.status_code, create.json()), (503, {"detail": "shutting_down"}))
            self.assertEqual((proc.status_code, proc.json()), (503, {"detail": "shutting_down"}))


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_owns_and_releases_resources(self):
        async with running_app() as (app, client):
            pipeline, http = app.state.pipeline, app.state.http
            self.assertFalse(http.is_closed)
            self.assertTrue(all(not t.done() for t in pipeline.tasks))
        self.assertTrue(http.is_closed)
        self.assertTrue(all(t.done() for t in pipeline.tasks))

    async def test_instances_are_isolated(self):
        async with running_app() as (app_a, client_a), running_app() as (app_b, client_b):
            rid = (await client_a.post("/v1/requests", json={"user_input": "x"})).json()["id"]
            self.assertEqual((await client_b.get(f"/v1/requests/{rid}")).status_code, 404)
            self.assertIsNot(app_a.state.store, app_b.state.store)


if __name__ == "__main__":
    unittest.main()
