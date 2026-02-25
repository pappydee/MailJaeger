"""
Unit tests for apply_token consumption logic
Tests verify that tokens are only consumed when actions succeed and dry_run is false.

Uses app.dependency_overrides instead of patch("src.main.get_db") because FastAPI
captures the get_db function object at route-definition time; patching the module
attribute has no effect on already-registered routes.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import os

# Set test environment variables
os.environ["API_KEY"] = "test_api_key_for_testing_123456"
os.environ["IMAP_HOST"] = "imap.test.com"
os.environ["IMAP_USERNAME"] = "test@test.com"
os.environ["IMAP_PASSWORD"] = "test_password"
os.environ["AI_ENDPOINT"] = "http://localhost:11434"
os.environ["SAFE_MODE"] = "false"
os.environ["ALLOW_DESTRUCTIVE_IMAP"] = "false"

from src.main import app
from src.config import get_settings, reload_settings
from src.database.connection import get_db as _get_db  # key for dependency_overrides
from src.models.database import Base, ProcessedEmail, PendingAction, ApplyToken


@pytest.fixture
def client():
    """Create test client with fresh settings."""
    reload_settings()
    import src.main
    src.main.settings = get_settings()
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Valid authentication headers"""
    return {"Authorization": "Bearer test_api_key_for_testing_123456"}


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_mock_db(
    token=None, actions=None, action=None, email=None
):
    """Build a MagicMock DB that routes queries to the right mock objects."""
    mock_db = MagicMock()

    def query_side_effect(*args, **kw):
        mock_query = MagicMock()
        model = args[0] if args else None
        if model == ApplyToken:
            mock_query.filter.return_value.first.return_value = token
        elif model == PendingAction:
            mock_query.filter.return_value.first.return_value = action
            mock_query.filter.return_value.all.return_value = actions or (
                [action] if action else []
            )
        elif model == ProcessedEmail:
            mock_query.filter.return_value.first.return_value = email
        return mock_query

    mock_db.query.side_effect = query_side_effect
    return mock_db


class TestTokenConsumptionBatchApply:
    """Test token consumption for batch apply endpoint"""

    def test_token_not_consumed_on_dry_run(self, client, auth_headers):
        """Test that token is NOT consumed when dry_run=true"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1, 2]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1
        mock_action.target_folder = None

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"
        mock_email.subject = "Test"

        mock_db = _make_mock_db(
            token=mock_token, actions=[mock_action], action=mock_action, email=mock_email
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        response = client.post(
            "/api/pending-actions/apply",
            json={"apply_token": "test_token", "dry_run": True},
            headers=auth_headers,
        )

        assert response.status_code == 200
        # Token should NOT be marked as used for dry run
        assert mock_token.is_used == False

    def test_token_not_consumed_on_imap_failure(self, client, auth_headers):
        """Test that token is NOT consumed when IMAP connection fails"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1

        mock_db = _make_mock_db(
            token=mock_token, actions=[mock_action], action=mock_action
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap:
            mock_imap.return_value.__enter__.side_effect = RuntimeError("Connection failed")

            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

        assert response.status_code == 503
        # Token should NOT be marked as used on IMAP failure
        assert mock_token.is_used == False

    def test_token_consumed_on_successful_apply(self, client, auth_headers):
        """Test that token IS consumed when actions successfully apply"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1
        mock_action.target_folder = None

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"

        mock_db = _make_mock_db(
            token=mock_token, actions=[mock_action], action=mock_action, email=mock_email
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap:
            mock_imap_instance = MagicMock()
            mock_imap_instance.client = True
            mock_imap_instance.mark_as_read = Mock(return_value=True)
            mock_imap.return_value.__enter__.return_value = mock_imap_instance

            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

        assert response.status_code == 200
        # Token SHOULD be marked as used after successful apply
        assert mock_token.is_used == True


class TestTokenConsumptionSingleApply:
    """Test token consumption for single-action apply endpoint"""

    def test_token_not_consumed_on_dry_run_single(self, client, auth_headers):
        """Test that token is NOT consumed when dry_run=true for single action"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1
        mock_action.target_folder = None

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"
        mock_email.subject = "Test"

        mock_db = _make_mock_db(
            token=mock_token, action=mock_action, email=mock_email
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        response = client.post(
            "/api/pending-actions/1/apply",
            json={"apply_token": "test_token", "dry_run": True},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["dry_run"] == True
        # Token should NOT be marked as used
        assert mock_token.is_used == False

    def test_token_not_consumed_on_imap_failure_single(self, client, auth_headers):
        """Test that token is NOT consumed when IMAP fails for single action"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"

        mock_db = _make_mock_db(
            token=mock_token, action=mock_action, email=mock_email
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap:
            mock_imap.return_value.__enter__.side_effect = RuntimeError("Connection failed")

            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

        assert response.status_code == 503
        # Token should NOT be marked as used
        assert mock_token.is_used == False

    def test_token_consumed_on_successful_single_apply(self, client, auth_headers):
        """Test that token IS consumed when single action successfully applies"""
        mock_token = Mock()
        mock_token.expires_at = datetime.utcnow() + timedelta(minutes=5)
        mock_token.action_ids = [1]
        mock_token.is_used = False

        mock_action = Mock()
        mock_action.id = 1
        mock_action.status = "APPROVED"
        mock_action.action_type = "MARK_READ"
        mock_action.email_id = 1

        mock_email = Mock()
        mock_email.id = 1
        mock_email.uid = "12345"
        mock_email.message_id = "test@example.com"

        mock_db = _make_mock_db(
            token=mock_token, action=mock_action, email=mock_email
        )
        app.dependency_overrides[_get_db] = lambda: mock_db

        with patch("src.main.IMAPService") as mock_imap:
            mock_imap_instance = MagicMock()
            mock_imap_instance.client = True
            mock_imap_instance.mark_as_read = Mock(return_value=True)
            mock_imap.return_value.__enter__.return_value = mock_imap_instance

            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

        assert response.status_code == 200
        assert response.json()["success"] == True
        # Token SHOULD be marked as used
        assert mock_token.is_used == True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
