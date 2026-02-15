"""HTTP client for the AI extract and notification provider."""

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

PROVIDER_BASE = "http://localhost:3001"
API_KEY = "test-dev-2026"

_HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

# Shared client with connection pooling to avoid EOF/reset under load
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


def _retry_on_rate_limit_or_server_error(exc: BaseException) -> bool:
    """Retry on 429 (rate limit) or 5xx (server error)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


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
    client = _get_client()
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


@retry(
    retry=retry_if_exception(_retry_on_rate_limit_or_server_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
async def notify(to: str, message: str, type_: str) -> None:
    """
    Send a notification via the provider. Retries on 429/5xx (up to 3 attempts).
    Raises httpx.HTTPStatusError on 4xx/5xx after retries exhausted.
    """
    payload = {"to": to, "message": message, "type": type_}
    client = _get_client()
    resp = await client.post(
        f"{PROVIDER_BASE}/v1/notify",
        json=payload,
        headers=_HEADERS,
    )
    resp.raise_for_status()
