"""HTTP client for the AI extract and notification provider."""

import httpx

PROVIDER_BASE = "http://localhost:3001"
API_KEY = "test-dev-2026"

_HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


async def ai_extract(system_prompt: str, user_input: str) -> str:
    """
    Call the AI extract endpoint and return the assistant content.
    Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{PROVIDER_BASE}/v1/ai/extract",
            json=payload,
            headers=_HEADERS,
        )
        resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


async def notify(to: str, message: str, type_: str) -> None:
    """
    Send a notification via the provider.
    Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    payload = {"to": to, "message": message, "type": type_}  # type_ -> "type" in JSON
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{PROVIDER_BASE}/v1/notify",
            json=payload,
            headers=_HEADERS,
        )
        resp.raise_for_status()
