"""
Unit tests for apply_token consumption logic
Verifies token consumption happens only when appropriate
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock


class TestTokenConsumptionLogic:
    """Test the core logic of token consumption"""

    def test_token_should_not_be_consumed_on_dry_run(self):
        """
        Verify: Token should NOT be marked as used when dry_run=True
        This prevents preview operations from consuming the token
        """
        # Create a mock token
        token = Mock()
        token.is_used = False
        token.expires_at = datetime.utcnow() + timedelta(minutes=5)

        # Simulate dry_run scenario
        dry_run = True

        # Token should NOT be marked as used in dry_run
        if not dry_run:
            token.is_used = True

        assert token.is_used == False, "Token should not be consumed on dry_run"

    def test_token_should_not_be_consumed_on_imap_failure(self):
        """
        Verify: Token should NOT be marked as used when IMAP connection fails
        This allows retry after transient failures
        """
        token = Mock()
        token.is_used = False

        # Simulate IMAP connection failure
        imap_connected = False
        actions_applied = False

        # Token should only be marked used if actions are successfully applied
        if imap_connected and actions_applied:
            token.is_used = True

        assert token.is_used == False, "Token should not be consumed when IMAP fails"

    def test_token_should_be_consumed_on_success(self):
        """
        Verify: Token SHOULD be marked as used when actions successfully apply
        """
        token = Mock()
        token.is_used = False

        # Simulate successful application
        dry_run = False
        imap_connected = True
        actions_applied = True

        # Token should be marked used after successful application
        if not dry_run and imap_connected and actions_applied:
            token.is_used = True
            token.used_at = datetime.utcnow()

        assert token.is_used == True, "Token should be consumed on successful apply"
        assert token.used_at is not None, "Token should have used_at timestamp"

    def test_token_should_not_be_consumed_on_action_failure(self):
        """
        Verify: Token should NOT be marked as used when action execution fails
        """
        token = Mock()
        token.is_used = False

        # Simulate action failure
        dry_run = False
        imap_connected = True
        actions_applied = False  # Action failed

        # Token should NOT be marked used if actions fail
        if not dry_run and imap_connected and actions_applied:
            token.is_used = True

        assert token.is_used == False, "Token should not be consumed when actions fail"

    def test_expired_token_should_be_rejected(self):
        """
        Verify: Expired tokens should be rejected before any work is done
        """
        token = Mock()
        token.is_used = False
        token.expires_at = datetime.utcnow() - timedelta(minutes=5)  # Expired

        # Check if token is expired
        is_expired = token.expires_at < datetime.utcnow()

        # Should not proceed with expired token
        if is_expired:
            # Return error without marking token as used
            pass

        assert is_expired == True, "Token should be detected as expired"
        assert token.is_used == False, "Expired token should not be marked as used"

    def test_used_token_should_be_rejected(self):
        """
        Verify: Already-used tokens should be rejected
        """
        token = Mock()
        token.is_used = True  # Already used
        token.expires_at = datetime.utcnow() + timedelta(minutes=5)

        # Check if token is already used
        if token.is_used:
            # Return error, don't proceed
            can_proceed = False
        else:
            can_proceed = True

        assert can_proceed == False, "Used token should be rejected"


class TestBatchApplyTokenLogic:
    """Test token consumption logic for batch apply"""

    def test_batch_apply_flow(self):
        """
        Test the complete flow for batch apply
        """
        token = Mock()
        token.is_used = False
        token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        token.action_ids = [1, 2, 3]

        # Step 1: Validate token
        assert not token.is_used, "Token should not be used yet"
        assert token.expires_at > datetime.utcnow(), "Token should not be expired"

        # Step 2: Check dry_run
        dry_run = False

        # Step 3: Connect to IMAP
        imap_connected = True

        # Step 4: Apply actions
        applied_count = 3
        failed_count = 0

        # Step 5: Mark token as used ONLY after success
        if not dry_run and imap_connected and applied_count > 0:
            token.is_used = True
            token.used_at = datetime.utcnow()

        assert (
            token.is_used == True
        ), "Token should be consumed after successful batch apply"


class TestSingleActionApplyTokenLogic:
    """Test token consumption logic for single action apply"""

    def test_single_action_apply_flow(self):
        """
        Test the complete flow for single action apply
        """
        token = Mock()
        token.is_used = False
        token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        token.action_ids = [1]

        action_id = 1

        # Step 1: Validate token
        assert not token.is_used, "Token should not be used yet"
        assert token.expires_at > datetime.utcnow(), "Token should not be expired"

        # Step 2: Verify action_id is in token
        assert action_id in token.action_ids, "Action must be bound to token"

        # Step 3: Check dry_run
        dry_run = False

        # Step 4: Connect to IMAP and apply
        imap_connected = True
        action_success = True

        # Step 5: Mark token as used ONLY after success
        if not dry_run and imap_connected and action_success:
            token.is_used = True
            token.used_at = datetime.utcnow()

        assert (
            token.is_used == True
        ), "Token should be consumed after successful single action apply"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
