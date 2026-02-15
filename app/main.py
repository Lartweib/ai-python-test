from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
import uuid

app = FastAPI(title="Notification Service (Technical Test)")


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


@app.get("/v1/requests/{req_id}", response_model=ResponseStatus)
def get_request_status(req_id: str) -> ResponseStatus:
    """Consulta de estado de una solicitud."""
    if req_id not in _store:
        raise HTTPException(status_code=404, detail="Request not found")
    return ResponseStatus(id=req_id, status=_store[req_id]["status"])
