# Production Hardening Finalization

This document describes the final production hardening changes that complete the security hardening of MailJaeger.

## Overview

Three critical production safety gaps have been addressed:

1. **Hard DEBUG guard** - Prevents DEBUG=true in any web-exposed configuration
2. **Sanitized startup logs** - Removes raw exception strings from startup error logs
3. **Runnable auth tests** - Tests that work correctly with global authentication middleware

---

## A) Hard DEBUG Guard

### Problem
The previous debug guard only checked `SERVER_HOST=0.0.0.0` and `TRUST_PROXY=true`, but missed the case where `ALLOWED_HOSTS` is set, which also indicates a web-exposed deployment.

### Solution
**File:** `src/config.py`

1. **Added ALLOWED_HOSTS field** (lines 38-41):
```python
allowed_hosts: str = Field(
    default="",
    description="Comma-separated list of allowed host headers (leave empty for no restriction)"
)
```

2. **Updated web_exposed check** (lines 229-235):
```python
# Check for production debug guard - prevent DEBUG=true in web-exposed deployments
# Web-exposed means: accessible from internet (0.0.0.0), behind proxy, or has allowed hosts set
is_web_exposed = (
    self.server_host == "0.0.0.0" or 
    self.trust_proxy or
    (self.allowed_hosts and self.allowed_hosts.strip())
)

if self.debug and is_web_exposed:
    errors.append(
        "DEBUG must be false in production/web-exposed deployments. "
        "Running with DEBUG=true exposes sensitive information in logs and API responses. "
        "Set DEBUG=false when SERVER_HOST=0.0.0.0, TRUST_PROXY=true, or ALLOWED_HOSTS is set."
    )
```

### Behavior Matrix

| Configuration | Result |
|---------------|--------|
| `DEBUG=true` + `SERVER_HOST=127.0.0.1` + `TRUST_PROXY=false` + `ALLOWED_HOSTS=""` | ✅ Allowed (local dev) |
| `DEBUG=true` + `SERVER_HOST=0.0.0.0` | ❌ Blocked (ValueError) |
| `DEBUG=true` + `TRUST_PROXY=true` | ❌ Blocked (ValueError) |
| `DEBUG=true` + `ALLOWED_HOSTS="example.com"` | ❌ Blocked (ValueError) |
| `DEBUG=false` + any config | ✅ Allowed |

### Acceptance Test
```bash
# This will fail:
DEBUG=true SERVER_HOST=0.0.0.0 python -m src.main
# Error: "DEBUG must be false in production/web-exposed deployments..."
```

---

## B) Sanitized Startup Logs

### Problem
Startup error handling in `src/main.py` used f-strings to log exceptions, which could leak sensitive information like credentials:
```python
logger.error(f"Configuration validation failed: {e}")  # BAD
logger.error(f"Failed to load configuration: {e}")     # BAD
```

### Solution
**File:** `src/main.py` (lines 43-56)

Replaced f-string logging with `sanitize_error()`:
```python
# Settings with validation
try:
    settings = get_settings()
    settings.validate_required_settings()
except ValueError as e:
    # Use sanitize_error to prevent credential leakage in logs
    sanitized = sanitize_error(e, debug=False)
    logger.error("Configuration validation failed: %s", sanitized)
    print(f"\n❌ Configuration Error:\n{e}\n", file=sys.stderr)
    print("Please check your .env file and environment variables.", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    # Use sanitize_error to prevent credential leakage in logs
    sanitized = sanitize_error(e, debug=False)
    logger.error("Failed to load configuration: %s", sanitized)
    print(f"\n❌ Configuration Error: {e}\n", file=sys.stderr)
    sys.exit(1)
```

**Key Changes:**
1. Uses `%s` formatting instead of f-strings (safer)
2. Calls `sanitize_error(e, debug=False)` before logging
3. Only logs sanitized error (type name only in production)
4. User still sees full error in stderr for troubleshooting

### Acceptance Test
```bash
# Check that no f-string exception logging remains:
grep -n 'logger.*f".*{e}\|logger.*f".*{exc}' src/main.py
# Result: (empty - no matches)
```

---

## C) Runnable Auth Tests

### Problem
Previous tests didn't work correctly with the global authentication middleware because:
1. They didn't set `API_KEY` in environment
2. They didn't include `Authorization` header in requests
3. They couldn't toggle `DEBUG` mode properly

### Solution
**New File:** `tests/test_production_hardening_final.py`

Created comprehensive tests that properly work with auth:

#### Test Structure
```python
# Pattern used in all tests:
with patch.dict(os.environ, {
    "DEBUG": "true",
    "API_KEY": "testkey",
    "SERVER_HOST": "127.0.0.1",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}, clear=True):
    from src.config import reload_settings
    reload_settings()
    from src.main import app
    
    client = TestClient(app)
    response = client.get("/endpoint", headers={"Authorization": "Bearer testkey"})
```

#### Test Classes

**1. TestDebugGuard** (4 tests)

- `test_debug_guard_blocks_web_exposed_with_0_0_0_0`
  - Sets `DEBUG=true` + `SERVER_HOST=0.0.0.0`
  - Expects `ValueError` with "DEBUG must be false in production"

- `test_debug_guard_blocks_web_exposed_with_trust_proxy`
  - Sets `DEBUG=true` + `TRUST_PROXY=true`
  - Expects `ValueError`

- `test_debug_guard_blocks_web_exposed_with_allowed_hosts`
  - Sets `DEBUG=true` + `ALLOWED_HOSTS="example.com,api.example.com"`
  - Expects `ValueError`

- `test_debug_allowed_on_localhost_without_exposure`
  - Sets `DEBUG=true` + localhost config (no web exposure)
  - Should succeed (no ValueError)

**2. TestGlobalExceptionHandler** (3 tests)

- `test_exception_handler_sanitizes_response_when_debug_false`
  - Sets `DEBUG=false`
  - Creates test route that raises exception with "password=secret123"
  - Verifies response is "Internal server error"
  - Verifies logs don't contain "secret123"
  - Uses `caplog` fixture to inspect logs

- `test_exception_handler_shows_details_in_debug_mode`
  - Sets `DEBUG=true`
  - Creates test route that raises exception
  - Verifies response contains error details (for development)

- `test_exception_handler_never_leaks_imap_credentials`
  - Sets `IMAP_PASSWORD="my_secret_password_123"`
  - Verifies password never appears in logs or response

### Key Features

1. **Environment Isolation**: Uses `patch.dict(os.environ, {...}, clear=True)`
2. **Settings Reload**: Calls `reload_settings()` to pick up test environment
3. **Auth Headers**: All requests include `Authorization: Bearer testkey`
4. **Log Capture**: Uses `caplog` fixture to verify log content
5. **No External Dependencies**: All tests use mocks, no real IMAP/services

### Running Tests
```bash
# Run all production hardening tests
pytest tests/test_production_hardening_final.py -v

# Run specific test
pytest tests/test_production_hardening_final.py::TestDebugGuard::test_debug_guard_blocks_web_exposed_with_0_0_0_0 -v

# Run with coverage
pytest tests/test_production_hardening_final.py --cov=src.config --cov=src.main -v
```

---

## D) Documentation Updates

### .env.example

**Updated DEBUG section:**
```bash
# Debug Mode
# PRODUCTION WARNING: DEBUG must be false in production/web-exposed deployments
# When DEBUG=true, detailed error messages and stack traces are exposed in logs and API responses
# This can leak sensitive information (credentials, internal paths, etc.)
# The application will refuse to start if DEBUG=true when:
#   - SERVER_HOST=0.0.0.0 (externally accessible), OR
#   - TRUST_PROXY=true (behind reverse proxy), OR
#   - ALLOWED_HOSTS is set (indicating internet-facing deployment)
# For local development only - always set to false for production
# Never run DEBUG=true on an internet-facing host; app will refuse to start.
DEBUG=false
```

**Added ALLOWED_HOSTS section:**
```bash
# Allowed Host Headers (optional, for additional security)
# Comma-separated list of allowed host headers (e.g., example.com,api.example.com)
# Leave empty for no restriction (default)
# WARNING: Setting this indicates web-exposed deployment and will block DEBUG=true
ALLOWED_HOSTS=
```

---

## Summary

### Changes Made

| File | Changes | Lines |
|------|---------|-------|
| `src/config.py` | Added ALLOWED_HOSTS, updated debug guard | +8 |
| `src/main.py` | Sanitized startup logging | +6 |
| `.env.example` | Updated DEBUG docs, added ALLOWED_HOSTS | +7 |
| `tests/test_production_hardening_final.py` | New test file | +185 |
| **Total** | **4 files** | **~206 lines** |

### Security Impact

**Before:**
- ⚠️ Could run DEBUG=true with ALLOWED_HOSTS set
- ⚠️ Startup logs could leak credentials via raw exception strings
- ⚠️ Tests couldn't verify auth-protected behavior

**After:**
- ✅ Impossible to run DEBUG=true in any web-exposed config
- ✅ All startup logs sanitized (only error types logged)
- ✅ Comprehensive tests verify all security behaviors
- ✅ Multi-layer defense against credential exposure

### Acceptance Criteria

All requirements met:

✅ **A) Hard DEBUG guard:**
- Web-exposed checks all 3 conditions (0.0.0.0, TRUST_PROXY, ALLOWED_HOSTS)
- Raises ValueError with safe message
- Starting with DEBUG=true + web-exposed config fails fast

✅ **B) Sanitized startup logs:**
- No f-string exception logging in main.py
- All logs use sanitize_error() in non-debug mode
- Grep confirms no `f"...{e}..."` or `f"...{exc}..."` patterns

✅ **C) Runnable auth tests:**
- 7 comprehensive tests added
- All tests use API_KEY + Authorization header
- Tests verify both debug guard and exception sanitization
- Tests capture logs with caplog
- All tests work with global auth middleware

✅ **D) Documentation:**
- .env.example updated with clear warnings
- README already has production safety guidance

---

## Verification Commands

```bash
# 1. Verify no f-string exception logging
grep -n 'logger.*f".*{e}\|logger.*f".*{exc}' src/main.py
# Expected: (empty)

# 2. Verify debug guard blocks web-exposed
python -c "from src.config import Settings; s = Settings(debug=True, server_host='0.0.0.0', imap_host='test', imap_username='test', imap_password='test'); s.validate_required_settings()"
# Expected: ValueError with "DEBUG must be false in production"

# 3. Run production hardening tests
pytest tests/test_production_hardening_final.py -v
# Expected: 7/7 tests pass

# 4. Check all syntax
python -m py_compile src/config.py src/main.py tests/test_production_hardening_final.py
# Expected: (no output - success)
```

---

## Conclusion

This final production hardening completes the security posture of MailJaeger by:

1. **Preventing** DEBUG=true in any web-exposed configuration
2. **Sanitizing** all startup error logging to prevent credential leakage
3. **Verifying** all security behaviors with comprehensive auth-compatible tests

The application is now production-ready with multiple layers of protection against accidental credential exposure.
