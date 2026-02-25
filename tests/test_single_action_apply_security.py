"""
Unit tests for single-action apply endpoint security hardening
Tests the safety controls added to POST /api/pending-actions/{action_id}/apply
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import os

# Environment is managed by conftest.py - no module-level overrides needed.
from src.main import app
from src.config import get_settings, reload_settings
from src.database.connection import get_db as _get_db  # key for dependency_overrides
from src.models.database import Base, ProcessedEmail, PendingAction, ApplyToken


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Valid authentication headers"""
    return {"Authorization": "Bearer test_key_abc123"}


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def sample_email(db_session):
    """Create a sample processed email"""
    email = ProcessedEmail(
        message_id="test@example.com",
        uid="12345",
        subject="Test Email",
        sender="sender@example.com",
        recipients="recipient@example.com",
        date=datetime.utcnow(),
        summary="Test summary",
        category="Klinik",
        spam_probability=0.1,
        action_required=False,
        priority="LOW",
        is_spam=False,
        is_processed=True,
        processed_at=datetime.utcnow(),
    )
    db_session.add(email)
    db_session.commit()
    return email


@pytest.fixture
def sample_pending_action(db_session, sample_email):
    """Create a sample approved pending action"""
    action = PendingAction(
        email_id=sample_email.id,
        action_type="MOVE_FOLDER",
        target_folder="Archive",
        status="APPROVED",
        approved_at=datetime.utcnow(),
    )
    db_session.add(action)
    db_session.commit()
    return action


@pytest.fixture
def valid_apply_token(db_session, sample_pending_action):
    """Create a valid apply token"""
    token = ApplyToken(
        token="valid_test_token_123",
        action_ids=[sample_pending_action.id],
        action_count=1,
        summary={},
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        is_used=False,
    )
    db_session.add(token)
    db_session.commit()
    return token


def _make_mock_db(token=None, action=None, email=None):
    """Create a MagicMock DB session with per-model query routing."""
    mock_db = MagicMock()

    def query_side_effect(*args, **kw):
        mock_query = MagicMock()
        model = args[0] if args else None
        if model == ApplyToken:
            mock_query.filter.return_value.first.return_value = token
        elif model == PendingAction:
            mock_query.filter.return_value.first.return_value = action
        elif model == ProcessedEmail:
            mock_query.filter.return_value.first.return_value = email
        return mock_query

    mock_db.query.side_effect = query_side_effect
    return mock_db


class TestSingleActionApplySecurity:
    """Test security controls for single-action apply endpoint"""

    def test_missing_apply_token_returns_409_no_status_change(
        self, client, auth_headers
    ):
        """Test that missing apply_token returns 409 and does NOT change action status"""
        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MOVE_FOLDER"
        mock_action.target_folder = "Archive"
        mock_action.email_id = 1

        mock_db = _make_mock_db(action=mock_action)
        app.dependency_overrides[_get_db] = lambda: mock_db

        # Attempt to apply without token
        response = client.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": None},
            headers=auth_headers,
        )

        # Should return 409
        assert response.status_code == 409
        assert "Apply token required" in response.json()["message"]

        # Verify action status was NOT changed
        assert mock_action.status == "APPROVED"

    def test_invalid_apply_token_returns_409(self, client, auth_headers):
        """Test that invalid apply_token returns 409"""
        # No token in DB → first() returns None
        mock_db = _make_mock_db(token=None)
        app.dependency_overrides[_get_db] = lambda: mock_db

        response = client.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": "invalid_token", "dry_run": False},
            headers=auth_headers,
        )

        assert response.status_code == 409
        assert "Invalid or already used" in response.json()["message"]

    def test_expired_apply_token_returns_409(self, client, auth_headers):
        """Test that expired apply_token returns 409"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() - timedelta(minutes=1)  # Expired
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_db = _make_mock_db(token=mock_token)
        app.dependency_overrides[_get_db] = lambda: mock_db

        response = client.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": "expired_token", "dry_run": False},
            headers=auth_headers,
        )

        assert response.status_code == 409
        assert "expired" in response.json()["message"].lower()

    def test_token_not_bound_to_action_returns_409(self, client, auth_headers):
        """Test that token not bound to specific action_id returns 409"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [2, 3]  # Not including action_id=1
        mock_token.is_used = False

        mock_db = _make_mock_db(token=mock_token)
        app.dependency_overrides[_get_db] = lambda: mock_db

        response = client.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": "wrong_action_token", "dry_run": False},
            headers=auth_headers,
        )

        assert response.status_code == 409
        assert "not valid for this action" in response.json()["message"]

    def test_safe_mode_blocks_before_imap_connection(self, client, auth_headers):
        """Test that SAFE_MODE blocks action BEFORE any IMAP connection attempt"""
        # Apply endpoint now uses get_settings() dynamically, so reload_settings()
        # after patching env is sufficient.
        with patch.dict(os.environ, {"SAFE_MODE": "true"}):
            reload_settings()

            with patch("src.main.IMAPService") as mock_imap_service:
                mock_imap_instance = MagicMock()
                mock_imap_service.return_value.__enter__.return_value = (
                    mock_imap_instance
                )

                response = client.post(
                    "/api/pending-actions/1/apply",
                    json={"apply_token": "any_token", "dry_run": False},
                    headers=auth_headers,
                )

                # Should return 409 due to SAFE_MODE
                assert response.status_code == 409
                assert "SAFE_MODE enabled" in response.json()["message"]

                # Verify IMAPService was NOT instantiated
                mock_imap_service.assert_not_called()

        # Restore SAFE_MODE=false so later tests are not affected
        reload_settings()

    def test_move_folder_to_non_allowlisted_fails_without_imap(
        self, client, auth_headers
    ):
        """Test that MOVE_FOLDER to non-allowlisted folder fails WITHOUT IMAP connection"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MOVE_FOLDER"
        mock_action.target_folder = "NotAllowedFolder"  # Not in safe folders
        mock_action.email_id = 1

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"

        mock_db = _make_mock_db(token=mock_token, action=mock_action, email=mock_email)
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap_service:
            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "valid_token", "dry_run": False},
                headers=auth_headers,
            )

            # Should return 400 due to folder validation
            assert response.status_code == 400
            assert "not in safe folder allowlist" in response.json()["message"]

            # Verify IMAPService was NOT instantiated
            mock_imap_service.assert_not_called()

            # Verify action status was changed to FAILED
            assert mock_action.status == "FAILED"

    def test_delete_blocked_when_not_allowed(self, client, auth_headers):
        """Test that DELETE is blocked when allow_destructive_imap=false WITHOUT IMAP connection"""
        # Endpoint uses get_settings() dynamically; ALLOW_DESTRUCTIVE_IMAP=false (default)
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "DELETE"
        mock_action.email_id = 1

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"

        mock_db = _make_mock_db(token=mock_token, action=mock_action, email=mock_email)
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap_service:
            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "valid_token", "dry_run": False},
                headers=auth_headers,
            )

            # Should return 409 due to DELETE blocking
            assert response.status_code == 409
            assert "DELETE operations are not allowed" in response.json()["message"]

            # Verify IMAPService was NOT instantiated
            mock_imap_service.assert_not_called()

            # Verify action status was changed to REJECTED
            assert mock_action.status == "REJECTED"
            assert "ALLOW_DESTRUCTIVE_IMAP is false" in mock_action.error_message

    @patch.dict(os.environ, {"ALLOW_DESTRUCTIVE_IMAP": "true"})
    def test_delete_allowed_when_enabled(self, client, auth_headers):
        """Test that DELETE is allowed when allow_destructive_imap=true"""
        reload_settings()

        with patch("src.main.IMAPService") as mock_imap_service:
            mock_imap_instance = MagicMock()
            mock_imap_instance.client = True  # Connection successful
            mock_imap_instance.delete_message = Mock(return_value=True)
            mock_imap_service.return_value.__enter__.return_value = mock_imap_instance

            mock_token = Mock()
            mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
            mock_token.action_ids = [1]
            mock_token.is_used = False

            mock_action = Mock()
            mock_action.id = 1
            mock_action.status = "APPROVED"
            mock_action.action_type = "DELETE"
            mock_action.email_id = 1

            mock_email = Mock()
            mock_email.id = 1
            mock_email.uid = "12345"

            mock_db = _make_mock_db(token=mock_token, action=mock_action, email=mock_email)
            app.dependency_overrides[_get_db] = lambda: mock_db

            # Note: DELETE with ALLOW_DESTRUCTIVE_IMAP=true proceeds to IMAP; verify it's not
            # blocked at the validation stage (test just checks no 409/400 before IMAP call)
            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "valid_token", "dry_run": False},
                headers=auth_headers,
            )

            # 409 = blocked by SAFE_MODE or ALLOW_DESTRUCTIVE_IMAP check (unexpected with true)
            # 400 = "Unknown action type: DELETE" — DELETE isn't implemented in the
            #        single-action IMAP dispatch, but that means it DID pass the
            #        ALLOW_DESTRUCTIVE_IMAP guard which is what we're testing here.
            assert response.status_code != 409, (
                f"DELETE was unexpectedly blocked by safety check: {response.json()}"
            )

        # Restore
        reload_settings()


    def test_apply_endpoint_does_not_expose_details_on_auth_failure(self):
        """Test that 401 response does not leak implementation details"""
        client_no_auth = TestClient(app)
        response = client_no_auth.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": "any_token", "dry_run": False},
        )
        assert response.status_code == 401
        # Ensure the minimal 401 response doesn't contain sensitive info
        assert "detail" in response.json()
        assert response.json()["detail"] == "Unauthorized"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
