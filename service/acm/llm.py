"""Ollama client helpers."""
import json
import os
import re

import httpx

OLLAMA_URL = os.environ.get("ACM_OLLAMA_URL", "http://localhost:11434").rstrip("/")
CHAT_MODEL = os.environ.get("ACM_CHAT_MODEL", "qwen3:4b")
EMBED_MODEL = os.environ.get("ACM_EMBED_MODEL", "qwen3-embedding:0.6b")
TIMEOUT = float(os.environ.get("ACM_LLM_TIMEOUT", "120"))

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def chat_json(system: str, user: str, schema_hint: str = "") -> dict | None:
    """Strict-JSON chat, repair-retry x2, None on failure."""
    if schema_hint:
        system = f"{system}\n\nReturn only a JSON object matching: {schema_hint}"
    for attempt in range(3):
        try:
            response = httpx.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": CHAT_MODEL,
                    "think": False,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0},
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            value = json.loads(_THINK_RE.sub("", response.json()["message"]["content"]).strip())
            if isinstance(value, dict):
                return value
            raise ValueError("expected a JSON object")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
            if attempt == 2:
                return None
            user = f"{user}\n\nPrevious reply was invalid ({err}). Return one valid JSON object only."
    return None


def embed(texts: list[str]) -> list[list[float]]:
    response = httpx.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["embeddings"]


def tokens(text: str) -> int:
    return len(text) // 4
