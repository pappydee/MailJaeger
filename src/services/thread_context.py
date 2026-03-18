"""Thread-level state inference and lightweight summary helpers."""

from datetime import datetime
from typing import Dict, Optional

from sqlalchemy.orm import Session

from src.models.database import ProcessedEmail
from src.services.thread_aggregator import (
    build_thread_context,
    infer_thread_state_from_emails,
    query_open_action_count,
)
from src.services.thread_summary_service import ThreadSummaryService


THREAD_STATES = {
    "open",
    "waiting_for_me",
    "waiting_for_other",
    "in_conversation",
    "resolved",
    "informational",
    "auto_generated",
}


def normalize_thread_state(value: Optional[str]) -> str:
    state = (value or "").strip().lower()
    return state if state in THREAD_STATES else "informational"


def infer_thread_state(
    *,
    has_action_required: bool,
    last_sender_is_user: bool,
    has_resolved: bool,
    open_actions_count: int,
) -> str:
    """Backward-compatible lightweight inference wrapper for unit tests."""
    synthetic_emails = [
        ProcessedEmail(
            message_id="synthetic-thread",
            sender="me@example.com" if last_sender_is_user else "other@example.com",
            action_required=has_action_required,
            is_resolved=has_resolved,
            date=datetime.utcnow(),
        )
    ]
    return infer_thread_state_from_emails(
        emails=synthetic_emails,
        user_address="me@example.com",
        open_actions_count=open_actions_count,
    )


def update_thread_state_for_thread(
    db: Session,
    *,
    thread_id: Optional[str],
    user_address: Optional[str],
) -> str:
    """Infer and persist one thread_state value across all emails in a thread."""
    if not thread_id:
        return "informational"

    emails = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.thread_id == thread_id)
        .order_by(
            ProcessedEmail.date.desc(),
            ProcessedEmail.processed_at.desc(),
            ProcessedEmail.created_at.desc(),
            ProcessedEmail.id.desc(),
        )
        .all()
    )
    if not emails:
        return "informational"

    open_actions_count = query_open_action_count(db, thread_id=thread_id)
    context = build_thread_context(
        thread_id=thread_id,
        emails=emails,
        user_address=user_address,
        open_actions_count=open_actions_count,
    )
    for email in emails:
        email.thread_state = context.thread_state
        email.thread_priority = context.thread_priority
        email.thread_importance_score = context.thread_importance_score
    db.flush()
    return context.thread_state


def get_thread_summary(
    db: Session,
    *,
    thread_id: Optional[str],
) -> Optional[Dict[str, Optional[str]]]:
    """Return cached thread summary plus latest metadata."""
    if not thread_id:
        return None

    emails = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.thread_id == thread_id)
        .order_by(
            ProcessedEmail.date.desc(),
            ProcessedEmail.processed_at.desc(),
            ProcessedEmail.created_at.desc(),
            ProcessedEmail.id.desc(),
        )
        .limit(10)
        .all()
    )
    if not emails:
        return None
    latest = emails[0]
    summary_service = ThreadSummaryService()
    context = build_thread_context(
        thread_id=thread_id,
        emails=emails,
        user_address=None,
        open_actions_count=query_open_action_count(db, thread_id=thread_id),
    )
    cached = summary_service.get_or_generate_summary(
        db,
        thread_id=thread_id,
        emails=emails,
        thread_state=context.thread_state,
        allow_generate=False,
    )
    summary_text = (
        (cached or {}).get("summary") or (latest.summary or latest.snippet or "")
    ).strip()
    if summary_text:
        summary_text = summary_text[:200]
    return {
        "latest_subject": latest.subject,
        "last_sender": latest.sender,
        "key_topic": (cached or {}).get("key_topic") if cached else None,
        "status": (cached or {}).get("status") if cached else None,
        "summary": summary_text or None,
    }
