"""
Test approval workflow functionality
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

from src.models.database import Base, ProcessedEmail, PendingAction
from src.database.connection import get_db


class TestApprovalWorkflow:
    """Test approval workflow for IMAP actions"""
    
    @pytest.fixture
    def test_db(self):
        """Create test database"""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(bind=engine)
        return TestingSessionLocal()
    
    @pytest.fixture
    def client(self, test_db):
        """Create test client with mocked database"""
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "REQUIRE_APPROVAL": "true",
            "SAFE_MODE": "false",
            "ALLOWED_MOVE_FOLDERS": "Quarantine,Archive"
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            
            # Override database dependency
            def override_get_db():
                try:
                    yield test_db
                finally:
                    pass
            
            app.dependency_overrides[get_db] = override_get_db
            
            client = TestClient(app)
            yield client
            
            # Cleanup
            app.dependency_overrides.clear()
    
    def test_require_approval_enqueues_actions(self, client, test_db):
        """Test that actions are enqueued when REQUIRE_APPROVAL=true"""
        # Create a test email
        email = ProcessedEmail(
            message_id="<test@example.com>",
            uid="123",
            subject="Test Email",
            sender="sender@example.com",
            recipients="recipient@example.com",
            date=datetime.utcnow(),
            summary="Test summary",
            category="Test",
            spam_probability=0.8,
            action_required=False,
            priority="LOW",
            is_spam=True,
            is_processed=True
        )
        test_db.add(email)
        test_db.commit()
        
        # Mock IMAP service to not actually connect
        with patch('src.services.email_processor.IMAPService') as mock_imap_service:
            mock_imap = MagicMock()
            mock_imap.client = MagicMock()
            mock_imap_service.return_value.__enter__.return_value = mock_imap
            mock_imap.get_unread_emails.return_value = []
            
            # Get pending actions for this email
            pending_actions = test_db.query(PendingAction).filter(
                PendingAction.email_id == email.id
            ).all()
            
            # Since we're testing with REQUIRE_APPROVAL=true and email is spam,
            # we expect a MOVE_FOLDER action to be created during processing
            # However, we need to trigger email processing first
            
            # For now, test the service directly
            from src.services.pending_actions_service import PendingActionsService
            service = PendingActionsService(test_db)
            
            # Enqueue a move action
            action = service.enqueue_action(
                email_id=email.id,
                action_type="MOVE_FOLDER",
                target_folder="Quarantine",
                reason="spam",
                proposed_by="system"
            )
            
            assert action is not None
            assert action.status == "PENDING"
            assert action.action_type == "MOVE_FOLDER"
            assert action.target_folder == "Quarantine"
    
    def test_approve_pending_action(self, client, test_db):
        """Test approving a pending action"""
        # Create a test email
        email = ProcessedEmail(
            message_id="<test@example.com>",
            uid="123",
            subject="Test Email",
            sender="sender@example.com",
            recipients="recipient@example.com",
            date=datetime.utcnow(),
            is_processed=True
        )
        test_db.add(email)
        test_db.commit()
        
        # Create a pending action
        action = PendingAction(
            email_id=email.id,
            action_type="MARK_READ",
            reason="archive_policy",
            proposed_by="system",
            status="PENDING"
        )
        test_db.add(action)
        test_db.commit()
        
        # Approve the action via API
        response = client.post(
            f"/api/pending-actions/{action.id}/approve",
            headers={"Authorization": "Bearer test_key_12345"},
            json={}
        )
        
        assert response.status_code == 200
        assert response.json()["status"] == "APPROVED"
        
        # Verify action status in database
        test_db.refresh(action)
        assert action.status == "APPROVED"
        assert action.approved_at is not None
    
    def test_apply_approved_action(self, client, test_db):
        """Test applying an approved action"""
        # Create a test email
        email = ProcessedEmail(
            message_id="<test@example.com>",
            uid="123",
            subject="Test Email",
            sender="sender@example.com",
            recipients="recipient@example.com",
            date=datetime.utcnow(),
            is_processed=True
        )
        test_db.add(email)
        test_db.commit()
        
        # Create an approved action
        action = PendingAction(
            email_id=email.id,
            action_type="MARK_READ",
            reason="archive_policy",
            proposed_by="system",
            status="APPROVED",
            approved_at=datetime.utcnow()
        )
        test_db.add(action)
        test_db.commit()
        
        # Mock IMAP service
        with patch('src.services.pending_actions_service.IMAPService') as mock_imap_service:
            mock_imap = MagicMock()
            mock_imap.client = MagicMock()
            mock_imap.mark_as_read.return_value = True
            mock_imap_service.return_value.__enter__.return_value = mock_imap
            
            # Apply the action via API
            response = client.post(
                f"/api/pending-actions/{action.id}/apply",
                headers={"Authorization": "Bearer test_key_12345"},
                json={}
            )
            
            assert response.status_code == 200
            assert response.json()["status"] == "APPLIED"
            
            # Verify action status in database
            test_db.refresh(action)
            assert action.status == "APPLIED"
            assert action.applied_at is not None
    
    def test_folder_allowlist_validation(self, client, test_db):
        """Test that folder allowlist is enforced"""
        # Create a test email
        email = ProcessedEmail(
            message_id="<test@example.com>",
            uid="123",
            subject="Test Email",
            sender="sender@example.com",
            recipients="recipient@example.com",
            date=datetime.utcnow(),
            is_processed=True
        )
        test_db.add(email)
        test_db.commit()
        
        # Try to enqueue action with disallowed folder
        from src.services.pending_actions_service import PendingActionsService
        service = PendingActionsService(test_db)
        
        action = service.enqueue_action(
            email_id=email.id,
            action_type="MOVE_FOLDER",
            target_folder="NotAllowedFolder",
            reason="test",
            proposed_by="system"
        )
        
        # Should create a FAILED action instead
        assert action is not None
        assert action.status == "FAILED"
        assert action.error_code == "FOLDER_NOT_ALLOWED"
    
    def test_pending_actions_list_endpoint(self, client, test_db):
        """Test listing pending actions"""
        # Create a test email
        email = ProcessedEmail(
            message_id="<test@example.com>",
            uid="123",
            subject="Test Email",
            sender="sender@example.com",
            recipients="recipient@example.com",
            date=datetime.utcnow(),
            is_processed=True
        )
        test_db.add(email)
        test_db.commit()
        
        # Create some pending actions
        action1 = PendingAction(
            email_id=email.id,
            action_type="MARK_READ",
            reason="archive_policy",
            proposed_by="system",
            status="PENDING"
        )
        action2 = PendingAction(
            email_id=email.id,
            action_type="FLAG",
            reason="action_required",
            proposed_by="system",
            status="PENDING"
        )
        test_db.add(action1)
        test_db.add(action2)
        test_db.commit()
        
        # Get list via API
        response = client.get(
            "/api/pending-actions",
            headers={"Authorization": "Bearer test_key_12345"}
        )
        
        assert response.status_code == 200
        actions = response.json()
        assert len(actions) >= 2
    
    def test_pending_actions_summary_endpoint(self, client, test_db):
        """Test getting pending actions summary"""
        response = client.get(
            "/api/pending-actions/summary",
            headers={"Authorization": "Bearer test_key_12345"}
        )
        
        assert response.status_code == 200
        summary = response.json()
        assert "status_pending" in summary
        assert "status_approved" in summary
        assert "type_mark_read" in summary
