# Allowed Hosts Middleware Implementation

## Overview

This document describes the implementation of runtime allowed_hosts enforcement for MailJaeger.

## Branch

**Name**: `copilot/allowed-hosts-enforcement`
**Created from**: `copilot/finish-security-implementation`

## Implementation

### 1. Middleware (`src/middleware/allowed_hosts.py`)

The `AllowedHostsMiddleware` class provides runtime enforcement of the `allowed_hosts` setting:

**Features**:
- Empty `allowed_hosts` = no restriction (default behavior)
- Non-empty = validates Host header against comma-separated allowlist
- Supports `X-Forwarded-Host` when `trust_proxy=true`
- Strips port from hostname for comparison (e.g., `example.com:443` matches `example.com`)
- Case-insensitive matching
- Returns HTTP 400 with minimal JSON error on rejection
- Logs rejections without sensitive data

**Key Methods**:
- `_parse_allowed_hosts()`: Parses comma-separated hostnames into a set
- `_get_effective_host()`: Extracts effective hostname from request (respects trust_proxy)
- `__call__()`: ASGI middleware implementation that validates hosts

### 2. Application Integration (`src/main.py`)

The middleware is wired into the FastAPI application:

```python
from src.middleware.allowed_hosts import AllowedHostsMiddleware

# After security headers, before CORS
app.add_middleware(AllowedHostsMiddleware, settings=settings)
```

**Position in middleware stack**:
1. Global auth middleware (decorator)
2. Request size limiter
3. Security headers
4. **Allowed hosts** ← NEW
5. CORS

### 3. Tests (`tests/test_allowed_hosts.py`)

Comprehensive test suite with 8 test cases:

| Test | Purpose |
|------|---------|
| `test_empty_allowed_hosts_allows_all` | Verify no restriction when empty |
| `test_allowed_host_succeeds` | Verify allowed host passes |
| `test_disallowed_host_returns_400` | Verify disallowed host rejected |
| `test_allowed_host_with_port_succeeds` | Verify port stripping works |
| `test_trust_proxy_with_x_forwarded_host_allowed` | Verify proxy support (allowed) |
| `test_trust_proxy_with_x_forwarded_host_disallowed` | Verify proxy support (rejected) |
| `test_trust_proxy_false_ignores_x_forwarded_host` | Verify X-Forwarded-Host ignored when trust_proxy=false |
| `test_case_insensitive_matching` | Verify case-insensitive matching |

All tests:
- Use `API_KEY` environment variable
- Include `Authorization` header for global auth
- Use `TestClient` from FastAPI
- Reload modules to ensure clean state

### 4. Documentation (`.env.example`)

Updated the `ALLOWED_HOSTS` section with detailed documentation:

```bash
# Allowed Host Headers (runtime enforcement)
# Comma-separated list of allowed hostnames (without scheme or port)
# Examples: example.com,api.example.com,www.example.com
# - Empty (default): No host validation, accepts any Host header
# - Set: Only listed hosts are accepted; rejects others with HTTP 400
# - When TRUST_PROXY=true: Uses X-Forwarded-Host if present, otherwise Host header
# - Ports are ignored during matching (example.com:443 matches example.com)
ALLOWED_HOSTS=
```

## Usage Examples

### Example 1: Local Development (No Restriction)
```bash
ALLOWED_HOSTS=
```
Result: All hosts accepted (default)

### Example 2: Production with Single Domain
```bash
ALLOWED_HOSTS=api.example.com
TRUST_PROXY=false
```
Result: Only requests with `Host: api.example.com` (or with port) accepted

### Example 3: Behind Reverse Proxy
```bash
ALLOWED_HOSTS=example.com,www.example.com
TRUST_PROXY=true
```
Result: Uses `X-Forwarded-Host` if present, validates against allowlist

### Example 4: Multiple Domains
```bash
ALLOWED_HOSTS=example.com,api.example.com,www.example.com
```
Result: All three domains accepted, others rejected with 400

## Security Benefits

1. **Host Header Attack Prevention**: Validates Host header to prevent host header injection attacks
2. **Proxy Support**: Safely handles X-Forwarded-Host when behind trusted proxy
3. **Fail-Safe**: Returns 400 (not 500) on validation failure
4. **Minimal Error Disclosure**: Error response contains no sensitive information
5. **Secure Logging**: Logs rejection without exposing credentials

## Testing

Run the test suite:
```bash
pytest tests/test_allowed_hosts.py -v
```

Expected output:
```
======================== 8 passed ========================
```

## Constraints Met

✅ No new database tables/models
✅ No IMAP logic changes
✅ No approval workflow changes
✅ No token/SAFE_MODE changes
✅ Small, reviewable diffs
✅ Readable code (no minified one-liners)

## Files Changed

- `src/middleware/allowed_hosts.py` (NEW - 117 lines)
- `src/main.py` (3 lines added)
- `tests/test_allowed_hosts.py` (NEW - 221 lines)
- `.env.example` (documentation updated)

**Total additions**: ~350 lines
**Total modifications**: 4 lines

## Verification

All checks pass:
- ✅ Code compiles: `python -m py_compile src/middleware/allowed_hosts.py`
- ✅ Tests pass: `pytest tests/test_allowed_hosts.py -v`
- ✅ Middleware loads: Application starts without errors
- ✅ No breaking changes: Existing functionality unchanged

## Deployment Notes

1. **Default Behavior**: With empty `ALLOWED_HOSTS`, there is no restriction (backward compatible)
2. **Gradual Rollout**: Can enable per environment by setting `ALLOWED_HOSTS`
3. **Monitoring**: Check logs for "Request rejected: host '...' not in allowed_hosts" messages
4. **Proxy Setup**: Set `TRUST_PROXY=true` only when behind a trusted reverse proxy

## Troubleshooting

**Problem**: Legitimate requests getting 400 errors
**Solution**: Add the hostname to `ALLOWED_HOSTS` (without port, without scheme)

**Problem**: X-Forwarded-Host not working
**Solution**: Ensure `TRUST_PROXY=true` is set

**Problem**: Port numbers causing issues
**Solution**: Don't include port in `ALLOWED_HOSTS` - ports are automatically stripped

## References

- Problem statement: See commit message
- Tests: `tests/test_allowed_hosts.py`
- Middleware: `src/middleware/allowed_hosts.py`
- Configuration: `.env.example`
