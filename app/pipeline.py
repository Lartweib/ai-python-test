"""Pipeline: extract -> guardrails -> notify."""

import asyncio

import httpx

from client import ai_extract, notify
from guardrails import parse_extraction
from store import get, update_status

SYSTEM_PROMPT = """You are a notification intent extractor. Your task is to
parse a natural language request and output structured JSON.

## Output Schema (STRICT)
Return exactly one raw JSON object with these three keys — no additional keys,
no nesting, no explanations:
- "to": string — destination (email address or phone number)
- "message": string — the text/body to send
- "type": string — MUST be exactly "email" or "sms" (lowercase)

The response must contain ONLY the JSON object.
Do not include markdown code blocks.
Do not include commentary.
Use double quotes for keys and string values.

## Extraction Rules

1. Destination ("to"):
   - Email: extract any valid email matching user@domain.tld
   - Phone: extract digits in formats like:
       - 6XXXXXXXXX
       - 6XX-XXX-XXX
   - Keep the phone number exactly as written.
   - If multiple valid destinations are present, use the first one.
   - Never invent a destination.

2. Type ("type"):
   - "email" if:
       - The text contains: email, correo, mail, e-mail
       - OR the destination is an email address
   - "sms" if:
       - The text contains: sms, teléfono, tel, móvil, celular, phone
       - OR the destination is a phone number
   - If both phone and email appear, choose the type that matches the
     extracted destination.
   - The value must be exactly "email" or "sms".

3. Message ("message"):
   - Extract text after indicators such as:
       - "diciendo"
       - ":"
       - "con el mensaje"
       - "que" (only if clearly introducing the message after the destination)
   - Preserve the user's wording.
   - Do not paraphrase.
   - Trim leading/trailing whitespace.
   - If no explicit message is found, use a short placeholder like
     "Recordatorio".

## Validation Rules
- Do not hallucinate or fabricate missing data.
- If a valid destination cannot be confidently extracted, return:
  {"to":"","message":"","type":""}
- Ensure the output is valid JSON.
- Output only the JSON object and nothing else.

## Example
Input: "Manda un correo a feda@test.com diciendo hola"
Output: {"to":"feda@test.com","message":"hola","type":"email"}
"""


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
