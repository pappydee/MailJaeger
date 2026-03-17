from datetime import datetime
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.main import app
from src.database.connection import get_db as _get_db
from src.models.database import Base, ProcessedEmail, ActionQueue, DecisionEvent


AUTH = {"Authorization": "Bearer test_key_abc123"}


def _make_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _mk_client(db_session):
    app.dependency_overrides[_get_db] = lambda: db_session
    return TestClient(app)


def _create_email(
    db_session,
    *,
    message_id: str,
    uid: str = "101",
    thread_id: str = "thread-1",
    action_required: bool = False,
    is_resolved: bool = False,
    is_spam: bool = False,
    is_archived: bool = False,
):
    email = ProcessedEmail(
        message_id=message_id,
        uid=uid,
        thread_id=thread_id,
        subject=f"Subject {message_id}",
        sender="sender@example.com",
        is_processed=True,
        processed_at=datetime.utcnow(),
        action_required=action_required,
        is_resolved=is_resolved,
        is_spam=is_spam,
        is_archived=is_archived,
    )
    db_session.add(email)
    db_session.commit()
    db_session.refresh(email)
    return email


def test_daily_report_sections_and_suggested_actions_present():
    db = _make_session()
    try:
        _create_email(db, message_id="m1@example.com", action_required=True, is_resolved=False)
        _create_email(db, message_id="m2@example.com", is_spam=True, is_archived=False)
        client = _mk_client(db)
        with patch("src.main.AIService") as mock_ai_cls:
            mock_ai = mock_ai_cls.return_value
            mock_ai.generate_report.return_value = "Report"
            resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in (
            "generated_at",
            "period_hours",
            "totals",
            "important_items",
            "action_items",
            "unresolved_items",
            "spam_items",
            "suggested_actions",
            "report_text",
        ):
            assert key in data
        assert isinstance(data["totals"], dict)
        assert data["totals"]["action_required"] >= 1
        assert any(a["action_type"] == "reply_draft" for a in data["suggested_actions"])
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_queue_report_suggestion_creates_action_queue_item_without_execution():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m3@example.com", action_required=True)
        client = _mk_client(db)
        resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={
                "email_id": email.id,
                "thread_id": email.thread_id,
                "action_type": "mark_resolved",
                "payload": {"reason": "test"},
                "safe_mode": True,
                "description": "Mark unresolved as resolved",
            },
        )
        assert resp.status_code == 200, resp.text
        queued = db.query(ActionQueue).filter(ActionQueue.email_id == email.id).one()
        assert queued.status == "proposed_action"
        assert queued.executed_at is None
        assert queued.payload["source"] == "daily_report_suggestion"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_reply_draft_queue_payload_contains_draft_fields():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m4@example.com", action_required=True)
        client = _mk_client(db)
        resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={
                "email_id": email.id,
                "action_type": "reply_draft",
                "safe_mode": True,
                "description": "Create draft reply",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action_type"] == "reply_draft"
        assert body["payload"]["draft_summary"]
        assert body["payload"]["draft_text"]
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_report_suggestion_approval_and_execution_record_learning_signals():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m5@example.com", uid="551")
        client = _mk_client(db)
        queue_resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={"email_id": email.id, "action_type": "mark_read", "safe_mode": False},
        )
        action_id = queue_resp.json()["id"]

        approve_resp = client.post(f"/api/actions/{action_id}/approve", headers=AUTH)
        assert approve_resp.status_code == 200

        with patch("src.main.IMAPService") as mock_imap_cls:
            imap = Mock()
            imap.mark_as_read.return_value = True
            mock_imap_cls.return_value.__enter__.return_value = imap
            execute_resp = client.post(f"/api/actions/{action_id}/execute", headers=AUTH)

        assert execute_resp.status_code == 200, execute_resp.text
        assert execute_resp.json()["status"] == "executed"
        event_types = {
            e.event_type
            for e in db.query(DecisionEvent).filter(DecisionEvent.email_id == email.id).all()
        }
        assert "approve_suggestion" in event_types
        assert "execute_suggestion" in event_types
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_report_suggestion_rejection_records_signal():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m6@example.com")
        client = _mk_client(db)
        queue_resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={"email_id": email.id, "action_type": "archive", "safe_mode": True},
        )
        action_id = queue_resp.json()["id"]
        reject_resp = client.post(f"/api/actions/{action_id}/reject", headers=AUTH)
        assert reject_resp.status_code == 200
        reject_event = (
            db.query(DecisionEvent)
            .filter(
                DecisionEvent.email_id == email.id,
                DecisionEvent.event_type == "reject_suggestion",
            )
            .first()
        )
        assert reject_event is not None
        assert reject_event.user_confirmed is False
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_safe_mode_blocks_execution_for_report_suggestion():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m7@example.com", uid="701")
        client = _mk_client(db)
        queue_resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={"email_id": email.id, "action_type": "mark_read", "safe_mode": True},
        )
        action_id = queue_resp.json()["id"]
        approve_resp = client.post(f"/api/actions/{action_id}/approve", headers=AUTH)
        assert approve_resp.status_code == 200

        with patch.dict("os.environ", {"SAFE_MODE": "true"}):
            from src.config import reload_settings, get_settings
            reload_settings()
            import src.main

            src.main.settings = get_settings()
            blocked = client.post(f"/api/actions/{action_id}/execute", headers=AUTH)
            assert blocked.status_code == 409
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_queue_endpoint_rejects_invalid_action_type():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m8@example.com")
        client = _mk_client(db)
        resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={"email_id": email.id, "action_type": "not_a_real_action", "safe_mode": True},
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()
        db.close()
