"""Validated compaction and maintenance consolidation."""
import json
import re
import os
from typing import Any

from . import db
from .architect import DEFAULT_ARCHITECTURE
from .entities import vector_literal
from .llm import chat_json, tokens
from .retrieve import scope_id

VALIDATION_THRESHOLD = 0.80
COMPACT_MAX_OUTPUT = int(os.environ.get("ACM_COMPACT_MAX_OUTPUT", "2048"))



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


def _summary(task: str, material: str, budget_tokens: int, policy: str | None) -> str | None:
    response = chat_json(
        f"Produce a faithful compact summary in at most {budget_tokens} tokens. Preserve explicit decisions, dates, preferences, and unresolved work. "
        "End with Exact validation evidence: one separate bullet for every reference_answer in Validation references and every Mandatory verbatim value. "
        "Copy each bullet value character-for-character; never paraphrase or reorder it. Summarize repeated facts instead of listing them.",
        f"TASK: {task}\nBudget: {budget_tokens} tokens\nPolicy: {policy or 'preserve durable context'}\n\n{material}",
        '{"summary":"string"}',
        max_tokens=min(budget_tokens, COMPACT_MAX_OUTPUT),
    )
    if response and isinstance(response.get("summary"), str) and response["summary"].strip():
        return response["summary"].strip()
    return None


def _evidence(material: str) -> tuple[list[dict[str, str]], list[str]] | None:
    categories = [
        {"name": category["name"], "must_preserve_verbatim": category.get("must_preserve_verbatim", False)}
        for category in DEFAULT_ARCHITECTURE["categories"]
    ]
    response = chat_json(
        "Generate exactly five distinct recoverability probes from full input: decisions first, then dates, entities, preferences, and files. "
        "Each reference_answer must be an exact source substring under 40 characters. "
        "List must_preserve_verbatim only for explicit decisions; use [] when none exist.",
        f"TASK: probes\nArchitecture:\n{_serialize(categories)}\n\nFull input:\n{material}",
        '{"probes":[{"question":"string","reference_answer":"string"}],"must_preserve_verbatim":["string"]}',
        max_tokens=256,
    )
    raw_pairs = response.get("probes") if response else None
    raw_mandatory = response.get("must_preserve_verbatim") if response else None
    if isinstance(raw_mandatory, str):
        raw_mandatory = [raw_mandatory]
    if not isinstance(raw_pairs, list) or not 5 <= len(raw_pairs) <= 8 or not isinstance(raw_mandatory, list):
        return None
    pairs = [
        {"question": item["question"].strip(), "reference_answer": item["reference_answer"].strip()}
        for item in raw_pairs
        if isinstance(item, dict)
        and isinstance(item.get("question"), str)
        and item["question"].strip()
        and isinstance(item.get("reference_answer"), str)
        and item["reference_answer"].strip()
    ]
    mandatory = [item.strip() for item in raw_mandatory if isinstance(item, str) and item.strip()]
    source = _normalized(material)
    if any(_normalized(pair["reference_answer"]) not in source for pair in pairs) or any(
        _normalized(item) not in source for item in mandatory
    ):
        return None
    return (pairs, mandatory) if len(pairs) == len(raw_pairs) and len(mandatory) == len(raw_mandatory) else None


def _normalized(value: str) -> str:
    return re.sub(r"\W+", "", value).casefold()


def _file_paths(file_ops: dict[str, list[str]] | None) -> list[str]:
    if not isinstance(file_ops, dict):
        return []
    return [
        path
        for key in ("read", "written", "edited")
        for path in file_ops.get(key, [])
        if isinstance(path, str) and "://" not in path
    ]


def _mandatory_values(verbatim: list[str], file_ops: dict[str, list[str]] | None) -> list[str]:
    return list(dict.fromkeys([*verbatim, *_file_paths(file_ops)]))

def _with_mandatory(summary: str, mandatory: list[str]) -> str:
    missing = [value for value in mandatory if _normalized(value) not in _normalized(summary)]
    return summary if not missing else f"{summary}\n\nExact mandatory evidence:\n" + "\n".join(f"- {value}" for value in missing)



def _validation(summary: str, pairs: list[dict[str, str]], mandatory: list[str]) -> tuple[float, list[dict[str, str]]]:
    answers_response = chat_json(
        "Answer every probe using only the supplied summary. Do not use outside knowledge.",
        f"TASK: summary-answers\nSummary:\n{summary}\n\nProbes:\n{_serialize([{'index': index, 'question': pair['question']} for index, pair in enumerate(pairs)])}",
        _serialize({"answers": ["string"] * len(pairs)}),
        max_tokens=256,
    )
    answers = answers_response.get("answers") if answers_response else None
    if not isinstance(answers, list) or len(answers) != len(pairs) or not all(isinstance(answer, str) for answer in answers):
        return 0.0, [
            {"question": pair["question"], "reference_answer": pair["reference_answer"], "summary_answer": "", "verdict": "wrong"}
            for pair in pairs
        ]
    judge_response = chat_json(
        "Compare each summary answer with its reference answer. Return correct, partial, or wrong only.",
        f"TASK: judge\nEvidence:\n{_serialize([{**pair, 'summary_answer': answer} for pair, answer in zip(pairs, answers, strict=True)])}",
        _serialize({"verdicts": ["correct|partial|wrong"] * len(pairs)}),
        max_tokens=64,
    )
    verdicts = judge_response.get("verdicts") if judge_response else None
    if not isinstance(verdicts, list) or len(verdicts) != len(pairs) or not all(verdict in {"correct", "partial", "wrong"} for verdict in verdicts):
        return 0.0, [
            {"question": pair["question"], "reference_answer": pair["reference_answer"], "summary_answer": answer, "verdict": "wrong"}
            for pair, answer in zip(pairs, answers, strict=True)
        ]
    results = [
        {"question": pair["question"], "reference_answer": pair["reference_answer"], "summary_answer": answer, "verdict": verdict}
        for pair, answer, verdict in zip(pairs, answers, verdicts, strict=True)
    ]
    scores = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}
    if any(_normalized(value) not in _normalized(summary) for value in mandatory):
        return 0.0, results
    return sum(scores[result["verdict"]] for result in results) / len(results), results


def compact(
    conversation: list[dict[str, Any]],
    budget_tokens: int,
    turn_prefix: list[dict[str, Any]] | None = None,
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
    file_ops: dict[str, list[str]] | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    """Compact complete OMP preparation; opaque preserve data remains host-side."""
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
    evidence = _evidence(full_input)
    final_summary = ""
    probes: list[dict[str, str]] = []
    score = 0.0
    attempt_budget = budget_tokens
    summary_material = material
    if evidence:
        pairs, verbatim = evidence
        mandatory = _mandatory_values(verbatim, file_ops)
        summary_material = (
            f"{material}\n\nValidation references to preserve:\n{_serialize(pairs)}"
            f"\n\nMandatory verbatim values:\n{_serialize(mandatory)}"
        )
        retry_evidence = ""
        for _ in range(3):
            history_summary = _summary("history", summary_material, attempt_budget, policy)
            if history_summary is None:
                final_summary = material[: attempt_budget * 4]
                break
            if turn_prefix:
                prefix_summary = _summary(
                    "turn-prefix",
                    f"History summary:\n{history_summary}\n\nTurn prefix:\n{_serialize(turn_prefix)}\n\n{files}{retry_evidence}",
                    attempt_budget,
                    policy,
                )
                if prefix_summary is None:
                    final_summary = history_summary
                    break
                final_summary = f"{history_summary}\n---\n**Turn Context (split turn):**\n{prefix_summary}"
            else:
                final_summary = history_summary
            final_summary = _with_mandatory(final_summary, mandatory)
            score, probes = _validation(final_summary, pairs, mandatory)
            if score >= VALIDATION_THRESHOLD:
                break
            failed = [
                {"question": probe["question"], "reference_answer": probe["reference_answer"], "verdict": probe["verdict"]}
                for probe in probes
                if probe["verdict"] != "correct"
            ]
            if failed:
                retry_evidence = f"\n\nFailed validation evidence to recover:\n{_serialize(failed)}"
                summary_material += retry_evidence
            attempt_budget = int(attempt_budget * 1.5)
    else:
        final_summary = _summary("history", summary_material, attempt_budget, policy) or material[: attempt_budget * 4]
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
