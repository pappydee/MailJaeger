# Location of New Code - Single-Action Apply Security Hardening

## Branch Information
**Current Branch**: `copilot/finish-security-implementation`
**Status**: Pushed to origin ✅

## What Was Implemented
Security hardening for the single-action apply endpoint to enforce token validation, folder allowlist, and DELETE blocking.

## Where to Find the New Code

### 1. Main Implementation
**File**: `src/main.py`

**Function**: `apply_single_action()` at **line 1026**

**Key Changes** (lines 1026-1248):
- **Lines 1042-1049**: SAFE_MODE check (returns 409 before IMAP)
- **Lines 1051-1093**: apply_token validation and enforcement
  - Line 1052: Check if apply_token is present
  - Lines 1062-1074: Validate token exists and not used
  - Lines 1076-1083: Check token not expired
  - Lines 1085-1093: Verify action_id is in token.action_ids
- **Lines 1114-1130**: DELETE operation blocking
  - Blocks if allow_destructive_imap=false
  - Sets status=REJECTED before IMAP connection
- **Lines 1132-1151**: Folder allowlist validation for MOVE_FOLDER
  - Validates target_folder against safe_folders
  - Blocks before IMAP connection
  - Sets status=FAILED with sanitized error
- **Lines 1153-1156**: Token marked as used (after all validation)
- **Line 1172**: IMAP connection (only after all checks pass)

### 2. Test Files

**File 1**: `tests/test_single_action_apply_security.py`
- 8 integration-style test cases
- Tests missing token, invalid token, expired token, SAFE_MODE, etc.
- Lines: 410 total

**File 2**: `tests/test_single_action_security_requirements.py`
- 7 requirement verification tests
- Validates all 5 requirements through code inspection
- Lines: 156 total

### 3. Import Changes
**File**: `src/main.py`
- **Line 4**: Added `Body` to FastAPI imports
  ```python
  from fastapi import FastAPI, Depends, HTTPException, Query, Request, status, Body
  ```

## How to View the Changes

### View the specific function:
```bash
cd /home/runner/work/MailJaeger/MailJaeger
# View lines 1026-1248 of src/main.py
sed -n '1026,1248p' src/main.py
```

### View the diff:
```bash
# Show what changed in the last 2 commits
git log -p -2 -- src/main.py

# Show just the stats
git diff e242d55..HEAD --stat
```

### View specific commits:
```bash
# Main implementation commit
git show af755ff

# Test additions commit
git show 0949d62
```

## Verification

All changes are on branch: `copilot/finish-security-implementation`

To verify the implementation:
```bash
# Compile check
python -m py_compile src/main.py

# Run tests
pytest tests/test_single_action_security_requirements.py -v
```

## Summary of Changes

| File | Lines Changed | Description |
|------|---------------|-------------|
| src/main.py | +105, -3 | Added security checks to apply_single_action() |
| tests/test_single_action_apply_security.py | +410 | Integration tests |
| tests/test_single_action_security_requirements.py | +156 | Requirement verification tests |

**Total**: 668 lines added, 3 lines modified
