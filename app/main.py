"""Intelligent Notification Service: FastAPI entry point (``uvicorn main:app``)."""

import asyncio
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from models import RequestCreated, RequestStatus, create_request_model
from pipeline import Pipeline
from provider_client import ProviderClient, build_http_client
from settings import Settings
from store import (
    AdmitResult,
    IdempotencyMismatch,
    QueueFull,
    RequestNotFound,
    RequestStore,
    ShuttingDown,
    StoreFull,
)

logging.basicConfig(level=os.environ.get("NOTIFY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream call is too noisy under load

TITLE = "Notification Service (Technical Test)"
NOT_FOUND = {"detail": "request_not_found"}
IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")  # visible ASCII, no spaces


def _unavailable(reason: str) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": reason})


def _sanitized_errors(exc: RequestValidationError) -> list[dict]:
    """Keep only the error type and a fixed location; never echo input, context or unknown field names."""
    errors = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        safe_loc = ["body", "user_input"] if loc[:2] == ("body", "user_input") else ["body"]
        errors.append({"type": error.get("type", "invalid"), "loc": safe_loc})
    return errors


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rand: Callable[[], float] | None = None,
) -> FastAPI:
    """Build an isolated, fully wired instance (used by tests)."""
    return configure(FastAPI(title=TITLE), settings, transport, clock, sleep, rand)


def configure(
    app: FastAPI,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rand: Callable[[], float] | None = None,
) -> FastAPI:
    """Attach the lifespan, error handler and routes to ``app``; resources start only with the lifespan."""
    settings = settings or Settings.from_env()
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    rand = rand or random.random
    CreateRequest = create_request_model(settings.input_max_chars)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = RequestStore(
            capacity=settings.record_capacity,
            unadmitted_ttl=settings.unadmitted_ttl_seconds,
            job_deadline=settings.job_deadline_seconds,
            clock=clock,
        )
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_capacity)
        http = build_http_client(settings, transport)
        pipeline = Pipeline(
            store=store,
            queue=queue,
            client=ProviderClient(http, settings),
            settings=settings,
            clock=clock,
            sleep=sleep,
            rand=rand,
        )
        app.state.store, app.state.queue, app.state.http, app.state.pipeline = store, queue, http, pipeline
        pipeline.start()
        try:
            yield
        finally:
            await pipeline.stop()
            await http.aclose()

    app.router.lifespan_context = lifespan

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": _sanitized_errors(exc)})

    @app.post(
        "/v1/requests",
        status_code=status.HTTP_201_CREATED,
        response_model=RequestCreated,
        responses={
            400: {"description": "Malformed Idempotency-Key"},
            422: {"description": "Invalid body, or Idempotency-Key reused with a different body"},
        },
    )
    async def create_request(
        body: CreateRequest,  # type: ignore[valid-type]
        request: Request,
        idempotency_key: str | None = Header(default=None, description="Optional; repeats return the original id"),
    ):
        store = request.app.state.store
        replayed = False
        try:
            if idempotency_key is None:
                snapshot = store.create(body.user_input)
            elif not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "invalid_idempotency_key"})
            else:
                snapshot, replayed = store.create_idempotent(body.user_input, idempotency_key)
        except IdempotencyMismatch:
            return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "idempotency_key_reused"})
        except StoreFull:
            return _unavailable("store_full")
        except ShuttingDown:
            return _unavailable("shutting_down")
        headers = {"Idempotent-Replayed": "true"} if replayed else None
        return JSONResponse(status_code=status.HTTP_201_CREATED, content={"id": snapshot.id}, headers=headers)

    @app.post(
        "/v1/requests/{request_id}/process",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RequestStatus,
        responses={200: {"model": RequestStatus, "description": "Already finished; nothing is resent"}},
    )
    async def process_request(request_id: str, request: Request):
        state = request.app.state
        try:
            result, snapshot = state.store.admit(request_id, state.queue)
        except RequestNotFound:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=NOT_FOUND)
        except QueueFull:
            return _unavailable("queue_full")
        except ShuttingDown:
            return _unavailable("shutting_down")
        code = status.HTTP_200_OK if result is AdmitResult.TERMINAL else status.HTTP_202_ACCEPTED
        return JSONResponse(status_code=code, content=snapshot.public())

    @app.get("/v1/requests/{request_id}", response_model=RequestStatus)
    async def get_request(request_id: str, request: Request):
        snapshot = request.app.state.store.get(request_id)
        if snapshot is None:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=NOT_FOUND)
        return snapshot.public()

    @app.get("/health", responses={503: {"description": "Not accepting work (stopping or degraded)"}})
    async def health(request: Request):
        # Reports this service only; the provider is never called.
        if request.app.state.store.closed:
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "unavailable"})
        return {"status": "ok"}

    return app


app = FastAPI(title=TITLE)
configure(app)
