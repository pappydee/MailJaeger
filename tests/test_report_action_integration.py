from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.main import app
from src.database.connection import get_db as _get_db
from src.models.database import (
    Base,
    ProcessedEmail,
    ActionQueue,
    DecisionEvent,
    DailyReport,
)


AUTH = {"Authorization": "Bearer test_key_abc123"}
VALID_THREAD_STATES = {
    "open",
    "waiting_for_me",
    "waiting_for_other",
    "resolved",
    "informational",
}


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
    priority: str = "LOW",
    summary: str = "summary",
    sender: str = "sender@example.com",
):
    email = ProcessedEmail(
        message_id=message_id,
        uid=uid,
        thread_id=thread_id,
        subject=f"Subject {message_id}",
        sender=sender,
        is_processed=True,
        processed_at=datetime.utcnow(),
        action_required=action_required,
        is_resolved=is_resolved,
        is_spam=is_spam,
        is_archived=is_archived,
        priority=priority,
        summary=summary,
    )
    db_session.add(email)
    db_session.commit()
    db_session.refresh(email)
    return email


def test_daily_report_sections_and_suggested_actions_present():
    db = _make_session()
    try:
        _create_email(
            db, message_id="m1@example.com", action_required=True, is_resolved=False
        )
        _create_email(db, message_id="m2@example.com", is_spam=True, is_archived=False)
        payload = {
            "generated_at": datetime.utcnow().isoformat(),
            "period_hours": 24,
            "totals": {
                "total_processed": 2,
                "action_required": 1,
                "unresolved": 1,
                "spam_detected": 1,
            },
            "total_processed": 2,
            "action_required": 1,
            "spam_detected": 1,
            "unresolved": 1,
            "important_items": [],
            "action_items": [],
            "unresolved_items": [],
            "spam_items": [],
            "suggested_actions": [
                {
                    "email_id": 1,
                    "thread_id": "thread-1",
                    "action_type": "reply_draft",
                    "payload": {"draft_summary": "x", "draft_text": "y"},
                    "description": "Antwort-Entwurf erstellen",
                    "safe_mode": True,
                    "queue_status": None,
                }
            ],
            "report_text": "Report",
        }
        db.add(
            DailyReport(
                generated_at=datetime.utcnow(),
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                report_json=payload,
                report_text=payload["report_text"],
                generation_status="ready",
            )
        )
        db.commit()
        client = _mk_client(db)
        resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ready"
        report = data["report"]
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
            assert key in report
        assert isinstance(report["totals"], dict)
        assert report["totals"]["action_required"] >= 1
        assert any(
            a["action_type"] == "reply_draft" for a in report["suggested_actions"]
        )
        assert "queue_status" in report["suggested_actions"][0]
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_daily_report_endpoint_returns_pending_and_queues_background_generation():
    db = _make_session()
    try:
        _create_email(db, message_id="m-pending@example.com", action_required=True)
        client = _mk_client(db)
        with patch("src.main._generate_daily_report_in_background") as bg_mock:
            resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "pending"
        bg_mock.assert_called_once()
        queued = db.query(DailyReport).order_by(DailyReport.id.desc()).first()
        assert queued is not None
        assert queued.generation_status == "pending"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_daily_report_endpoint_uses_cached_ready_report_without_regeneration():
    db = _make_session()
    try:
        cached = DailyReport(
            generated_at=datetime.utcnow(),
            period_start=datetime.utcnow() - timedelta(hours=24),
            period_end=datetime.utcnow(),
            report_json={
                "generated_at": datetime.utcnow().isoformat(),
                "period_hours": 24,
                "totals": {
                    "total_processed": 1,
                    "action_required": 0,
                    "unresolved": 0,
                    "spam_detected": 0,
                },
                "total_processed": 1,
                "action_required": 0,
                "spam_detected": 0,
                "unresolved": 0,
                "important_items": [],
                "action_items": [],
                "unresolved_items": [],
                "spam_items": [],
                "suggested_actions": [],
                "report_text": "Cached report",
            },
            report_text="Cached report",
            generation_status="ready",
        )
        db.add(cached)
        db.commit()
        client = _mk_client(db)
        with patch("src.main._generate_daily_report_in_background") as bg_mock:
            resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ready"
        assert data["report"]["report_text"] == "Cached report"
        bg_mock.assert_not_called()
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
        assert body["payload"]["draft_state"] == "proposed_manual_send"
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
            execute_resp = client.post(
                f"/api/actions/{action_id}/execute", headers=AUTH
            )

        assert execute_resp.status_code == 200, execute_resp.text
        assert execute_resp.json()["status"] == "executed"
        event_types = {
            e.event_type
            for e in db.query(DecisionEvent)
            .filter(DecisionEvent.email_id == email.id)
            .all()
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
        assert reject_resp.json()["status"] == "rejected"
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
            json={
                "email_id": email.id,
                "action_type": "not_a_real_action",
                "safe_mode": True,
            },
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_queue_endpoint_validates_malformed_reply_draft_payload():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m8b@example.com")
        client = _mk_client(db)
        resp = client.post(
            "/api/reports/daily/suggested-actions",
            headers=AUTH,
            json={
                "email_id": email.id,
                "action_type": "reply_draft",
                "payload": {"draft_text": ""},
                "safe_mode": True,
            },
        )
        assert resp.status_code == 400
        assert "draft_text" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_duplicate_report_suggestion_returns_conflict_and_does_not_duplicate_row():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m9@example.com")
        client = _mk_client(db)
        payload = {
            "email_id": email.id,
            "thread_id": email.thread_id,
            "action_type": "mark_read",
            "safe_mode": True,
        }
        first = client.post(
            "/api/reports/daily/suggested-actions", headers=AUTH, json=payload
        )
        assert first.status_code == 200
        second = client.post(
            "/api/reports/daily/suggested-actions", headers=AUTH, json=payload
        )
        assert second.status_code == 409
        assert "already queued" in second.json()["detail"]
        assert (
            db.query(ActionQueue).filter(ActionQueue.email_id == email.id).count() == 1
        )
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_daily_report_reflects_existing_queue_state_for_suggestions():
    db = _make_session()
    try:
        email = _create_email(
            db,
            message_id="m10@example.com",
            action_required=True,
            is_resolved=False,
        )
        action = ActionQueue(
            email_id=email.id,
            thread_id=email.thread_id,
            action_type="mark_resolved",
            payload={"source": "daily_report_suggestion"},
            status="approved",
        )
        db.add(action)
        db.commit()
        payload = {
            "generated_at": datetime.utcnow().isoformat(),
            "period_hours": 24,
            "totals": {
                "total_processed": 1,
                "action_required": 1,
                "unresolved": 1,
                "spam_detected": 0,
            },
            "total_processed": 1,
            "action_required": 1,
            "spam_detected": 0,
            "unresolved": 1,
            "important_items": [],
            "action_items": [],
            "unresolved_items": [],
            "spam_items": [],
            "suggested_actions": [
                {
                    "email_id": email.id,
                    "thread_id": email.thread_id,
                    "action_type": "mark_resolved",
                    "payload": {"reason": "daily_report_unresolved"},
                    "description": "Als erledigt markieren",
                    "safe_mode": True,
                    "queue_status": "approved",
                    "queue_action_id": action.id,
                }
            ],
            "report_text": "Report",
        }
        db.add(
            DailyReport(
                generated_at=datetime.utcnow(),
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                report_json=payload,
                report_text=payload["report_text"],
                generation_status="ready",
            )
        )
        db.commit()

        client = _mk_client(db)
        resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        report = data["report"]
        resolved = next(
            a
            for a in report["suggested_actions"]
            if a["email_id"] == email.id and a["action_type"] == "mark_resolved"
        )
        assert resolved["queue_status"] == "approved"
        assert resolved["queue_action_id"] == action.id
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_report_event_endpoint_records_preview_and_open_events():
    db = _make_session()
    try:
        email = _create_email(db, message_id="m11@example.com", action_required=True)
        client = _mk_client(db)
        preview_resp = client.post(
            "/api/reports/daily/events",
            headers=AUTH,
            json={
                "event_type": "preview_reply_draft",
                "email_id": email.id,
                "thread_id": email.thread_id,
                "source": "report_suggestion",
            },
        )
        assert preview_resp.status_code == 200
        open_resp = client.post(
            "/api/reports/daily/events",
            headers=AUTH,
            json={
                "event_type": "open_related_email_from_report",
                "email_id": email.id,
                "thread_id": email.thread_id,
                "source": "report_suggestion",
            },
        )
        assert open_resp.status_code == 200

        events = (
            db.query(DecisionEvent)
            .filter(DecisionEvent.email_id == email.id)
            .order_by(DecisionEvent.created_at.asc())
            .all()
        )
        assert [e.event_type for e in events][-2:] == [
            "preview_reply_draft",
            "open_related_email_from_report",
        ]
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_actions_endpoint_includes_thread_state_and_summary_fields():
    db = _make_session()
    try:
        email = _create_email(
            db,
            message_id="m-thread-context@example.com",
            thread_id="thread-context-1",
            summary="Bitte Termin bestätigen.",
            sender="patient@example.com",
            action_required=True,
        )
        db.add(
            ActionQueue(
                email_id=email.id,
                thread_id=email.thread_id,
                action_type="mark_read",
                payload={"source": "daily_report_suggestion"},
                status="approved",
            )
        )
        db.add(
            ActionQueue(
                email_id=email.id,
                thread_id=None,
                action_type="mark_read",
                payload={"source": "daily_report_suggestion"},
                status="proposed",
            )
        )
        db.commit()

        client = _mk_client(db)
        resp = client.get("/api/actions", headers=AUTH)
        assert resp.status_code == 200
        actions = resp.json()
        assert len(actions) == 2
        for action in actions:
            assert "thread_state" in action
            assert action["thread_state"] in VALID_THREAD_STATES
            assert "thread_summary" in action
            assert isinstance(action["thread_summary"], dict)
            assert {"latest_subject", "last_sender", "summary"} <= set(
                action["thread_summary"].keys()
            )
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_daily_report_includes_thread_state_in_items_and_handles_missing_thread_data():
    db = _make_session()
    try:
        _create_email(
            db,
            message_id="m-thread-report-1@example.com",
            thread_id="thread-report-1",
            action_required=True,
            is_resolved=False,
            priority="HIGH",
            summary="Rückruf dringend benötigt.",
            sender="patient@example.com",
        )
        _create_email(
            db,
            message_id="m-thread-report-2@example.com",
            thread_id=None,
            action_required=False,
            is_resolved=False,
            priority="LOW",
            summary="FYI",
            sender="info@example.com",
        )

        from src.main import _build_daily_report_response

        with patch("src.main.AIService.generate_report", return_value="Report"):
            report = _build_daily_report_response(
                db,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
            )
        payload = report.model_dump()
        assert payload["action_items"], "expected at least one action item"
        assert all("thread_state" in item for item in payload["action_items"])
        assert all(
            item["thread_state"] in VALID_THREAD_STATES
            for item in payload["action_items"]
        )
        assert all("thread_state" in item for item in payload["important_items"])

        db.add(
            DailyReport(
                generated_at=datetime.utcnow(),
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                report_json=payload,
                report_text=payload["report_text"],
                generation_status="ready",
            )
        )
        db.commit()

        client = _mk_client(db)
        resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        report_json = body["report"]
        assert all("thread_state" in item for item in report_json["action_items"])
    finally:
        app.dependency_overrides.clear()
        db.close()
