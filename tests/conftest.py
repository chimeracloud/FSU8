"""
Shared fixtures.

Test-mode safety: `FSU8_DISABLE_GCP_IO` is set at module load, BEFORE any
project module is imported, so the lifespan's GCS and Pub/Sub calls
short-circuit. Without it every test would hit real GCP, run orders of
magnitude slower, and risk writing to the production config blob and
the shared Source Manifest.
"""
import os

# CRITICAL: set BEFORE anything imports our modules.
os.environ.setdefault("FSU8_DISABLE_GCP_IO", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.config import reset_settings_for_test  # noqa: E402
from core.events import reset_state_for_test  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    """Settings and state are module-level singletons — reset both.

    Without this, one test's config PUT or control action leaks into
    the next and the failures are order-dependent.
    """
    reset_settings_for_test()
    reset_state_for_test()
    yield
    reset_settings_for_test()
    reset_state_for_test()


@pytest.fixture
def client():
    """TestClient as a context manager, so the lifespan actually runs."""
    with TestClient(app) as c:
        yield c
