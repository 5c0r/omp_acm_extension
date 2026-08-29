import subprocess
import sys


def test_ingest_worker_persists_detail_and_skips_duplicate_digest():
    """Fails if queued ingestion loses source detail or stores an identical payload twice."""
    script = """
import time
import uuid
import os

os.environ["ACM_OLLAMA_URL"] = "http://127.0.0.1:1"
from acm import db
from acm.ingest import start_worker, status, submit

start_worker()
scope = "project:ingest-" + uuid.uuid4().hex
text = "We upgraded from Starter to Pro on April 3."
first = submit(scope, text, "test")
for _ in range(300):
    first_status = status(first)
    if first_status["status"] != "pending":
        break
    time.sleep(0.1)
assert first_status["status"] == "done", first_status

second = submit(scope, text, "test")
for _ in range(300):
    second_status = status(second)
    if second_status["status"] != "pending":
        break
    time.sleep(0.1)
assert second_status["status"] == "skipped", second_status

scope_id = db.get_or_create_scope("project", scope.split(":", 1)[1])
with db.connect() as conn:
    rows = conn.execute("SELECT content FROM memory WHERE scope_id = %s", (scope_id,)).fetchall()
assert len(rows) == 1, rows
assert "Pro" in rows[0]["content"] and "April 3" in rows[0]["content"]
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_ingest_worker_skips_empty_payload():
    """Fails if blank input becomes a durable memory."""
    script = """
import time
import uuid
from acm.ingest import start_worker, status, submit

start_worker()
job = submit("project:empty-" + uuid.uuid4().hex, "   ", "test")
for _ in range(100):
    current = status(job)
    if current["status"] != "pending":
        break
    time.sleep(0.1)
assert current["status"] == "skipped", current
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_ingest_extracts_entities_edges_temporal_facts_and_merges_cosine_duplicates():
    """Fails if structured extraction or same-kind cosine deduplication is removed."""
    script = """
import hashlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

extraction = {
    "memories": [{
        "kind": "fact",
        "content": "The company upgraded to Pro on April 3.",
        "importance": 0.8,
        "valid_from": "2025-04-03",
        "valid_until": None,
        "entities": [
            {"name": "Sarah Chen", "aliases": ["Sarah", "SC"], "role": "subject"},
            {"name": "Acme Corp", "aliases": ["Acme"], "role": "organization"},
        ],
    }],
    "relations": [{"from": "Sarah Chen", "to": "Acme Corp", "relation": "works_at"}],
}

def vector(text):
    value = [0.0] * 1024
    value[int(hashlib.sha256(text.encode()).hexdigest(), 16) % 1024] = 1.0
    return value

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/embed":
            inputs = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"]
            body = json.dumps({"embeddings": [vector(value) for value in inputs]}).encode()
        else:
            body = json.dumps({"message": {"content": json.dumps(extraction)}}).encode()
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

from acm import db
from acm.ingest import start_worker, status, submit

def await_done(job):
    for _ in range(100):
        current = status(job)
        if current["status"] != "pending":
            return current
        time.sleep(0.1)
    raise AssertionError("ingest did not finish")

start_worker()
scope = "project:structured-" + uuid.uuid4().hex
assert await_done(submit(scope, "first source", "first"))["status"] == "done"
assert await_done(submit(scope, "second source", "second"))["status"] == "done"

scope_id = db.get_or_create_scope("project", scope.split(":", 1)[1])
with db.connect() as conn:
    memory = conn.execute(
        "SELECT content, valid_from, source_ref, importance FROM memory WHERE scope_id = %s",
        (scope_id,),
    ).fetchall()
    sarah = conn.execute(
        "SELECT aliases FROM entity WHERE scope_id = %s AND canonical_name = 'Sarah Chen'",
        (scope_id,),
    ).fetchone()
    edge_count = conn.execute("SELECT count(*) AS count FROM edge WHERE scope_id = %s", (scope_id,)).fetchone()["count"]

assert len(memory) == 1, memory
assert memory[0]["content"] == "The company upgraded to Pro on April 3."
assert str(memory[0]["valid_from"]) == "2025-04-03"
assert memory[0]["source_ref"] == "first"
assert memory[0]["importance"] > 0.8
assert {"Sarah", "SC"}.issubset(sarah["aliases"])
assert edge_count == 1
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_ingest_recovers_possessive_proper_names_when_extraction_omits_them():
    """Fails if a possessive shared name cannot form a graph bridge."""
    result = subprocess.run(
        [sys.executable, "-c", "from acm.ingest import _possessive_entities; assert _possessive_entities(\"Sarah's renewal\") == [{'name': 'Sarah', 'aliases': [], 'role': None}]"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
