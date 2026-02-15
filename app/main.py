"""FastAPI application for the notification service."""

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import Response

from client import close_client
from pipeline import run as run_pipeline
from schemas import RequestCreate, ResponseCreate, ResponseStatus
from store import create, get, update_status


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_client()


app = FastAPI(title="Notification Service (Technical Test)", lifespan=lifespan)


# --- Endpoints ---
@app.post("/v1/requests", status_code=201, response_model=ResponseCreate)
def create_request(body: RequestCreate) -> ResponseCreate:
    """Ingesta de intenciones: crea una nueva solicitud en cola."""
    req_id = create(body.user_input)
    return ResponseCreate(id=req_id)


@app.post("/v1/requests/{req_id}/process")
async def process_request(req_id: str, background_tasks: BackgroundTasks) -> Response:
    """Procesamiento de envío: extrae, valida y notifica en background."""
    entry = get(req_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Request not found")

    status = entry["status"]
    if status in ("processing", "sent", "failed"):
        return Response(status_code=200)

    update_status(req_id, "processing")
    background_tasks.add_task(run_pipeline, req_id)
    return Response(status_code=202)


@app.get("/v1/requests/{req_id}", response_model=ResponseStatus)
def get_request_status(req_id: str) -> ResponseStatus:
    """Consulta de estado de una solicitud."""
    entry = get(req_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Request not found")
    return ResponseStatus(id=req_id, status=entry["status"])
