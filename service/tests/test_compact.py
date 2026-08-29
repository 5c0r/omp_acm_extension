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
    monkeypatch.setattr(module, "_evidence", lambda _input: (pairs, []))
    monkeypatch.setattr(module, "_summary", lambda *_args: None)

    result = module.compact([{"role": "user", "content": "Launch date is April 3."}], budget_tokens=100)

    assert result["validation_score"] == 0

def test_compaction_carries_mandatory_values_into_summary_before_validation(monkeypatch):
    from acm import compact as module

    pairs = [{"question": f"q{index}", "reference_answer": "April 3"} for index in range(5)]
    monkeypatch.setattr(module, "_evidence", lambda _input: (pairs, ["release-lock"]))
    monkeypatch.setattr(module, "_summary", lambda *_args: "Launch date is April 3.")

    def validate(summary, _pairs, _mandatory):
        assert "release-lock" in summary
        return 1.0, []

    monkeypatch.setattr(module, "_validation", validate)
    assert module.compact([{"role": "user", "content": "Launch date is April 3; release-lock is required."}], 100)["validation_score"] == 1.0
