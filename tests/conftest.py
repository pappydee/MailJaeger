"""
Global pytest configuration and fixtures for MailJaeger test suite.

Solves five classes of test-ordering flakiness:

A) AllowedHosts "Invalid host" 400 errors
   Fix: src/middleware/allowed_hosts.py always adds "testserver"/"localhost";
   test_allowed_hosts.py's get_fresh_app() no longer clears src.models.* so
   SQLAlchemy mappers remain intact.

B) Rate limiting 429 interference
   Fix: reset() the in-memory limiter before every test.

C) Settings caching and env-var pollution
   Root cause: tests mutate os.environ (API_KEY, SAFE_MODE, REQUIRE_APPROVAL)
   and call reload_settings() without restoring.  Subsequent tests get stale
   or wrong settings, causing auth failures (401) or logic failures.
   Fix: autouse fixture forces a canonical env baseline before EVERY test,
   then calls reload_settings() so get_settings() cache is always fresh.

D) FastAPI dependency_overrides cleanup
   Fix: autouse fixture clears overrides after every test.

E) SQLAlchemy mapper corruption from module reimports
   Fix: test_allowed_hosts.py's get_fresh_app() preserves src.models.* so
   the mapper registry is not clobbered.
"""

import os
import sys
import pytest

# ---------------------------------------------------------------------------
# Canonical test API key used by the entire test suite.
# Tests that need a DIFFERENT key must use patch.dict(os.environ, {...}) AND
# call reload_settings() inside the patch context.
# ---------------------------------------------------------------------------
TEST_API_KEY = "test_key_abc123"

# ---------------------------------------------------------------------------
# Minimal environment required by src.main at import time.
# Set these BEFORE any test module is imported so that Settings() validation
# succeeds regardless of test collection order.
# ---------------------------------------------------------------------------
_MINIMAL_ENV = {
    "API_KEY": TEST_API_KEY,
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "false",
    "ALLOW_DESTRUCTIVE_IMAP": "false",
    "REQUIRE_APPROVAL": "false",
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
    Reset ALL shared mutable state before and after each test so that test
    execution order does not influence results.
    """
    # ---- before test -------------------------------------------------------
    # Force canonical env vars so every test starts from a clean baseline,
    # regardless of what earlier tests may have mutated in os.environ.
    _restore_canonical_env()
    _reload_main_settings()
    _reset_rate_limiter()

    yield  # <-- test executes here

    # ---- after test --------------------------------------------------------
    _reset_rate_limiter()
    _clear_dependency_overrides()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _restore_canonical_env():
    """Force canonical env vars for all settings that tests commonly mutate."""
    canonical = {
        "API_KEY": TEST_API_KEY,
        "SAFE_MODE": "false",
        "REQUIRE_APPROVAL": "false",
        "ALLOW_DESTRUCTIVE_IMAP": "false",
    }
    for key, val in canonical.items():
        os.environ[key] = val


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
            import src.main  # noqa: F401
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
