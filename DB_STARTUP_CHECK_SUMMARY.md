# DB Startup Check Implementation Summary

## Overview

Implemented a minimal, production-safe startup check that ensures the `pending_actions` table exists before the application serves requests. This prevents silent failures where REQUIRE_APPROVAL queues actions but the DB schema is incomplete.

## Branch

**Name**: `copilot/db-startup-check-pending-actions`
**Created from**: `copilot/finish-security-implementation`
**Commit**: `f068b60` - "Startup: fail-closed if pending_actions table missing"

## Implementation Details

### 1. Startup Check Module

**File**: `src/database/startup_checks.py` (NEW - 54 lines)

**Function**: `verify_pending_actions_table(engine, debug=False)`

**Features**:
- Uses SQLAlchemy inspection (DB-agnostic, no raw SQL)
- Checks if `pending_actions` table exists in database
- Returns True if table found
- Raises RuntimeError with clear message if table missing
- Uses `sanitize_error()` to prevent credential leakage
- Respects DEBUG setting for error verbosity

**Implementation**:
```python
inspector = inspect(engine)
table_names = inspector.get_table_names()

if "pending_actions" not in table_names:
    error_msg = (
        "Database schema incomplete: 'pending_actions' table not found. "
        "This table is required when REQUIRE_APPROVAL=true. "
        "Please run database initialization to create all tables."
    )
    raise RuntimeError(error_msg)
```

### 2. Integration into Application Startup

**File**: `src/main.py` (14 lines added)

**Location**: In `startup_event()` function, after `init_db()` call

**Behavior**:
1. Calls `init_db()` to initialize database
2. Gets engine via `get_engine()`
3. Calls `verify_pending_actions_table(engine, debug=settings.debug)`
4. If RuntimeError raised:
   - Sanitizes error message
   - Logs error
   - Prints to stderr
   - **Exits with code 1** (fail-closed)

**Code**:
```python
try:
    engine = get_engine()
    verify_pending_actions_table(engine, debug=settings.debug)
except RuntimeError as e:
    sanitized = sanitize_error(e, debug=False)
    logger.error("Startup check failed: %s", sanitized)
    print(f"\n❌ Startup Error: {sanitized}\n", file=sys.stderr)
    sys.exit(1)
```

### 3. Test Suite

**File**: `tests/test_db_startup_checks.py` (NEW - 180 lines, 7 tests)

**Test Coverage**:
1. `test_table_exists_check_passes` - Verify check passes when table exists
2. `test_table_missing_raises_error` - Verify RuntimeError when table missing
3. `test_debug_false_sanitizes_errors` - Verify no credential leakage in production
4. `test_debug_true_includes_more_details` - Verify verbose errors in debug mode
5. `test_runtime_error_preserved` - Verify our error messages are not double-wrapped
6. `test_inspector_exception_wrapped` - Verify DB errors are caught and wrapped
7. `test_startup_check_in_main_file` - Verify integration in main.py

**Test Approach**:
- Mock SQLAlchemy inspector
- No real database required
- Verify error messages and sanitization
- Test both success and failure cases

**Results**:
```bash
pytest tests/test_db_startup_checks.py -q
7 passed, 1 warning in 0.26s
```

### 4. Documentation

**File**: `PRODUCTION_NOTES.md` (NEW - 130 lines)

**Sections**:
- **Critical Tables**: Explains pending_actions requirement
- **Startup Check**: Documents fail-closed behavior
- **Database Initialization**: How tables are created automatically
- **Manual Initialization**: Commands for manual setup
- **Database Migrations**: Current approach (create_all, no Alembic yet)
- **Troubleshooting**: Common errors and solutions
- **Security Considerations**: Credential sanitization
- **Related Settings**: DATABASE_URL, REQUIRE_APPROVAL, DEBUG

## Behavior Examples

### Success Case
```
[INFO] Database initialized
[INFO] Startup check passed: pending_actions table exists
[INFO] Scheduler started
[INFO] MailJaeger startup complete
```

### Failure Case (Missing Table)
```
[ERROR] Startup check failed: Database schema incomplete: 'pending_actions' table not found...
❌ Startup Error: Database schema incomplete: 'pending_actions' table not found. 
This table is required when REQUIRE_APPROVAL=true. 
Please run database initialization to create all tables.

Process exits with code 1
```

### Failure Case (Connection Error)
```
[ERROR] Startup check failed: Failed to verify database schema: Connection failed
❌ Startup Error: Failed to verify database schema: Connection failed

Process exits with code 1
```

## Verification

### Compilation
```bash
python -m py_compile src/main.py src/database/startup_checks.py
✅ Success
```

### Tests
```bash
pytest tests/test_db_startup_checks.py -q
✅ 7 passed
```

### Changes Summary
```
4 files changed, 371 insertions(+), 1 deletion(-)

src/main.py                      | 14 +++
src/database/startup_checks.py   | 54 +++++++
tests/test_db_startup_checks.py  | 180 ++++++++++++++
PRODUCTION_NOTES.md              | 130 ++++++++
```

## Requirements Met

✅ **Startup DB schema check (minimal)**
- Uses SQLAlchemy inspection (DB-agnostic)
- Verifies pending_actions table exists
- Fails closed with clear error on missing table
- Logs sanitized errors only

✅ **Tests (pytest)**
- 7 comprehensive tests
- Mock inspector (no real DB)
- Verify error sanitization when DEBUG=false
- All tests passing

✅ **Documentation (minimal)**
- PRODUCTION_NOTES.md created
- Explains when pending_actions is required
- Documents how to create DB schema
- Includes troubleshooting guide

✅ **Constraints**
- No refactors
- No new features beyond check
- No changes to approval workflow, tokens, IMAP, or auth
- Small, reviewable diffs
- Check only (no auto-create/auto-migrate)

## Security Features

1. **Fail-Closed**: Application exits if check fails (doesn't serve requests)
2. **Error Sanitization**: Uses `sanitize_error()` to prevent credential leakage
3. **Production-Safe**: In production mode (DEBUG=false), only sanitized errors
4. **Clear Messages**: User-friendly error messages without internal details
5. **DB-Agnostic**: Works with any SQLAlchemy-supported database

## Usage

The check runs automatically on application startup. No configuration needed.

If the check fails, follow the error message instructions:
1. Verify database file exists and is writable
2. Check that init_db() successfully created tables
3. If tables are missing, delete DB file and restart (tables will be recreated)
4. For Docker deployments, ensure data volume is properly mounted

## Related Settings

```bash
# Database location
DATABASE_URL=sqlite:///./data/mailjaeger.db

# Approval workflow (requires pending_actions table)
REQUIRE_APPROVAL=false  # Set to true to enable

# Debug mode (affects error verbosity)
DEBUG=false  # Keep false in production
```

## See Also

- [PRODUCTION_NOTES.md](PRODUCTION_NOTES.md) - Full production deployment guide
- [README.md](README.md) - General setup instructions
- [SECURITY.md](SECURITY.md) - Security best practices
- [src/database/connection.py](src/database/connection.py) - Database initialization
- [src/database/startup_checks.py](src/database/startup_checks.py) - Startup check implementation
