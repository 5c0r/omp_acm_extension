import os
import subprocess
import sys


def run_python(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_architecture_falls_back_to_three_categories_without_llm():
    """Fails if an unavailable model leaves a scope without a usable architecture."""
    result = run_python(
        "from acm.architect import generate_architecture; "
        "spec = generate_architecture('coding assistant'); "
        "assert len(spec['categories']) >= 3, spec",
        env={"ACM_OLLAMA_URL": "http://127.0.0.1:1"},
    )

    assert result.returncode == 0, result.stderr


def test_architecture_is_persisted_per_scope():
    """Fails if generated architecture is not stored in the scope architecture row."""
    result = run_python(
        "import uuid; "
        "from acm import db; db.ensure_schema(); "
        "scope = db.get_or_create_scope('project', 'architect-' + uuid.uuid4().hex); "
        "from acm.architect import generate_architecture; "
        "spec = generate_architecture('coding assistant', scope_id=scope); "
        "row = db.connect().execute('SELECT spec FROM architecture WHERE scope_id = %s', (scope,)).fetchone(); "
        "assert row and row['spec']['categories'] == spec['categories'], row",
        env={"ACM_OLLAMA_URL": "http://127.0.0.1:1"},
    )

    assert result.returncode == 0, result.stderr


def test_architecture_reads_an_editable_scope_file_first():
    """Fails if an edited architecture file is ignored in favor of stale generated data."""
    result = run_python(
        """
import json
import os
import tempfile
from pathlib import Path

from acm import db

db.ensure_schema()
scope = db.get_or_create_scope("project", "editable-architecture")
custom = {
    "categories": [
        {"name": "identity", "description": "people", "examples": [], "retention": "forever", "must_preserve_verbatim": True},
        {"name": "decision", "description": "choices", "examples": [], "retention": "long", "must_preserve_verbatim": False},
        {"name": "fact", "description": "facts", "examples": [], "retention": "short", "must_preserve_verbatim": False},
    ],
    "extraction_guidance": "extract facts",
    "compaction_policy": "keep decisions",
}
with tempfile.TemporaryDirectory() as directory:
    Path(directory, f"{scope}.json").write_text(json.dumps(custom))
    os.environ["ACM_ARCH_DIR"] = directory
    from acm.architect import generate_architecture

    assert generate_architecture("ignored", scope_id=scope) == custom
""",
    )

    assert result.returncode == 0, result.stderr
