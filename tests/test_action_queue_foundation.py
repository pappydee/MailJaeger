"""
Focused tests for action_queue foundation API and execution flow.
"""

from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.main import app
from src.database.connection import get_db as _get_db
from src.models.database import Base, ProcessedEmail, ActionQueue
from src.services.email_processor import EmailProcessor
from src.config import Settings


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test_key_abc123"}


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def override_db(db_session):
    app.dependency_overrides[_get_db] = lambda: db_session
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def sample_email_data():
    return {
        "message_id": "aqf-1@example.com",
        "uid": "12345",
        "subject": "Action Queue Foundation",
        "sender": "sender@example.com",
        "recipients": "recipient@example.com",
        "date": datetime.utcnow(),
        "body_plain": "body",
        "body_html": "<p>body</p>",
        "integrity_hash": "hash1",
    }


def _settings_for_processor():
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
    settings.debug = False
    return settings


def _create_email(db_session, uid: str = "42") -> ProcessedEmail:
    email = ProcessedEmail(
        message_id=f"email-{uid}@example.com",
        uid=uid,
        subject="Subject",
        sender="sender@example.com",
        is_processed=True,
        processed_at=datetime.utcnow(),
    )
    db_session.add(email)
    db_session.commit()
    db_session.refresh(email)
    return email


def test_action_creation_from_analysis(db_session, sample_email_data):
    """Analysis should enqueue optional structured action proposals."""
    analysis = {
        "summary": "summary",
        "category": "Klinik",
        "spam_probability": 0.1,
        "action_required": False,
        "priority": "LOW",
        "suggested_folder": "Archive",
        "reasoning": "reason",
        "tasks": [],
    }

    with patch(
        "src.services.email_processor.get_settings",
        return_value=_settings_for_processor(),
    ):
        with patch("src.services.email_processor.AIService") as mock_ai_class:
            mock_ai = Mock()
            mock_ai.analyze_email.return_value = analysis
            mock_ai_class.return_value = mock_ai

            processor = EmailProcessor(db_session)
            processor._process_single_email(sample_email_data, imap=Mock())

    queued = db_session.query(ActionQueue).all()
    assert len(queued) == 1
    assert queued[0].status == "proposed"
    assert queued[0].action_type == "move"
    assert queued[0].payload == {"target_folder": "Archive"}


def test_approve_flow(client, auth_headers, db_session, override_db):
    email = _create_email(db_session)
    action = ActionQueue(
        email_id=email.id,
        action_type="move",
        payload={"target_folder": "Archive"},
        status="proposed",
    )
    db_session.add(action)
    db_session.commit()

    response = client.post(f"/api/actions/{action.id}/approve", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_execution_flow_mock_imap(client, auth_headers, db_session, override_db):
    email = _create_email(db_session, uid="314")
    action = ActionQueue(
        email_id=email.id,
        action_type="move",
        payload={"target_folder": "Archive"},
        status="approved",
    )
    db_session.add(action)
    db_session.commit()

    with patch("src.main.IMAPService") as mock_imap_cls:
        imap = Mock()
        imap.move_to_folder.return_value = True
        mock_imap_cls.return_value.__enter__.return_value = imap

        response = client.post(f"/api/actions/{action.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed_at"] is not None
    imap.move_to_folder.assert_called_once_with(314, "Archive")


def test_invalid_payload_handling(client, auth_headers, db_session, override_db):
    email = _create_email(db_session, uid="99")
    action = ActionQueue(
        email_id=email.id,
        action_type="move",
        payload={},
        status="approved",
    )
    db_session.add(action)
    db_session.commit()

    with patch("src.main.IMAPService") as mock_imap_cls:
        response = client.post(f"/api/actions/{action.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "target_folder" in (body["error_message"] or "")
    mock_imap_cls.return_value.__enter__.return_value.move_to_folder.assert_not_called()


def test_double_execution_prevention(client, auth_headers, db_session, override_db):
    email = _create_email(db_session, uid="100")
    action = ActionQueue(
        email_id=email.id,
        action_type="move",
        payload={"target_folder": "Archive"},
        status="executed",
        executed_at=datetime.utcnow(),
    )
    db_session.add(action)
    db_session.commit()

    with patch("src.main.IMAPService") as mock_imap_cls:
        response = client.post(f"/api/actions/{action.id}/execute", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    mock_imap_cls.assert_not_called()
