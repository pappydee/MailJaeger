"""
Pipeline: Learning Phase (skeleton)

Responsibilities:
  - Consume DecisionEvents for structured learning signals
  - Aggregate classification context per sender/domain
  - Provide hooks for future ML integration
  - No actual ML — structured logging + signal aggregation only

Entry points:
  - ``record_classification_context(db, email, analysis, source)``
  - ``record_user_feedback(db, email_id, event_type, old_value, new_value)``
  - ``aggregate_sender_stats(db, sender_domain)``
  - ``get_learning_summary(db)``

This module is the foundation for a real learning system.
Decision events + classification context form the training data.
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import DecisionEvent, ProcessedEmail, LearningSignal
from src.utils.logging import get_logger

logger = get_logger(__name__)


def record_classification_context(
    db: Session,
    email: ProcessedEmail,
    analysis: Dict[str, Any],
    source: str,
) -> None:
    """
    Store classification context alongside a DecisionEvent.

    Called after every email classification to build training data
    for future learning. The context captures the input features
    (sender domain, subject keywords, thread participation) alongside
    the classification output.
    """
    try:
        sender = (email.sender or "").lower()
        domain = sender.split("@")[-1] if "@" in sender else ""

        event = DecisionEvent(
            email_id=email.id,
            thread_id=email.thread_id,
            event_type="classification",
            source=source,
            new_value=analysis.get("category"),
            confidence=analysis.get("spam_probability", 0.5),
            model_version=analysis.get("analysis_version", "1.0.0"),
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        logger.debug(
            "learning_context_recorded email_id=%s source=%s category=%s domain=%s",
            email.id,
            source,
            analysis.get("category"),
            domain,
        )
    except Exception as e:
        logger.warning("Failed to record classification context: %s", e)


def record_user_feedback(
    db: Session,
    email_id: int,
    event_type: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    user_confirmed: bool = True,
) -> Optional[DecisionEvent]:
    """
    Record a user feedback event (approve, reject, reclassify).

    This is the primary learning signal — user corrections teach
    the system what it got wrong.
    """
    try:
        event = DecisionEvent(
            email_id=email_id,
            event_type=event_type,
            source="user",
            old_value=old_value,
            new_value=new_value,
            confidence=1.0 if user_confirmed else 0.5,
            user_confirmed=user_confirmed,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.flush()
        logger.info(
            "learning_user_feedback email_id=%s event=%s old=%s new=%s confirmed=%s",
            email_id,
            event_type,
            old_value,
            new_value,
            user_confirmed,
        )
        return event
    except Exception as e:
        logger.warning("Failed to record user feedback: %s", e)
        return None


def aggregate_sender_stats(db: Session, sender_domain: str) -> Dict[str, Any]:
    """
    Aggregate classification stats for a sender domain.

    Returns a summary of how emails from this domain have been classified,
    useful for building sender profiles in a future learning iteration.
    """
    try:
        domain_filter = ProcessedEmail.sender.ilike(f"%@{sender_domain}")
        total = (
            db.query(func.count(ProcessedEmail.id))
            .filter(domain_filter, ProcessedEmail.is_processed.is_(True))
            .scalar()
            or 0
        )
        if total == 0:
            return {"domain": sender_domain, "total": 0}

        spam_count = (
            db.query(func.count(ProcessedEmail.id))
            .filter(domain_filter, ProcessedEmail.is_spam.is_(True))
            .scalar()
            or 0
        )
        action_count = (
            db.query(func.count(ProcessedEmail.id))
            .filter(domain_filter, ProcessedEmail.action_required.is_(True))
            .scalar()
            or 0
        )

        # Category distribution
        categories = (
            db.query(ProcessedEmail.category, func.count(ProcessedEmail.id))
            .filter(domain_filter, ProcessedEmail.is_processed.is_(True))
            .group_by(ProcessedEmail.category)
            .all()
        )

        # User correction rate
        corrections = (
            db.query(func.count(DecisionEvent.id))
            .join(
                ProcessedEmail, DecisionEvent.email_id == ProcessedEmail.id
            )
            .filter(
                domain_filter,
                DecisionEvent.source == "user",
            )
            .scalar()
            or 0
        )

        return {
            "domain": sender_domain,
            "total": total,
            "spam_rate": spam_count / total if total else 0.0,
            "action_rate": action_count / total if total else 0.0,
            "correction_rate": corrections / total if total else 0.0,
            "categories": {cat: cnt for cat, cnt in categories if cat},
        }
    except Exception as e:
        logger.warning("Failed to aggregate sender stats for %s: %s", sender_domain, e)
        return {"domain": sender_domain, "total": 0}


def get_learning_summary(db: Session) -> Dict[str, Any]:
    """
    Return a summary of the learning state: event counts, signal counts,
    top corrected domains.

    Used for monitoring learning readiness and data quality.
    """
    try:
        total_events = db.query(func.count(DecisionEvent.id)).scalar() or 0
        user_events = (
            db.query(func.count(DecisionEvent.id))
            .filter(DecisionEvent.source == "user")
            .scalar()
            or 0
        )
        system_events = total_events - user_events

        total_signals = db.query(func.count(LearningSignal.id)).scalar() or 0
        recent_signals = (
            db.query(func.count(LearningSignal.id))
            .filter(
                LearningSignal.detected_at
                >= datetime.now(timezone.utc) - timedelta(days=30)
            )
            .scalar()
            or 0
        )

        # Top corrected domains (learning opportunities)
        corrections_by_domain = (
            db.query(
                ProcessedEmail.sender,
                func.count(DecisionEvent.id).label("correction_count"),
            )
            .join(
                DecisionEvent, DecisionEvent.email_id == ProcessedEmail.id
            )
            .filter(DecisionEvent.source == "user")
            .group_by(ProcessedEmail.sender)
            .order_by(func.count(DecisionEvent.id).desc())
            .limit(10)
            .all()
        )

        return {
            "total_decision_events": total_events,
            "user_feedback_events": user_events,
            "system_decision_events": system_events,
            "total_learning_signals": total_signals,
            "recent_signals_30d": recent_signals,
            "top_corrected_senders": [
                {"sender": s, "corrections": c}
                for s, c in corrections_by_domain
            ],
            "learning_ready": user_events >= 10,  # minimum threshold for meaningful learning
        }
    except Exception as e:
        logger.warning("Failed to get learning summary: %s", e)
        return {"total_decision_events": 0, "learning_ready": False}


# =====================================================================
# Override re-application — deterministic reclassification
# =====================================================================


def apply_override_to_matching_emails(
    db: Session,
    rule_id: int,
    *,
    limit: int = 500,
) -> Dict[str, Any]:
    """
    Re-apply a ClassificationOverride rule to all *pending* or previously
    classified emails that match its sender/subject pattern but were not
    yet classified by this rule.

    This enables simple deterministic learning: when a user creates an
    override, the system retroactively applies it without ML.

    Only emails that have NOT been manually overridden (``overridden=False``)
    are affected, to respect user decisions.

    Returns: {matched, updated, skipped}
    """
    from src.models.database import ClassificationOverride

    rule = db.query(ClassificationOverride).filter(ClassificationOverride.id == rule_id).first()
    if not rule:
        logger.warning("Override rule %s not found", rule_id)
        return {"matched": 0, "updated": 0, "skipped": 0}

    # Build candidate query
    query = db.query(ProcessedEmail).filter(
        ProcessedEmail.overridden.isnot(True),
    )

    # Apply sender pattern filter
    if rule.sender_pattern:
        pattern = rule.sender_pattern.lower()
        if not pattern.startswith("@"):
            pattern = "@" + pattern
        query = query.filter(ProcessedEmail.sender.ilike(f"%{pattern}"))

    # Apply subject pattern filter
    if rule.subject_pattern:
        query = query.filter(
            ProcessedEmail.subject.ilike(f"%{rule.subject_pattern}%")
        )

    # Exclude emails already classified by this exact rule
    query = query.filter(
        (ProcessedEmail.override_rule_id != rule.id)
        | (ProcessedEmail.override_rule_id.is_(None))
    )

    candidates = query.limit(limit).all()

    if not candidates:
        return {"matched": 0, "updated": 0, "skipped": 0}

    updated = 0
    skipped = 0
    for email in candidates:
        # Double-check subject match if both patterns are set
        if rule.subject_pattern and rule.sender_pattern:
            subject = (email.subject or "").lower()
            if rule.subject_pattern.lower() not in subject:
                skipped += 1
                continue

        # Apply the override values
        if rule.category is not None:
            email.category = rule.category
        if rule.priority is not None:
            email.priority = rule.priority
        if rule.spam is not None:
            email.is_spam = rule.spam
            email.spam_probability = 0.95 if rule.spam else 0.05
        if rule.action_required is not None:
            email.action_required = rule.action_required
        if rule.suggested_folder is not None:
            email.suggested_folder = rule.suggested_folder

        email.override_rule_id = rule.id
        email.reasoning = f"Re-applied override rule ID={rule.id}"

        # Record a decision event for audit
        try:
            event = DecisionEvent(
                email_id=email.id,
                thread_id=email.thread_id,
                event_type="override_reapplication",
                source=f"override_rule:{rule.id}",
                new_value=rule.category,
                confidence=0.95,
                user_confirmed=False,
                created_at=datetime.now(timezone.utc),
            )
            db.add(event)
        except Exception:
            pass  # decision recording must not break the re-application

        db.add(email)
        updated += 1

    db.commit()
    logger.info(
        "override_reapplication rule_id=%s matched=%s updated=%s skipped=%s",
        rule_id,
        len(candidates),
        updated,
        skipped,
    )
    return {"matched": len(candidates), "updated": updated, "skipped": skipped}
