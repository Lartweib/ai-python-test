"""Pydantic schemas for the notification service API."""

from typing import Literal

from pydantic import BaseModel, Field


class RequestCreate(BaseModel):
    user_input: str = Field(
        ..., example="Manda un mail a feda@test.com diciendo hola"
    )


class ResponseCreate(BaseModel):
    id: str = Field(  # noqa: E501
        ..., example="550e8400-e29b-41d4-a716-446655440000"
    )


RequestStatus = Literal["queued", "processing", "sent", "failed"]


class ResponseStatus(BaseModel):
    id: str = Field(..., example="550e8400-e29b-41d4-a716-446655440000")
    status: RequestStatus = Field(..., example="queued")
