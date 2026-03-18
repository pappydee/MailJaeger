"""
Focused tests for action_queue foundation API and execution flow.
"""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch, call

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.main import app
from src.database.connection import get_db as _get_db
from src.models.database import Base, ProcessedEmail, ActionQueue, AppSetting
from src.services.email_processor import EmailProcessor
from src.config import Settings

VALID_THREAD_STATES = {
    "open",
    "waiting_for_me",
    "waiting_for_other",
    "resolved",
    "informational",
}


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
    settings.safe_mode = False
    settings.require_approval = False
    settings.spam_threshold = 0.7
    settings.ai_batch_size = 10
    settings.max_emails_per_run = 200
    settings.store_email_body = False
    settings.inbox_folder = "INBOX"
    settings.quarantine_folder = "Quarantine"
    settings.archive_folder = "Archive"
    settings.spam_folder = "Spam"
    settings.mark_as_read = False
    settings.delete_spam = False
    settings.debug = False
    return settings


def _create_email(
    db_session,
    uid: str = "42",
    *,
    thread_id: str = "thread-default",
    sender: str = "sender@example.com",
    action_required: bool = False,
    is_resolved: bool = False,
) -> ProcessedEmail:
    email = ProcessedEmail(
        message_id=f"email-{uid}@example.com",
        uid=uid,
        thread_id=thread_id,
        subject="Subject",
        sender=sender,
        is_processed=True,
        processed_at=datetime.utcnow(),
        action_required=action_required,
        is_resolved=is_resolved,
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

        response = client.post(
            f"/api/actions/{action.id}/execute", headers=auth_headers
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed_at"] is not None
    assert body["thread_state"] in VALID_THREAD_STATES
    assert isinstance(body["thread_summary"], dict)
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
        response = client.post(
            f"/api/actions/{action.id}/execute", headers=auth_headers
        )

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
        response = client.post(
            f"/api/actions/{action.id}/execute", headers=auth_headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    mock_imap_cls.assert_not_called()


def test_mark_resolved_execution_updates_email_without_imap(
    client, auth_headers, db_session, override_db
):
    email = _create_email(db_session, uid="110")
    email.is_resolved = False
    action = ActionQueue(
        email_id=email.id,
        action_type="mark_resolved",
        payload={"reason": "daily_report_unresolved"},
        status="approved",
    )
    db_session.add(action)
    db_session.commit()

    with patch("src.main.IMAPService") as mock_imap_cls:
        response = client.post(
            f"/api/actions/{action.id}/execute", headers=auth_headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert response.json()["thread_state"] == "resolved"
    db_session.refresh(email)
    assert email.is_resolved is True
    assert email.action_required is False
    assert email.thread_state == "resolved"
    mock_imap_cls.assert_called_once()
    mock_imap_cls.return_value.__enter__.return_value.move_to_folder.assert_not_called()


def test_reply_draft_execution_is_safe_and_keeps_draft_payload(
    client, auth_headers, db_session, override_db
):
    email = _create_email(db_session, uid="111")
    action = ActionQueue(
        email_id=email.id,
        action_type="reply_draft",
        payload={"draft_summary": "Kurzantwort", "draft_text": "Hallo,\nDanke."},
        status="approved",
    )
    db_session.add(action)
    db_session.commit()

    with patch("src.main.IMAPService") as mock_imap_cls:
        response = client.post(
            f"/api/actions/{action.id}/execute", headers=auth_headers
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["payload"]["draft_state"] == "proposed_manual_send"
    mock_imap_cls.assert_called_once()
    mock_imap_cls.return_value.__enter__.return_value.delete_message.assert_not_called()


def test_process_run_auto_executes_only_approved_actions_when_safe_mode_disabled(db_session):
    email_approved = _create_email(db_session, uid="801")
    email_proposed = _create_email(db_session, uid="802")
    approved = ActionQueue(
        email_id=email_approved.id,
        action_type="mark_read",
        payload={"source": "test"},
        status="approved",
    )
    proposed = ActionQueue(
        email_id=email_proposed.id,
        action_type="mark_read",
        payload={"source": "test"},
        status="proposed",
    )
    db_session.add_all([approved, proposed])
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = False
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            imap.mark_as_read.return_value = True
            mock_imap_cls.return_value = imap

            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="MANUAL")

    db_session.refresh(approved)
    db_session.refresh(proposed)
    assert run.status == "SUCCESS"
    assert approved.status == "executed"
    assert approved.executed_at is not None
    assert proposed.status == "proposed"
    imap.mark_as_read.assert_called_once_with(801)


def test_process_run_skips_auto_execution_when_safe_mode_enabled(db_session):
    email = _create_email(db_session, uid="804")
    approved = ActionQueue(
        email_id=email.id,
        action_type="mark_read",
        payload={"source": "test"},
        status="approved",
    )
    db_session.add(approved)
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = True
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            imap.mark_as_read.return_value = True
            mock_imap_cls.return_value = imap

            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="MANUAL")

    db_session.refresh(approved)
    assert run.status == "SUCCESS"
    assert approved.status == "approved"
    assert approved.executed_at is None
    imap.mark_as_read.assert_not_called()


def test_process_run_marks_failed_when_approved_action_execution_fails(db_session):
    email = _create_email(db_session, uid="803")
    approved = ActionQueue(
        email_id=email.id,
        action_type="move",
        payload={"target_folder": "Archive"},
        status="approved",
    )
    db_session.add(approved)
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = False
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            imap.move_to_folder.return_value = False
            mock_imap_cls.return_value = imap

            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="SCHEDULED")

    db_session.refresh(approved)
    assert run.status == "SUCCESS"
    assert approved.status == "failed"
    assert approved.error_message == "IMAP operation failed"
    imap.move_to_folder.assert_called_once_with(803, "Archive")


def test_settings_endpoint_updates_and_persists_safe_mode(
    client, auth_headers, db_session, override_db
):
    import src.main

    src.main.settings.safe_mode = False

    response = client.post(
        "/api/settings",
        headers=auth_headers,
        json={"safe_mode": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["safe_mode"] is True
    assert "safe_mode" in body["updated_fields"]

    persisted = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == "safe_mode")
        .first()
    )
    assert persisted is not None
    assert persisted.value is True

    # Simulate stale in-memory config; GET must still return persisted state.
    src.main.settings.safe_mode = False
    effective = client.get("/api/settings", headers=auth_headers)
    assert effective.status_code == 200
    assert effective.json()["safe_mode"] is True


def test_persisted_safe_mode_is_applied_after_settings_reload(db_session):
    import src.main
    from src.config import get_settings, reload_settings

    src.main._set_app_setting(db_session, key="safe_mode", value=True)
    db_session.commit()

    # Simulate process restart config reload from env (SAFE_MODE=false in tests)
    reload_settings()
    src.main.settings = get_settings()
    assert src.main.settings.safe_mode is False

    src.main._apply_persisted_safe_mode(db_session)
    assert src.main.settings.safe_mode is True


def test_process_run_does_not_retry_failed_actions(db_session):
    email_failed = _create_email(db_session, uid="901", thread_id="thread-failed")
    email_approved = _create_email(db_session, uid="902", thread_id="thread-failed")
    already_failed = ActionQueue(
        email_id=email_failed.id,
        thread_id=email_failed.thread_id,
        action_type="mark_read",
        payload={"source": "test"},
        status="failed",
        error_message="previous failure",
    )
    approved = ActionQueue(
        email_id=email_approved.id,
        thread_id=email_approved.thread_id,
        action_type="mark_read",
        payload={"source": "test"},
        status="approved",
    )
    db_session.add_all([already_failed, approved])
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = False
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            imap.mark_as_read.return_value = True
            mock_imap_cls.return_value = imap
            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="SCHEDULED")

    db_session.refresh(already_failed)
    db_session.refresh(approved)
    assert run.status == "SUCCESS"
    assert already_failed.status == "failed"
    assert already_failed.error_message == "previous failure"
    assert approved.status == "executed"
    imap.mark_as_read.assert_called_once_with(902)


def test_process_run_executes_approved_in_created_order(db_session):
    early_email = _create_email(db_session, uid="911", thread_id="thread-order")
    late_email = _create_email(db_session, uid="912", thread_id="thread-order")
    early = ActionQueue(
        email_id=early_email.id,
        thread_id=early_email.thread_id,
        action_type="mark_read",
        payload={"source": "test"},
        status="approved",
        created_at=datetime.utcnow() - timedelta(minutes=5),
    )
    late = ActionQueue(
        email_id=late_email.id,
        thread_id=late_email.thread_id,
        action_type="mark_read",
        payload={"source": "test"},
        status="approved",
        created_at=datetime.utcnow(),
    )
    db_session.add_all([late, early])
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = False
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            imap.mark_as_read.return_value = True
            mock_imap_cls.return_value = imap
            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="MANUAL")

    assert run.status == "SUCCESS"
    assert imap.mark_as_read.call_args_list == [call(911), call(912)]


def test_process_run_does_not_reexecute_already_executed_actions(db_session):
    email = _create_email(db_session, uid="920", thread_id="thread-once")
    executed_action = ActionQueue(
        email_id=email.id,
        thread_id=email.thread_id,
        action_type="mark_read",
        payload={"source": "test"},
        status="executed",
        executed_at=datetime.utcnow(),
    )
    db_session.add(executed_action)
    db_session.commit()

    settings = _settings_for_processor()
    settings.safe_mode = True
    settings.require_approval = True

    with patch("src.services.email_processor.get_settings", return_value=settings):
        with patch("src.services.email_processor.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.connect.return_value = True
            mock_imap_cls.return_value = imap
            processor = EmailProcessor(db_session)
            with patch.object(
                processor,
                "_run_ingestion",
                return_value={"new": 0, "skipped": 0, "failed": 0},
            ):
                run = processor.process_emails(trigger_type="MANUAL")

    db_session.refresh(executed_action)
    assert run.status == "SUCCESS"
    assert executed_action.status == "executed"
    imap.mark_as_read.assert_not_called()
