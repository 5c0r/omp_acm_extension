import json
import os
import uuid

_OLLAMA_URL = os.environ.get("ACM_OLLAMA_URL")
os.environ["ACM_OLLAMA_URL"] = "http://127.0.0.1:1"

from fastapi.testclient import TestClient

from acm import db
from acm import anticipate as anticipate_module
from acm.api import app

if _OLLAMA_URL is None:
    os.environ.pop("ACM_OLLAMA_URL")
else:
    os.environ["ACM_OLLAMA_URL"] = _OLLAMA_URL


def _scope() -> str:
    return f"project:ui-{uuid.uuid4().hex}"


def _scope_id(scope: str) -> int:
    return db.get_or_create_scope("project", scope.split(":", maxsplit=1)[1])


def _memory(scope: str, content: str, *, importance: float = 0.5, status: str = "active") -> int:
    with db.connect() as conn:
        row = conn.execute(
            "INSERT INTO memory (scope_id, kind, content, importance, status) "
            "VALUES (%s, 'fact', %s, %s, %s) RETURNING id",
            (_scope_id(scope), content, importance, status),
        ).fetchone()
    return row["id"]


def test_usage_events_capture_fetch_bundle_and_compaction() -> None:
    """Fails if a served memory has no complete usage history."""
    scope = _scope()
    session_id = f"bundle-{uuid.uuid4().hex}"

    with TestClient(app) as client:
        memory_id = _memory(scope, "ACM usage fixture keeps billing priority explicit.")
        fetched = client.post("/fetch", json={"scope": scope, "query": "billing priority", "session_id": session_id})
        assert fetched.status_code == 200
        assert [item["id"] for item in fetched.json()["items"]] == [memory_id]

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO bundle (session_id, scope_id, rendered, predicted_intents, memory_ids, memory_ids_version, expires_at) "
                "VALUES (%s, %s, %s, '[]'::jsonb, %s::jsonb, 1, now() + interval '5 minutes')",
                (session_id, _scope_id(scope), "[acm memory]", json.dumps([memory_id])),
            )
        assert client.get(f"/bundle/{session_id}").status_code == 200

        compacted = client.post(
            "/compact",
            json={
                "scope": scope,
                "session_id": session_id,
                "conversation": [{"role": "user", "content": "ACM usage fixture keeps billing priority explicit."}],
                "budget_tokens": 32,
            },
        )
        assert compacted.status_code == 200

        usage = client.get(f"/api/ui/usage/{memory_id}")
        assert usage.status_code == 200
        assert {event["use_type"] for event in usage.json()["items"]} == {"fetch", "bundle", "compact"}

        detail = client.get(f"/api/ui/memories/{memory_id}")
        assert detail.status_code == 200
        assert {event["use_type"] for event in detail.json()["usage"]} == {"fetch", "bundle", "compact"}


def test_manage_archive_restore_merge_pin_and_alias() -> None:
    """Fails if management mutates retrieval visibility or loses provenance."""
    scope = _scope()

    with TestClient(app) as client:
        target_id = _memory(scope, "Target content keeps deployment decision.", importance=0.6)
        source_id = _memory(scope, "Source content adds rollout reason.", importance=0.8)

        patched = client.patch(
            f"/memories/{source_id}",
            json={"content": "Source content adds rollout reason.", "importance": 0.9, "pinned": True},
        )
        assert patched.status_code == 200
        assert patched.json()["importance"] == 0.9
        assert patched.json()["pinned"] is True

        assert client.post(f"/memories/{source_id}/archive").status_code == 200
        hidden = client.post("/fetch", json={"scope": scope, "query": "rollout reason"})
        assert hidden.status_code == 200
        assert source_id not in [item["id"] for item in hidden.json()["items"]]
        assert client.post(f"/memories/{source_id}/restore").status_code == 200

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO usage_log (memory_id, use_type, scope) VALUES (%s, 'fetch', %s)",
                (source_id, scope),
            )
            entity = conn.execute(
                "INSERT INTO entity (scope_id, canonical_name) VALUES (%s, 'Release train') RETURNING id",
                (_scope_id(scope),),
            ).fetchone()

        alias = client.post(f"/entities/{entity['id']}/aliases", json={"alias": "train"})
        assert alias.status_code == 200
        assert alias.json()["aliases"] == ["train"]
        assert client.get(f"/entities/{entity['id']}/aliases").json()["aliases"] == ["train"]

        merged = client.post(f"/memories/{source_id}/merge", json={"target_id": target_id})
        assert merged.status_code == 200
        assert merged.json()["id"] == target_id
        assert "Source content adds rollout reason." in merged.json()["content"]

        source = client.get(f"/api/ui/memories/{source_id}")
        assert source.status_code == 200
        assert source.json()["status"] == "archived"
        assert source.json()["merged_into"] == target_id

        provenance = client.get(f"/api/ui/usage/{target_id}")
        assert provenance.status_code == 200
        assert any(event["use_type"] == "fetch" for event in provenance.json()["items"])

        result = client.post("/fetch", json={"scope": scope, "query": "rollout reason"})
        assert result.status_code == 200
        assert [item["id"] for item in result.json()["items"]] == [target_id]


def test_dashboard_aggregates_seeded_service_data() -> None:
    """Fails if dashboard aggregates disagree with persisted ACM state."""
    scope = _scope()

    with TestClient(app) as client:
        scope_id = _scope_id(scope)
        active_id = _memory(scope, "Active dashboard memory", importance=0.9)
        _memory(scope, "Archived dashboard memory", status="archived")
        with db.connect() as conn:
            conn.execute("UPDATE memory SET access_count = 1000000 WHERE id = %s", (active_id,))
            session_id = f"dashboard-{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO bundle (session_id, scope_id, rendered, predicted_intents, memory_ids, memory_ids_version, expires_at) "
                "VALUES (%s, %s, '[acm memory]', '[]'::jsonb, '[]'::jsonb, 1, now() + interval '5 minutes')",
                (session_id, scope_id),
            )
            conn.execute(
                "INSERT INTO ingest_job (scope_id, payload, digest, status, result) "
                "VALUES (%s, '{}'::jsonb, %s, 'done', '{}'::jsonb), (%s, '{}'::jsonb, %s, 'failed', '{}'::jsonb)",
                (scope_id, uuid.uuid4().hex, scope_id, uuid.uuid4().hex),
            )
            compaction = conn.execute(
                "INSERT INTO compaction (scope_id, summary, validation_score, compression_ratio) "
                "VALUES (%s, 'dashboard proof', 0.9, 0.2) RETURNING id",
                (scope_id,),
            ).fetchone()
        before = client.get("/api/ui/dashboard").json()["bundles"]
        assert client.get(f"/bundle/{session_id}").status_code == 200
        assert client.get(f"/bundle/missing-{uuid.uuid4().hex}").status_code == 404
        db.bump_stat("explicit_fetch")

        dashboard = client.get("/api/ui/dashboard")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["totals"]["scope"][scope] == 2
        assert body["totals"]["status"]["active"] >= 1
        assert body["totals"]["status"]["archived"] >= 1
        assert body["bundles"]["served"] == before["served"] + 1
        assert body["bundles"]["hit"] == before["hit"] + 1
        assert body["bundles"]["miss"] == before["miss"] + 1
        assert body["explicit_fetch"] >= 1
        assert body["ingest_outcomes"]["done"] >= 1
        assert body["ingest_outcomes"]["failed"] >= 1
        assert any(score["id"] == compaction["id"] and score["validation_score"] == 0.9 for score in body["compaction_scores"])
        assert any(memory["id"] == active_id for memory in body["top_used"])


def test_browse_filters_and_paginate_memories() -> None:
    """Fails if UI clients cannot page active and archived memories independently."""
    scope = _scope()

    with TestClient(app) as client:
        first_id = _memory(scope, "Browse matched first.")
        second_id = _memory(scope, "Browse matched second.")
        archived_id = _memory(scope, "Browse archived match.", status="archived")

        active = client.get(
            "/api/ui/memories",
            params={"scope": scope, "kind": "fact", "status": "active", "q": "Browse matched", "limit": 1},
        )
        assert active.status_code == 200
        assert active.json()["total"] == 2
        assert len(active.json()["items"]) == 1

        page_two = client.get(
            "/api/ui/memories",
            params={"scope": scope, "kind": "fact", "status": "active", "q": "Browse matched", "limit": 1, "offset": 1},
        )
        assert page_two.status_code == 200
        assert {active.json()["items"][0]["id"], page_two.json()["items"][0]["id"]} == {first_id, second_id}

        archived = client.get("/api/ui/memories", params={"scope": scope, "status": "archived"})
        assert archived.status_code == 200
        assert archived.json()["items"][0]["id"] == archived_id


def test_archive_and_merge_invalidate_cached_bundles() -> None:
    """Fails if cached rendering serves archived or merged memory content."""
    scope = _scope()

    with TestClient(app) as client:
        archived_id = _memory(scope, "Archive bundle source.")
        merge_source_id = _memory(scope, "Merge bundle source.")
        merge_target_id = _memory(scope, "Merge bundle target.")
        sessions = {
            "archive": (f"archive-{uuid.uuid4().hex}", archived_id),
            "merge-source": (f"merge-source-{uuid.uuid4().hex}", merge_source_id),
            "merge-target": (f"merge-target-{uuid.uuid4().hex}", merge_target_id),
        }
        with db.connect() as conn:
            for session_id, memory_id in sessions.values():
                conn.execute(
                    "INSERT INTO bundle (session_id, scope_id, rendered, predicted_intents, memory_ids, memory_ids_version, expires_at) "
                    "VALUES (%s, %s, '[acm memory]', '[]'::jsonb, %s::jsonb, 1, now() + interval '5 minutes')",
                    (session_id, _scope_id(scope), json.dumps([memory_id])),
                )

        assert client.get(f"/bundle/{sessions['archive'][0]}").status_code == 200
        assert client.post(f"/memories/{archived_id}/archive").status_code == 200
        assert client.get(f"/bundle/{sessions['archive'][0]}").status_code == 404

        assert client.post(f"/memories/{merge_source_id}/merge", json={"target_id": merge_target_id}).status_code == 200
        assert client.get(f"/bundle/{sessions['merge-source'][0]}").status_code == 404
        assert client.get(f"/bundle/{sessions['merge-target'][0]}").status_code == 404


def test_patch_invalidates_cached_bundles() -> None:
    """Fails if cached rendering keeps serving pre-edit memory content."""
    scope = _scope()

    with TestClient(app) as client:
        memory_id = _memory(scope, "Patch bundle original.")
        session_id = f"patch-{uuid.uuid4().hex}"
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO bundle (session_id, scope_id, rendered, predicted_intents, memory_ids, memory_ids_version, expires_at) "
                "VALUES (%s, %s, '[acm memory]', '[]'::jsonb, %s::jsonb, 1, now() + interval '5 minutes')",
                (session_id, _scope_id(scope), json.dumps([memory_id])),
            )

        assert client.get(f"/bundle/{session_id}").status_code == 200
        assert client.patch(f"/memories/{memory_id}", json={"content": "Patch bundle edited."}).status_code == 200
        assert client.get(f"/bundle/{session_id}").status_code == 404


def test_compaction_uses_session_usage_since_prior_compaction() -> None:
    """Fails if compact attribution uses content matching or reuses older session usage."""
    scope = _scope()
    session_id = f"compact-{uuid.uuid4().hex}"

    with TestClient(app) as client:
        stale_id = _memory(scope, "Stale served memory.")
        recent_id = _memory(scope, "Recent served memory.")
        unrelated_id = _memory(scope, "Unrelated served memory.")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO usage_log (memory_id, use_type, session_id, scope) VALUES (%s, 'fetch', %s, %s)",
                (stale_id, session_id, scope),
            )
            conn.execute(
                "INSERT INTO usage_log (memory_id, use_type, session_id, scope) VALUES (%s, 'compact', %s, %s)",
                (stale_id, session_id, scope),
            )
            conn.execute(
                "INSERT INTO usage_log (memory_id, use_type, session_id, scope) VALUES (%s, 'bundle', %s, %s)",
                (recent_id, session_id, scope),
            )
            conn.execute(
                "INSERT INTO usage_log (memory_id, use_type, session_id, scope) VALUES (%s, 'fetch', %s, %s)",
                (unrelated_id, f"other-{session_id}", scope),
            )

        compacted = client.post(
            "/compact",
            json={
                "scope": scope,
                "session_id": session_id,
                "conversation": [{"role": "user", "content": "Session provenance transcript."}],
                "budget_tokens": 32,
            },
        )
        assert compacted.status_code == 200

        with db.connect() as conn:
            rows = conn.execute(
                "SELECT memory_id, count(*) AS count FROM usage_log WHERE session_id = %s AND use_type = 'compact' "
                "GROUP BY memory_id",
                (session_id,),
            ).fetchall()
        compact_counts = {row["memory_id"]: row["count"] for row in rows}
        assert compact_counts[stale_id] == 1
        assert compact_counts[recent_id] == 1
        assert compact_counts.get(unrelated_id, 0) == 0


def test_bundle_usage_records_only_rendered_memories(monkeypatch) -> None:
    """Fails if a budget-truncated candidate is persisted as served provenance."""
    scope = _scope()
    session_id = f"rendered-{uuid.uuid4().hex}"
    content = "needle " + "x" * (anticipate_module.BUNDLE_BUDGET * 4 - len("needle ") - 4)
    monkeypatch.setattr(anticipate_module, "_predict", lambda _: ["needle"])

    with TestClient(app) as client:
        memory_id = _memory(scope, content)
        composed = anticipate_module.anticipate(session_id, scope, [{"role": "user", "content": "Needle context"}])
        assert composed["rendered"] == "[acm memory]"
        assert client.get(f"/bundle/{session_id}").status_code == 200

        usage = client.get(f"/api/ui/usage/{memory_id}")
        assert usage.status_code == 200
        assert usage.json()["items"] == []


def test_decision_merge_preserves_source_block_verbatim() -> None:
    """Fails if a singular decision misses plural architecture precedence."""
    scope = _scope()
    source_content = "Keep the release gate.\n\n- Exact approval condition\n- Exact rollback condition"

    with TestClient(app) as client:
        target_id = _memory(scope, "Target merge content.")
        with db.connect() as conn:
            source = conn.execute(
                "INSERT INTO memory (scope_id, kind, content) VALUES (%s, 'decision', %s) RETURNING id",
                (_scope_id(scope), source_content),
            ).fetchone()

        merged = client.post(f"/memories/{source['id']}/merge", json={"target_id": target_id})
        assert merged.status_code == 200
        assert source_content in merged.json()["content"]


def test_memory_console_serves_index() -> None:
    with TestClient(app) as client:
        response = client.get("/ui/")

    assert response.status_code == 200
    assert "<title>ACM Memory Console</title>" in response.text


def test_demo_seed_is_idempotent() -> None:
    with TestClient(app) as client:
        first = client.post("/api/ui/seed-demo")
        second = client.post("/api/ui/seed-demo")
        memories = client.get("/api/ui/memories", params={"scope": "project:browse-live", "q": "Widget API key rotates weekly"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["memory_id"] == second.json()["memory_id"]
    assert len(memories.json()["items"]) == 1
    assert "Widget API key rotates weekly" in memories.json()["items"][0]["content"]


def test_entity_alias_can_be_removed() -> None:
    scope_id = _scope_id(_scope())
    with db.connect() as conn:
        entity = conn.execute(
            "INSERT INTO entity (scope_id, canonical_name, aliases) VALUES (%s, 'widget', ARRAY['legacy-widget']) RETURNING id",
            (scope_id,),
        ).fetchone()

    with TestClient(app) as client:
        response = client.request("DELETE", f"/entities/{entity['id']}/aliases", json={"alias": "legacy-widget"})

    assert response.status_code == 200
    assert response.json()["aliases"] == []
