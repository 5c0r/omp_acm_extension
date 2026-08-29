"""Entity resolution: exact aliases, trigram names, then embedding cosine."""
import os
from typing import Any

from . import db
from .sanitize import sanitize

from .llm import embed

TRIGRAM_THRESHOLD = float(os.environ.get("ACM_ENTITY_TRIGRAM", "0.75"))
COSINE_THRESHOLD = float(os.environ.get("ACM_ENTITY_COSINE", "0.90"))


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


def _scope_order(scope_id: int) -> list[int]:
    """User scope before project scope for identity continuity."""
    return list(reversed(db.scope_chain(scope_id)))


def _with_aliases(row: dict[str, Any], aliases: list[str]) -> dict[str, Any]:
    combined = list(dict.fromkeys([*row["aliases"], *aliases]))
    if combined != row["aliases"]:
        with db.connect() as conn:
            conn.execute("UPDATE entity SET aliases = %s WHERE id = %s", (combined, row["id"]))
        row = {**row, "aliases": combined}
    return row


def _exact(name: str, scopes: list[int]) -> dict[str, Any] | None:
    for scope_id in scopes:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM entity WHERE scope_id = %s AND (lower(canonical_name) = lower(%s) "
                "OR EXISTS (SELECT 1 FROM unnest(aliases) alias WHERE lower(alias) = lower(%s)))",
                (scope_id, name, name),
            ).fetchone()
        if row:
            return row
    return None


def _trigram(name: str, scopes: list[int]) -> dict[str, Any] | None:
    for scope_id in scopes:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT *, similarity(canonical_name, %s) AS match_score FROM entity "
                "WHERE scope_id = %s ORDER BY match_score DESC LIMIT 1",
                (name, scope_id),
            ).fetchone()
        if row and row["match_score"] >= TRIGRAM_THRESHOLD:
            return row
    return None


def _cosine(name: str, scopes: list[int]) -> tuple[dict[str, Any] | None, list[float] | None]:
    try:
        vector = embed([name])[0]
    except (IndexError, KeyError, OSError):
        return None, None
    if len(vector) != db.EMBED_DIM:
        return None, None
    literal = vector_literal(vector)
    for scope_id in scopes:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT *, 1 - (embedding <=> %s::vector) AS match_score FROM entity "
                "WHERE scope_id = %s AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 1",
                (literal, scope_id, literal),
            ).fetchone()
        if row and row["match_score"] >= COSINE_THRESHOLD:
            return row, vector
    return None, vector


def resolve_entity(name: str, scope_id: int, aliases: list[str] | None = None) -> dict[str, Any]:
    """Resolve or register `name`; never creates a duplicate above configured thresholds."""
    name = sanitize(name)
    if not name:
        raise ValueError("entity name is required")
    aliases = list(dict.fromkeys(sanitize(alias) for alias in aliases or [] if isinstance(alias, str) and sanitize(alias)))
    scopes = _scope_order(scope_id)
    for candidate in (_exact(name, scopes), _trigram(name, scopes)):
        if candidate:
            return _with_aliases(candidate, [name, *aliases])
    candidate, vector = _cosine(name, scopes)
    if candidate:
        return _with_aliases(candidate, [name, *aliases])
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO entity (scope_id, canonical_name, aliases, embedding) "
            "VALUES (%s, %s, %s, %s::vector) RETURNING *",
            (scope_id, name, aliases, vector_literal(vector) if vector else None),
        ).fetchone()
    return row
