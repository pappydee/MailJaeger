"""
Final security sweep tests - comprehensive safety and invariant validation.

Tests cover:
1. IMAPService fail-fast behavior
2. Apply endpoints return 503 on IMAP failure
3. SAFE_MODE blocks before IMAP connect
4. Missing/invalid apply_token blocks both endpoints
5. MOVE_FOLDER to non-allowlisted folder fails
6. DELETE blocked unless allow_destructive_imap=true
"""

import pytest
import os
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

# Environment is managed by conftest.py
from src.main import app
from src.models.database import PendingAction, ProcessedEmail, ApplyToken
from src.config import reload_settings, get_settings
from src.database.connection import get_db as _get_db


# Test client with authentication (uses canonical key from conftest)
def get_test_client():
    client = TestClient(app)
    client.headers = {"Authorization": "Bearer test_key_abc123"}
    return client


class TestIMAPServiceFailFast:
    """Test IMAPService fail-fast behavior and context manager"""

    def test_connect_failure_leaves_client_none(self):
        """When connect() fails, client must be None"""
        with patch("src.services.imap_service.IMAPClient") as mock_client_class:
            mock_client_class.side_effect = Exception("Connection failed")

            from src.services.imap_service import IMAPService

            imap = IMAPService()
            result = imap.connect()

            assert result is False
            assert imap.client is None

    def test_login_failure_leaves_client_none(self):
        """When login() fails, client must be None"""
        with patch("src.services.imap_service.IMAPClient") as mock_client_class:
            mock_instance = MagicMock()
            mock_instance.login.side_effect = Exception("Login failed")
            mock_client_class.return_value = mock_instance

            from src.services.imap_service import IMAPService

            imap = IMAPService()
            result = imap.connect()

            assert result is False
            assert imap.client is None
            # Verify best-effort cleanup was attempted
            mock_instance.logout.assert_called_once()

    def test_context_manager_raises_on_connect_failure(self):
        """__enter__ must raise RuntimeError if connect() fails"""
        with patch("src.services.imap_service.IMAPClient") as mock_client_class:
            mock_client_class.side_effect = Exception("Connection failed")

            from src.services.imap_service import IMAPService

            with pytest.raises(RuntimeError, match="IMAP connection failed"):
                with IMAPService() as imap:
                    pass  # Should not reach here

    def test_context_manager_exit_sets_client_none(self):
        """__exit__ must always set client to None"""
        with patch("src.services.imap_service.IMAPClient") as mock_client_class:
            mock_instance = MagicMock()
            mock_client_class.return_value = mock_instance

            from src.services.imap_service import IMAPService

            imap = IMAPService()
            with imap:
                assert imap.client is not None

            # After exit, client must be None
            assert imap.client is None


class TestApplyEndpointsIMAPFailure:
    """Test apply endpoints return 503 on IMAP connection failure"""

    def test_batch_apply_returns_503_on_imap_failure(self):
        """Batch apply returns 503 when IMAP connection fails"""
        # Create mock session with valid token + action
        mock_session = MagicMock()

        mock_token = ApplyToken(
            id=1,
            token="test-token",
            action_ids=[1],
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            is_used=False,
        )
        mock_action = PendingAction(
            id=1, email_id=1, action_type="MOVE_FOLDER", status="APPROVED"
        )

        def query_side_effect(model):
            mock_query = MagicMock()
            if model == ApplyToken:
                mock_query.filter.return_value.first.return_value = mock_token
            elif model == PendingAction:
                mock_query.filter.return_value.all.return_value = [mock_action]
            return mock_query

        mock_session.query.side_effect = query_side_effect

        # Use dependency_overrides (proper FastAPI injection)
        app.dependency_overrides[_get_db] = lambda: mock_session

        with patch("src.main.IMAPService") as mock_imap_class:
            mock_imap_class.return_value.__enter__.side_effect = RuntimeError(
                "IMAP connection failed"
            )

            client = get_test_client()
            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test-token", "action_ids": [1], "dry_run": False},
            )

        app.dependency_overrides.pop(_get_db, None)

        assert response.status_code == 503
        data = response.json()
        assert data["success"] is False
        assert "unavailable" in data["message"].lower() or "failed" in data["message"].lower()

    def test_single_apply_returns_503_on_imap_failure(self):
        """Single apply returns 503 when IMAP connection fails"""
        mock_session = MagicMock()

        mock_token = ApplyToken(
            id=1,
            token="test-token",
            action_ids=[1],
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            is_used=False,
        )
        mock_action = PendingAction(
            id=1,
            email_id=1,
            action_type="MOVE_FOLDER",
            target_folder="Archive",
            status="APPROVED",
        )
        mock_email = ProcessedEmail(id=1, uid="123", message_id="test@example.com")

        def query_side_effect(model):
            mock_query = MagicMock()
            if model == ApplyToken:
                mock_query.filter.return_value.first.return_value = mock_token
            elif model == PendingAction:
                mock_query.filter.return_value.first.return_value = mock_action
            elif model == ProcessedEmail:
                mock_query.filter.return_value.first.return_value = mock_email
            return mock_query

        mock_session.query.side_effect = query_side_effect
        app.dependency_overrides[_get_db] = lambda: mock_session

        with patch("src.main.IMAPService") as mock_imap_class:
            mock_imap_class.return_value.__enter__.side_effect = RuntimeError(
                "IMAP connection failed"
            )

            client = get_test_client()
            response = client.post(
                "/api/pending-actions/1/apply", json={"apply_token": "test-token"}
            )

        app.dependency_overrides.pop(_get_db, None)

        assert response.status_code == 503
        data = response.json()
        assert data["success"] is False


class TestSafeModeBlocks:
    """Test SAFE_MODE blocks apply endpoints before IMAP connect"""

    def test_safe_mode_blocks_batch_apply_before_imap(self):
        """SAFE_MODE blocks batch apply without attempting IMAP connection"""
        with patch.dict(os.environ, {"SAFE_MODE": "true"}):
            reload_settings()

            with patch("src.main.IMAPService") as mock_imap_service:
                client = get_test_client()
                response = client.post(
                    "/api/pending-actions/apply",
                    json={"apply_token": "any-token", "action_ids": [1]},
                )

                assert response.status_code == 409
                data = response.json()
                assert data["success"] is False
                assert "SAFE_MODE" in data["message"]
                mock_imap_service.assert_not_called()

        # Restore safe settings
        reload_settings()

    def test_safe_mode_blocks_single_apply_before_imap(self):
        """SAFE_MODE blocks single apply without attempting IMAP connection"""
        with patch.dict(os.environ, {"SAFE_MODE": "true"}):
            reload_settings()

            with patch("src.main.IMAPService") as mock_imap_service:
                client = get_test_client()
                response = client.post(
                    "/api/pending-actions/1/apply", json={"apply_token": "any-token"}
                )

                assert response.status_code == 409
                data = response.json()
                assert data["success"] is False
                assert "SAFE_MODE" in data["message"]
                mock_imap_service.assert_not_called()

        reload_settings()


class TestApplyTokenRequired:
    """Test missing/invalid apply_token blocks both endpoints"""

    def test_batch_apply_missing_token_returns_409(self):
        """Batch apply without token returns 409"""
        client = get_test_client()
        response = client.post(
            "/api/pending-actions/apply", json={"action_ids": [1]}  # No token
        )

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert "token" in data["message"].lower()

    def test_batch_apply_invalid_token_returns_409(self):
        """Batch apply with invalid token returns 409"""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[_get_db] = lambda: mock_session

        client = get_test_client()
        response = client.post(
            "/api/pending-actions/apply",
            json={"apply_token": "invalid-token", "action_ids": [1]},
        )
        app.dependency_overrides.pop(_get_db, None)

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False

    def test_single_apply_missing_token_returns_409(self):
        """Single apply without token returns 409"""
        client = get_test_client()
        response = client.post(
            "/api/pending-actions/1/apply", json={}  # No token
        )

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert "token" in data["message"].lower()


class TestFolderAllowlist:
    """Test MOVE_FOLDER to non-allowlisted folder fails without IMAP connect"""

    def test_move_to_non_allowlisted_folder_fails(self):
        """MOVE_FOLDER to non-allowlisted folder fails before IMAP connect"""
        mock_session = MagicMock()

        mock_token = ApplyToken(
            id=1,
            token="test-token",
            action_ids=[1],
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            is_used=False,
        )
        mock_action = PendingAction(
            id=1,
            email_id=1,
            action_type="MOVE_FOLDER",
            target_folder="NotAllowed",  # Not in allowlist
            status="APPROVED",
        )
        mock_email = ProcessedEmail(id=1, uid="123")

        def query_side_effect(model):
            mock_query = MagicMock()
            if model == ApplyToken:
                mock_query.filter.return_value.first.return_value = mock_token
            elif model == PendingAction:
                mock_query.filter.return_value.first.return_value = mock_action
                mock_query.filter.return_value.all.return_value = [mock_action]
            elif model == ProcessedEmail:
                mock_query.filter.return_value.first.return_value = mock_email
            return mock_query

        mock_session.query.side_effect = query_side_effect

        with patch.dict(os.environ, {"SAFE_FOLDERS": "Archive,Processed"}):
            reload_settings()
            app.dependency_overrides[_get_db] = lambda: mock_session

            with patch("src.main.IMAPService"):
                client = get_test_client()
                response = client.post(
                    "/api/pending-actions/apply",
                    json={"apply_token": "test-token", "action_ids": [1]},
                )

            app.dependency_overrides.pop(_get_db, None)

        reload_settings()

        # Should succeed with the action marked as FAILED (folder not allowed)
        assert response.status_code == 200
        data = response.json()
        assert data["failed"] >= 1


class TestDeleteBlocked:
    """Test DELETE blocked unless allow_destructive_imap=true"""

    def test_delete_blocked_when_destructive_false(self):
        """DELETE action blocked when allow_destructive_imap=false"""
        mock_session = MagicMock()

        mock_token = ApplyToken(
            id=1,
            token="test-token",
            action_ids=[1],
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            is_used=False,
        )
        mock_action = PendingAction(
            id=1, email_id=1, action_type="DELETE", status="APPROVED"
        )
        mock_email = ProcessedEmail(id=1, uid="123")

        def query_side_effect(model):
            mock_query = MagicMock()
            if model == ApplyToken:
                mock_query.filter.return_value.first.return_value = mock_token
            elif model == PendingAction:
                mock_query.filter.return_value.first.return_value = mock_action
                mock_query.filter.return_value.all.return_value = [mock_action]
            elif model == ProcessedEmail:
                mock_query.filter.return_value.first.return_value = mock_email
            return mock_query

        mock_session.query.side_effect = query_side_effect
        app.dependency_overrides[_get_db] = lambda: mock_session

        with patch("src.main.IMAPService"):
            client = get_test_client()
            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test-token", "action_ids": [1]},
            )

        app.dependency_overrides.pop(_get_db, None)

        # Should succeed but DELETE action should be REJECTED
        assert response.status_code == 200
        data = response.json()
        assert data["failed"] >= 1

        # Verify action was marked as REJECTED (not APPLIED)
        assert mock_action.status == "REJECTED"
        assert "DELETE blocked" in mock_action.error_message

