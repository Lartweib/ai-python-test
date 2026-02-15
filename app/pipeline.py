"""Pipeline: extract -> guardrails -> notify."""

import asyncio

import httpx

from client import ai_extract, notify
from guardrails import parse_extraction
from store import get, update_status

SYSTEM_PROMPT = (
    "Extract notification intent from the user message. "
    "Return ONLY a valid JSON object with keys: "
    '"to" (email or phone), "message" (content), "type" ("email" or "sms"). '
    "No explanation, no markdown."
)

_pipeline_semaphore = asyncio.Semaphore(30)


async def run(req_id: str) -> None:
    """Extract -> guardrails -> notify. Limited by semaphore."""
    async with _pipeline_semaphore:
        entry = get(req_id)
        if not entry:
            return

        update_status(req_id, "processing")
        user_input = entry.get("user_input", "")

        try:
            content = await ai_extract(SYSTEM_PROMPT, user_input)
        except httpx.HTTPStatusError:
            update_status(req_id, "failed")
            return

        parsed = parse_extraction(content)
        if not parsed:
            update_status(req_id, "failed")
            return

        try:
            await notify(
                to=parsed["to"],
                message=parsed["message"],
                type_=parsed["type"],
            )
        except httpx.HTTPStatusError:
            update_status(req_id, "failed")
            return

        update_status(req_id, "sent")
