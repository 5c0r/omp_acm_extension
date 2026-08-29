import os
import time
import uuid

_OLLAMA_URL = os.environ.get("ACM_OLLAMA_URL")
os.environ["ACM_OLLAMA_URL"] = "http://127.0.0.1:1"

from fastapi.testclient import TestClient

from acm.api import app

if _OLLAMA_URL is None:
    os.environ.pop("ACM_OLLAMA_URL")
else:
    os.environ["ACM_OLLAMA_URL"] = _OLLAMA_URL


def test_api_exposes_lifecycle_routes_under_lifespan():
    """Fails if service startup or any public ACM route is absent."""
    scope = f"project:api-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        architecture = client.post("/architect", json={"scope": scope, "description": "test scope"})
        assert architecture.status_code == 200
        assert len(architecture.json()["categories"]) >= 3

        queued = client.post("/ingest", json={"scope": scope, "text": "The Pro plan was chosen.", "source_ref": "api-test"})
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/status/{job_id}").json()
            if job["status"] != "pending":
                break
            time.sleep(0.05)
        assert job["status"] == "done", job

        assert client.post("/fetch", json={"scope": scope, "query": "Pro plan"}).status_code == 200
        assert client.post(
            "/anticipate",
            json={"session_id": f"session-{uuid.uuid4().hex}", "scope": scope, "trajectory": [{"role": "user", "content": "Need plan"}]},
        ).status_code == 202
        assert client.get("/bundle/missing").status_code == 404
        assert client.post(
            "/compact",
            json={
                "conversation": [{"role": "user", "content": "Keep this decision."}],
                "turn_prefix": [{"role": "assistant", "content": "split turn"}],
                "previous_summary": "older",
                "custom_instructions": "keep decisions",
                "file_ops": {"read": ["a.ts"], "written": [], "edited": ["a.ts"]},
                "previous_preserve_data": {"snapcompact": {"version": 1}},
                "budget_tokens": 100,
            },
        ).status_code == 200
        assert client.post("/consolidate", json={"scope": scope}).status_code == 200
        assert client.get("/stats").status_code == 200
        assert client.post(
            "/anticipate",
            json={"session_id": "bad-scope", "scope": "invalid", "trajectory": []},
        ).status_code == 400
