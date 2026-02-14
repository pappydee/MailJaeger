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
        'message_id': 'test@example.com',
        'uid': '12345',
        'subject': 'Test Email',
        'sender': 'sender@example.com',
        'recipients': 'recipient@example.com',
        'date': datetime.utcnow(),
        'body_plain': 'Test body',
        'body_html': '<p>Test body</p>',
        'integrity_hash': 'testhash123'
    }


@pytest.fixture
def sample_ai_analysis():
    """Sample AI analysis result"""
    return {
        'summary': 'Test email summary',
        'category': 'Klinik',
        'spam_probability': 0.1,
        'action_required': True,
        'priority': 'HIGH',
        'suggested_folder': 'Archive',
        'reasoning': 'Test reasoning',
        'tasks': []
    }


def test_safe_mode_skips_imap_actions(db_session, mock_settings_safe_mode, sample_email_data, sample_ai_analysis):
    """Test that SAFE_MODE=True prevents all IMAP actions"""
    with patch('src.services.email_processor.get_settings', return_value=mock_settings_safe_mode):
        with patch('src.services.email_processor.IMAPService') as mock_imap_class:
            with patch('src.services.email_processor.AIService') as mock_ai_class:
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


def test_require_approval_enqueues_actions(db_session, mock_settings_require_approval, sample_email_data, sample_ai_analysis):
    """Test that REQUIRE_APPROVAL=True enqueues PendingActions instead of executing"""
    with patch('src.services.email_processor.get_settings', return_value=mock_settings_require_approval):
        with patch('src.services.email_processor.IMAPService') as mock_imap_class:
            with patch('src.services.email_processor.AIService') as mock_ai_class:
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
                pending_actions = db_session.query(PendingAction).filter_by(email_id=email.id).all()
                assert len(pending_actions) > 0
                
                # Check for expected actions (mark_as_read, move to archive, add_flag)
                action_types = [action.action_type for action in pending_actions]
                assert "MARK_READ" in action_types  # mark_as_read is True
                assert "MOVE_FOLDER" in action_types  # move to archive
                assert "ADD_FLAG" in action_types  # action_required is True
                
                # Verify all actions are PENDING
                for action in pending_actions:
                    assert action.status == "PENDING"


def test_require_approval_spam_enqueues_quarantine(db_session, mock_settings_require_approval, sample_email_data):
    """Test that spam emails are enqueued to quarantine folder when REQUIRE_APPROVAL=True"""
    # Modify analysis to indicate spam
    spam_analysis = {
        'summary': 'Spam email',
        'category': 'Unklar',
        'spam_probability': 0.9,
        'action_required': False,
        'priority': 'LOW',
        'suggested_folder': 'Spam',
        'reasoning': 'High spam probability',
        'tasks': []
    }
    
    with patch('src.services.email_processor.get_settings', return_value=mock_settings_require_approval):
        with patch('src.services.email_processor.IMAPService') as mock_imap_class:
            with patch('src.services.email_processor.AIService') as mock_ai_class:
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
                pending_actions = db_session.query(PendingAction).filter_by(email_id=email.id).all()
                
                # Should have exactly one action: MOVE to quarantine
                assert len(pending_actions) == 1
                assert pending_actions[0].action_type == "MOVE_FOLDER"
                assert pending_actions[0].target_folder == "Quarantine"
                assert pending_actions[0].status == "PENDING"


def test_normal_mode_executes_imap_actions(db_session, mock_settings_normal, sample_email_data, sample_ai_analysis):
    """Test that normal mode (no safe_mode, no require_approval) executes IMAP actions immediately"""
    with patch('src.services.email_processor.get_settings', return_value=mock_settings_normal):
        with patch('src.services.email_processor.IMAPService') as mock_imap_class:
            with patch('src.services.email_processor.AIService') as mock_ai_class:
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
                pending_actions = db_session.query(PendingAction).filter_by(email_id=email.id).all()
                assert len(pending_actions) == 0


def test_safe_mode_takes_precedence_over_require_approval(db_session, sample_email_data, sample_ai_analysis):
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
    
    with patch('src.services.email_processor.get_settings', return_value=settings):
        with patch('src.services.email_processor.IMAPService') as mock_imap_class:
            with patch('src.services.email_processor.AIService') as mock_ai_class:
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
                pending_actions = db_session.query(PendingAction).filter_by(email_id=email.id).all()
                assert len(pending_actions) == 0


def test_pending_action_model():
    """Test PendingAction model structure"""
    action = PendingAction(
        email_id=1,
        action_type="MOVE_FOLDER",
        target_folder="Archive",
        status="PENDING"
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
        imap_password="testpass"
    )
    
    assert hasattr(settings, 'require_approval')
    assert settings.require_approval is False  # Default should be False
