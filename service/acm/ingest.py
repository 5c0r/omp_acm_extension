"""Asynchronous ingestion: qualify, extract, resolve, persist, deduplicate."""
import hashlib
import json
import queue
import re
import threading
from datetime import date
from typing import Any

import httpx

from . import db
from .architect import generate_architecture
from .entities import COSINE_THRESHOLD, resolve_entity, vector_literal
from .llm import chat_json, embed
from .sanitize import sanitize


_jobs: queue.Queue[int] = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

_EXTRACT_SCHEMA = (
    '{"memories":[{"kind":"fact|preference|episode|decision","content":"string",'
    '"importance":0.5,"valid_from":"YYYY-MM-DD|null","valid_until":"YYYY-MM-DD|null",'
    '"entities":[{"name":"string","aliases":["string"],"role":"string"}]}],'
    '"relations":[{"from":"entity name","to":"entity name","relation":"string"}]}'
)


def _scope_id(scope: str) -> int:
    kind, separator, name = scope.partition(":")
    if not separator or kind not in {"project", "user"} or not name:
        raise ValueError("scope must be project:<name> or user:<name>")
    db.ensure_schema()
    return db.get_or_create_scope(kind, name)


def start_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="acm-ingest", daemon=True)
        _worker.start()


def submit(scope: str, text: str, source_ref: str | None = None) -> int:
    text = sanitize(text)
    scope_id = _scope_id(scope)
    digest = hashlib.sha256(text.encode()).hexdigest()
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO ingest_job (scope_id, payload, digest) VALUES (%s, %s::jsonb, %s) RETURNING id",
            (scope_id, json.dumps({"text": text, "source_ref": source_ref}), digest),
        ).fetchone()
    _jobs.put(row["id"])
    return row["id"]


def status(job_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT id, status, result FROM ingest_job WHERE id = %s", (job_id,)).fetchone()
    if not row:
        raise KeyError(f"unknown ingest job {job_id}")
    return row


def _finish(job_id: int, state: str, result: dict[str, Any]) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE ingest_job SET status = %s, result = %s::jsonb WHERE id = %s",
            (state, json.dumps(result), job_id),
        )


def _valid_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _embedding(content: str) -> list[float] | None:
    try:
        vector = embed([content])[0]
    except (httpx.HTTPError, IndexError, KeyError, TypeError):
        return None
    return vector if len(vector) == db.EMBED_DIM else None


def _extract(scope_id: int, text: str) -> dict[str, Any] | None:
    architecture = generate_architecture("conversation and project context", scope_id=scope_id)
    extracted = chat_json(
        "Extract durable memories faithfully. Do not infer details not stated.",
        f"Architecture:\n{json.dumps(architecture)}\n\nText:\n{text}",
        _EXTRACT_SCHEMA,
    )
    if isinstance(extracted, dict) and isinstance(extracted.get("memories"), list) and extracted["memories"]:
        return extracted
    return None


def _memory(scope_id: int, item: dict[str, Any], source_ref: str | None) -> int | None:
    content = item.get("content")
    if not isinstance(content, str):
        return None
    content = sanitize(content)
    if not content:
        return None
    kind = item.get("kind") if item.get("kind") in {"fact", "preference", "episode", "decision"} else "fact"
    importance = item.get("importance", 0.5)
    importance = min(1.0, max(0.0, float(importance))) if isinstance(importance, (int, float)) else 0.5
    vector = _embedding(content)
    literal = vector_literal(vector) if vector else None
    if literal:
        with db.connect() as conn:
            duplicate = conn.execute(
                "SELECT id, 1 - (embedding <=> %s::vector) AS similarity FROM memory "
                "WHERE scope_id = %s AND kind = %s AND status = 'active' AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                (literal, scope_id, kind, literal),
            ).fetchone()
            if duplicate and duplicate["similarity"] >= COSINE_THRESHOLD + 0.05:
                conn.execute(
                    "UPDATE memory SET importance = LEAST(1.0, importance + 0.1), "
                    "last_accessed = now(), access_count = access_count + 1 WHERE id = %s",
                    (duplicate["id"],),
                )
                return duplicate["id"]
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO memory (scope_id, kind, content, importance, valid_from, valid_until, source_ref, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id",
            (
                scope_id,
                kind,
                content,
                importance,
                _valid_date(item.get("valid_from")),
                _valid_date(item.get("valid_until")),
                source_ref,
                literal,
            ),
        ).fetchone()
    return row["id"]




def _possessive_entities(content: Any) -> list[dict[str, Any]]:
    # ponytail: only recover possessive proper names; model extraction covers broader entity shapes.
    return [{"name": name, "aliases": [], "role": None} for name in re.findall(r"\b([A-Z][a-z]+)'s\b", content if isinstance(content, str) else "")]
def _mentions(scope_id: int, memory_id: int, items: Any) -> dict[str, int]:
    resolved: dict[str, int] = {}
    if not isinstance(items, list):
        return resolved
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        entity = resolve_entity(item["name"], scope_id, aliases)
        resolved[entity["canonical_name"].casefold()] = entity["id"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO mention (memory_id, entity_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (memory_id, entity_id) DO UPDATE SET role = EXCLUDED.role",
                (memory_id, entity["id"], item.get("role")),
            )
    return resolved


def _edges(scope_id: int, memory_id: int, relations: Any, entities: dict[str, int]) -> None:
    if not isinstance(relations, list):
        return
    for relation in relations:
        if not isinstance(relation, dict) or not all(isinstance(relation.get(key), str) for key in ("from", "to", "relation")):
            continue
        from_name, to_name, relation_name = (sanitize(relation[key]) for key in ("from", "to", "relation"))
        if not all((from_name, to_name, relation_name)):
            continue
        from_id = entities.get(from_name.casefold()) or resolve_entity(from_name, scope_id)["id"]
        to_id = entities.get(to_name.casefold()) or resolve_entity(to_name, scope_id)["id"]
        with db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM edge WHERE scope_id = %s AND from_entity = %s AND to_entity = %s "
                "AND relation = %s AND memory_id = %s",
                (scope_id, from_id, to_id, relation_name, memory_id),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO edge (scope_id, from_entity, to_entity, relation, memory_id) VALUES (%s, %s, %s, %s, %s)",
                    (scope_id, from_id, to_id, relation_name, memory_id),
                )


def _process(job_id: int) -> None:
    with db.connect() as conn:
        job = conn.execute("SELECT scope_id, payload, digest FROM ingest_job WHERE id = %s", (job_id,)).fetchone()
    if not job:
        return
    text = job["payload"]["text"]
    if not text.strip():
        _finish(job_id, "skipped", {"reason": "empty"})
        return
    with db.connect() as conn:
        previous = conn.execute(
            "SELECT id FROM ingest_job WHERE scope_id = %s AND digest = %s "
            "AND id <> %s AND status = 'done' LIMIT 1",
            (job["scope_id"], job["digest"], job_id),
        ).fetchone()
    if previous:
        _finish(job_id, "skipped", {"reason": "duplicate", "duplicate_of": previous["id"]})
        return

    extracted = _extract(job["scope_id"], text)
    if extracted is None:
        _finish(job_id, "failed", {"reason": "extraction unavailable"})
        return
    memory_ids: list[int] = []
    entities: dict[str, int] = {}
    for item in extracted["memories"]:
        if not isinstance(item, dict):
            continue
        if memory_id := _memory(job["scope_id"], item, job["payload"].get("source_ref")):
            memory_ids.append(memory_id)
            mentions = item.get("entities") or _possessive_entities(item.get("content"))
            entities.update(_mentions(job["scope_id"], memory_id, mentions))
    for memory_id in set(memory_ids):
        _edges(job["scope_id"], memory_id, extracted.get("relations"), entities)
    _finish(job_id, "done", {"memory_ids": sorted(set(memory_ids))})


def _run() -> None:
    while True:
        job_id = _jobs.get()
        try:
            _process(job_id)
        except Exception as err:  # worker failures must surface as job state, not kill future jobs
            _finish(job_id, "failed", {"error": str(err)})
        finally:
            _jobs.task_done()
