import subprocess
import sys


def test_exact_alias_resolution_reuses_canonical_entity():
    """Fails if an alias creates a second entity instead of resolving canonically."""
    script = """
import uuid
from acm import db

db.ensure_schema()
scope = db.get_or_create_scope("project", "entities-" + uuid.uuid4().hex)
from acm.entities import resolve_entity

canonical = resolve_entity("Sarah Chen", scope, aliases=["Sarah", "SC"])
alias = resolve_entity("SC", scope)
assert alias["id"] == canonical["id"]
assert alias["canonical_name"] == "Sarah Chen"
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_trigram_resolution_reuses_near_canonical_name():
    """Fails if a high-similarity spelling variant fragments an entity."""
    script = """
import uuid
from acm import db

db.ensure_schema()
scope = db.get_or_create_scope("project", "trigram-" + uuid.uuid4().hex)
from acm.entities import resolve_entity

canonical = resolve_entity("Diana Grant", scope)
variant = resolve_entity("Diana Grant!", scope)
assert variant["id"] == canonical["id"]
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_cosine_resolution_reuses_semantic_entity_when_names_do_not_match():
    """Fails if cosine fallback is removed after exact/trigram resolution misses."""
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
scope = db.get_or_create_scope("project", "cosine-" + uuid.uuid4().hex)
literal = "[" + ",".join(str(value) for value in vector) + "]"
with db.connect() as conn:
    entity = conn.execute(
        "INSERT INTO entity (scope_id, canonical_name, embedding) VALUES (%s, %s, %s::vector) RETURNING id",
        (scope, "Vector Target", literal),
    ).fetchone()

from acm.entities import resolve_entity

matched = resolve_entity("unrelated words", scope)
assert matched["id"] == entity["id"]
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
