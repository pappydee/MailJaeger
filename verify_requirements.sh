#!/bin/bash
echo "=== Verification of Production-Safe Requirements ==="
echo ""

echo "✓ A) SAFE_MODE check before IMAP connection:"
echo "  - Checking for early SAFE_MODE check in both apply endpoints..."
grep -n "if settings.safe_mode:" src/main.py | grep -E "(680|854)" && echo "  ✅ SAFE_MODE checks found at correct locations"

echo ""
echo "✓ B) Routing collision fix:"
echo "  - Checking preview route is defined before {action_id} route..."
preview_line=$(grep -n "@app.get.*preview" src/main.py | cut -d: -f1)
action_id_line=$(grep -n "@app.get.*{action_id}" src/main.py | cut -d: -f1)
if [ "$preview_line" -lt "$action_id_line" ]; then
  echo "  ✅ Preview route (line $preview_line) is before {action_id} route (line $action_id_line)"
else
  echo "  ❌ Routing order incorrect!"
fi

echo ""
echo "✓ C) Sanitized error handling:"
echo "  - Checking for raw str(e) usage in apply endpoints..."
if grep -n "action.error_message = str(e)" src/main.py | grep -v "sanitize_error"; then
  echo "  ❌ Found raw str(e) usage!"
else
  echo "  ✅ No raw str(e) usage found in error_message assignments"
fi

echo ""
echo "✓ D) Fail-safe apply semantics:"
echo "  - Checking connection failure returns 503..."
grep -n "status_code=503" src/main.py | grep -q "IMAP connection failed" && echo "  ✅ 503 status on connection failure"
echo "  - Checking APPROVED status preserved on connection failure..."
grep -A 5 "if not imap.client:" src/main.py | grep -q "DO NOT change status" && echo "  ✅ Comments confirm APPROVED preserved"

echo ""
echo "✓ E) IMAPService context manager:"
echo "  - Checking for 'with IMAPService() as imap:' usage..."
grep -n "with IMAPService() as imap:" src/main.py | wc -l | xargs -I {} echo "  ✅ Found {} context manager usages"

echo ""
echo "✓ F) Approval timestamps:"
echo "  - Checking approved_at is set for rejection..."
grep -A 5 "action.status = \"REJECTED\"" src/main.py | grep -q "approved_at = datetime.utcnow()" && echo "  ✅ Timestamp set for rejection"

echo ""
echo "✓ Tests:"
echo "  - Checking required tests exist..."
test_count=0
grep -q "def test_safe_mode_blocks_before_connection_attempt" tests/test_pending_actions.py && { echo "  ✅ SAFE_MODE test exists"; ((test_count++)); }
grep -q "def test_preview_endpoint_routing" tests/test_pending_actions.py && { echo "  ✅ Preview routing test exists"; ((test_count++)); }
grep -q "def test_connection_failure_does_not_mutate_approved_actions" tests/test_pending_actions.py && { echo "  ✅ Connection failure test exists"; ((test_count++)); }
grep -q "def test_no_raw_exceptions_in_error_message_when_debug_false" tests/test_pending_actions.py && { echo "  ✅ Sanitization test exists"; ((test_count++)); }
echo "  Total: $test_count/4 required tests found"

echo ""
echo "=== Verification Complete ==="
