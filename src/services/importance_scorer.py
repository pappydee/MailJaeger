"""
Importance scoring service — reusable, stateless email prioritization.

Computes a 0–100 importance score for an email based on:
  - Newsletter / bulk-mail penalty
  - Recency bonus
  - Thread participation
  - Urgent-keyword heuristics
  - Sender domain reputation (historical action-required rate)
  - Learned sender-profile behavior (address-level > domain fallback)

Higher score → higher priority → processed first.

This module has no side effects and does not depend on EmailProcessor.
"""

from datetime import datetime, timezone
from typing import Optional, Tuple
from sqlalchemy.orm import Session

from src.models.database import ProcessedEmail, SenderProfile
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Keywords that indicate bulk / newsletter / promotional mail.
BULK_INDICATORS: Tuple[str, ...] = (
    "newsletter",
    "unsubscribe",
    "abmelden",
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "list-unsubscribe",
    "bulk",
    "promo",
    "marketing",
    "digest",
    "weekly",
    "monthly",
    "angebot",
    "rabatt",
    "sale",
    "offer",
    "deal",
)

# Subject keyword heuristics (German + English)
URGENT_KEYWORDS: Tuple[str, ...] = (
    "dringend",
    "urgent",
    "sofort",
    "immediately",
    "frist",
    "deadline",
    "termin",
    "notfall",
    "emergency",
    "bitte antworten",
    "please reply",
    "antwort erforderlich",
    "response required",
)


def compute_importance_score(
    db: Session, email_record: ProcessedEmail
) -> float:
    """
    Compute an importance score in the range 0–100 for a single email.

    This is a pure function (no IMAP side effects).  The DB session is
    used only for sender-domain reputation queries.
    """
    score = 30.0  # neutral baseline

    subject = (email_record.subject or "").lower()
    sender = (email_record.sender or "").lower()

    # Newsletter / bulk mail penalty
    if any(ind in sender for ind in BULK_INDICATORS) or any(
        ind in subject for ind in BULK_INDICATORS
    ):
        score -= 20

    # Recency bonus: emails received in the last 48 h score higher
    try:
        if email_record.received_at or email_record.date:
            ts = email_record.received_at or email_record.date
            age_hours = (datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds() / 3600
            if age_hours <= 24:
                score += 20
            elif age_hours <= 48:
                score += 10
    except Exception:
        pass

    # Thread participation
    if email_record.thread_id:
        score += 10

    # Urgent keyword heuristics
    if any(kw in subject for kw in URGENT_KEYWORDS):
        score += 20

    # Sender domain reputation
    try:
        domain = sender.split("@")[-1] if "@" in sender else ""
        if domain:
            total_from_domain = (
                db.query(ProcessedEmail)
                .filter(
                    ProcessedEmail.sender.ilike(f"%@{domain}"),
                    ProcessedEmail.is_processed == True,  # noqa: E712
                )
                .count()
            )
            if total_from_domain > 0:
                action_from_domain = (
                    db.query(ProcessedEmail)
                    .filter(
                        ProcessedEmail.sender.ilike(f"%@{domain}"),
                        ProcessedEmail.action_required == True,  # noqa: E712
                        ProcessedEmail.is_processed == True,  # noqa: E712
                    )
                    .count()
                )
                action_rate = action_from_domain / total_from_domain
                score += action_rate * 20
    except Exception:
        pass

    # Learned sender-profile behavior (address-level preferred, domain fallback)
    score += _learned_behavior_boost(db, sender)

    return max(0.0, min(100.0, score))


def compute_pending_importance_scores(db: Session) -> None:
    """Compute and persist importance_score for all 'pending' emails that lack one."""
    try:
        unscored = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.analysis_state == "pending",
                ProcessedEmail.importance_score.is_(None),
            )
            .all()
        )
        if not unscored:
            return
        logger.info("Computing importance scores for %s emails", len(unscored))
        for email_record in unscored:
            email_record.importance_score = compute_importance_score(db, email_record)
            db.add(email_record)
        db.commit()
    except Exception as e:
        logger.warning("Failed to compute importance scores: %s", e)


# Minimum historical emails before learned profile influences scoring
_MIN_PROFILE_SUPPORT = 3


def _learned_behavior_boost(db: Session, sender: str) -> float:
    """Compute an importance adjustment from learned SenderProfile data.

    Resolves the best matching profile via the centralized
    ``resolve_sender_profile`` helper (address > domain precedence).

    Signals used (each adds up to a small bounded bonus/penalty):
      - importance_tendency (marked-important rate)
      - kept_in_inbox_count / total_emails (engagement rate)
      - spam_tendency (spam penalty)
      - reply_rate (high reply rate = importance signal)

    Returns a bounded adjustment in the range [-10, +15].
    """
    try:
        from src.services.sender_precedence import resolve_sender_profile

        profile = resolve_sender_profile(db, sender, min_support=_MIN_PROFILE_SUPPORT)
        if not profile:
            return 0.0

        boost = 0.0

        # Importance tendency: +5 if > 10%
        if (profile.importance_tendency or 0) > 0.1:
            boost += 5.0

        # Kept-in-inbox engagement: +5 if > 50%
        total = profile.total_emails or 1
        inbox_rate = (profile.kept_in_inbox_count or 0) / total
        if inbox_rate > 0.5:
            boost += 5.0

        # Reply rate: +5 if > 50%
        if (profile.reply_rate or 0) > 0.5:
            boost += 5.0

        # Spam tendency penalty: -10 if > 50%
        if (profile.spam_tendency or 0) > 0.5:
            boost -= 10.0

        return max(-10.0, min(15.0, boost))
    except Exception:
        return 0.0
