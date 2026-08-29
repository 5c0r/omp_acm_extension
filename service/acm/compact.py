"""Validated compaction and maintenance consolidation."""
import json
from typing import Any

from . import db
from .entities import vector_literal
from .llm import chat_json, tokens
from .retrieve import scope_id

VALIDATION_THRESHOLD = 0.80


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _files(file_ops: dict[str, list[str]] | None) -> str:
    if not isinstance(file_ops, dict):
        return ""
    read = {path for path in file_ops.get("read", []) if isinstance(path, str) and "://" not in path}
    modified = {
        path
        for key in ("written", "edited")
        for path in file_ops.get(key, [])
        if isinstance(path, str) and "://" not in path
    }
    if not read and not modified:
        return ""
    rows = []
    for path in sorted(read | modified)[:20]:
        mode = "RW" if path in read and path in modified else "Write" if path in modified else "Read"
        rows.append(f"{path} ({mode})")
    if len(read | modified) > 20:
        rows.append(f"[…{len(read | modified) - 20} files elided…]")
    return "<files>\n" + "\n".join(rows) + "\n</files>"


def _summary(task: str, material: str, budget_tokens: int, policy: str | None) -> str:
    response = chat_json(
        "Produce a faithful compact summary. Preserve explicit decisions, dates, preferences, and unresolved work.",
        f"TASK: {task}\nBudget: {budget_tokens} tokens\nPolicy: {policy or 'preserve durable context'}\n\n{material}",
        '{"summary":"string"}',
    )
    if response and isinstance(response.get("summary"), str) and response["summary"].strip():
        return response["summary"].strip()
    return material[: budget_tokens * 4]


def _probes(material: str) -> list[str]:
    response = chat_json(
        "Write five to eight factual questions whose answers must survive compaction.",
        f"TASK: probes\nFull input:\n{material}",
        '{"probes":["string"]}',
    )
    probes = response.get("probes") if response else None
    if isinstance(probes, list):
        probes = [probe for probe in probes if isinstance(probe, str) and probe.strip()]
        if 5 <= len(probes) <= 8:
            return probes
    return ["What explicit decision must remain?" for _ in range(5)]


def _validation(summary: str, probes: list[str]) -> float:
    schema = '{"answers":[' + ",".join('{"answerable":true}' for _ in probes) + "]}"
    response = chat_json(
        "For each probe, say whether its answer is recoverable from the summary alone.",
        f"TASK: validate\nSummary:\n{summary}\n\nProbes:\n{_serialize(probes)}",
        schema,
    )
    answers = response.get("answers") if response else None
    if not isinstance(answers, list) or not answers:
        return 0.0
    return sum(isinstance(answer, dict) and answer.get("answerable") is True for answer in answers[: len(probes)]) / len(probes)


def compact(
    conversation: list[dict[str, Any]],
    budget_tokens: int,
    turn_prefix: list[dict[str, Any]] | None = None,
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
    file_ops: dict[str, list[str]] | None = None,
    previous_preserve_data: dict[str, Any] | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    """Compact complete OMP preparation; opaque preserve data is carried by caller, never summarized."""
    files = _files(file_ops)
    history = _serialize(conversation)
    material = "\n\n".join(
        part
        for part in (
            f"Previous summary:\n{previous_summary}" if previous_summary else "",
            f"Custom instructions:\n{custom_instructions}" if custom_instructions else "",
            f"Conversation:\n{history}",
            files,
        )
        if part
    )
    full_input = "\n\n".join(
        part for part in (material, f"Turn prefix:\n{_serialize(turn_prefix)}" if turn_prefix else "") if part
    )
    final_summary = ""
    probes: list[str] = []
    score = 0.0
    attempt_budget = budget_tokens
    summary_material = material
    for _ in range(3):
        history_summary = _summary("history", summary_material, attempt_budget, policy)
        if turn_prefix:
            prefix_summary = _summary(
                "turn-prefix", f"History summary:\n{history_summary}\n\nTurn prefix:\n{_serialize(turn_prefix)}\n\n{files}", attempt_budget, policy
            )
            final_summary = f"{history_summary}\n---\n**Turn Context (split turn):**\n{prefix_summary}"
        else:
            final_summary = history_summary
        probes = _probes(full_input)
        score = _validation(final_summary, probes)
        if score >= VALIDATION_THRESHOLD:
            break
        attempt_budget = int(attempt_budget * 1.5)
        summary_material = f"{material}\n\nValidation probes that must be answerable from next summary:\n{_serialize(probes)}"
    ratio = tokens(final_summary) / max(1, tokens(full_input))
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO compaction (summary, validation_score, compression_ratio) VALUES (%s, %s, %s)",
            (final_summary, score, ratio),
        )
    return {"summary": final_summary, "validation_score": score, "compression_ratio": ratio, "probes": probes}


def _literal(vector: Any) -> str:
    return vector if isinstance(vector, str) else vector_literal(vector)


def consolidate(scope: str) -> dict[str, int]:
    """Merge same-kind near duplicates, decay active importance, archive expired facts."""
    current_scope = scope_id(scope)
    with db.connect() as conn:
        expired = conn.execute(
            "UPDATE memory SET status = 'archived' WHERE scope_id = %s AND status = 'active' "
            "AND valid_until IS NOT NULL AND valid_until < current_date RETURNING id",
            (current_scope,),
        ).fetchall()
        rows = conn.execute(
            "SELECT id, kind, embedding FROM memory WHERE scope_id = %s AND status = 'active' "
            "AND embedding IS NOT NULL ORDER BY id",
            (current_scope,),
        ).fetchall()
    merged = 0
    for row in rows:
        with db.connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM memory WHERE scope_id = %s AND kind = %s AND status = 'active' AND id > %s "
                "AND embedding IS NOT NULL AND embedding <=> %s::vector <= 0.05 ORDER BY id LIMIT 1",
                (current_scope, row["kind"], row["id"], _literal(row["embedding"])),
            ).fetchone()
            if duplicate:
                conn.execute("UPDATE memory SET importance = LEAST(1.0, importance + 0.1) WHERE id = %s", (row["id"],))
                conn.execute("UPDATE memory SET status = 'archived' WHERE id = %s", (duplicate["id"],))
                merged += 1

    with db.connect() as conn:
        conn.execute(
            "UPDATE memory SET importance = GREATEST(0.05, importance / "
            "(1 + EXTRACT(EPOCH FROM now() - created_at) / 86400 / (1 + access_count))) "
            "WHERE scope_id = %s AND status = 'active'",
            (current_scope,),
        )
    return {"merged": merged, "archived": len(expired)}
