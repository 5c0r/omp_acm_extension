import os
import time
import uuid

import httpx

from acm import db

BASE_URL = os.environ.get("ACM_TEST_BASE_URL", "http://localhost:8927")


def api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{BASE_URL}{path}", timeout=120, **kwargs)
    response.raise_for_status()
    return response.json()


def scope(prefix: str) -> str:
    return f"project:{prefix}-{uuid.uuid4().hex}"


def ingest(target_scope: str, text: str) -> None:
    job_id = api("POST", "/ingest", json={"scope": target_scope, "text": text, "source_ref": "lifecycle"})["job_id"]
    for _ in range(240):
        state = api("GET", f"/status/{job_id}")
        if state["status"] != "pending":
            assert state["status"] == "done", state
            return
        time.sleep(0.25)
    raise AssertionError(f"ingest job {job_id} did not finish")


def fetch(query: str, target_scope: str):
    return api("POST", "/fetch", json={"scope": target_scope, "query": query})["items"]


def test_lost_detail_survives_ingest_and_fetch():
    target = scope("detail")
    ingest(target, "We upgraded from Starter to Pro on April 3.")

    items = fetch("what plan did we upgrade to", target)

    assert any("Pro" in item["content"] and "April 3" in item["content"] for item in items), items


def test_aliases_resolve_to_one_canonical_entity():
    target = scope("identity")
    ingest(target, "Sarah Chen, also called Sarah and SC, approved the plan.")

    scope_id = db.get_or_create_scope("project", target.split(":", 1)[1])
    with db.connect() as conn:
        entity = conn.execute(
            "SELECT canonical_name, aliases FROM entity WHERE scope_id = %s AND canonical_name = 'Sarah Chen'",
            (scope_id,),
        ).fetchall()
    assert len(entity) == 1, entity
    assert {"Sarah", "SC"}.issubset(entity[0]["aliases"])


def test_project_memory_never_bleeds_into_another_project():
    project_a, project_b = scope("scope-a"), scope("scope-b")
    ingest(project_a, "Only project A uses the cypress blue deployment code.")

    items = fetch("cypress blue deployment code", project_b)

    assert not any("cypress blue" in item["content"] for item in items), items


def test_shared_entity_surfaces_graph_bridge_memory():
    target = scope("bridge")
    ingest(target, "Sarah approved the Pro plan for the company.")
    ingest(target, "Sarah's renewal record requires follow-up next April.")

    items = fetch("which plan did Sarah approve", target)

    assert any("renewal record" in item["content"] and item["via_graph"] for item in items), items


def test_long_compaction_validates_and_compresses():
    conversation = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"Turn {index}: retain project decision, owner, and April deadline. " * 8}
        for index in range(30)
    ]

    result = api("POST", "/compact", json={"conversation": conversation, "budget_tokens": 1500})

    assert result["validation_score"] >= 0.8, result
    assert result["compression_ratio"] < 0.5, result


def test_anticipation_precomputes_session_bundle_and_explicit_fetch_remains_available():
    target = scope("anticipate")
    ingest(target, "The Pro plan upgrade happened on April 3.")
    session_id = f"session-{uuid.uuid4().hex}"
    api(
        "POST",
        "/anticipate",
        json={
            "session_id": session_id,
            "scope": target,
            "trajectory": [{"role": "user", "content": "We reviewed the Pro plan upgrade and April timing."}],
        },
    )
    for _ in range(240):
        response = httpx.get(f"{BASE_URL}/bundle/{session_id}", timeout=30)
        if response.status_code == 200:
            bundle = response.json()
            break
        assert response.status_code == 404
        time.sleep(0.25)
    else:
        raise AssertionError("anticipated bundle was not created")

    assert bundle["rendered"].startswith("[acm memory]")
    assert fetch("Pro plan upgrade", target)
    stats = api("GET", "/stats")["stats"]
    assert int(stats["bundle_injected"]) >= 1
