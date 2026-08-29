"""Trajectory-based anticipation that precomputes a session bundle off-path."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .llm import chat_json, tokens
from .sanitize import sanitize

from .retrieve import fetch, scope_id

BUNDLE_TTL = timedelta(minutes=10)
BUNDLE_BUDGET = 1500


def _predict(trajectory: list[dict[str, Any]]) -> list[str]:
    compact = [
        {"role": turn.get("role", "unknown"), "content": str(turn.get("content", ""))[:2000]}
        for turn in trajectory[-8:]
        if isinstance(turn, dict)
    ]
    predicted = chat_json(
        "Predict likely next information needs from an agent trajectory. Do not answer the current question.",
        json.dumps(compact),
        '{"intents":["short likely next information need"]}',
    )
    if not predicted or not isinstance(predicted.get("intents"), list):
        return []
    intents: list[str] = []
    for intent in predicted["intents"]:
        value = intent.get("query") if isinstance(intent, dict) else intent
        if isinstance(value, str) and value.strip() and value not in intents:
            intents.append(value.strip())
    return intents[:3]


def _render(items: list[dict[str, Any]]) -> str:
    lines = ["[acm memory]"]
    used = tokens(lines[0])
    for item in items:
        provenance = f"{item['scope']} · {item['kind']}"
        if item["source_ref"]:
            provenance += f" · {item['source_ref']}"
        line = sanitize(f"- [{provenance}] {item['content']}")
        if used + tokens(line) > BUNDLE_BUDGET:
            break
        used += tokens(line)
        lines.append(line)
    return "\n".join(lines)


def anticipate(session_id: str, scope: str, trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    """Predict, fetch, render, and upsert latest session bundle. Caller schedules this off-path."""
    if not session_id:
        raise ValueError("session_id is required")
    intents = _predict(trajectory)
    if not intents:
        return {"predicted_intents": [], "rendered": None}
    per_intent = max(1, BUNDLE_BUDGET // len(intents))
    items: dict[int, dict[str, Any]] = {}
    for intent in intents:
        for item in fetch(intent, scope, budget_tokens=per_intent, record_stat=False)["items"]:
            items.setdefault(item["id"], item)
    rendered = _render(sorted(items.values(), key=lambda item: item["score"], reverse=True))
    current_scope = scope_id(scope)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO bundle (session_id, scope_id, rendered, predicted_intents, expires_at) "
            "VALUES (%s, %s, %s, %s::jsonb, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET scope_id = EXCLUDED.scope_id, rendered = EXCLUDED.rendered, "
            "predicted_intents = EXCLUDED.predicted_intents, served_count = 0, expires_at = EXCLUDED.expires_at",
            (session_id, current_scope, rendered, json.dumps(intents), datetime.now(timezone.utc) + BUNDLE_TTL),
        )
    return {"predicted_intents": intents, "rendered": rendered}


def get_bundle(session_id: str) -> dict[str, Any] | None:
    """Indexed session-key lookup only: no embedding, FTS, graph, or model work."""
    with db.connect() as conn:
        row = conn.execute(
            "UPDATE bundle SET served_count = served_count + 1 WHERE session_id = %s AND expires_at > now() "
            "RETURNING rendered, predicted_intents, served_count, expires_at",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    db.bump_stat("bundle_injected")
    return {**row, "rendered": sanitize(row["rendered"])}
