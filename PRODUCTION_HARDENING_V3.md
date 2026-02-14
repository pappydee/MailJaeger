# Production Hardening V3 - Final Credential Leakage Prevention

## Overview
This document summarizes the final production-hardening fixes implemented to prevent ANY credential leakage (IMAP credentials, API keys, tokens) from being exposed in logs, stderr, or API responses.

## What Changed

### 1. Startup Error Handling (TASK A)
**File**: `src/main.py` (lines 47-69)

**Problem**: During startup, configuration validation errors could potentially expose sensitive credentials to stderr output, even though they were already sanitized in log files.

**Solution**: 
- Implemented DEBUG-aware stderr printing in both ValueError and Exception handlers
- When `DEBUG=false` (production):
  - Prints generic error messages to stderr: "Configuration validation failed" or "Failed to load configuration"
  - Does NOT print the raw exception that may contain credentials
- When `DEBUG=true` (development):
  - Prints full exception details to stderr for developer convenience
  - Helps with debugging configuration issues locally

**Code Pattern**:
```python
except ValueError as e:
    sanitized = sanitize_error(e, debug=False)
    logger.error("Configuration validation failed: %s", sanitized)
    
    # DEBUG guard for stderr
    debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    if debug_mode:
        print(f"\n❌ Configuration Error:\n{e}\n", file=sys.stderr)
    else:
        print(f"\n❌ Configuration Error: Configuration validation failed\n", file=sys.stderr)
    sys.exit(1)
```

### 2. Documentation Updates (TASK D)
**File**: `.env.example` (lines 155-165)

**Enhancement**:
- Clarified that DEBUG mode affects logs, API responses, AND stderr output
- Emphasized that DEBUG=false sanitizes error messages to prevent credential leakage
- Listed specific credential types that could leak: IMAP credentials, API keys, internal paths

**Key Addition**:
> "When DEBUG=true, detailed error messages and stack traces are exposed in logs, API responses, and stderr"
> "This can leak sensitive information (IMAP credentials, API keys, internal paths, etc.)"
> "When DEBUG=false, error messages are sanitized to prevent credential leakage"

### 3. Comprehensive Testing (TASK C)
**File**: `tests/test_startup_error_handling.py` (new file)

**Tests Added**:
1. `test_startup_error_debug_false_no_leak()`: Verifies that when DEBUG=false, startup errors containing credentials don't leak to stderr
2. `test_startup_error_debug_true_shows_details()`: Verifies that when DEBUG=true, full details are shown for developer convenience
3. `test_startup_error_both_exception_types_follow_same_policy()`: Ensures both ValueError and generic Exception handlers follow the same policy
4. `test_startup_succeeds_with_valid_config()`: Sanity check that valid configuration doesn't cause exits

**Test Approach**:
- Uses subprocess to simulate actual startup scenarios
- Captures stderr output to verify what would be visible to users/operators
- Tests with sensitive data (passwords, API keys) to ensure they don't leak
- Works with global auth middleware (uses API keys in test environment)

## Why This Prevents Credential Leakage

### Defense in Depth
1. **Log Files**: Already sanitized via `sanitize_error()` function
2. **API Responses**: Already sanitized via `sanitize_error()` in exception handlers
3. **Stderr Output** (NEW): Now also protected by DEBUG guard

### Attack Vectors Closed
- **Docker/Kubernetes logs**: Console output (stderr) is captured by container orchestration - now safe
- **Systemd journals**: System logs capture stderr - now safe
- **Terminal output**: Operators running the app manually won't see credentials - now safe
- **CI/CD pipelines**: Build logs capture stderr - now safe

### Production vs Development
- **Production** (`DEBUG=false`): Maximum security, minimal information disclosure
- **Development** (`DEBUG=true`): Full details for debugging, but blocked when web-exposed

## How to Verify

### 1. Compile Check
```bash
python -m py_compile src/main.py src/config.py
```
Expected: No output (success)

### 2. Run Tests
```bash
cd /home/runner/work/MailJaeger/MailJaeger
pytest tests/test_startup_error_handling.py -v
```
Expected: All tests pass

### 3. Manual Verification - Production Mode
```bash
# Set invalid IMAP password to trigger startup error
export DEBUG=false
export API_KEY=test_key_12345
export IMAP_HOST=imap.test.com
export IMAP_USERNAME=test@example.com
export IMAP_PASSWORD=secret_password_123
export AI_ENDPOINT=http://localhost:11434
export SERVER_HOST=0.0.0.0  # Web-exposed

python -c "from src.main import app" 2>&1 | tee /tmp/startup_error.log

# Check that secret_password_123 is NOT in output
grep "secret_password_123" /tmp/startup_error.log && echo "FAIL: Password leaked!" || echo "PASS: Password not in stderr"
```

Expected: "PASS: Password not in stderr" (because DEBUG guard blocks this web-exposed + DEBUG combo)

### 4. Manual Verification - Debug Mode (Local Only)
```bash
# Set invalid IMAP password with debug enabled (local mode)
export DEBUG=true
export API_KEY=test_key_12345
export IMAP_HOST=imap.test.com
export IMAP_USERNAME=test@example.com
# Missing IMAP_PASSWORD to trigger error
export AI_ENDPOINT=http://localhost:11434
export SERVER_HOST=127.0.0.1  # Local only

python -c "from src.main import app" 2>&1 | tee /tmp/startup_debug.log

# Check that detailed error IS in output
grep "IMAP_PASSWORD" /tmp/startup_debug.log && echo "PASS: Detailed error shown" || echo "FAIL: Error not detailed"
```

Expected: "PASS: Detailed error shown" (DEBUG=true shows details for local development)

### 5. Pattern Check
```bash
# Ensure no logger.error with raw exception variables
cd /home/runner/work/MailJaeger/MailJaeger
grep -n 'logger.error.*{e}' src/main.py && echo "FAIL: Found unsafe pattern" || echo "PASS: No unsafe patterns"
```

Expected: "PASS: No unsafe patterns"

## Security Impact

### Before V3
- ❌ Startup errors could leak credentials to stderr
- ❌ Docker/K8s logs might contain IMAP passwords
- ❌ CI/CD build logs might expose API keys
- ❌ Systemd journals could store sensitive data

### After V3
- ✅ Startup errors sanitized in production mode
- ✅ Docker/K8s logs contain only generic messages
- ✅ CI/CD build logs safe from credential leakage
- ✅ Systemd journals contain no sensitive data
- ✅ Debug mode still available for local development

## Configuration

### Production Deployment
```env
DEBUG=false
SERVER_HOST=0.0.0.0
API_KEY=<secure-key>
IMAP_HOST=imap.example.com
IMAP_USERNAME=user@example.com
IMAP_PASSWORD=<secure-password>
```

### Local Development
```env
DEBUG=true
SERVER_HOST=127.0.0.1
API_KEY=dev-key
IMAP_HOST=imap.test.com
IMAP_USERNAME=dev@test.com
IMAP_PASSWORD=dev-password
```

### Behind Reverse Proxy
```env
DEBUG=false
TRUST_PROXY=true
ALLOWED_HOSTS=example.com,api.example.com
API_KEY=<secure-key>
```

## Related Files
- `src/main.py`: Startup error handling (lines 47-69)
- `src/config.py`: Settings validation (lines 229-278)
- `src/utils/error_handling.py`: Error sanitization utilities
- `.env.example`: Configuration documentation (lines 155-165)
- `tests/test_startup_error_handling.py`: Verification tests

## Validation Checklist
- [x] Code compiles without errors
- [x] Tests pass
- [x] No `logger.error(f"...{e}...")` patterns remain
- [x] Both ValueError and Exception paths follow same policy
- [x] DEBUG=false prevents credential leakage to stderr
- [x] DEBUG=true shows full details for development
- [x] Documentation updated
- [x] .env.example clarifies DEBUG guard behavior

## Future Considerations
- Consider adding log monitoring alerts for sensitive pattern detection
- Implement structured logging for better parsing in production
- Add metrics for startup failure rates by error type (without leaking details)

---
**Version**: 3.0  
**Date**: 2026-02-14  
**Branch**: copilot/finalize-production-hardening-v3
