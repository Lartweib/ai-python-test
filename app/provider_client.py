"""Authenticated HTTP calls to the AI and notification provider.

Each call reads at most ``MAX_RESPONSE_BYTES`` and raises a typed error that
says whether a retry is safe. Error messages never contain upstream bodies,
user input or credentials.
"""

import asyncio
import json
import time
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx

from extraction import build_messages
from models import FailureCode, Notification
from settings import HTTP_MAX_CONNECTIONS, MAX_CONTENT_BYTES, MAX_RESPONSE_BYTES, Settings

AI_PATH = "/v1/ai/extract"
NOTIFY_PATH = "/v1/notify"
_AI_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
# Failures that prove the request never left this process.
_PRESEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class AIError(Exception):
    def __init__(self, code: FailureCode, reason: str, retryable: bool):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.retryable = retryable


class NotifyOutcome(Enum):
    RATE_LIMITED = "rate_limited"  # definitive rejection; safe to resend after cooldown
    PRESEND_FAILURE = "presend_failure"  # never transmitted; safe to resend
    REJECTED = "rejected"  # permanent client error; do not resend
    UNKNOWN = "unknown"  # may have been delivered; never resend


class NotifyError(Exception):
    def __init__(self, outcome: NotifyOutcome, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.retry_after = retry_after


class _BodyTooLarge(Exception):
    pass


def build_http_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.provider_base_url,
        headers={"X-API-Key": settings.provider_api_key},
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.ai_read_timeout_seconds,
            write=settings.connect_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_MAX_CONNECTIONS),
        # retries=0: attempt counters must match the pipeline's retry policy exactly.
        transport=transport or httpx.AsyncHTTPTransport(retries=0),
    )


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        now = time.time() if now is None else now
        return max(0.0, when.timestamp() - now)
    return seconds if seconds >= 0 else None


async def _read_capped(response: httpx.Response) -> bytes:
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise _BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


class ProviderClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._settings = settings
        self._notify_timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.notify_read_timeout_seconds,
            write=settings.connect_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )

    async def extract(self, user_input: str, retry_feedback: str | None = None) -> str:
        """Return the AI's raw message content, or raise ``AIError``."""
        body = {"messages": build_messages(user_input, retry_feedback)}
        try:
            async with asyncio.timeout(self._settings.ai_attempt_timeout_seconds):
                async with self._http.stream("POST", AI_PATH, json=body) as response:
                    status = response.status_code
                    if status != 200:
                        if status in _AI_RETRYABLE_STATUSES:
                            raise AIError(FailureCode.AI_UNAVAILABLE, f"ai_status_{status}", retryable=True)
                        raise AIError(FailureCode.AI_REJECTED, f"ai_status_{status}", retryable=False)
                    raw = await _read_capped(response)
        except TimeoutError:
            raise AIError(FailureCode.AI_UNAVAILABLE, "ai_attempt_timeout", retryable=True) from None
        except _BodyTooLarge:
            raise AIError(FailureCode.EXTRACTION_INVALID, "ai_body_too_large", retryable=False) from None
        except httpx.TransportError as exc:
            raise AIError(FailureCode.AI_UNAVAILABLE, f"ai_transport_{type(exc).__name__}", retryable=True) from None
        except httpx.HTTPError as exc:
            raise AIError(FailureCode.AI_REJECTED, f"ai_http_{type(exc).__name__}", retryable=False) from None
        return self._content(raw)

    @staticmethod
    def _content(raw: bytes) -> str:
        def malformed():
            return AIError(FailureCode.AI_UNAVAILABLE, "ai_malformed_envelope", retryable=True)

        try:
            envelope = json.loads(raw)
        except ValueError:
            raise malformed() from None
        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise malformed() from None
        if not isinstance(content, str) or not content.strip():
            raise malformed()
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise AIError(FailureCode.EXTRACTION_INVALID, "ai_content_too_large", retryable=False)
        return content

    async def notify(self, notification: Notification) -> str:
        """Send one notification; return the provider receipt ID or raise ``NotifyError``."""
        try:
            async with asyncio.timeout(self._settings.notify_attempt_timeout_seconds):
                async with self._http.stream(
                    "POST", NOTIFY_PATH, json=notification.as_payload(), timeout=self._notify_timeout
                ) as response:
                    status = response.status_code
                    if status == 429:
                        retry_after = parse_retry_after(response.headers.get("Retry-After"))
                        raise NotifyError(NotifyOutcome.RATE_LIMITED, "notify_status_429", retry_after)
                    if status >= 500 or (200 < status < 300):
                        raise NotifyError(NotifyOutcome.UNKNOWN, f"notify_status_{status}")
                    if status != 200:
                        raise NotifyError(NotifyOutcome.REJECTED, f"notify_status_{status}")
                    raw = await _read_capped(response)
        except TimeoutError:
            raise NotifyError(NotifyOutcome.UNKNOWN, "notify_attempt_timeout") from None
        except _BodyTooLarge:
            raise NotifyError(NotifyOutcome.UNKNOWN, "notify_body_too_large") from None
        except _PRESEND_ERRORS as exc:
            raise NotifyError(NotifyOutcome.PRESEND_FAILURE, f"notify_transport_{type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise NotifyError(NotifyOutcome.UNKNOWN, f"notify_transport_{type(exc).__name__}") from None
        return self._receipt(raw)

    @staticmethod
    def _receipt(raw: bytes) -> str:
        try:
            receipt = json.loads(raw)
        except ValueError:
            receipt = None
        if (
            isinstance(receipt, dict)
            and receipt.get("status") == "delivered"
            and isinstance(receipt.get("provider_id"), str)
            and receipt["provider_id"]
        ):
            return receipt["provider_id"]
        raise NotifyError(NotifyOutcome.UNKNOWN, "notify_invalid_receipt")
