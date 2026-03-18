from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.connection import get_db as _get_db
from src.main import app, _build_daily_report_response
from src.models.database import ActionQueue, AppSetting, Base, DailyReport, ProcessedEmail


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
    uid: str = "501",
    sender: str = "sender@example.com",
    thread_id: str = "thread-x",
    action_required: bool = False,
    is_spam: bool = False,
    is_archived: bool = False,
    category: str = "Klinik",
):
    email = ProcessedEmail(
        message_id=message_id,
        uid=uid,
        sender=sender,
        thread_id=thread_id,
        subject=f"Subject {message_id}",
        summary="summary",
        action_required=action_required,
        is_spam=is_spam,
        is_archived=is_archived,
        category=category,
        is_processed=True,
        processed_at=datetime.utcnow(),
    )
    db_session.add(email)
    db_session.commit()
    db_session.refresh(email)
    return email


def test_stale_cached_daily_report_is_rejected_and_regenerated():
    db = _make_session()
    try:
        stale_payload = {
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
            "action_items": [{"email_id": 1, "thread_id": "t-1", "subject": "old"}],
            "unresolved_items": [],
            "spam_items": [],
            "suggested_actions": [],
            "report_text": "old",
        }
        db.add(
            DailyReport(
                generated_at=datetime.utcnow(),
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                report_json=stale_payload,
                report_text="old",
                generation_status="ready",
            )
        )
        db.commit()
        client = _mk_client(db)
        with patch("src.main._generate_daily_report_in_background") as bg_mock:
            resp = client.get("/api/reports/daily", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        bg_mock.assert_called_once()
        refreshed = db.query(DailyReport).order_by(DailyReport.id.desc()).first()
        assert refreshed.generation_status == "pending"
        assert "stale_or_incompatible" in (refreshed.error_message or "")
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_regenerated_report_contains_populated_thread_intelligence_and_legacy_fields():
    db = _make_session()
    try:
        _create_email(
            db,
            message_id="regen-1@example.com",
            thread_id="thread-regen",
            sender="patient@example.com",
            action_required=True,
        )
        with patch("src.main.AIService.generate_report", return_value="Report"):
            report = _build_daily_report_response(
                db,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
            )
        payload = report.model_dump()
        assert payload["report_version"] == 2
        assert "threads" in payload
        assert "important_items" in payload
        assert "action_items" in payload
        for section in ("important_items", "action_items", "unresolved_items", "spam_items"):
            for item in payload.get(section, []):
                assert item["thread_state"]
                assert item["thread_priority"]
                assert item["thread_importance_score"] is not None
    finally:
        db.close()


def test_get_folders_endpoint_returns_live_imap_folders():
    db = _make_session()
    try:
        client = _mk_client(db)
        with patch("src.main.IMAPService") as imap_cls:
            imap = Mock()
            imap.list_folders.return_value = [
                {
                    "name": "INBOX",
                    "normalized_name": "inbox",
                    "delimiter": "/",
                    "flags": [],
                },
                {
                    "name": "Alles ab Juni 2025",
                    "normalized_name": "alles ab juni 2025",
                    "delimiter": "/",
                    "flags": [],
                },
            ]
            imap_cls.return_value.__enter__.return_value = imap
            response = client.get("/api/folders", headers=AUTH)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "folders" in body
        assert any(f["name"] == "Alles ab Juni 2025" for f in body["folders"])
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_archive_folder_setting_can_be_saved_and_loaded():
    db = _make_session()
    try:
        client = _mk_client(db)
        save = client.post(
            "/api/settings",
            headers=AUTH,
            json={"archive_folder": "Alles ab Juni 2025"},
        )
        assert save.status_code == 200
        assert save.json()["archive_folder"] == "Alles ab Juni 2025"
        load = client.get("/api/settings", headers=AUTH)
        assert load.status_code == 200
        assert load.json()["archive_folder"] == "Alles ab Juni 2025"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_archive_suggestions_use_configured_archive_folder():
    db = _make_session()
    try:
        _create_email(db, message_id="archive-suggest-1@example.com", is_archived=False)
        db.add(AppSetting(key="archive_folder", value="Alles ab Juni 2025"))
        db.commit()
        with patch("src.main.AIService.generate_report", return_value="Report"):
            report = _build_daily_report_response(
                db,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
            )
        archive_suggestions = [
            action for action in report.suggested_actions if action.action_type == "archive"
        ]
        assert archive_suggestions
        assert archive_suggestions[0].payload["target_folder"] == "Alles ab Juni 2025"
    finally:
        db.close()


def test_move_execution_validates_missing_folder_with_explicit_error():
    db = _make_session()
    try:
        email = _create_email(db, message_id="move-error-1@example.com", uid="777")
        action = ActionQueue(
            email_id=email.id,
            thread_id=email.thread_id,
            action_type="move",
            payload={"target_folder": "NichtVorhanden"},
            status="approved",
        )
        db.add(action)
        db.commit()
        client = _mk_client(db)
        with patch("src.main.IMAPService") as imap_cls:
            imap = Mock()
            imap.move_to_folder.return_value = False
            imap.last_error = "Target folder not found on IMAP server: 'NichtVorhanden'"
            imap_cls.return_value.__enter__.return_value = imap
            response = client.post(f"/api/actions/{action.id}/execute", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "NichtVorhanden" in (body["error_message"] or "")
        assert "Target folder not found" in (body["error_message"] or "")
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_learned_folder_preference_is_reused_for_future_archive_suggestions():
    db = _make_session()
    try:
        first_email = _create_email(
            db,
            message_id="learn-1@example.com",
            sender="owner@clinic.example",
            category="Klinik",
            uid="901",
            thread_id="thread-learn-1",
        )
        action = ActionQueue(
            email_id=first_email.id,
            thread_id=first_email.thread_id,
            action_type="move",
            payload={"target_folder": "Alles ab Juni 2025"},
            status="approved",
        )
        db.add(action)
        db.commit()
        client = _mk_client(db)
        with patch("src.main.IMAPService") as imap_cls:
            imap = Mock()
            imap.move_to_folder.return_value = True
            imap_cls.return_value.__enter__.return_value = imap
            executed = client.post(f"/api/actions/{action.id}/execute", headers=AUTH)
        assert executed.status_code == 200
        assert executed.json()["status"] == "executed"

        _create_email(
            db,
            message_id="learn-2@example.com",
            sender="owner@clinic.example",
            category="Klinik",
            thread_id="thread-learn-2",
            is_archived=False,
        )
        with patch("src.main.AIService.generate_report", return_value="Report"):
            report = _build_daily_report_response(
                db,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
            )
        archive_suggestions = [
            action for action in report.suggested_actions if action.action_type == "archive"
        ]
        assert archive_suggestions
        assert archive_suggestions[0].payload["target_folder"] == "Alles ab Juni 2025"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_archive_suggestions_can_use_discovered_folder_cache_without_default_assumption():
    db = _make_session()
    try:
        _create_email(db, message_id="discover-1@example.com", is_archived=False)
        db.query(AppSetting).filter(AppSetting.key == "archive_folder").delete()
        db.add(
            AppSetting(
                key="imap_folders_cache",
                value={
                    "folders": [
                        {
                            "name": "INBOX",
                            "normalized_name": "inbox",
                            "delimiter": "/",
                            "flags": [],
                        },
                        {
                            "name": "Alles ab Juni 2025",
                            "normalized_name": "alles ab juni 2025",
                            "delimiter": "/",
                            "flags": [],
                        },
                    ],
                    "fetched_at": datetime.utcnow().isoformat(),
                },
            )
        )
        db.commit()

        with patch("src.main.AIService.generate_report", return_value="Report"):
            report = _build_daily_report_response(
                db,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
            )
        archive_suggestions = [
            action for action in report.suggested_actions if action.action_type == "archive"
        ]
        assert archive_suggestions
        assert archive_suggestions[0].payload["target_folder"] == "Alles ab Juni 2025"
    finally:
        db.close()
