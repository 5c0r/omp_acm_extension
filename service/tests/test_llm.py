import os

import subprocess
import sys


def test_tokens_estimates_characters_in_fourths():
    """Fails if token estimation ceases to use len(text)//4."""
    result = subprocess.run(
        [sys.executable, "-c", "from acm.llm import tokens; print(tokens('abcde'))"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_chat_json_returns_requested_object():
    """Fails if JSON chat stops returning a parsed response."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from acm.llm import chat_json; "
                "value = chat_json('Return JSON only.', "
                "'Return exactly {\"status\":\"ok\"}.', "
                "'{\"status\":\"string\"}'); "
                "assert value == {'status': 'ok'}, value"
            ),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_chat_json_returns_none_when_ollama_is_unreachable():
    """Fails if transport errors escape instead of degrading to None."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from acm.llm import chat_json; assert chat_json('x', 'y') is None",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ACM_OLLAMA_URL": "http://127.0.0.1:1"},
    )

    assert result.returncode == 0, result.stderr


def test_chat_json_repairs_a_malformed_model_reply_twice_at_most():
    """Fails if a malformed first reply does not trigger a repair request."""
    script = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    requests = []
    calls = 0

    def do_POST(self):
        Handler.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        Handler.calls += 1
        content = "not json" if Handler.calls == 1 else '{"ok": true}'
        response = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["ACM_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"
from acm.llm import chat_json
assert chat_json("x", "y", max_tokens=64) == {"ok": True}
assert Handler.calls == 2
assert Handler.requests[0]["think"] is False
assert Handler.requests[0]["options"]["num_predict"] == 64
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_embed_returns_1024_dimensional_vectors():
    """Fails if the configured embedding model or batch shape changes."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from acm.llm import embed; value = embed(['context']); assert len(value) == 1 and len(value[0]) == 1024",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_chat_json_strips_thinking_before_parsing_without_retry():
    """Fails if qwen thinking tags turn a valid JSON reply into three repair calls."""
    script = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Handler.calls += 1
        body = json.dumps({"message": {"content": '<think>reasoning</think>{"ok": true}'}}).encode()
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
from acm.llm import chat_json
assert chat_json("x", "y") == {"ok": True}
assert Handler.calls == 1
server.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
