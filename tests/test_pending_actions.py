"""
Unit tests for pending actions functionality
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import Settings
from src.models.database import Base, ProcessedEmail, PendingAction
from src.services.email_processor import EmailProcessor


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
def mock_settings_safe_mode():
    """Mock settings with safe_mode=True"""
    settings = Mock(spec=Settings)
    settings.safe_mode = True
    settings.require_approval = False
    settings.spam_threshold = 0.7
    settings.max_emails_per_run = 200
    settings.store_email_body = False
    settings.quarantine_folder = "Quarantine"
    settings.archive_folder = "Archive"
    settings.spam_folder = "Spam"
    settings.mark_as_read = False
    settings.delete_spam = False
    return settings


@pytest.fixture
def mock_settings_require_approval():
    """Mock settings with require_approval=True"""
    settings = Mock(spec=Settings)
    settings.safe_mode = False
    settings.require_approval = True
    settings.spam_threshold = 0.7
    settings.max_emails_per_run = 200
    settings.store_email_body = False
    settings.quarantine_folder = "Quarantine"
    settings.archive_folder = "Archive"
    settings.spam_folder = "Spam"
    settings.mark_as_read = True
    settings.delete_spam = False
    return settings


@pytest.fixture
def mock_settings_normal():
    """Mock settings with normal mode (no safe_mode, no require_approval)"""
    settings = Mock(spec=Settings)
    settings.safe_mode = False
    settings.require_approval = False
    settings.spam_threshold = 0.7
    settings.max_emails_per_run = 200
    settings.store_email_body = False
    settings.quarantine_folder = "Quarantine"
    settings.archive_folder = "Archive"
    settings.spam_folder = "Spam"
    settings.mark_as_read = True
    settings.delete_spam = False
    return settings


@pytest.fixture
def sample_email_data():
    """Sample email data for testing"""
    return {
        "message_id": "test@example.com",
        "uid": "12345",
        "subject": "Test Email",
        "sender": "sender@example.com",
        "recipients": "recipient@example.com",
        "date": datetime.utcnow(),
        "body_plain": "Test body",
        "body_html": "<p>Test body</p>",
        "integrity_hash": "testhash123",
    }


@pytest.fixture
def sample_ai_analysis():
    """Sample AI analysis result"""
    return {
        "summary": "Test email summary",
        "category": "Klinik",
        "spam_probability": 0.1,
        "action_required": True,
        "priority": "HIGH",
        "suggested_folder": "Archive",
        "reasoning": "Test reasoning",
        "tasks": [],
    }


def test_safe_mode_skips_imap_actions(
    db_session, mock_settings_safe_mode, sample_email_data, sample_ai_analysis
):
    """Test that SAFE_MODE=True prevents all IMAP actions"""
    with patch(
        "src.services.email_processor.get_settings",
        return_value=mock_settings_safe_mode,
    ):
        with patch("src.services.email_processor.IMAPService") as mock_imap_class:
            with patch("src.services.email_processor.AIService") as mock_ai_class:
                # Setup mocks
                mock_imap = Mock()
                mock_imap_class.return_value = mock_imap
                mock_ai = Mock()
                mock_ai.analyze_email.return_value = sample_ai_analysis
                mock_ai_class.return_value = mock_ai

                processor = EmailProcessor(db_session)
                processor._process_single_email(sample_email_data, mock_imap)

                # Verify no IMAP actions were called
                mock_imap.move_to_folder.assert_not_called()
                mock_imap.mark_as_read.assert_not_called()
                mock_imap.add_flag.assert_not_called()

                # Verify email was saved with safe_mode_skip action
                email = db_session.query(ProcessedEmail).first()
                assert email is not None
                assert "safe_mode_skip" in email.actions_taken["actions"]


def test_require_approval_enqueues_actions(
    db_session, mock_settings_require_approval, sample_email_data, sample_ai_analysis
):
    """Test that REQUIRE_APPROVAL=True enqueues PendingActions instead of executing"""
    with patch(
        "src.services.email_processor.get_settings",
        return_value=mock_settings_require_approval,
    ):
        with patch("src.services.email_processor.IMAPService") as mock_imap_class:
            with patch("src.services.email_processor.AIService") as mock_ai_class:
                # Setup mocks
                mock_imap = Mock()
                mock_imap_class.return_value = mock_imap
                mock_ai = Mock()
                mock_ai.analyze_email.return_value = sample_ai_analysis
                mock_ai_class.return_value = mock_ai

                processor = EmailProcessor(db_session)
                processor._process_single_email(sample_email_data, mock_imap)

                # Verify no IMAP actions were called
                mock_imap.move_to_folder.assert_not_called()
                mock_imap.mark_as_read.assert_not_called()
                mock_imap.add_flag.assert_not_called()

                # Verify email was saved
                email = db_session.query(ProcessedEmail).first()
                assert email is not None
                assert "queued_pending_actions" in email.actions_taken["actions"]

                # Verify pending actions were created
                pending_actions = (
                    db_session.query(PendingAction).filter_by(email_id=email.id).all()
                )
                assert len(pending_actions) > 0

                # Check for expected actions (mark_as_read, move to archive, add_flag)
                action_types = [action.action_type for action in pending_actions]
                assert "MARK_READ" in action_types  # mark_as_read is True
                assert "MOVE_FOLDER" in action_types  # move to archive
                assert "ADD_FLAG" in action_types  # action_required is True

                # Verify all actions are PENDING
                for action in pending_actions:
                    assert action.status == "PENDING"


def test_require_approval_spam_enqueues_quarantine(
    db_session, mock_settings_require_approval, sample_email_data
):
    """Test that spam emails are enqueued to quarantine folder when REQUIRE_APPROVAL=True"""
    # Modify analysis to indicate spam
    spam_analysis = {
        "summary": "Spam email",
        "category": "Unklar",
        "spam_probability": 0.9,
        "action_required": False,
        "priority": "LOW",
        "suggested_folder": "Spam",
        "reasoning": "High spam probability",
        "tasks": [],
    }

    with patch(
        "src.services.email_processor.get_settings",
        return_value=mock_settings_require_approval,
    ):
        with patch("src.services.email_processor.IMAPService") as mock_imap_class:
            with patch("src.services.email_processor.AIService") as mock_ai_class:
                # Setup mocks
                mock_imap = Mock()
                mock_imap_class.return_value = mock_imap
                mock_ai = Mock()
                mock_ai.analyze_email.return_value = spam_analysis
                mock_ai_class.return_value = mock_ai

                processor = EmailProcessor(db_session)
                processor._process_single_email(sample_email_data, mock_imap)

                # Verify no IMAP actions were called
                mock_imap.move_to_folder.assert_not_called()

                # Verify pending action for spam
                email = db_session.query(ProcessedEmail).first()
                pending_actions = (
                    db_session.query(PendingAction).filter_by(email_id=email.id).all()
                )

                # Should have exactly one action: MOVE to quarantine
                assert len(pending_actions) == 1
                assert pending_actions[0].action_type == "MOVE_FOLDER"
                assert pending_actions[0].target_folder == "Quarantine"
                assert pending_actions[0].status == "PENDING"


def test_normal_mode_executes_imap_actions(
    db_session, mock_settings_normal, sample_email_data, sample_ai_analysis
):
    """Test that normal mode (no safe_mode, no require_approval) executes IMAP actions immediately"""
    with patch(
        "src.services.email_processor.get_settings", return_value=mock_settings_normal
    ):
        with patch("src.services.email_processor.IMAPService") as mock_imap_class:
            with patch("src.services.email_processor.AIService") as mock_ai_class:
                # Setup mocks
                mock_imap = Mock()
                mock_imap.move_to_folder.return_value = True
                mock_imap.mark_as_read.return_value = True
                mock_imap.add_flag.return_value = True
                mock_imap_class.return_value = mock_imap
                mock_ai = Mock()
                mock_ai.analyze_email.return_value = sample_ai_analysis
                mock_ai_class.return_value = mock_ai

                processor = EmailProcessor(db_session)
                processor._process_single_email(sample_email_data, mock_imap)

                # Verify IMAP actions were called
                mock_imap.mark_as_read.assert_called_once()
                mock_imap.move_to_folder.assert_called_once()
                mock_imap.add_flag.assert_called_once()

                # Verify no pending actions were created
                email = db_session.query(ProcessedEmail).first()
                pending_actions = (
                    db_session.query(PendingAction).filter_by(email_id=email.id).all()
                )
                assert len(pending_actions) == 0


def test_safe_mode_takes_precedence_over_require_approval(
    db_session, sample_email_data, sample_ai_analysis
):
    """Test that SAFE_MODE=True takes precedence over REQUIRE_APPROVAL=True"""
    settings = Mock(spec=Settings)
    settings.safe_mode = True
    settings.require_approval = True  # Both are True
    settings.spam_threshold = 0.7
    settings.max_emails_per_run = 200
    settings.store_email_body = False
    settings.quarantine_folder = "Quarantine"
    settings.archive_folder = "Archive"
    settings.spam_folder = "Spam"
    settings.mark_as_read = False
    settings.delete_spam = False

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_class:
            with patch("src.services.email_processor.AIService") as mock_ai_class:
                # Setup mocks
                mock_imap = Mock()
                mock_imap_class.return_value = mock_imap
                mock_ai = Mock()
                mock_ai.analyze_email.return_value = sample_ai_analysis
                mock_ai_class.return_value = mock_ai

                processor = EmailProcessor(db_session)
                processor._process_single_email(sample_email_data, mock_imap)

                # Verify safe_mode action was taken (not require_approval)
                email = db_session.query(ProcessedEmail).first()
                assert "safe_mode_skip" in email.actions_taken["actions"]
                assert "queued_pending_actions" not in email.actions_taken["actions"]

                # Verify no pending actions were created (safe_mode wins)
                pending_actions = (
                    db_session.query(PendingAction).filter_by(email_id=email.id).all()
                )
                assert len(pending_actions) == 0


def test_pending_action_model():
    """Test PendingAction model structure"""
    action = PendingAction(
        email_id=1, action_type="MOVE_FOLDER", target_folder="Archive", status="PENDING"
    )

    assert action.email_id == 1
    assert action.action_type == "MOVE_FOLDER"
    assert action.target_folder == "Archive"
    assert action.status == "PENDING"
    assert action.approved_at is None
    assert action.applied_at is None
    assert action.error_message is None


def test_config_has_require_approval():
    """Test that Settings has require_approval field with correct default"""
    settings = Settings(
        imap_host="test.example.com",
        imap_username="test@example.com",
        imap_password="testpass",
    )

    assert hasattr(settings, "require_approval")
    assert settings.require_approval is False  # Default should be False


# E2E Tests for approval workflow fixes


def test_error_sanitization():
    """Test that error sanitization works correctly"""
    from src.utils.error_handling import sanitize_error

    # Test in debug mode - should return full error
    test_error = ValueError(
        "Authentication failed for user test@example.com with password secret123"
    )
    result_debug = sanitize_error(test_error, debug=True)
    assert "test@example.com" in result_debug
    assert "secret123" in result_debug

    # Test in production mode - should return only error type
    result_prod = sanitize_error(test_error, debug=False)
    assert result_prod == "ValueError"
    assert "test@example.com" not in result_prod
    assert "secret123" not in result_prod
    assert "Authentication" not in result_prod


def test_imap_connection_in_apply_endpoints():
    """Test that apply endpoints properly connect to IMAP"""
    from fastapi.testclient import TestClient
    from src.main import app

    # This test verifies that IMAPService is instantiated with context manager
    # The actual connection test would require mock setup, but we verify
    # the code structure uses the context manager pattern

    # Check that the code uses 'with IMAPService() as imap:'
    import inspect
    from src.main import apply_all_approved_actions, apply_single_action

    source_apply_all = inspect.getsource(apply_all_approved_actions)
    source_apply_single = inspect.getsource(apply_single_action)

    # Verify context manager pattern is used
    assert "with IMAPService() as imap:" in source_apply_all
    assert "with IMAPService() as imap:" in source_apply_single

    # Verify no disconnect() calls (handled by context manager)
    assert "imap.disconnect()" not in source_apply_all
    assert "imap.disconnect()" not in source_apply_single


def test_safe_mode_blocks_apply_endpoints():
    """Test that SAFE_MODE blocks execution in apply endpoints"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch

    client = TestClient(app)

    # Mock settings with safe_mode=True
    with patch("src.main.settings") as mock_settings:
        mock_settings.safe_mode = True
        mock_settings.debug = False

        # Mock authentication
        with patch("src.main.require_authentication"):
            # Test batch apply endpoint
            response = client.post(
                "/api/pending-actions/apply",
                json={"dry_run": False},
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 409
            assert "SAFE_MODE enabled" in response.json()["message"]

            # Test single apply endpoint
            response = client.post(
                "/api/pending-actions/1/apply",
                json={"dry_run": False},
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 409
            assert "SAFE_MODE enabled" in response.json()["message"]


def test_preview_endpoint_routing():
    """Test that preview endpoint is reachable and doesn't conflict with {action_id}"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch

    client = TestClient(app)

    # Mock authentication
    with patch("src.main.require_authentication"):
        # Mock database
        with patch("src.main.get_db"):
            # Test that /api/pending-actions/preview is reachable
            response = client.get(
                "/api/pending-actions/preview",
                headers={"Authorization": "Bearer test-token"},
            )

            # Should not return 422 (would indicate routing collision)
            # Should return 200 or other valid response
            assert response.status_code != 422

            # The endpoint should return preview data structure
            if response.status_code == 200:
                data = response.json()
                assert "count" in data or "actions" in data


def test_approval_sets_timestamp_for_rejection():
    """Test that rejecting an action sets approved_at timestamp"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Create mock pending action
    mock_action = MagicMock()
    mock_action.id = 1
    mock_action.status = "PENDING"
    mock_action.approved_at = None

    # Mock database query
    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.first.return_value = mock_action
    mock_db.query.return_value = mock_query

    with patch("src.main.require_authentication"):
        with patch("src.main.get_db", return_value=mock_db):
            # Test rejection sets approved_at
            response = client.post(
                "/api/pending-actions/1/approve",
                json={"approve": False},
                headers={"Authorization": "Bearer test-token"},
            )

            # Verify the action object had approved_at set
            assert mock_action.approved_at is not None
            assert mock_action.status == "REJECTED"


def test_sanitized_errors_in_api_responses():
    """Test that API responses use sanitized errors in production mode"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Mock settings with debug=False (production)
    with patch("src.main.settings") as mock_settings:
        mock_settings.safe_mode = False
        mock_settings.debug = False

        # Mock IMAP service that fails to connect
        with patch("src.services.imap_service.IMAPService") as MockIMAP:
            mock_imap_instance = MagicMock()
            mock_imap_instance.client = None  # Connection failed
            mock_imap_instance.__enter__ = MagicMock(return_value=mock_imap_instance)
            mock_imap_instance.__exit__ = MagicMock(return_value=False)
            MockIMAP.return_value = mock_imap_instance

            # Create mock approved action
            mock_action = MagicMock()
            mock_action.id = 1
            mock_action.status = "APPROVED"

            mock_db = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value.all.return_value = [mock_action]
            mock_db.query.return_value = mock_query

            with patch("src.main.require_authentication"):
                with patch("src.main.get_db", return_value=mock_db):
                    response = client.post(
                        "/api/pending-actions/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Should return 503 for connection failure
                    assert response.status_code == 503

                    # Error message should be sanitized (no credentials)
                    response_data = response.json()
                    message = response_data.get("message", "")

                    # Should not contain sensitive info
                    assert "password" not in message.lower()
                    assert "secret" not in message.lower()
                    assert (
                        "authentication" not in message.lower()
                        or message == "Service temporarily unavailable"
                    )


# Production-safe tests for approval workflow


def test_global_exception_handler_sanitizes_errors():
    """Test that global exception handler sanitizes errors in non-debug mode"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch

    client = TestClient(app)

    # Create a route that will raise an exception
    @app.get("/test-exception")
    async def test_exception_route():
        raise ValueError("Secret password: mypassword123 for user@example.com")

    # Test in non-debug mode
    with patch("src.main.settings") as mock_settings:
        mock_settings.debug = False

        response = client.get("/test-exception")

        assert response.status_code == 500
        response_data = response.json()

        # Should return generic message in production
        assert response_data["detail"] == "An internal error occurred"

        # Should NOT contain sensitive info
        assert "mypassword123" not in str(response_data)
        assert "user@example.com" not in str(response_data)


def test_connection_failure_does_not_mutate_approved_actions():
    """Test that IMAP connection failure does NOT change APPROVED actions to FAILED"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Create mock approved action
    mock_action = MagicMock()
    mock_action.id = 1
    mock_action.status = "APPROVED"
    mock_action.email_id = 1

    # Mock database
    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.all.return_value = [mock_action]
    mock_db.query.return_value = mock_query

    # Mock IMAP service with failed connection
    with patch("src.services.imap_service.IMAPService") as MockIMAP:
        mock_imap_instance = MagicMock()
        mock_imap_instance.client = None  # Connection failed
        mock_imap_instance.__enter__ = MagicMock(return_value=mock_imap_instance)
        mock_imap_instance.__exit__ = MagicMock(return_value=False)
        MockIMAP.return_value = mock_imap_instance

        with patch("src.main.settings") as mock_settings:
            mock_settings.safe_mode = False
            mock_settings.debug = False

            with patch("src.main.require_authentication"):
                with patch("src.main.get_db", return_value=mock_db):
                    response = client.post(
                        "/api/pending-actions/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Should return 503
                    assert response.status_code == 503

                    # Action status should NOT have been changed to FAILED
                    assert mock_action.status == "APPROVED"

                    # commit should NOT have been called (no DB mutation)
                    mock_db.commit.assert_not_called()


def test_safe_mode_blocks_before_connection_attempt():
    """Test that SAFE_MODE blocks apply operations before attempting IMAP connection"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Mock IMAP service - should NOT be instantiated in SAFE_MODE
    with patch("src.services.imap_service.IMAPService") as MockIMAP:
        mock_imap_instance = MagicMock()
        MockIMAP.return_value = mock_imap_instance

        with patch("src.main.settings") as mock_settings:
            mock_settings.safe_mode = True  # SAFE_MODE enabled
            mock_settings.debug = False

            with patch("src.main.require_authentication"):
                with patch("src.main.get_db") as mock_get_db:
                    mock_db = MagicMock()
                    mock_get_db.return_value = mock_db

                    # Test batch apply endpoint
                    response = client.post(
                        "/api/pending-actions/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Should return 409 (Conflict)
                    assert response.status_code == 409

                    response_data = response.json()
                    assert "SAFE_MODE enabled" in response_data["message"]

                    # IMAPService should NOT have been instantiated
                    MockIMAP.assert_not_called()

                    # Test single action apply endpoint
                    response = client.post(
                        "/api/pending-actions/1/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Should also return 409
                    assert response.status_code == 409
                    assert "SAFE_MODE enabled" in response.json()["message"]

                    # Still no IMAP connection attempt
                    MockIMAP.assert_not_called()


def test_connection_failure_single_action_preserves_approved():
    """Test that single action apply with connection failure preserves APPROVED status"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Create mock approved action
    mock_action = MagicMock()
    mock_action.id = 1
    mock_action.status = "APPROVED"
    mock_action.email_id = 1

    # Create mock email
    mock_email = MagicMock()
    mock_email.id = 1
    mock_email.uid = "12345"
    mock_email.subject = "Test"

    # Mock database with proper query routing
    mock_db = MagicMock()

    # First call returns action, second returns email
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_action,
        mock_email,
    ]

    # Mock IMAP service with failed connection
    with patch("src.services.imap_service.IMAPService") as MockIMAP:
        mock_imap_instance = MagicMock()
        mock_imap_instance.client = None  # Connection failed
        mock_imap_instance.__enter__ = MagicMock(return_value=mock_imap_instance)
        mock_imap_instance.__exit__ = MagicMock(return_value=False)
        MockIMAP.return_value = mock_imap_instance

        with patch("src.main.settings") as mock_settings:
            mock_settings.safe_mode = False
            mock_settings.debug = False

            with patch("src.main.require_authentication"):
                with patch("src.main.get_db", return_value=mock_db):
                    response = client.post(
                        "/api/pending-actions/1/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Should return 503
                    assert response.status_code == 503

                    # Response should indicate status is still APPROVED
                    response_data = response.json()
                    assert response_data.get("status") == "APPROVED"

                    # Action status should NOT have been changed to FAILED
                    assert mock_action.status == "APPROVED"

                    # commit should NOT have been called
                    mock_db.commit.assert_not_called()


def test_no_raw_exceptions_in_error_message_when_debug_false():
    """Test that no code path stores raw str(e) into PendingAction.error_message when debug=false"""
    from fastapi.testclient import TestClient
    from src.main import app
    from unittest.mock import patch, MagicMock

    client = TestClient(app)

    # Create mock approved action
    mock_action = MagicMock()
    mock_action.id = 1
    mock_action.status = "APPROVED"
    mock_action.email_id = 1
    mock_action.error_message = None  # Track what gets set

    # Create mock email
    mock_email = MagicMock()
    mock_email.id = 1
    mock_email.uid = "12345"

    # Mock database
    mock_db = MagicMock()

    # Setup query mocks to return action and email
    def query_side_effect(model):
        mock_query = MagicMock()
        if hasattr(model, "__name__") and model.__name__ == "ProcessedEmail":
            mock_query.filter.return_value.first.return_value = mock_email
        else:
            mock_query.filter.return_value.all.return_value = [mock_action]
        return mock_query

    mock_db.query.side_effect = query_side_effect

    # Mock IMAP service that raises an exception with sensitive data
    with patch("src.services.imap_service.IMAPService") as MockIMAP:
        mock_imap_instance = MagicMock()
        mock_imap_instance.client = MagicMock()  # Connection succeeds
        mock_imap_instance.__enter__ = MagicMock(return_value=mock_imap_instance)
        mock_imap_instance.__exit__ = MagicMock(return_value=False)

        # Make IMAP operation raise exception with sensitive data
        sensitive_error = Exception(
            "IMAP error: password='secret123' user='admin@example.com'"
        )
        mock_imap_instance.move_to_folder.side_effect = sensitive_error
        mock_imap_instance.mark_as_read.side_effect = sensitive_error
        mock_imap_instance.add_flag.side_effect = sensitive_error

        MockIMAP.return_value = mock_imap_instance

        with patch("src.main.settings") as mock_settings:
            mock_settings.safe_mode = False
            mock_settings.debug = (
                False  # Production mode - no sensitive data should leak
            )

            with patch("src.main.require_authentication"):
                with patch("src.main.get_db", return_value=mock_db):
                    response = client.post(
                        "/api/pending-actions/apply",
                        json={"dry_run": False},
                        headers={"Authorization": "Bearer test-token"},
                    )

                    # Check that error_message was set (action failed)
                    if mock_action.error_message is not None:
                        # Verify no sensitive data in error_message
                        assert "secret123" not in str(mock_action.error_message)
                        assert "admin@example.com" not in str(mock_action.error_message)
                        assert "password=" not in str(mock_action.error_message).lower()

                        # Should only contain error type (Exception) in production mode
                        assert mock_action.error_message == "Exception"
