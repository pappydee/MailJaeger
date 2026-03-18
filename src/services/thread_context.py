"""Thread-level state inference and lightweight summary helpers."""

from typing import Dict, Optional

from sqlalchemy.orm import Session

from src.models.database import ActionQueue, ProcessedEmail


THREAD_STATES = {
    "open",
    "waiting_for_me",
    "waiting_for_other",
    "resolved",
    "informational",
}


def normalize_thread_state(value: Optional[str]) -> str:
    state = (value or "").strip().lower()
    return state if state in THREAD_STATES else "informational"


def _sender_is_user(sender: Optional[str], user_address: Optional[str]) -> bool:
    if not sender or not user_address:
        return False
    sender_l = sender.lower()
    user_l = user_address.lower()
    if user_l in sender_l:
        return True
    user_local = user_l.split("@", 1)[0]
    return bool(user_local and user_local in sender_l)


def infer_thread_state(
    *,
    has_action_required: bool,
    last_sender_is_user: bool,
    has_resolved: bool,
    open_actions_count: int,
) -> str:
    """Infer thread state using the lightweight rule-set."""
    if has_action_required:
        return "waiting_for_me"
    if last_sender_is_user:
        return "waiting_for_other"
    if has_resolved or open_actions_count == 0:
        return "resolved"
    return "informational"


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

    has_action_required = any(bool(email.action_required) for email in emails)
    has_resolved = any(bool(email.is_resolved) for email in emails)
    last_sender_is_user = _sender_is_user(emails[0].sender, user_address)
    open_actions_count = (
        db.query(ActionQueue)
        .filter(
            ActionQueue.thread_id == thread_id,
            ActionQueue.status.in_(
                ("proposed", "proposed_action", "approved", "approved_action")
            ),
        )
        .count()
    )
    new_state = infer_thread_state(
        has_action_required=has_action_required,
        last_sender_is_user=last_sender_is_user,
        has_resolved=has_resolved,
        open_actions_count=open_actions_count,
    )
    for email in emails:
        email.thread_state = new_state
    db.flush()
    return new_state


def get_thread_summary(
    db: Session,
    *,
    thread_id: Optional[str],
) -> Optional[Dict[str, Optional[str]]]:
    """Return lightweight thread summary from the latest email in a thread."""
    if not thread_id:
        return None
    latest = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.thread_id == thread_id)
        .order_by(
            ProcessedEmail.date.desc(),
            ProcessedEmail.processed_at.desc(),
            ProcessedEmail.created_at.desc(),
            ProcessedEmail.id.desc(),
        )
        .first()
    )
    if not latest:
        return None
    summary_text = (latest.summary or latest.snippet or "").strip()
    if summary_text:
        summary_text = summary_text[:200]
    return {
        "latest_subject": latest.subject,
        "last_sender": latest.sender,
        "summary": summary_text or None,
    }
