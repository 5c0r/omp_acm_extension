"""FastAPI boundary for ACM lifecycle primitives."""
from contextlib import asynccontextmanager
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from . import db
from .anticipate import anticipate, get_bundle
from .architect import generate_architecture
from .compact import compact, consolidate
from .ingest import start_worker, status as ingest_status, submit
from .retrieve import fetch, scope_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.ensure_schema()
    start_worker()
    yield


app = FastAPI(title="ACM service", lifespan=lifespan)


class ArchitectRequest(BaseModel):
    scope: str
    description: str = Field(min_length=1)
    reference: str | None = None


class IngestRequest(BaseModel):
    scope: str
    text: str
    source_ref: str | None = None


class FetchRequest(BaseModel):
    query: str
    scope: str
    budget_tokens: int = Field(default=1500, ge=0)
    deep: bool = False


class AnticipateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    scope: str
    trajectory: list[dict[str, Any]]


class CompactRequest(BaseModel):
    conversation: list[dict[str, Any]]
    budget_tokens: int = Field(ge=1)
    turn_prefix: list[dict[str, Any]] | None = None
    previous_summary: str | None = None
    custom_instructions: str | None = None
    file_ops: dict[str, list[str]] | None = None
    policy: str | None = None


class ConsolidateRequest(BaseModel):
    scope: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/architect")
def architect(request: ArchitectRequest) -> dict[str, Any]:
    try:
        return generate_architecture(request.description, request.reference, scope_id(request.scope))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@app.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
def ingest(request: IngestRequest) -> dict[str, int]:
    try:
        return {"job_id": submit(request.scope, request.text, request.source_ref)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@app.get("/status/{job_id}")
def job_status(job_id: int) -> dict[str, Any]:
    try:
        return ingest_status(job_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.post("/fetch")
def fetch_memories(request: FetchRequest) -> dict[str, list[dict[str, Any]]]:
    try:
        return fetch(request.query, request.scope, request.budget_tokens, request.deep)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@app.post("/anticipate", status_code=status.HTTP_202_ACCEPTED)
def anticipate_turn(request: AnticipateRequest) -> dict[str, bool]:
    try:
        scope_id(request.scope)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    threading.Thread(target=anticipate, args=(request.session_id, request.scope, request.trajectory), daemon=True).start()
    return {"accepted": True}


@app.get("/bundle/{session_id}")
def bundle(session_id: str) -> dict[str, Any]:
    result = get_bundle(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="bundle not found")
    return result


@app.post("/compact")
def compact_session(request: CompactRequest) -> dict[str, Any]:
    return compact(
        request.conversation,
        request.budget_tokens,
        request.turn_prefix,
        request.previous_summary,
        request.custom_instructions,
        request.file_ops,
        request.policy,
    )


@app.post("/consolidate")
def consolidate_scope(request: ConsolidateRequest) -> dict[str, int]:
    try:
        return consolidate(request.scope)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@app.get("/stats")
def stats() -> dict[str, dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value FROM stats ORDER BY key").fetchall()
    return {"stats": {row["key"]: row["value"] for row in rows}}
