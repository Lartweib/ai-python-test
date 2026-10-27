"""Intelligent Notification Service: FastAPI entry point (``uvicorn main:app``)."""

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from models import RequestCreated, RequestStatus, create_request_model
from pipeline import Pipeline
from provider_client import ProviderClient, build_http_client
from settings import Settings
from store import AdmitResult, QueueFull, RequestNotFound, RequestStore, ShuttingDown, StoreFull

logging.basicConfig(level=os.environ.get("NOTIFY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream call is too noisy under load

NOT_FOUND = {"detail": "request_not_found"}


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

    app = FastAPI(title="Notification Service (Technical Test)", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": _sanitized_errors(exc)})

    @app.post("/v1/requests", status_code=status.HTTP_201_CREATED, response_model=RequestCreated)
    async def create_request(body: CreateRequest, request: Request):  # type: ignore[valid-type]
        try:
            snapshot = request.app.state.store.create(body.user_input)
        except StoreFull:
            return _unavailable("store_full")
        except ShuttingDown:
            return _unavailable("shutting_down")
        return {"id": snapshot.id}

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

    return app


app = create_app()
