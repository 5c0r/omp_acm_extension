"""Explicit hybrid retrieval: FTS + vector RRF, then entity graph expansion."""
import json
import os
from typing import Any

import httpx

from . import db
from .entities import vector_literal
from .llm import chat_json, embed, tokens

RRF_K = int(os.environ.get("ACM_RRF_K", "60"))


def scope_id(scope: str) -> int:
    kind, separator, name = scope.partition(":")
    if not separator or kind not in {"project", "user"} or not name:
        raise ValueError("scope must be project:<name> or user:<name>")
    db.ensure_schema()
    return db.get_or_create_scope(kind, name)


def _rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def _fts(query: str, scopes: list[int]) -> list[dict[str, Any]]:
    return _rows(
        "SELECT m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m "
        "JOIN scope s ON s.id = m.scope_id "
        "WHERE m.status = 'active' AND m.scope_id = ANY(%s) "
        "AND m.tsv @@ websearch_to_tsquery('english', %s) "
        "ORDER BY ts_rank_cd(m.tsv, websearch_to_tsquery('english', %s)) DESC LIMIT 20",
        (scopes, query, query),
    )


def _vector(query: str, scopes: list[int]) -> list[dict[str, Any]]:
    try:
        vector = embed([query])[0]
    except (httpx.HTTPError, IndexError, KeyError, TypeError):
        return []
    if len(vector) != db.EMBED_DIM:
        return []
    literal = vector_literal(vector)
    return _rows(
        "SELECT m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m "
        "JOIN scope s ON s.id = m.scope_id "
        "WHERE m.status = 'active' AND m.scope_id = ANY(%s) AND m.embedding IS NOT NULL "
        "ORDER BY m.embedding <=> %s::vector LIMIT 20",
        (scopes, literal),
    )


def _graph(memory_ids: list[int], scopes: list[int]) -> list[dict[str, Any]]:
    if not memory_ids:
        return []
    entity_rows = _rows("SELECT DISTINCT entity_id FROM mention WHERE memory_id = ANY(%s)", (memory_ids,))
    entities = [row["entity_id"] for row in entity_rows]
    if not entities:
        return []
    neighbor_rows = _rows(
        "SELECT DISTINCT CASE WHEN from_entity = ANY(%s) THEN to_entity ELSE from_entity END AS entity_id "
        "FROM edge WHERE from_entity = ANY(%s) OR to_entity = ANY(%s)",
        (entities, entities, entities),
    )
    related_entities = [*entities, *(row["entity_id"] for row in neighbor_rows)]
    return _rows(
        "SELECT DISTINCT ON (m.id) m.*, s.kind AS scope_kind, s.name AS scope_name FROM memory m "
        "JOIN scope s ON s.id = m.scope_id JOIN mention n ON n.memory_id = m.id "
        "WHERE m.status = 'active' AND m.scope_id = ANY(%s) AND n.entity_id = ANY(%s) "
        "AND NOT (m.id = ANY(%s)) ORDER BY m.id, m.importance DESC LIMIT 10",
        (scopes, related_entities, memory_ids),
    )


def _item(row: dict[str, Any], score: float, via_graph: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "content": row["content"],
        "importance": row["importance"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "source_ref": row["source_ref"],
        "scope": f"{row['scope_kind']}:{row['scope_name']}",
        "score": score,
        "via_graph": via_graph,
    }


def _rerank(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response = chat_json(
        "Rank candidate memory IDs by relevance to the query. Return every ID exactly once.",
        json.dumps({"query": query, "candidates": [{"id": item["id"], "content": item["content"]} for item in items]}),
        '{"ids":[integer]}',
    )
    if not response or not isinstance(response.get("ids"), list):
        return items
    by_id = {item["id"]: item for item in items}
    ranked = [by_id[item_id] for item_id in response["ids"] if isinstance(item_id, int) and item_id in by_id]
    return [*ranked, *(item for item in items if item["id"] not in response["ids"])]


def fetch(
    query: str, scope: str, budget_tokens: int = 1500, deep: bool = False, record_stat: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """Return provenance-tagged explicit retrieval; no anticipation cache is consulted."""
    if not query.strip():
        return {"items": []}
    current_scope = scope_id(scope)
    scopes = db.scope_chain(current_scope)
    records: dict[int, dict[str, Any]] = {}
    scores: dict[int, float] = {}
    via_graph: set[int] = set()

    def add(rows: list[dict[str, Any]], graph: bool = False) -> None:
        for rank, row in enumerate(rows, start=1):
            records[row["id"]] = row
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1 / (RRF_K + rank)
            if graph:
                via_graph.add(row["id"])

    add(_fts(query, scopes))
    add(_vector(query, scopes))
    top_ids = [memory_id for memory_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:5]]
    add(_graph(top_ids[:1], scopes), graph=True)

    ranked = [
        _item(records[memory_id], score, memory_id in via_graph)
        for memory_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ]
    if deep:
        ranked = _rerank(query, ranked)
    budget = max(0, budget_tokens)
    used = 0
    items: list[dict[str, Any]] = []
    for item in ranked:
        cost = tokens(item["content"])
        if used + cost > budget:
            continue
        used += cost
        items.append(item)
    if record_stat:
        db.bump_stat("explicit_fetch")
    return {"items": items}
