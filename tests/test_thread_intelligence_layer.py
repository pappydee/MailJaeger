from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.connection import get_db as _get_db
from src.main import app, _build_daily_report_response
from src.models.database import ActionQueue, Base, ProcessedEmail
from src.services.thread_aggregator import (
    build_thread_context,
    infer_thread_state_from_emails,
    thread_sort_key,
)
from src.services.thread_summary_service import ThreadSummaryService


AUTH = {"Authorization": "Bearer test_key_abc123"}


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _mk_email(
    *,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str = "Subject",
    summary: str = "Summary",
    action_required: bool = False,
    is_resolved: bool = False,
    is_spam: bool = False,
    spam_probability: float = 0.0,
    priority: str | None = None,
    date: datetime | None = None,
    processed_at: datetime | None = None,
    flags=None,
):
    return ProcessedEmail(
        message_id=message_id,
        uid=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        summary=summary,
        action_required=action_required,
        is_resolved=is_resolved,
        is_spam=is_spam,
        spam_probability=spam_probability,
        priority=priority,
        is_processed=True,
        date=date,
        processed_at=processed_at or datetime.now(timezone.utc),
        flags=flags,
    )


def _mk_client(db_session):
    app.dependency_overrides[_get_db] = lambda: db_session
    return TestClient(app)


def test_thread_state_inference_all_branches():
    now = datetime.utcnow()
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(
                    message_id="a1",
                    thread_id="t1",
                    sender="x@example.com",
                    action_required=True,
                )
            ],
            user_address="me@example.com",
        )
        == "waiting_for_me"
    )
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(
                    message_id="a2",
                    thread_id="t2",
                    sender="me@example.com",
                    action_required=False,
                )
            ],
            user_address="me@example.com",
        )
        == "waiting_for_other"
    )
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(message_id="a3", thread_id="t3", sender="other@example.com"),
                _mk_email(message_id="a4", thread_id="t3", sender="me@example.com"),
                _mk_email(message_id="a5", thread_id="t3", sender="other@example.com"),
            ],
            user_address="me@example.com",
        )
        == "in_conversation"
    )
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(
                    message_id="a6",
                    thread_id="t4",
                    sender="other@example.com",
                    date=now - timedelta(days=4),
                )
            ],
            user_address="me@example.com",
            open_actions_count=0,
            now=now,
        )
        == "resolved"
    )
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(
                    message_id="a7",
                    thread_id="t5",
                    sender="noreply@example.com",
                    subject="Newsletter Digest",
                    date=now - timedelta(hours=1),
                )
            ],
            user_address="me@example.com",
            open_actions_count=1,
            now=now,
        )
        == "auto_generated"
    )
    assert (
        infer_thread_state_from_emails(
            emails=[
                _mk_email(
                    message_id="a8",
                    thread_id="t6",
                    sender="colleague@example.com",
                    date=now - timedelta(hours=2),
                )
            ],
            user_address="me@example.com",
            open_actions_count=1,
            now=now,
        )
        == "informational"
    )


def test_importance_priority_and_sort_key():
    urgent_context = build_thread_context(
        thread_id="urgent-thread",
        emails=[
            _mk_email(
                message_id="u1",
                thread_id="urgent-thread",
                sender="boss@example.com",
                action_required=True,
                date=datetime.utcnow(),
                flags=[],
            ),
            _mk_email(
                message_id="u2",
                thread_id="urgent-thread",
                sender="me@example.com",
                date=datetime.utcnow() - timedelta(hours=1),
            ),
        ],
        user_address="me@example.com",
    )
    low_context = build_thread_context(
        thread_id="low-thread",
        emails=[
            _mk_email(
                message_id="l1",
                thread_id="low-thread",
                sender="noreply@example.com",
                subject="Newsletter",
                date=datetime.utcnow() - timedelta(days=5),
                is_spam=True,
                spam_probability=0.99,
            )
        ],
        user_address="me@example.com",
    )
    assert urgent_context.thread_importance_score > low_context.thread_importance_score
    assert urgent_context.thread_priority in {"urgent", "high"}
    assert low_context.thread_priority == "low"
    assert urgent_context.has_unread is True
    assert thread_sort_key(urgent_context) < thread_sort_key(low_context)


def test_thread_summary_regeneration_only_on_change():
    db = _session()
    try:
        now = datetime.utcnow()
        emails = [
            _mk_email(
                message_id="s1",
                thread_id="summary-thread",
                sender="a@example.com",
                summary="Initial summary",
                date=now,
            )
        ]
        db.add_all(emails)
        db.commit()

        ai_mock = Mock()
        ai_mock.generate_report.return_value = (
            "Summary: Kurz.\nTopic: Termin.\nStatus: You need to act."
        )
        service = ThreadSummaryService(ai_service=ai_mock)

        first = service.get_or_generate_summary(
            db,
            thread_id="summary-thread",
            emails=emails,
            thread_state="waiting_for_me",
            allow_generate=True,
        )
        second = service.get_or_generate_summary(
            db,
            thread_id="summary-thread",
            emails=emails,
            thread_state="waiting_for_me",
            allow_generate=True,
        )
        assert first["signature"] == second["signature"]
        assert ai_mock.generate_report.call_count == 1

        newer = _mk_email(
            message_id="s2",
            thread_id="summary-thread",
            sender="b@example.com",
            summary="Neue Antwort",
            date=now + timedelta(minutes=1),
        )
        db.add(newer)
        db.commit()
        emails = [newer] + emails
        third = service.get_or_generate_summary(
            db,
            thread_id="summary-thread",
            emails=emails,
            thread_state="in_conversation",
            allow_generate=True,
        )
        assert third["signature"] != first["signature"]
        assert ai_mock.generate_report.call_count == 2
    finally:
        db.close()


def test_actions_api_exposes_thread_intelligence_and_sorting():
    db = _session()
    try:
        waiting_email = _mk_email(
            message_id="api1",
            thread_id="thread-waiting",
            sender="user@example.com",
            action_required=True,
            date=datetime.utcnow(),
        )
        low_email = _mk_email(
            message_id="api2",
            thread_id="thread-low",
            sender="noreply@example.com",
            subject="newsletter digest",
            is_spam=True,
            date=datetime.utcnow() - timedelta(days=2),
        )
        db.add_all([waiting_email, low_email])
        db.flush()
        db.add_all(
            [
                ActionQueue(
                    email_id=waiting_email.id,
                    thread_id=waiting_email.thread_id,
                    action_type="mark_read",
                    payload={"source": "queue_test"},
                    status="proposed",
                ),
                ActionQueue(
                    email_id=low_email.id,
                    thread_id=low_email.thread_id,
                    action_type="mark_read",
                    payload={"source": "queue_test"},
                    status="proposed",
                ),
            ]
        )
        db.commit()
        client = _mk_client(db)
        response = client.get("/api/actions", headers=AUTH)
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 2
        assert payload[0]["thread_state"] == "waiting_for_me"
        for action in payload:
            assert "thread_priority" in action
            assert "thread_importance_score" in action
            assert "thread_last_activity_at" in action
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_daily_report_groups_by_threads_and_keeps_compatibility_fields():
    db = _session()
    try:
        now = datetime.utcnow()
        db.add_all(
            [
                _mk_email(
                    message_id="dr1",
                    thread_id="thread-high",
                    sender="patient@example.com",
                    action_required=True,
                    priority="HIGH",
                    date=now,
                ),
                _mk_email(
                    message_id="dr2",
                    thread_id="thread-high",
                    sender="me@example.com",
                    action_required=False,
                    date=now - timedelta(minutes=2),
                ),
                _mk_email(
                    message_id="dr3",
                    thread_id="thread-low",
                    sender="noreply@example.com",
                    subject="newsletter digest",
                    is_spam=True,
                    date=now - timedelta(hours=6),
                ),
            ]
        )
        db.commit()

        with patch(
            "src.services.ai_service.AIService.generate_report",
            return_value="Summary: Kurz.\nTopic: Thread.\nStatus: Waiting for reply.",
        ):
            report = _build_daily_report_response(
                db,
                period_start=now - timedelta(hours=24),
                period_end=now,
            )

        data = report.model_dump()
        assert "threads" in data
        assert isinstance(data["threads"], list)
        assert len(data["threads"]) >= 2
        assert data["threads"][0]["importance_score"] >= data["threads"][1]["importance_score"]
        assert "important_items" in data
        assert "action_items" in data
        assert "suggested_actions" in data
    finally:
        db.close()


def test_stability_with_missing_data():
    context = build_thread_context(
        thread_id="missing-data",
        emails=[
            ProcessedEmail(
                message_id="missing-1",
                uid="m1",
                thread_id="missing-data",
                sender=None,
                subject=None,
                summary=None,
                date=None,
                processed_at=None,
                created_at=None,
                flags=None,
                action_required=False,
                is_resolved=False,
                is_spam=False,
            )
        ],
        user_address=None,
    )
    assert context.thread_state in {
        "informational",
        "resolved",
        "auto_generated",
        "in_conversation",
        "waiting_for_me",
        "waiting_for_other",
    }
    assert 0.0 <= context.thread_importance_score <= 100.0
