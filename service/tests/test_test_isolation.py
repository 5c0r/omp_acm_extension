import os
import subprocess
import sys
from pathlib import Path


def run_pytest(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_sanitize.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )


def test_missing_test_stack_marker_aborts():
    """Fails if a non-live URL can bypass isolated-stack enforcement."""
    env = {**os.environ, "ACM_TEST_BASE_URL": "http://example.test:9999"}
    env.pop("ACM_TEST_STACK", None)
    result = run_pytest(env)

    assert result.returncode == 2, result.stderr
    assert "refusing to run against live acm stack: ACM_TEST_STACK=1 required" in result.stdout + result.stderr


def test_unmarked_compose_url_aborts():
    """Fails if Compose's internal hostname bypasses the live-target guard."""
    env = {**os.environ, "ACM_TEST_BASE_URL": "http://acm-service:8927"}
    env.pop("ACM_TEST_STACK", None)
    result = run_pytest(env)

    assert result.returncode == 2, result.stderr
    assert "refusing to run against live acm stack: ACM_TEST_STACK=1 required" in result.stdout + result.stderr


def test_marked_live_url_aborts():
    result = run_pytest({**os.environ, "ACM_TEST_STACK": "1", "ACM_TEST_BASE_URL": "http://127.0.0.1:8927"})

    assert result.returncode == 2, result.stderr
    assert "refusing to run against live acm stack: ACM_TEST_BASE_URL=http://127.0.0.1:8927" in result.stdout + result.stderr


def test_marked_test_stack_runs():
    result = run_pytest({**os.environ, "ACM_TEST_STACK": "1", "ACM_TEST_BASE_URL": "http://acm-service:8927"})

    assert result.returncode == 0, result.stderr
    assert "1 passed" in result.stdout
