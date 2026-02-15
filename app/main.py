from fastapi import FastAPI
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
