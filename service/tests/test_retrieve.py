import subprocess
import sys


def test_fetch_expands_shared_entity_bridge_with_provenance():
    """Fails if graph expansion does not surface a linked memory as via_graph."""
    script = """
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps({"embeddings": [[0.0] * 1024]}).encode()
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

db.ensure_schema()
scope_name = "retrieve-" + uuid.uuid4().hex
scope = "project:" + scope_name
scope_id = db.get_or_create_scope("project", scope_name)
with db.connect() as conn:
    entity = conn.execute(
        "INSERT INTO entity (scope_id, canonical_name) VALUES (%s, %s) RETURNING id",
        (scope_id, "Sarah Chen"),
    ).fetchone()
    source = conn.execute(
        "INSERT INTO memory (scope_id, kind, content, source_ref) VALUES (%s, 'fact', %s, 'choice') RETURNING id",
        (scope_id, "Sarah chose the Pro plan.",),
    ).fetchone()
    bridge = conn.execute(
        "INSERT INTO memory (scope_id, kind, content, source_ref) VALUES (%s, 'episode', %s, 'onboarding') RETURNING id",
        (scope_id, "Sarah's onboarding record is current.",),
    ).fetchone()
    conn.execute("INSERT INTO mention (memory_id, entity_id) VALUES (%s, %s), (%s, %s)", (source["id"], entity["id"], bridge["id"], entity["id"]))

from acm.retrieve import fetch

items = fetch("Pro plan", scope)["items"]
linked = next(item for item in items if item["id"] == bridge["id"])
assert linked["via_graph"] is True
assert linked["source_ref"] == "onboarding"
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_anticipation_writes_session_bundle_readable_without_http_work():
    """Fails if context-bundle reads replay prediction or retrieval instead of serving stored text."""
    script = """
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Handler.calls += 1
        if self.path == "/api/embed":
            body = json.dumps({"embeddings": [[0.0] * 1024]}).encode()
        else:
            body = json.dumps({"message": {"content": '{"intents":["Pro plan"]}'}}).encode()
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

db.ensure_schema()
scope_name = "bundle-" + uuid.uuid4().hex
scope = "project:" + scope_name
scope_id = db.get_or_create_scope("project", scope_name)
with db.connect() as conn:
    conn.execute(
        "INSERT INTO memory (scope_id, kind, content, source_ref) VALUES (%s, 'fact', %s, 'test')",
        (scope_id, "The Pro plan was selected on April 3.",),
    )

from acm.anticipate import anticipate, get_bundle
from acm.retrieve import fetch

session_id = "session-" + uuid.uuid4().hex
anticipate(session_id, scope, [{"role": "user", "content": "Which plan did we choose?"}])
calls_before_read = Handler.calls
bundle = get_bundle(session_id)
assert bundle and "Pro plan" in bundle["rendered"], bundle
assert Handler.calls == calls_before_read
assert fetch("Pro plan", scope)["items"]
with db.connect() as conn:
    bundle_stat = conn.execute("SELECT value FROM stats WHERE key = 'bundle_injected'").fetchone()
    fetch_stat = conn.execute("SELECT value FROM stats WHERE key = 'explicit_fetch'").fetchone()
assert bundle_stat and int(bundle_stat["value"]) >= 1
assert fetch_stat and int(fetch_stat["value"]) >= 1
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_fetch_unions_vector_results_and_honors_token_budget():
    """Fails if vector-only recall disappears or budget trimming leaks a second item."""
    script = """
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

vector = [1.0] + [0.0] * 1023

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps({"embeddings": [vector]}).encode()
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

db.ensure_schema()
scope_name = "hybrid-" + uuid.uuid4().hex
scope = "project:" + scope_name
scope_id = db.get_or_create_scope("project", scope_name)
literal = "[" + ",".join(str(value) for value in vector) + "]"
with db.connect() as conn:
    lexical = conn.execute(
        "INSERT INTO memory (scope_id, kind, content) VALUES (%s, 'fact', 'Pro plan') RETURNING id",
        (scope_id,),
    ).fetchone()
    semantic = conn.execute(
        "INSERT INTO memory (scope_id, kind, content, embedding) VALUES (%s, 'fact', 'Private topic records', %s::vector) RETURNING id",
        (scope_id, literal),
    ).fetchone()

from acm.retrieve import fetch

full = fetch("Pro plan", scope, budget_tokens=100)["items"]
assert any(item["id"] == semantic["id"] for item in full), full
limited = fetch("Pro plan", scope, budget_tokens=2)["items"]
assert [item["id"] for item in limited] == [lexical["id"]], limited
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_deep_fetch_uses_model_rerank_order():
    """Fails if deep retrieval returns RRF order instead of model-selected IDs."""
    script = """
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    order = []

    def do_POST(self):
        if self.path == "/api/embed":
            body = json.dumps({"embeddings": [[0.0] * 1024]}).encode()
        else:
            body = json.dumps({"message": {"content": json.dumps({"ids": Handler.order})}}).encode()
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

db.ensure_schema()
scope_name = "rerank-" + uuid.uuid4().hex
scope = "project:" + scope_name
scope_id = db.get_or_create_scope("project", scope_name)
with db.connect() as conn:
    first = conn.execute(
        "INSERT INTO memory (scope_id, kind, content) VALUES (%s, 'fact', 'Plan record') RETURNING id",
        (scope_id,),
    ).fetchone()
    second = conn.execute(
        "INSERT INTO memory (scope_id, kind, content) VALUES (%s, 'fact', 'Plan record') RETURNING id",
        (scope_id,),
    ).fetchone()
from acm.retrieve import fetch

shallow = fetch("Plan", scope)["items"]
assert len(shallow) == 2, shallow
Handler.order = [shallow[1]["id"], shallow[0]["id"]]
items = fetch("Plan", scope, deep=True)["items"]
assert [item["id"] for item in items] == Handler.order, items
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
