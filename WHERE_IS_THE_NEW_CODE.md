# 📍 Where is the New Code?

## Quick Answer
The new security hardening code is in **3 files** on branch `copilot/finish-security-implementation`:

1. **`src/main.py`** - Main implementation (lines 1026-1248)
2. **`tests/test_single_action_apply_security.py`** - Integration tests (410 lines)
3. **`tests/test_single_action_security_requirements.py`** - Requirement tests (156 lines)

## 📂 File Locations

### 1. Main Implementation: `src/main.py`

**Function**: `apply_single_action()` starting at **line 1026**

#### What Changed:
```
Line 1029: Added Body import for request handling
Line 1032-1040: Added comprehensive docstring explaining safety requirements
Line 1042-1049: ✅ SAFE_MODE check (first, before everything)
Line 1051-1093: ✅ apply_token validation (required, valid, not expired, bound to action)
Line 1114-1130: ✅ DELETE blocking (if allow_destructive_imap=false)
Line 1132-1151: ✅ Folder allowlist validation (for MOVE_FOLDER)
Line 1153-1156: Token marked as used (only after all validation)
Line 1172: IMAP connection (happens LAST, after all checks)
```

### 2. Test File 1: `tests/test_single_action_apply_security.py`

**Purpose**: Integration-style tests using FastAPI TestClient
**Lines**: 410
**Test Cases**: 8

Tests:
- Missing apply_token returns 409
- Invalid apply_token returns 409  
- Expired apply_token returns 409
- Token not bound to action returns 409
- SAFE_MODE blocks before IMAP
- MOVE_FOLDER to non-allowlisted folder fails
- DELETE blocked when not allowed
- DELETE allowed when enabled

### 3. Test File 2: `tests/test_single_action_security_requirements.py`

**Purpose**: Requirement verification through code inspection
**Lines**: 156
**Test Cases**: 7

Tests verify all 5 requirements:
1. SAFE_MODE always wins
2. apply_token required and enforced
3. Folder allowlist enforced
4. DELETE blocked when not allowed
5. Error sanitization everywhere

## 🔍 How to View the Code

### Option 1: View in your editor
```bash
# Open the main file at the right line
code /home/runner/work/MailJaeger/MailJaeger/src/main.py:1026
```

### Option 2: View in terminal
```bash
cd /home/runner/work/MailJaeger/MailJaeger

# View the function (lines 1026-1248)
sed -n '1026,1248p' src/main.py | less

# Or just the security checks (lines 1042-1156)
sed -n '1042,1156p' src/main.py
```

### Option 3: View the diff
```bash
# Show what changed
git show af755ff -- src/main.py

# Or show changes from base
git diff e242d55..HEAD -- src/main.py
```

## 📊 Code Statistics

```
File                                         Lines Added  Lines Modified
---------------------------------------------------------------------------
src/main.py                                  +105         -3
tests/test_single_action_apply_security.py   +410         (new file)
tests/test_single_action_security_requirements.py +156   (new file)
---------------------------------------------------------------------------
Total                                        +671         -3
```

## ✅ Verification

To verify the code is there:

```bash
cd /home/runner/work/MailJaeger/MailJaeger

# Check compilation
python -m py_compile src/main.py
# Should exit with 0

# Run tests  
pytest tests/test_single_action_security_requirements.py -v
# Should show 7 passed

# View the function signature
grep -A 10 "def apply_single_action" src/main.py
```

## 🌳 Branch Status

- **Current branch**: `copilot/finish-security-implementation`
- **Pushed to origin**: ✅ Yes
- **Commits**: 
  - `af755ff` - "Add apply_token validation and safety checks..."
  - `0949d62` - "Hardening: enforce token + allowlist..."

## 🎯 Key Features Implemented

1. **Token Validation** (lines 1051-1093)
   - Requires apply_token
   - Validates not used, not expired
   - Binds to specific action_id

2. **DELETE Blocking** (lines 1114-1130)
   - Blocks before IMAP if allow_destructive_imap=false
   - Sets status=REJECTED

3. **Folder Allowlist** (lines 1132-1151)
   - Validates MOVE_FOLDER targets
   - Blocks before IMAP if not in allowlist
   - Sets status=FAILED with sanitized error

4. **SAFE_MODE** (lines 1042-1049)
   - Checked FIRST
   - Returns 409 immediately
   - Never touches IMAP or database

All security checks happen **BEFORE** the IMAP connection at line 1172! 🔒
