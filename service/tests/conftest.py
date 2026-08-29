import os
from urllib.parse import urlsplit

import pytest


def pytest_sessionstart(session):
    target = os.environ.get("ACM_TEST_BASE_URL", "http://127.0.0.1:8927")
    if os.environ.get("ACM_TEST_STACK") != "1":
        pytest.exit(f"refusing to run against live acm stack: ACM_TEST_STACK=1 required (ACM_TEST_BASE_URL={target}) — use the acm-test stack", returncode=2)
    base_url = urlsplit(target)
    if base_url.hostname in {"127.0.0.1", "localhost"} and base_url.port == 8927:
        pytest.exit(f"refusing to run against live acm stack: ACM_TEST_BASE_URL={target} — use the acm-test stack", returncode=2)
