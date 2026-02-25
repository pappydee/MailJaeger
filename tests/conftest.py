"""
Global pytest configuration and fixtures for MailJaeger test suite.

This module solves three classes of test-ordering flakiness:

A) AllowedHosts "Invalid host" 400 errors
   Root cause: test_allowed_hosts.py calls get_fresh_app() which reimports
   src.main with ALLOWED_HOSTS="example.com", leaving AllowedHostsMiddleware
   with a restricted allowed-hosts set for the rest of the test run.
   Fix: src/middleware/allowed_hosts.py always adds "testserver" and
   "localhost" to the allowed set; conftest ensures settings reload.

B) Rate limiting 429 interference
   Root cause: rate limiter uses per-process in-memory state; trigger tests
   after other trigger tests can hit the 5/minute limit.
   Fix: reset() the in-memory limiter before every test.

C) Settings caching and src.main module-level 'settings' pollution
   Root cause: src.main captures 'settings = get_settings()' at import time.
   When test_allowed_hosts.py reimports src.main (via get_fresh_app) with
   different env vars, src.main.settings becomes stale (e.g. safe_mode=True).
   Even calling reload_settings() only updates src.config._settings; the
   reference stored in src.main.settings is not refreshed.
   Fix: autouse fixture refreshes src.main.settings after every test.

D) FastAPI dependency_overrides cleanup
   Some tests set app.dependency_overrides to inject mock DB sessions.
   If a test crashes or forgets to clean up, the override leaks into the next
   test.  The autouse fixture clears overrides after every test.
"""

import os
import sys
import pytest

# ---------------------------------------------------------------------------
# Minimal environment required by src.main at import time.
# Set these BEFORE any test module is imported so that Settings() validation
# succeeds regardless of test collection order.
# ---------------------------------------------------------------------------
_MINIMAL_ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "false",
    "ALLOW_DESTRUCTIVE_IMAP": "false",
}

for _k, _v in _MINIMAL_ENV.items():
    if _k not in os.environ:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Autouse fixture – runs around every single test in the suite.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_global_state():
    """
    Reset shared mutable state before and after each test so that test
    execution order does not influence results.
    """
    # ---- before test -------------------------------------------------------
    # Ensure rate-limiter counters are zeroed so no test is affected by
    # earlier tests consuming the quota.
    _reset_rate_limiter()

    yield  # <-- test executes here

    # ---- after test --------------------------------------------------------
    # Reset rate limiter again (in case the test itself consumed quota).
    _reset_rate_limiter()

    # Clear any FastAPI dependency overrides set by the test so they do not
    # leak into the next test.
    _clear_dependency_overrides()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _reset_rate_limiter():
    """Reset the slowapi in-memory rate-limiter, ignoring errors."""
    try:
        from src.middleware.rate_limiting import limiter
        limiter.reset()
    except Exception:
        pass


def _reload_main_settings():
    """
    Reload src.config._settings and update the module-level 'settings'
    reference inside src.main (if it has already been imported).
    """
    try:
        from src.config import reload_settings, get_settings
        reload_settings()
        if "src.main" in sys.modules:
            import src.main  # noqa: F401  (already imported, no re-import)
            src.main.settings = get_settings()
    except Exception:
        pass


def _clear_dependency_overrides():
    """Clear FastAPI dependency_overrides on src.main.app."""
    try:
        if "src.main" in sys.modules:
            import src.main  # noqa: F401
            src.main.app.dependency_overrides.clear()
    except Exception:
        pass
