# ALLOWED_HOSTS Runtime Enforcement

## Summary

This implementation enforces `Settings.allowed_hosts` at runtime to protect against host header attacks.

## Implementation

### Middleware: `src/middleware/allowed_hosts.py`

**Behavior**:
- Empty `allowed_hosts` = no restriction (default)
- Non-empty = validates Host header (or X-Forwarded-Host if trust_proxy=true)
- Returns HTTP 400 with `{"success": false, "message": "Invalid host"}` on rejection
- Logs rejections without exposing credentials

**Host Determination**:
- If `trust_proxy=true`: Prefer X-Forwarded-Host (first value), fallback to Host header
- If `trust_proxy=false`: Use Host header only
- Ports are stripped for comparison (e.g., `example.com:443` matches `example.com`)
- Case-insensitive matching

### Wiring: `src/main.py`

Middleware is added at line 190:
```python
app.add_middleware(AllowedHostsMiddleware, settings=settings)
```

Position: After security headers, before CORS

### Tests: `tests/test_allowed_hosts.py`

8 comprehensive tests covering all requirements:
- ✅ Empty ALLOWED_HOSTS allows all hosts
- ✅ ALLOWED_HOSTS set with allowed host succeeds
- ✅ ALLOWED_HOSTS set with disallowed host returns 400
- ✅ Port stripping works correctly
- ✅ TRUST_PROXY=true uses X-Forwarded-Host when allowed
- ✅ TRUST_PROXY=true rejects disallowed X-Forwarded-Host
- ✅ TRUST_PROXY=false ignores X-Forwarded-Host
- ✅ Case-insensitive matching works

All tests use API_KEY and Authorization header for global auth.

### Documentation: `.env.example`

Lines 31-38 document:
- Comma-separated format
- Empty disables validation
- Example values
- trust_proxy and X-Forwarded-Host behavior

## Changes Made

Only 2 files modified with minimal changes:
- `src/middleware/allowed_hosts.py`: Updated JSON response format (1 line)
- `tests/test_allowed_hosts.py`: Updated test assertions (6 lines)

Total: 11 lines changed

## Verification

```bash
# Compilation
python -m py_compile src/main.py
✅ Success

# Tests
pytest tests/test_allowed_hosts.py -v
✅ 8 passed
```

## Security Features

- No credentials in logs
- No request host value in error response
- Minimal error disclosure
- Fail-safe (returns 400, not 500)
- Works with global auth middleware
