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
            content = json.dumps({"probes": ["p1", "p2", "p3", "p4", "p5"]})
        elif "TASK: validate" in prompt:
            content = json.dumps({"answers": [{"answerable": True}] * 5})
        elif "TASK: turn-prefix" in prompt:
            content = json.dumps({"summary": "Turn prefix kept file context."})
        else:
            content = json.dumps({"summary": "History summary keeps the decision."})
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
    [{"role": "user", "content": "x" * 2000}],
    budget_tokens=500,
    turn_prefix=[{"role": "assistant", "content": "split turn file result"}],
    previous_summary="Earlier summary preserves decision.",
    custom_instructions="Prioritize deployment risks.",
    file_ops={"read": ["deploy.py"], "written": [], "edited": ["deploy.py"]},
    previous_preserve_data={"snapcompact": {"version": 1}},
)
prompts = chr(10).join(Handler.prompts)
expected = "History summary keeps the decision." + chr(10) + "---" + chr(10) + "**Turn Context (split turn):**" + chr(10) + "Turn prefix kept file context."
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
