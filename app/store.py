"""In-memory store for notification request state."""

import uuid
from typing import Any

_store: dict[str, dict[str, Any]] = {}


def create(user_input: str) -> str:
    """Create a new request and return its id."""
    req_id = str(uuid.uuid4())
    _store[req_id] = {"user_input": user_input, "status": "queued"}
    return req_id


def get(req_id: str) -> dict[str, Any] | None:
    """Get a request by id."""
    return _store.get(req_id)


def update_status(req_id: str, status: str) -> None:
    """Update the status of a request."""
    if req_id in _store:
        _store[req_id]["status"] = status


def exists(req_id: str) -> bool:
    """Check if a request exists."""
    return req_id in _store
