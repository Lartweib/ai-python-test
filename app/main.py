from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal
import uuid

import httpx

from client import ai_extract, notify
from guardrails import parse_extraction

app = FastAPI(title="Notification Service (Technical Test)")

SYSTEM_PROMPT = (
    "Extract notification intent from the user message. Return ONLY a valid JSON "
    'object with exactly these keys: "to" (email or phone), "message" (content), '
    '"type" (either "email" or "sms"). No explanation, no markdown.'
)


# --- Pydantic models ---

class RequestCreate(BaseModel):
    user_input: str = Field(
        ..., example="Manda un mail a feda@test.com diciendo hola"
    )


class ResponseCreate(BaseModel):
    id: str = Field(..., example="550e8400-e29b-41d4-a716-446655440000")  # noqa: E501


RequestStatus = Literal["queued", "processing", "sent", "failed"]


class ResponseStatus(BaseModel):
    id: str = Field(..., example="550e8400-e29b-41d4-a716-446655440000")
    status: RequestStatus = Field(..., example="queued")


# --- In-memory store ---
_store: dict[str, dict] = {}  # id -> {user_input, status, created_at}


def _generate_id() -> str:
    return str(uuid.uuid4())


async def _run_pipeline(req_id: str) -> None:
    """Extract -> guardrails -> notify. Updates _store status."""
    entry = _store.get(req_id)
    if not entry:
        return

    entry["status"] = "processing"
    user_input = entry.get("user_input", "")

    try:
        content = await ai_extract(SYSTEM_PROMPT, user_input)
    except httpx.HTTPStatusError:
        entry["status"] = "failed"
        return

    parsed = parse_extraction(content)
    if not parsed:
        entry["status"] = "failed"
        return

    try:
        await notify(
            to=parsed["to"],
            message=parsed["message"],
            type_=parsed["type"],
        )
    except httpx.HTTPStatusError:
        entry["status"] = "failed"
        return

    entry["status"] = "sent"


# --- Endpoints ---
@app.post("/v1/requests", status_code=201, response_model=ResponseCreate)
def create_request(body: RequestCreate) -> ResponseCreate:
    """Ingesta de intenciones: crea una nueva solicitud en cola."""
    req_id = _generate_id()
    _store[req_id] = {
        "user_input": body.user_input,
        "status": "queued",
    }
    return ResponseCreate(id=req_id)


@app.post("/v1/requests/{req_id}/process")
async def process_request(req_id: str, background_tasks: BackgroundTasks) -> Response:
    """Procesamiento de envío: extrae, valida y notifica en background."""
    if req_id not in _store:
        raise HTTPException(status_code=404, detail="Request not found")

    status = _store[req_id]["status"]
    if status in ("processing", "sent", "failed"):
        return Response(status_code=200)

    _store[req_id]["status"] = "processing"
    background_tasks.add_task(_run_pipeline, req_id)
    return Response(status_code=202)


@app.get("/v1/requests/{req_id}", response_model=ResponseStatus)
def get_request_status(req_id: str) -> ResponseStatus:
    """Consulta de estado de una solicitud."""
    if req_id not in _store:
        raise HTTPException(status_code=404, detail="Request not found")
    return ResponseStatus(id=req_id, status=_store[req_id]["status"])
