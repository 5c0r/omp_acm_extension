"""Per-scope memory architecture generation and editable persistence."""
import json
import os
from pathlib import Path
from typing import Any

from . import db
from .llm import chat_json

_ARCH_DIR = Path(os.environ.get("ACM_ARCH_DIR", "/app/architectures"))

DEFAULT_ARCHITECTURE = {
    "categories": [
        {
            "name": "facts",
            "description": "Stable facts with temporal validity.",
            "examples": ["The team upgraded to Pro on April 3."],
            "retention": "until superseded",
            "must_preserve_verbatim": False,
        },
        {
            "name": "decisions",
            "description": "Chosen approaches and their reasons.",
            "examples": ["Use Postgres for durable ACM storage."],
            "retention": "long-term",
            "must_preserve_verbatim": True,
        },
        {
            "name": "preferences",
            "description": "Explicit user or project working preferences.",
            "examples": ["Use no new dependencies unless needed."],
            "retention": "long-term",
            "must_preserve_verbatim": False,
        },
    ],
    "extraction_guidance": "Extract only concrete facts, decisions, preferences, episodes, entities, and relations.",
    "compaction_policy": "Preserve decisions, temporal facts, user preferences, and unresolved work verbatim when possible.",
}


def _normalize(candidate: Any) -> dict | None:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("categories"), list):
        return None
    if len(candidate["categories"]) < 3 or any(not isinstance(item, dict) or not item.get("name") for item in candidate["categories"]):
        return None
    if not isinstance(candidate.get("extraction_guidance"), str) or not isinstance(candidate.get("compaction_policy"), str):
        return None
    return candidate


def _file(scope_id: int) -> Path:
    return _ARCH_DIR / f"{scope_id}.json"


def _read_file(scope_id: int) -> dict | None:
    try:
        return _normalize(json.loads(_file(scope_id).read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def _write_file(scope_id: int, spec: dict) -> None:
    try:
        _ARCH_DIR.mkdir(parents=True, exist_ok=True)
        _file(scope_id).write_text(json.dumps(spec, indent=2) + "\n")
    except OSError:
        pass  # ponytail: table remains authoritative when an optional editable path is read-only.


def _stored(scope_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT spec FROM architecture WHERE scope_id = %s", (scope_id,)).fetchone()
    return _normalize(row["spec"]) if row else None


def _save(scope_id: int, spec: dict) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO architecture (scope_id, spec) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (scope_id) DO UPDATE SET spec = EXCLUDED.spec, updated_at = now()",
            (scope_id, json.dumps(spec)),
        )


def generate_architecture(description: str, reference: str | None = None, scope_id: int | None = None) -> dict:
    """Return scope architecture; editable file overrides generated/table data."""
    if scope_id is not None:
        if spec := _read_file(scope_id):
            _save(scope_id, spec)
            return spec
        if spec := _stored(scope_id):
            _write_file(scope_id, spec)
            return spec

    prompt = f"Design a memory architecture for this scope: {description}"
    if reference:
        prompt += f"\nReference material:\n{reference}"
    spec = _normalize(
        chat_json(
            "Create a practical memory extraction and compaction architecture.",
            prompt,
            '{"categories":[{"name":"string","description":"string","examples":[],"retention":"string","must_preserve_verbatim":false}],"extraction_guidance":"string","compaction_policy":"string"}',
        )
    ) or json.loads(json.dumps(DEFAULT_ARCHITECTURE))
    if scope_id is not None:
        _save(scope_id, spec)
        _write_file(scope_id, spec)
    return spec
