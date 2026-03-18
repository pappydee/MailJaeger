"""Thread-level aggregation, scoring and prioritization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Iterable, List, Optional, Set, Tuple

from src.models.database import ActionQueue, ProcessedEmail


class ThreadPriority(str, Enum):
    urgent = "urgent"
    high = "high"
    normal = "normal"
    low = "low"


@dataclass
class ThreadContext:
    thread_id: str
    thread_state: str
    thread_priority: str
    thread_importance_score: float
    thread_last_activity_at: Optional[datetime]
    participants: List[str]
    message_count: int
    has_unread: bool
    has_action_required: bool
    has_user_reply_pending: bool
    has_recent_activity: bool


_NEWSLETTER_HINTS = (
    "newsletter",
    "unsubscribe",
    "abmelden",
    "digest",
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "list-",
    "promo",
    "marketing",
    "angebot",
    "sale",
)


def _sender_is_user(sender: Optional[str], user_address: Optional[str]) -> bool:
    if not sender or not user_address:
        return False
    sender_l = sender.lower()
    user_l = user_address.lower()
    if user_l in sender_l:
        return True
    user_local = user_l.split("@", 1)[0]
    return bool(user_local and user_local in sender_l)


def _is_newsletter_like(email: ProcessedEmail) -> bool:
    sender = (email.sender or "").lower()
    subject = (email.subject or "").lower()
    category = (email.category or "").lower()
    haystack = f"{sender} {subject} {category}"
    if any(marker in haystack for marker in _NEWSLETTER_HINTS):
        return True
    return bool(email.is_spam)


def _is_seen_flags(flags) -> Optional[bool]:
    if not isinstance(flags, list):
        return None
    lowered = {str(flag).lower() for flag in flags}
    if "\\seen" in lowered or "seen" in lowered:
        return True
    return False


def infer_thread_state_from_emails(
    *,
    emails: Iterable[ProcessedEmail],
    user_address: Optional[str],
    open_actions_count: int = 0,
    now: Optional[datetime] = None,
    recent_hours: int = 48,
) -> str:
    ordered = list(emails)
    if not ordered:
        return "informational"

    current_time = now or datetime.utcnow()
    latest = ordered[0]
    has_action_required = any(bool(email.action_required) for email in ordered)
    if has_action_required:
        return "waiting_for_me"

    last_sender_is_user = _sender_is_user(latest.sender, user_address)
    if last_sender_is_user:
        return "waiting_for_other"

    sender_sequence: List[str] = []
    for email in ordered[:6]:
        sender_sequence.append(
            "user" if _sender_is_user(email.sender, user_address) else "other"
        )
    if len(sender_sequence) >= 3 and len(set(sender_sequence)) > 1:
        alternating = all(
            sender_sequence[idx] != sender_sequence[idx + 1]
            for idx in range(len(sender_sequence) - 1)
        )
        if alternating:
            return "in_conversation"

    has_resolved = any(bool(email.is_resolved) for email in ordered)
    latest_dt = latest.date or latest.processed_at or latest.created_at
    has_recent_activity = bool(
        latest_dt and latest_dt >= current_time - timedelta(hours=recent_hours)
    )
    if has_resolved or (not has_recent_activity and open_actions_count == 0):
        return "resolved"

    if all(_is_newsletter_like(email) for email in ordered):
        return "auto_generated"

    return "informational"


def compute_thread_importance_score(
    *,
    emails: Iterable[ProcessedEmail],
    user_address: Optional[str],
    has_action_required: bool,
    has_recent_activity: bool,
    known_important_sender_resolver: Optional[Callable[[str], bool]] = None,
) -> float:
    ordered = list(emails)
    if not ordered:
        return 0.0

    score = 10.0
    if has_action_required:
        score += 35.0
    if has_recent_activity:
        score += 15.0

    senders = [email.sender for email in ordered if email.sender]
    user_involved = any(_sender_is_user(sender, user_address) for sender in senders)
    if user_involved:
        score += 10.0

    score += min(15.0, float(len(ordered)) * 2.0)

    if known_important_sender_resolver:
        important_sender = any(
            known_important_sender_resolver(sender or "") for sender in senders
        )
        if important_sender:
            score += 10.0

    spam_penalty = 20.0 if any(bool(email.is_spam) for email in ordered) else 0.0
    avg_spam_probability = (
        sum(float(email.spam_probability or 0.0) for email in ordered) / len(ordered)
    )
    if avg_spam_probability >= 0.75:
        spam_penalty = max(spam_penalty, 20.0)
    score -= spam_penalty

    if all(_is_newsletter_like(email) for email in ordered):
        score -= 30.0

    return max(0.0, min(100.0, score))


def derive_thread_priority(importance_score: float) -> ThreadPriority:
    if importance_score >= 80:
        return ThreadPriority.urgent
    if importance_score >= 60:
        return ThreadPriority.high
    if importance_score >= 35:
        return ThreadPriority.normal
    return ThreadPriority.low


def build_thread_context(
    *,
    thread_id: str,
    emails: Iterable[ProcessedEmail],
    user_address: Optional[str],
    open_actions_count: int = 0,
    now: Optional[datetime] = None,
) -> ThreadContext:
    ordered = list(emails)
    ordered.sort(
        key=lambda email: (
            email.date or datetime.min,
            email.processed_at or datetime.min,
            email.created_at or datetime.min,
            email.id or 0,
        ),
        reverse=True,
    )

    latest = ordered[0] if ordered else None
    last_activity = (
        latest.date or latest.processed_at or latest.created_at if latest else None
    )
    has_recent_activity = bool(
        last_activity and last_activity >= (now or datetime.utcnow()) - timedelta(hours=48)
    )
    has_action_required = any(bool(email.action_required) for email in ordered)
    state = infer_thread_state_from_emails(
        emails=ordered,
        user_address=user_address,
        open_actions_count=open_actions_count,
        now=now,
    )
    importance = compute_thread_importance_score(
        emails=ordered,
        user_address=user_address,
        has_action_required=has_action_required,
        has_recent_activity=has_recent_activity,
    )
    priority = derive_thread_priority(importance)
    participants: Set[str] = {email.sender for email in ordered if email.sender}
    has_unread = any(_is_seen_flags(email.flags) is False for email in ordered)
    return ThreadContext(
        thread_id=thread_id,
        thread_state=state,
        thread_priority=priority.value,
        thread_importance_score=importance,
        thread_last_activity_at=last_activity,
        participants=sorted(participants),
        message_count=len(ordered),
        has_unread=has_unread,
        has_action_required=has_action_required,
        has_user_reply_pending=state == "waiting_for_other",
        has_recent_activity=has_recent_activity,
    )


def thread_sort_key(context: Optional[ThreadContext]) -> Tuple[int, float, float]:
    if context is None:
        return (2, 0.0, 0.0)
    waiting_rank = 0 if context.thread_state == "waiting_for_me" else 1
    activity_ts = (
        context.thread_last_activity_at.timestamp()
        if context.thread_last_activity_at
        else 0.0
    )
    return (waiting_rank, float(context.thread_importance_score), activity_ts)


def query_open_action_count(db, *, thread_id: Optional[str]) -> int:
    if not thread_id:
        return 0
    return (
        db.query(ActionQueue)
        .filter(
            ActionQueue.thread_id == thread_id,
            ActionQueue.status.in_(
                ("proposed", "proposed_action", "approved", "approved_action")
            ),
        )
        .count()
    )
