import uuid
import json
import re
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-service")

app = FastAPI(title="Notification Service")

PROVIDER_URL = "http://localhost:3001"
API_KEY = "test-dev-2026"
HEADERS = {"X-API-Key": API_KEY}

# Simple in-memory store
requests_store: dict[str, dict] = {}


class UserInputRequest(BaseModel):
    user_input: str


FIELD_ALIASES = {
    "to": ["to", "recipient", "email", "phone"],
    "message": ["message", "body", "text"],
    "type": ["type", "channel", "method"],
}

VALID_TYPES = {"email", "sms"}


def normalize_keys(data: dict) -> dict:
    """Map possible field names to expected ones."""
    result = {}
    for canonical, aliases in FIELD_ALIASES.items():
        for key, value in data.items():
            if key.lower() in aliases:
                result[canonical] = value
                break
    return result


def extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction from LLM output."""
    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Try inside ```json block
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Try first {...}
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def parse_ai_response(content: str) -> dict | None:
    """Validate AI output and return normalized notification."""
    parsed = extract_json(content)

    if not parsed:
        logger.warning("No valid JSON found in AI response")
        return None

    data = normalize_keys(parsed)

    to = data.get("to")
    message = data.get("message")
    notif_type = data.get("type")

    if not to or not message or not notif_type:
        logger.warning(f"Incomplete data: {data}")
        return None

    notif_type = str(notif_type).lower()
    if notif_type not in VALID_TYPES:
        logger.warning(f"Invalid type: {notif_type}")
        return None

    return {
        "to": str(to),
        "message": str(message),
        "type": notif_type,
    }


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=5),
    retry=retry_if_exception(is_retryable),
    reraise=True,
)
async def send_notification(client: httpx.AsyncClient, payload: dict) -> dict:
    response = await client.post(
        f"{PROVIDER_URL}/v1/notify",
        json=payload,
        headers=HEADERS,
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


async def process_request(request_id: str, user_input: str):
    requests_store[request_id]["status"] = "processing"
    logger.info(f"{request_id} - processing")

    try:
        async with httpx.AsyncClient() as client:
            # Call AI
            ai_payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a notification dispatcher. Your job is to parse a user's natural language "
                            "request and extract the details needed to send a notification.\n\n"
                            "Respond ONLY with a JSON object using exactly these keys:\n"
                            '  "to"      -- the recipient email address or phone number\n'
                            '  "message" -- the text content to be sent\n'
                            '  "type"    -- either "email" (if sending to an email address) or "sms" (if sending to a phone number)\n\n'
                            "Rules:\n"
                            "- Infer the type from context: email addresses -> email, phone numbers -> sms.\n"
                            "- Keep the message faithful to what the user wants to communicate.\n"
                            "- Do not add explanations, markdown, or any text outside the JSON object.\n\n"
                            "Example input: 'Send an email to ana@company.com saying her invoice is ready'\n"
                            'Example output: {"to": "ana@company.com", "message": "Your invoice is ready", "type": "email"}'
                        ),
                    },
                    {"role": "user", "content": user_input},
                ]
            }

            ai_response = await client.post(
                f"{PROVIDER_URL}/v1/ai/extract",
                json=ai_payload,
                headers=HEADERS,
                timeout=30.0,
            )
            ai_response.raise_for_status()

            ai_data = ai_response.json()
            choices = ai_data.get("choices", [])

            if not choices:
                requests_store[request_id]["status"] = "failed"
                return

            content = choices[0].get("message", {}).get("content", "")

            notification = parse_ai_response(content)
            if not notification:
                requests_store[request_id]["status"] = "failed"
                return

            await send_notification(client, notification)
            requests_store[request_id]["status"] = "sent"

    except Exception as e:
        logger.error(f"{request_id} failed: {e}")
        requests_store[request_id]["status"] = "failed"


@app.post("/v1/requests", status_code=201)
async def create_request(body: UserInputRequest):
    request_id = str(uuid.uuid4())
    requests_store[request_id] = {
        "user_input": body.user_input,
        "status": "queued",
    }
    return {"id": request_id}


@app.post("/v1/requests/{request_id}/process")
async def process_request_endpoint(request_id: str, background_tasks: BackgroundTasks):
    if request_id not in requests_store:
        raise HTTPException(status_code=404, detail="Request not found")

    background_tasks.add_task(
        process_request,
        request_id,
        requests_store[request_id]["user_input"],
    )

    return {"id": request_id, "status": "processing"}


@app.get("/v1/requests/{request_id}")
async def get_request_status(request_id: str):
    if request_id not in requests_store:
        raise HTTPException(status_code=404, detail="Request not found")

    return {
        "id": request_id,
        "status": requests_store[request_id]["status"],
    }