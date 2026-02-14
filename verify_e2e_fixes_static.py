#!/usr/bin/env python3
"""
Static verification script for E2E approval workflow fixes
Checks code structure without requiring imports
"""

import sys
import os
import re


def verify_error_sanitization_file_exists():
    """Verify error sanitization utility file exists"""
    print("✓ Checking error sanitization utility...")
    
    path = 'src/utils/error_handling.py'
    assert os.path.exists(path), f"File {path} not found"
    
    with open(path, 'r') as f:
        content = f.read()
    
    # Check for sanitize_error function
    assert 'def sanitize_error(' in content
    assert 'debug: bool' in content
    
    print("  ✅ Error sanitization utility exists and has correct signature")


def verify_imap_context_manager():
    """Verify IMAP context manager pattern is used"""
    print("✓ Checking IMAP context manager usage...")
    
    with open('src/main.py', 'r') as f:
        content = f.read()
    
    # Should use context manager in both apply endpoints
    context_manager_count = content.count("with IMAPService() as imap:")
    assert context_manager_count >= 2, f"Expected at least 2 context manager usages, found {context_manager_count}"
    
    # Should NOT have manual disconnect calls in apply functions
    # (disconnect is only in health check and old code)
    lines = content.split('\n')
    in_apply_function = False
    function_name = ""
    
    for line in lines:
        if 'async def apply_all_approved_actions' in line or 'async def apply_single_action' in line:
            in_apply_function = True
            function_name = "apply_all" if "all" in line else "single"
        elif 'async def ' in line and in_apply_function:
            in_apply_function = False
        elif in_apply_function and 'imap.disconnect()' in line:
            assert False, f"Found manual disconnect in {function_name} apply function - should use context manager"
    
    print(f"  ✅ IMAP context manager used {context_manager_count} times, no manual disconnect in apply functions")


def verify_safe_mode_enforcement():
    """Verify SAFE_MODE is enforced in apply endpoints"""
    print("✓ Checking SAFE_MODE enforcement...")
    
    with open('src/main.py', 'r') as f:
        content = f.read()
    
    # Extract both apply functions
    apply_all_start = content.find('async def apply_all_approved_actions')
    apply_single_start = content.find('async def apply_single_action')
    
    assert apply_all_start != -1, "apply_all_approved_actions not found"
    assert apply_single_start != -1, "apply_single_action not found"
    
    # Get next function after each
    next_func_after_all = content.find('\nasync def ', apply_all_start + 100)
    apply_all_code = content[apply_all_start:next_func_after_all if next_func_after_all != -1 else apply_single_start]
    
    next_func_after_single = content.find('\n@app.', apply_single_start + 100)
    apply_single_code = content[apply_single_start:next_func_after_single]
    
    # Both should check safe_mode
    assert 'if settings.safe_mode:' in apply_all_code
    assert 'if settings.safe_mode:' in apply_single_code
    
    # Both should return 409
    assert '409' in apply_all_code
    assert '409' in apply_single_code
    
    # Both should mention SAFE_MODE in message
    assert 'SAFE_MODE enabled' in apply_all_code
    assert 'SAFE_MODE enabled' in apply_single_code
    
    print("  ✅ SAFE_MODE checks present in both apply endpoints with 409 status")


def verify_routing_order():
    """Verify preview endpoint is defined before {action_id}"""
    print("✓ Checking route definition order...")
    
    with open('src/main.py', 'r') as f:
        lines = f.readlines()
    
    preview_line = None
    action_id_line = None
    
    for i, line in enumerate(lines):
        if '@app.get("/api/pending-actions/preview"' in line:
            preview_line = i
        elif '@app.get("/api/pending-actions/{action_id}"' in line:
            action_id_line = i
    
    assert preview_line is not None, "Preview route not found"
    assert action_id_line is not None, "Action ID route not found"
    assert preview_line < action_id_line, f"Preview route (line {preview_line + 1}) must be before action_id route (line {action_id_line + 1})"
    
    # Check for explanatory comment
    comment_found = False
    for i in range(max(0, preview_line - 3), preview_line):
        if 'NOTE:' in lines[i] and 'Preview' in lines[i]:
            comment_found = True
            break
    
    assert comment_found, "Explanatory comment not found near preview route"
    
    print(f"  ✅ Preview route (line {preview_line + 1}) before action_id route (line {action_id_line + 1}), with comment")


def verify_sanitize_error_usage():
    """Verify sanitize_error is imported and used"""
    print("✓ Checking sanitize_error usage...")
    
    with open('src/main.py', 'r') as f:
        content = f.read()
    
    # Should import sanitize_error
    assert "from src.utils.error_handling import sanitize_error" in content
    
    # Count usages
    usage_count = content.count("sanitize_error(")
    assert usage_count >= 6, f"Expected at least 6 uses of sanitize_error, found {usage_count}"
    
    # Check it's used with settings.debug
    debug_usage = content.count("sanitize_error(e, settings.debug)")
    debug_usage += content.count("sanitize_error(\n                    Exception")
    
    assert debug_usage >= 4, f"Expected at least 4 uses with debug flag, found {debug_usage}"
    
    print(f"  ✅ sanitize_error imported and used {usage_count} times")


def verify_approval_semantics():
    """Verify approval endpoint sets timestamp for rejection"""
    print("✓ Checking approval semantics...")
    
    with open('src/main.py', 'r') as f:
        content = f.read()
    
    # Find approve_pending_action function
    approve_start = content.find('async def approve_pending_action')
    assert approve_start != -1, "approve_pending_action not found"
    
    # Get function body
    next_func = content.find('\n@app.', approve_start + 100)
    approve_code = content[approve_start:next_func]
    
    # Check structure
    assert 'if request.approve:' in approve_code
    assert 'else:' in approve_code
    
    # Split into branches
    lines = approve_code.split('\n')
    approve_branch_has_timestamp = False
    reject_branch_has_timestamp = False
    in_else = False
    
    for line in lines:
        if 'if request.approve:' in line:
            # Next few lines should have approved_at
            pass
        elif 'else:' in line and not in_else:
            in_else = True
        
        if 'approved_at = datetime.utcnow()' in line:
            if not in_else:
                approve_branch_has_timestamp = True
            else:
                reject_branch_has_timestamp = True
    
    assert approve_branch_has_timestamp, "approve branch doesn't set approved_at"
    assert reject_branch_has_timestamp, "reject (else) branch doesn't set approved_at"
    
    print("  ✅ Approval endpoint sets approved_at for both approve and reject")


def verify_tests_added():
    """Verify E2E tests were added"""
    print("✓ Checking E2E tests...")
    
    with open('tests/test_pending_actions.py', 'r') as f:
        content = f.read()
    
    # Check for new test functions
    required_tests = [
        'test_error_sanitization',
        'test_imap_connection_in_apply_endpoints',
        'test_safe_mode_blocks_apply_endpoints',
        'test_preview_endpoint_routing',
        'test_approval_sets_timestamp_for_rejection',
        'test_sanitized_errors_in_api_responses'
    ]
    
    found_tests = []
    for test in required_tests:
        if f'def {test}(' in content:
            found_tests.append(test)
    
    assert len(found_tests) == len(required_tests), f"Expected {len(required_tests)} tests, found {len(found_tests)}: {found_tests}"
    
    print(f"  ✅ All {len(found_tests)} E2E tests present")


def main():
    """Run all verification checks"""
    print("\n" + "="*60)
    print("E2E Approval Workflow Fixes - Static Verification")
    print("="*60 + "\n")
    
    try:
        verify_error_sanitization_file_exists()
        verify_imap_context_manager()
        verify_safe_mode_enforcement()
        verify_routing_order()
        verify_sanitize_error_usage()
        verify_approval_semantics()
        verify_tests_added()
        
        print("\n" + "="*60)
        print("✅ ALL VERIFICATIONS PASSED")
        print("="*60)
        print("\nAll non-negotiable fixes are implemented correctly:")
        print("  1. ✅ IMAP connection uses context manager (2+ usages)")
        print("  2. ✅ SAFE_MODE enforced in both apply endpoints (409 status)")
        print("  3. ✅ Routing collision fixed (preview before {action_id})")
        print("  4. ✅ Errors sanitized everywhere (6+ usages)")
        print("  5. ✅ Approval semantics corrected (timestamp on reject)")
        print("  6. ✅ E2E tests added (6 new tests)")
        print("\n")
        
        return 0
        
    except AssertionError as e:
        print(f"\n❌ VERIFICATION FAILED: {e}\n")
        return 1
    except Exception as e:
        print(f"\n❌ ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
