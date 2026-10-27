"""Public API schemas, request state and internal failure categories."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, create_model, field_validator


class Status(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (Status.SENT, Status.FAILED)


class FailureCode(str, Enum):
    """Internal reason for a ``failed`` request. Never returned by the public API."""

    EXTRACTION_REFUSED = "extraction_refused"
    EXTRACTION_UNSTRUCTURED = "extraction_unstructured"
    EXTRACTION_INVALID = "extraction_invalid"
    EXTRACTION_MALFORMED = "extraction_malformed"
    AI_UNAVAILABLE = "ai_unavailable"
    AI_REJECTED = "ai_rejected"
    NOTIFICATION_REJECTED = "notification_rejected"
    NOTIFICATION_UNAVAILABLE = "notification_unavailable"
    DELIVERY_UNKNOWN = "delivery_unknown"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNADMITTED_EXPIRED = "unadmitted_expired"
    SHUTDOWN = "shutdown"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Notification:
    to: str
    message: str
    type: Literal["email", "sms"]

    def as_payload(self) -> dict:
        return {"to": self.to, "message": self.message, "type": self.type}


@dataclass(frozen=True)
class RequestSnapshot:
    """Immutable copy of a stored request; the store never hands out live records."""

    id: str
    user_input: str
    status: Status
    admitted: bool
    created_at: float
    admitted_at: float | None = None
    deadline: float | None = None
    finished_at: float | None = None
    ai_attempts: int = 0
    notify_attempts: int = 0
    provider_id: str | None = None
    failure_code: FailureCode | None = None

    def public(self) -> dict:
        return {"id": self.id, "status": self.status.value}


class RequestCreated(BaseModel):
    id: str


class RequestStatus(BaseModel):
    id: str
    status: Status


class _CreateRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_input: StrictStr

    @field_validator("user_input")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_input must not be blank")
        return value  # stored exactly as received


def create_request_model(max_chars: int) -> type[BaseModel]:
    """Build the creation schema with the configured input length bound."""
    return create_model(
        "CreateRequest",
        __base__=_CreateRequestBase,
        user_input=(StrictStr, Field(max_length=max_chars)),
    )
