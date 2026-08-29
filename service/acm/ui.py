"""Memory management and browse API shared by future terminal and web clients."""
import json

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import db
from .architect import load_architecture
from .entities import vector_literal
from .llm import embed
from .sanitize import sanitize

_LOG = logging.getLogger(__name__)

manage_router = APIRouter()
ui_router = APIRouter(prefix="/api/ui")


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None


class MergeRequest(BaseModel):
    target_id: int = Field(gt=0)


class AliasRequest(BaseModel):
    alias: str = Field(min_length=1)


def _memory_payload(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["scope"] = f"{result.pop('scope_kind')}:{result.pop('scope_name')}"
    result.pop("scope_id", None)
    return result


def _memory(memory_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m "
            "JOIN scope s ON s.id = m.scope_id WHERE m.id = %s",
            (memory_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="memory not found")
    return row

def _invalidate_bundles(conn, memory_ids: list[int]) -> None:
    conn.execute(
        "DELETE FROM bundle WHERE EXISTS ("
        "SELECT 1 FROM jsonb_array_elements_text(COALESCE(memory_ids, '[]'::jsonb)) AS item(value) "
        "WHERE item.value::bigint = ANY(%s))",
        (memory_ids,),
    )


def _embedding_literal(content: str) -> str | None:
    try:
        vector = embed([content])[0]
        return vector_literal(vector) if len(vector) == db.EMBED_DIM else None
    except Exception:
        _LOG.warning("ACM memory re-embedding failed", exc_info=True)
        return None


def _detail(memory_id: int) -> dict[str, Any]:
    memory = _memory(memory_id)
    with db.connect() as conn:
        entities = conn.execute(
            "SELECT e.id, e.canonical_name, e.aliases, n.role FROM mention n "
            "JOIN entity e ON e.id = n.entity_id WHERE n.memory_id = %s ORDER BY e.canonical_name",
            (memory_id,),
        ).fetchall()
        usage = conn.execute(
            "SELECT id, memory_id, use_type, session_id, scope, ts FROM usage_log "
            "WHERE memory_id = %s ORDER BY ts DESC, id DESC LIMIT 20",
            (memory_id,),
        ).fetchall()
    return {**_memory_payload(memory), "entities": entities, "usage": usage}


def _kind_key(value: Any) -> str:
    return str(value).strip().casefold().removesuffix("s")


def _merge_content(target: dict[str, Any], source: dict[str, Any]) -> str:
    target_content = target["content"].strip()
    source_content = source["content"]
    if not source_content.strip() or source_content in target_content:
        return target_content
    architecture = load_architecture(target["scope_id"])
    source_kind = _kind_key(source["kind"])
    preserve = next(
        (
            category.get("must_preserve_verbatim", False)
            for category in architecture["categories"]
            if _kind_key(category.get("name", "")) == source_kind
        ),
        False,
    )
    if preserve:
        return f"{target_content}\n{source_content}"
    extra = [line for line in source_content.splitlines() if line.strip() and line not in target_content]
    return "\n".join((target_content, *extra))


@manage_router.patch("/memories/{memory_id}")
def patch_memory(memory_id: int, request: MemoryPatch) -> dict[str, Any]:
    current = _memory(memory_id)
    values = request.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="at least one field is required")
    sets = ["updated_at = now()"]
    params: list[Any] = []
    if "content" in values:
        content = sanitize(values["content"])
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        sets.extend(("content = %s", "embedding = %s::vector"))
        params.extend((content, _embedding_literal(content)))
    if "importance" in values:
        sets.append("importance = %s")
        params.append(values["importance"])
    if "pinned" in values:
        sets.append("pinned = %s")
        params.append(values["pinned"])
    params.append(current["id"])
    with db.connect() as conn, conn.transaction():
        conn.execute(f"UPDATE memory SET {', '.join(sets)} WHERE id = %s", params)
        _invalidate_bundles(conn, [memory_id])
    return _memory_payload(_memory(memory_id))


@manage_router.post("/memories/{memory_id}/archive")
def archive_memory(memory_id: int) -> dict[str, Any]:
    _memory(memory_id)
    with db.connect() as conn, conn.transaction():
        conn.execute("UPDATE memory SET status = 'archived', updated_at = now() WHERE id = %s", (memory_id,))
        _invalidate_bundles(conn, [memory_id])
    return _memory_payload(_memory(memory_id))


@manage_router.post("/memories/{memory_id}/restore")
def restore_memory(memory_id: int) -> dict[str, Any]:
    current = _memory(memory_id)
    if current["merged_into"] is not None:
        raise HTTPException(status_code=409, detail="merged memory cannot be restored")
    with db.connect() as conn:
        conn.execute("UPDATE memory SET status = 'active', updated_at = now() WHERE id = %s", (memory_id,))
    return _memory_payload(_memory(memory_id))


@manage_router.post("/memories/{memory_id}/merge")
def merge_memory(memory_id: int, request: MergeRequest) -> dict[str, Any]:
    if memory_id == request.target_id:
        raise HTTPException(status_code=400, detail="target memory must differ")
    with db.connect() as conn, conn.transaction():
        source = conn.execute("SELECT * FROM memory WHERE id = %s FOR UPDATE", (memory_id,)).fetchone()
        target = conn.execute("SELECT * FROM memory WHERE id = %s FOR UPDATE", (request.target_id,)).fetchone()
        if not source or not target:
            raise HTTPException(status_code=404, detail="memory not found")
        if source["scope_id"] != target["scope_id"]:
            raise HTTPException(status_code=400, detail="memories must share a scope")
        if source["merged_into"] is not None:
            raise HTTPException(status_code=409, detail="source memory is already merged")
        if target["status"] != "active":
            raise HTTPException(status_code=400, detail="target memory must be active")
        content = _merge_content(target, source)
        conn.execute(
            "UPDATE memory SET content = %s, embedding = %s::vector, "
            "importance = LEAST(1.0, GREATEST(importance, %s) + 0.1), pinned = pinned OR %s, "
            "access_count = access_count + %s, updated_at = now() WHERE id = %s",
            (content, _embedding_literal(content), source["importance"], source["pinned"], source["access_count"], target["id"]),
        )
        conn.execute("UPDATE usage_log SET memory_id = %s WHERE memory_id = %s", (target["id"], source["id"]))
        conn.execute(
            "INSERT INTO mention (memory_id, entity_id, role) SELECT %s, entity_id, role FROM mention "
            "WHERE memory_id = %s ON CONFLICT DO NOTHING",
            (target["id"], source["id"]),
        )
        conn.execute("DELETE FROM mention WHERE memory_id = %s", (source["id"],))
        conn.execute("UPDATE edge SET memory_id = %s WHERE memory_id = %s", (target["id"], source["id"]))
        conn.execute(
            "UPDATE memory SET status = 'archived', merged_into = %s, updated_at = now() WHERE id = %s",
            (target["id"], source["id"]),
        )
        _invalidate_bundles(conn, [source["id"], target["id"]])
    return _memory_payload(_memory(request.target_id))


@manage_router.get("/entities/{entity_id}/aliases")
def entity_aliases(entity_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        entity = conn.execute("SELECT id, canonical_name, aliases FROM entity WHERE id = %s", (entity_id,)).fetchone()
    if not entity:
        raise HTTPException(status_code=404, detail="entity not found")
    return entity


@manage_router.post("/entities/{entity_id}/aliases")
def add_entity_alias(entity_id: int, request: AliasRequest) -> dict[str, Any]:
    alias = sanitize(request.alias)
    if not alias:
        raise HTTPException(status_code=400, detail="alias is required")
    with db.connect() as conn:
        entity = conn.execute("SELECT id, canonical_name, aliases FROM entity WHERE id = %s FOR UPDATE", (entity_id,)).fetchone()
        if not entity:
            raise HTTPException(status_code=404, detail="entity not found")
        aliases = entity["aliases"]
        if not any(existing.lower() == alias.lower() for existing in aliases):
            aliases = [*aliases, alias]
            entity = conn.execute("UPDATE entity SET aliases = %s WHERE id = %s RETURNING id, canonical_name, aliases", (aliases, entity_id)).fetchone()
    return entity


@manage_router.delete("/entities/{entity_id}/aliases")
def remove_entity_alias(entity_id: int, request: AliasRequest) -> dict[str, Any]:
    alias = sanitize(request.alias)
    if not alias:
        raise HTTPException(status_code=400, detail="alias is required")
    with db.connect() as conn:
        entity = conn.execute("SELECT id, canonical_name, aliases FROM entity WHERE id = %s FOR UPDATE", (entity_id,)).fetchone()
        if not entity:
            raise HTTPException(status_code=404, detail="entity not found")
        aliases = [existing for existing in entity["aliases"] if existing.casefold() != alias.casefold()]
        if len(aliases) != len(entity["aliases"]):
            entity = conn.execute("UPDATE entity SET aliases = %s WHERE id = %s RETURNING id, canonical_name, aliases", (aliases, entity_id)).fetchone()
    return entity


_DEMO_SCOPE = "browse-live"
_DEMO_CONTENT = "Widget API key rotates weekly; regenerate it through the internal secrets console every Monday."
_DEMO_SEED_LOCK = 8_465_415


@ui_router.post("/seed-demo")
def seed_demo() -> dict[str, Any]:
    embedding = _embedding_literal(_DEMO_CONTENT)
    scope_id = db.get_or_create_scope("project", _DEMO_SCOPE)
    with db.connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DEMO_SEED_LOCK,))
        memory = conn.execute(
            "SELECT id FROM memory WHERE scope_id = %s AND content = %s",
            (scope_id, _DEMO_CONTENT),
        ).fetchone()
        if not memory:
            memory = conn.execute(
                "INSERT INTO memory (scope_id, kind, content, importance, source_ref, embedding) "
                "VALUES (%s, 'fact', %s, 0.8, 'seed:browse-live', %s::vector) RETURNING id",
                (scope_id, _DEMO_CONTENT, embedding),
            ).fetchone()
            created = True
        else:
            created = False
    return {"memory_id": memory["id"], "created": created}


@ui_router.get("/memories")
def browse_memories(
    scope: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if status not in {None, "active", "archived"}:
        raise HTTPException(status_code=400, detail="status must be active or archived")
    clauses = ["TRUE"]
    params: list[Any] = []
    if scope:
        clauses.append("s.kind || ':' || s.name = %s")
        params.append(scope)
    if kind:
        clauses.append("m.kind = %s")
        params.append(kind)
    if status:
        clauses.append("m.status = %s")
        params.append(status)
    if q and q.strip():
        clauses.append("m.content ILIKE %s")
        params.append(f"%{q.strip()}%")
    where = " AND ".join(clauses)
    with db.connect() as conn:
        total = conn.execute(f"SELECT count(*) AS count FROM memory m JOIN scope s ON s.id = m.scope_id WHERE {where}", params).fetchone()["count"]
        rows = conn.execute(
            f"SELECT m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m "
            f"JOIN scope s ON s.id = m.scope_id WHERE {where} ORDER BY m.updated_at DESC, m.id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "items": [_memory_payload(row) for row in rows]}


@ui_router.get("/memories/{memory_id}")
def memory_detail(memory_id: int) -> dict[str, Any]:
    return _detail(memory_id)


@ui_router.get("/usage/{memory_id}")
def memory_usage(
    memory_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    _memory(memory_id)
    with db.connect() as conn:
        total = conn.execute("SELECT count(*) AS count FROM usage_log WHERE memory_id = %s", (memory_id,)).fetchone()["count"]
        items = conn.execute(
            "SELECT id, memory_id, use_type, session_id, scope, ts FROM usage_log WHERE memory_id = %s "
            "ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
            (memory_id, limit, offset),
        ).fetchall()
    return {"total": total, "items": items}


@ui_router.get("/dashboard")
def dashboard() -> dict[str, Any]:
    with db.connect() as conn:
        scope_totals = conn.execute(
            "SELECT s.kind || ':' || s.name AS key, count(*) AS count FROM memory m JOIN scope s ON s.id = m.scope_id GROUP BY key"
        ).fetchall()
        kind_totals = conn.execute("SELECT kind AS key, count(*) AS count FROM memory GROUP BY kind").fetchall()
        status_totals = conn.execute("SELECT status AS key, count(*) AS count FROM memory GROUP BY status").fetchall()
        usage_totals = conn.execute("SELECT use_type AS key, count(*) AS count FROM usage_log GROUP BY use_type").fetchall()
        stats = conn.execute(
            "SELECT key, value FROM stats WHERE key IN ('explicit_fetch', 'bundle_injected', 'bundle_missed')"
        ).fetchall()
        ingest = conn.execute("SELECT status AS key, count(*) AS count FROM ingest_job GROUP BY status").fetchall()
        compaction_scores = conn.execute(
            "SELECT id, scope_id, validation_score, compression_ratio, status, created_at FROM compaction ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        top_used = conn.execute(
            "SELECT m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m JOIN scope s ON s.id = m.scope_id "
            "WHERE m.status = 'active' ORDER BY m.access_count DESC, m.last_accessed DESC LIMIT 10"
        ).fetchall()
    stat_values = {row["key"]: int(row["value"]) for row in stats}
    return {
        "totals": {
            "scope": {row["key"]: row["count"] for row in scope_totals},
            "kind": {row["key"]: row["count"] for row in kind_totals},
            "status": {row["key"]: row["count"] for row in status_totals},
            "usage": {row["key"]: row["count"] for row in usage_totals},
        },
        "bundles": {
            "served": stat_values.get("bundle_injected", 0),
            "hit": stat_values.get("bundle_injected", 0),
            "miss": stat_values.get("bundle_missed", 0),
        },
        "explicit_fetch": stat_values.get("explicit_fetch", 0),
        "ingest_outcomes": {row["key"]: row["count"] for row in ingest},
        "compaction_scores": compaction_scores,
        "top_used": [_memory_payload(row) for row in top_used],
    }
