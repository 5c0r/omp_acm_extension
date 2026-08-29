import subprocess
import sys


def test_compact_carries_full_preparation_into_summary_and_probes():
    """Fails if split-turn, files, prior summary, or instructions are omitted from compaction."""
    script = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    prompts = []

    def do_POST(self):
        prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"]))) ["messages"][-1]["content"]
        Handler.prompts.append(prompt)
        if "TASK: probes" in prompt:
            content = json.dumps({"probes": [{"question": f"p{index}", "reference_answer": f"a{index}"} for index in range(1, 6)], "must_preserve_verbatim": []})
        elif "TASK: summary-answers" in prompt:
            content = json.dumps({"answers": [f"a{index}" for index in range(1, 6)]})
        elif "TASK: judge" in prompt:
            content = json.dumps({"verdicts": ["correct"] * 5})
        elif "TASK: turn-prefix" in prompt:
            content = json.dumps({"summary": "Turn prefix kept deploy.py file context."})
        else:
            content = json.dumps({"summary": "History summary keeps deploy.py decision."})
        body = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["ACM_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"

from acm.compact import compact

result = compact(
    [{"role": "user", "content": "a1 a2 a3 a4 a5 " + "x" * 2000}],
    budget_tokens=500,
    turn_prefix=[{"role": "assistant", "content": "split turn file result"}],
    previous_summary="Earlier summary preserves decision.",
    custom_instructions="Prioritize deployment risks.",
    file_ops={"read": ["deploy.py"], "written": [], "edited": ["deploy.py"]},
)
prompts = chr(10).join(Handler.prompts)
answer_prompt = next(prompt for prompt in Handler.prompts if "TASK: summary-answers" in prompt)
assert "reference_answer" not in answer_prompt and "a1" not in answer_prompt
history_prompt = next(prompt for prompt in Handler.prompts if "TASK: history" in prompt)
assert "Mandatory verbatim values" in history_prompt and "deploy.py" in history_prompt
expected = "History summary keeps deploy.py decision." + chr(10) + "---" + chr(10) + "**Turn Context (split turn):**" + chr(10) + "Turn prefix kept deploy.py file context."
assert result["summary"] == expected
assert result["validation_score"] == 1.0
assert len(result["probes"]) == 5
assert result["compression_ratio"] < 0.5
assert "Earlier summary preserves decision." in prompts
assert "Prioritize deployment risks." in prompts
assert "<files>" in prompts and "deploy.py (RW)" in prompts
assert "split turn file result" in prompts
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_consolidate_merges_vector_duplicates_and_archives_expired_memory():
    """Fails if consolidation leaves duplicate active memories or expired facts searchable."""
    script = """
import uuid
from acm import db

db.ensure_schema()
scope_name = "consolidate-" + uuid.uuid4().hex
scope = "project:" + scope_name
scope_id = db.get_or_create_scope("project", scope_name)
vector = "[1," + ",".join(["0"] * 1023) + "]"
with db.connect() as conn:
    conn.execute(
        "INSERT INTO memory (scope_id, kind, content, source_ref, importance, embedding) VALUES (%s, 'fact', 'first', 'first', 0.8, %s::vector)",
        (scope_id, vector),
    )
    conn.execute(
        "INSERT INTO memory (scope_id, kind, content, source_ref, importance, embedding) VALUES (%s, 'fact', 'second', 'second', 0.8, %s::vector)",
        (scope_id, vector),
    )
    conn.execute(
        "INSERT INTO memory (scope_id, kind, content, valid_until) VALUES (%s, 'fact', 'expired', current_date - 1)",
        (scope_id,),
    )

from acm.compact import consolidate

result = consolidate(scope)
with db.connect() as conn:
    active = conn.execute("SELECT content, source_ref FROM memory WHERE scope_id = %s AND status = 'active'", (scope_id,)).fetchall()
assert result["merged"] == 1, result
assert result["archived"] == 1, result
assert active == [{"content": "first", "source_ref": "first"}], active
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_compaction_validator_retries_when_reference_fact_is_missing():
    """Fails if validation self-grades or cannot reject an omitted reference fact."""
    script = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

pairs = [{"question": f"Question {index}", "reference_answer": "April 3"} for index in range(3)] + [{"question": f"Lock {index}", "reference_answer": "release-lock"} for index in range(2)]

class Handler(BaseHTTPRequestHandler):
    prompts = []

    def do_POST(self):
        prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][-1]["content"]
        Handler.prompts.append(prompt)
        if "TASK: probes" in prompt:
            content = json.dumps({"probes": pairs, "must_preserve_verbatim": []})
        elif "TASK: summary-answers" in prompt:
            content = json.dumps({"answers": ["April 3"] * 5})
        elif "TASK: judge" in prompt:
            content = json.dumps({"verdicts": ["correct"] * 3 + ["wrong"] * 2})
        else:
            content = json.dumps({"summary": "Launch date is April 3."})
        body = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["ACM_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"
from acm.compact import _evidence, compact
assert _evidence("Launch date is April 3.") is None

result = compact([{"role": "user", "content": "Launch date is April 3; release-lock is required."}], budget_tokens=100)
assert result["validation_score"] < 0.8, result
assert result["probes"][0]["reference_answer"] == "April 3"
history = [prompt for prompt in Handler.prompts if "TASK: history" in prompt]
assert len(history) == 3 and "Budget: 150" in history[1] and "Budget: 225" in history[2]
assert any("TASK: summary-answers" in prompt for prompt in Handler.prompts)
assert any("TASK: judge" in prompt for prompt in Handler.prompts)
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr

def test_compaction_retry_relaxes_budget_and_supplies_failed_evidence():
    """Fails if retry keeps the first output ceiling or omits failed probes."""
    script = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

pairs = [{"question": f"Question {index}", "reference_answer": "April 3"} for index in range(3)] + [{"question": f"Question Lock {index}", "reference_answer": "release-lock"} for index in range(3, 5)]

class Handler(BaseHTTPRequestHandler):
    history_requests = []
    summaries = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = request["messages"][-1]["content"]
        if "TASK: probes" in prompt:
            content = json.dumps({"probes": pairs, "must_preserve_verbatim": []})
        elif "TASK: history" in prompt:
            Handler.history_requests.append(request)
            if len(Handler.history_requests) == 1:
                summary = "Launch date is April 3."
            else:
                summary = "Launch date is April 3. The release-lock is required before deployment."
            Handler.summaries.append(summary)
            content = json.dumps({"summary": summary})
        elif "TASK: turn-prefix" in prompt:
            Handler.prefix_request = request
            content = json.dumps({"summary": "Turn prefix preserved."})
        elif "TASK: summary-answers" in prompt:
            answers = ["April 3"] * 3 + ([""] * 2 if len(Handler.history_requests) == 1 else ["release-lock"] * 2)
            content = json.dumps({"answers": answers})
        elif "TASK: judge" in prompt:
            verdicts = ["correct"] * 3 + (["wrong"] * 2 if len(Handler.history_requests) == 1 else ["correct"] * 2)
            content = json.dumps({"verdicts": verdicts})
        else:
            raise AssertionError(prompt)
        body = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["ACM_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"
from acm.compact import compact

result = compact(
    [{"role": "user", "content": "Launch date is April 3; release-lock is required."}],
    budget_tokens=1500,
    turn_prefix=[{"role": "assistant", "content": "Keep this split turn."}],
)
first, second = Handler.history_requests
assert result["validation_score"] >= 0.8, result
assert len(Handler.summaries[1]) > len(Handler.summaries[0])
assert "Budget: 1500" in first["messages"][-1]["content"]
assert "Budget: 2250" in second["messages"][-1]["content"]
assert "at most 2250 tokens" in second["messages"][0]["content"]
assert "Question Lock 3" in second["messages"][-1]["content"]
assert "Failed validation evidence" in second["messages"][-1]["content"]
assert second["options"]["num_predict"] == 2048
assert "Budget: 2250" in Handler.prefix_request["messages"][-1]["content"]
assert Handler.prefix_request["options"]["num_predict"] == 2048
assert "Failed validation evidence" in Handler.prefix_request["messages"][-1]["content"]
assert "Question Lock 3" in Handler.prefix_request["messages"][-1]["content"]
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_compaction_refuses_to_validate_when_summary_model_fails(monkeypatch):
    from acm import compact as module

    pairs = [{"question": f"q{index}", "reference_answer": "April 3"} for index in range(5)]
    monkeypatch.setattr(module, "_evidence", lambda *_args: (pairs, []))
    monkeypatch.setattr(module, "_summary", lambda *_args: None)

    result = module.compact([{"role": "user", "content": "Launch date is April 3."}], budget_tokens=100)

    assert result["validation_score"] == 0

def test_compaction_carries_mandatory_values_into_summary_before_validation(monkeypatch):
    from acm import compact as module

    pairs = [{"question": f"q{index}", "reference_answer": "April 3"} for index in range(5)]
    monkeypatch.setattr(module, "_evidence", lambda *_args: (pairs, ["release-lock"]))
    monkeypatch.setattr(module, "_summary", lambda *_args: "Launch date is April 3.")

    def validate(summary, _pairs, _mandatory):
        assert "release-lock" in summary
        return 1.0, []

    monkeypatch.setattr(module, "_validation", validate)
    assert module.compact([{"role": "user", "content": "Launch date is April 3; release-lock is required."}], 100)["validation_score"] == 1.0


def test_async_compaction_matches_only_its_exact_canonical_input():
    """Fails if an armed summary is synchronous, unavailable, or reused for different input."""
    script = """
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        prompt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))[ "messages"][-1]["content"]
        if "TASK: probes" in prompt:
            content = json.dumps({"probes": [{"question": f"q{index}", "reference_answer": f"a{index}"} for index in range(5)], "must_preserve_verbatim": []})
        elif "TASK: summary-answers" in prompt:
            content = json.dumps({"answers": [f"a{index}" for index in range(5)]})
        elif "TASK: judge" in prompt:
            content = json.dumps({"verdicts": ["correct"] * 5})
        else:
            content = json.dumps({"summary": "validated armed summary"})
        body = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["ACM_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"

from fastapi.testclient import TestClient
from acm.api import app

scope = f"project:armed-{uuid.uuid4().hex}"
payload = {
    "scope": scope,
    "conversation": [{"role": "user", "content": "Keep a0 a1 a2 a3 a4."}],
    "turn_prefix": None,
    "previous_summary": "previous",
    "custom_instructions": "preserve facts",
    "file_ops": {"read": ["a.ts"], "written": [], "edited": []},
    "budget_tokens": 100,
    "async": True,
    "from_extension": True,
}
with TestClient(app) as client:
    armed = client.post("/compact", json=payload)
    assert armed.status_code == 202, armed.text
    assert set(armed.json()) == {"id"}
    match_payload = {key: value for key, value in payload.items() if key not in {"async", "from_extension"}}
    for _ in range(100):
        hit = client.post("/compact/match", json=match_payload)
        if hit.status_code == 200:
            break
        assert hit.status_code == 404, hit.text
        time.sleep(0.02)
    result = hit.json()
    assert result["summary"].startswith("validated armed summary")
    assert result["validation_score"] >= 0.8
    assert result["from_extension"] is True
    mismatch = dict(match_payload, conversation=[{"role": "user", "content": "Different fact."}])
    assert client.post("/compact/match", json=mismatch).status_code == 404
    budget_mismatch = dict(match_payload, budget_tokens=1500)
    assert client.post("/compact/match", json=budget_mismatch).status_code == 404
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_armed_compaction_serializes_background_jobs(monkeypatch):
    from acm import compact as module
    from acm import db
    import threading
    import time
    import uuid

    active = 0
    peak_active = 0
    lock = threading.Lock()

    def fake_compact(*args):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        with db.connect() as conn:
            conn.execute("UPDATE compaction SET status = 'done' WHERE id = %s", (args[9],))
        return {"summary": "done", "validation_score": 1.0, "compression_ratio": 0.1, "probes": []}

    monkeypatch.setattr(module, "compact", fake_compact)
    scope = f"project:serial-{uuid.uuid4().hex}"
    first = module.arm_compaction(scope, [{"role": "user", "content": "first"}], 100)
    second = module.arm_compaction(scope, [{"role": "user", "content": "second"}], 100)
    for _ in range(100):
        with db.connect() as conn:
            states = conn.execute("SELECT status FROM compaction WHERE id IN (%s, %s)", (first, second)).fetchall()
        if len(states) == 2 and all(row["status"] == "done" for row in states):
            break
        time.sleep(0.02)
    assert all(row["status"] == "done" for row in states)
    assert peak_active == 1


def test_compaction_digest_is_exact_but_file_order_independent():
    from acm.compact import compaction_digest

    conversation = [{"role": "user", "content": "Keep 2026-05-01 ledger evidence."}]
    first = compaction_digest(conversation, file_ops={"written": ["b.ts", "a.ts"], "read": ["z.ts"]})
    reordered = compaction_digest(conversation, file_ops={"read": ["z.ts"], "written": ["a.ts", "b.ts"]})
    changed = compaction_digest(conversation, file_ops={"read": ["z.ts"], "written": ["a.ts", "c.ts"]})

    assert first == reordered
    assert first != changed


def test_duplicate_armed_compaction_reuses_done_job_within_ttl(monkeypatch):
    from acm import compact as module
    from acm import db
    import time
    import uuid

    def fake_compact(*args):
        with db.connect() as conn:
            conn.execute("UPDATE compaction SET status = 'done' WHERE id = %s", (args[9],))
        return {"summary": "done", "validation_score": 1.0, "compression_ratio": 0.1, "probes": []}

    monkeypatch.setattr(module, "compact", fake_compact)
    scope = f"project:dedup-{uuid.uuid4().hex}"
    first = module.arm_compaction(scope, [{"role": "user", "content": "same"}], 100)
    for _ in range(100):
        with db.connect() as conn:
            row = conn.execute("SELECT status FROM compaction WHERE id = %s", (first,)).fetchone()
        if row and row["status"] == "done":
            break
        time.sleep(0.02)
    second = module.arm_compaction(scope, [{"role": "user", "content": "same"}], 100)

    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM compaction WHERE id IN (%s, %s)", (first, second)).fetchall()
    assert first == second
    assert len(rows) == 1


def test_concurrent_same_digest_arms_enqueue_the_inserter_once(monkeypatch):
    """Fails if the conflict loser queues an already in-progress compaction."""
    from contextlib import contextmanager
    from queue import Queue
    from acm import compact as module
    from acm import db
    from acm.retrieve import scope_id
    import threading
    import uuid

    db.ensure_schema()
    scope = f"project:race-{uuid.uuid4().hex}"
    scope_id(scope)
    original_connect = db.connect
    selected = threading.Barrier(2)
    selected_lock = threading.Lock()
    selected_count = 0

    @contextmanager
    def gated_connect():
        nonlocal selected_count
        with original_connect() as conn:
            class GatedConnection:
                def execute(self, query, *args, **kwargs):
                    nonlocal selected_count
                    result = conn.execute(query, *args, **kwargs)
                    if "FROM compaction WHERE scope_id = %s AND digest = %s AND " in query:
                        with selected_lock:
                            selected_count += 1
                            wait = selected_count <= 2
                        if wait:
                            selected.wait(timeout=5)
                    return result
            yield GatedConnection()

    jobs = Queue()
    monkeypatch.setattr(module.db, "connect", gated_connect)
    monkeypatch.setattr(module.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(module, "start_compaction_worker", lambda: None)
    monkeypatch.setattr(module, "_armed_jobs", jobs)
    ids = []
    errors = []

    def arm():
        try:
            ids.append(module.arm_compaction(scope, [{"role": "user", "content": "same"}], 100))
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=arm)
    second = threading.Thread(target=arm)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not errors
    assert len(ids) == 2
    assert ids[0] == ids[1]
    assert jobs.qsize() == 1

def test_scoped_architecture_controls_compaction_policy_probes_and_digest(monkeypatch):
    from acm import compact as module
    from acm import db
    from acm.retrieve import scope_id
    import json
    import uuid

    prompts = []
    compliance = "COMPLIANCE-7"

    def fake_chat(_system, user, _schema_hint="", max_tokens=None):
        prompts.append(user)
        if "TASK: probes" in user:
            return {
                "probes": [{"question": f"p{index}", "reference_answer": f"a{index}"} for index in range(1, 6)],
                "must_preserve_verbatim": [compliance] if "compliance_obligations" in user else [],
            }
        if "TASK: summary-answers" in user:
            return {"answers": [f"a{index}" for index in range(1, 6)]}
        if "TASK: judge" in user:
            return {"verdicts": ["correct"] * 5}
        return {"summary": "a1 a2 a3 a4 a5"}

    monkeypatch.setattr(module, "chat_json", fake_chat)
    scope = f"project:architecture-{uuid.uuid4().hex}"
    custom_spec = {
        "categories": [
            {"name": "compliance_obligations", "description": "Binding controls.", "examples": [], "retention": "long-term", "must_preserve_verbatim": True},
            {"name": "facts", "description": "Stable facts.", "examples": [], "retention": "until superseded", "must_preserve_verbatim": False},
            {"name": "preferences", "description": "Working preferences.", "examples": [], "retention": "long-term", "must_preserve_verbatim": False},
        ],
        "extraction_guidance": "Extract compliance obligations.",
        "compaction_policy": "Preserve compliance obligations verbatim.",
    }
    changed_spec = {
        **custom_spec,
        "categories": [
            {"name": "facts", "description": "Stable facts.", "examples": [], "retention": "until superseded", "must_preserve_verbatim": False},
            {"name": "preferences", "description": "Working preferences.", "examples": [], "retention": "long-term", "must_preserve_verbatim": False},
            {"name": "episodes", "description": "Past work.", "examples": [], "retention": "short-term", "must_preserve_verbatim": False},
        ],
        "compaction_policy": "Preserve revised records.",
    }
    db.ensure_schema()
    current_scope = scope_id(scope)
    with db.connect() as conn:
        conn.execute("INSERT INTO architecture (scope_id, spec) VALUES (%s, %s::jsonb)", (current_scope, json.dumps(custom_spec)))
    conversation = [{"role": "user", "content": f"a1 a2 a3 a4 a5 {compliance}"}]
    custom = module.compact(conversation, 100, scope=scope, policy="Also retain audit trail.")
    custom_prompts = prompts[:]
    with db.connect() as conn:
        conn.execute("UPDATE architecture SET spec = %s::jsonb WHERE scope_id = %s", (json.dumps(changed_spec), current_scope))
    changed = module.compact(conversation, 100, scope=scope)

    with db.connect() as conn:
        digests = conn.execute(
            "SELECT digest FROM compaction WHERE scope_id = %s ORDER BY id DESC LIMIT 2", (current_scope,)
        ).fetchall()
    assert any("compliance_obligations" in prompt for prompt in custom_prompts)
    assert any("Preserve compliance obligations verbatim." in prompt for prompt in custom_prompts)
    assert any("Also retain audit trail." in prompt for prompt in custom_prompts)
    assert compliance in custom["summary"]
    assert compliance not in changed["summary"]
    assert digests[0]["digest"] != digests[1]["digest"]
    assert module.match_compaction(scope, conversation, budget_tokens=100)["summary"] == changed["summary"]
