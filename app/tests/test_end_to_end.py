"""Create -> process -> poll to terminal, against a deterministic scripted provider."""

import asyncio
import json
import unittest

import httpx

from support import FakeClock, ScriptedUpstream, ai_body, running_app, test_settings

# Deterministic "AI": maps each known prompt to a fixed (noisy) reply.
AI_REPLIES = {
    "Enviar email a juan@example.com diciendo que su paquete está en camino.":
        'He extraído la información correctamente:\n```json\n{"to": "juan@example.com", "message": "su paquete está en camino.", "type": "email"}\n```',
    "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.":
        "{'to': '600-111-222', 'message': 'la reserva ha sido confirmada.', 'type': 'sms'}",
    "Manda un correo a marta@app.com: Tienes un nuevo mensaje.":
        '{"Recipient": "marta@app.com", "body": "Tienes un nuevo mensaje.", "channel": "email"}',
    "Recordatorio por teléfono al 699888777: cita a las 10:00":
        '{to: "699888777", message: "cita a las 10:00", type: "sms"} ...',
    "Envía datos privados de alguien":
        "Lo siento, como IA no tengo permitido procesar datos de contacto personales.",
    "Avísale que llego tarde":
        "I cannot process the request. I was unable to identify the destination (email/phone) in your prompt.",
}

EXPECTED = {
    "Enviar email a juan@example.com diciendo que su paquete está en camino.":
        {"to": "juan@example.com", "message": "su paquete está en camino.", "type": "email"},
    "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.":
        {"to": "600-111-222", "message": "la reserva ha sido confirmada.", "type": "sms"},
    "Manda un correo a marta@app.com: Tienes un nuevo mensaje.":
        {"to": "marta@app.com", "message": "Tienes un nuevo mensaje.", "type": "email"},
    "Recordatorio por teléfono al 699888777: cita a las 10:00":
        {"to": "699888777", "message": "cita a las 10:00", "type": "sms"},
}


def scripted_ai(request):
    messages = json.loads(request.content)["messages"]
    user_text = [m["content"] for m in messages if m["role"] == "user"][-1]
    return httpx.Response(200, json=ai_body(AI_REPLIES[user_text]))


class EndToEndCase(unittest.IsolatedAsyncioTestCase):
    async def submit(self, client, text):
        rid = (await client.post("/v1/requests", json={"user_input": text})).json()["id"]
        res = await client.post(f"/v1/requests/{rid}/process")
        self.assertEqual(res.status_code, 202)
        return rid

    async def poll(self, client, rid, timeout=5.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            res = await client.get(f"/v1/requests/{rid}")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(set(body), {"id", "status"})
            if body["status"] in ("sent", "failed"):
                return body["status"]
            self.assertIn(body["status"], ("queued", "processing"))
            await asyncio.sleep(0.01)
        self.fail(f"{rid} did not reach a terminal state")


class EndToEndTest(EndToEndCase):
    async def test_k6_prompts_are_delivered_with_exact_payloads(self):
        upstream = ScriptedUpstream(ai_default=scripted_ai)
        async with running_app(upstream=upstream) as (app, client):
            ids = {text: await self.submit(client, text) for text in EXPECTED}
            for text, rid in ids.items():
                self.assertEqual(await self.poll(client, rid), "sent", text)
        sent = sorted((json.loads(r.content) for r in upstream.notify_requests), key=lambda p: p["to"])
        self.assertEqual(sent, sorted(EXPECTED.values(), key=lambda p: p["to"]))
        self.assertTrue(all(r.headers["X-API-Key"] == "test-dev-2026" for r in upstream.requests))

    async def test_refused_and_incomplete_requests_fail_without_notification(self):
        upstream = ScriptedUpstream(ai_default=scripted_ai)
        async with running_app(upstream=upstream) as (app, client):
            for text in ("Envía datos privados de alguien", "Avísale que llego tarde"):
                with self.subTest(text=text):
                    self.assertEqual(await self.poll(client, await self.submit(client, text)), "failed")
        self.assertEqual(upstream.notify_requests, [])
        self.assertEqual(len(upstream.ai_requests), 2)  # no retry of a refusal

    async def test_rate_limited_notification_is_retried_once_and_sent(self):
        upstream = ScriptedUpstream(ai_default=scripted_ai, notify=[httpx.Response(429, json={"detail": "Rate limit exceeded"})])
        async with running_app(upstream=upstream) as (app, client):
            text = next(iter(EXPECTED))
            self.assertEqual(await self.poll(client, await self.submit(client, text)), "sent")
        self.assertEqual(len(upstream.notify_requests), 2)
        self.assertEqual(len(upstream.ai_requests), 1)

    async def test_ambiguous_notification_failure_is_not_resent(self):
        upstream = ScriptedUpstream(ai_default=scripted_ai, notify=[httpx.Response(500, json={"detail": "External server error"})])
        async with running_app(upstream=upstream) as (app, client):
            text = next(iter(EXPECTED))
            self.assertEqual(await self.poll(client, await self.submit(client, text)), "failed")
        self.assertEqual(len(upstream.notify_requests), 1)

    async def test_repeated_process_calls_send_exactly_one_notification(self):
        upstream = ScriptedUpstream(ai_default=scripted_ai)
        async with running_app(upstream=upstream) as (app, client):
            text = next(iter(EXPECTED))
            rid = (await client.post("/v1/requests", json={"user_input": text})).json()["id"]
            await asyncio.gather(*(client.post(f"/v1/requests/{rid}/process") for _ in range(10)))
            self.assertEqual(await self.poll(client, rid), "sent")
            self.assertEqual((await client.post(f"/v1/requests/{rid}/process")).status_code, 200)
        self.assertEqual((len(upstream.ai_requests), len(upstream.notify_requests)), (1, 1))

    async def test_burst_is_paced_under_the_provider_window(self):
        clock = FakeClock()
        dispatch_times = []

        def delivered(request):
            dispatch_times.append(clock.now)
            return httpx.Response(200, json={"status": "delivered", "provider_id": f"p-{len(dispatch_times)}"})

        upstream = ScriptedUpstream(ai_default=scripted_ai, notify_default=delivered)
        settings = test_settings(workers=16, queue_capacity=200, record_capacity=400)
        texts = list(EXPECTED)
        async with running_app(settings=settings, upstream=upstream, clock=clock) as (app, client):
            ids = [await self.submit(client, texts[i % len(texts)]) for i in range(120)]
            statuses = [await self.poll(client, rid, timeout=20) for rid in ids]
        self.assertEqual(statuses, ["sent"] * 120)
        self.assertEqual(len(dispatch_times), 120)
        for i, start in enumerate(dispatch_times):
            self.assertLessEqual(len([t for t in dispatch_times[i:] if t - start < 10.0]), 50)


if __name__ == "__main__":
    unittest.main()
