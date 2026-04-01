"""
Prediction Engine for MailJaeger.

Uses learned aggregates (SenderProfile, FolderPlacementAggregate, ReplyPattern)
to generate internal predictions for newly ingested emails.

Predictions are:
  - Stored internally in EmailPrediction table
  - NOT auto-executed (no external behavior change)
  - Include human-readable explanations
  - Deterministic (based on counts/statistics, no ML)
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from src.models.database import (
    ProcessedEmail,
    SenderProfile,
    FolderPlacementAggregate,
    ReplyPattern,
    EmailPrediction,
)
from src.services.folder_classifier import extract_sender_domain, extract_sender_address
from src.services.sender_precedence import (
    build_folder_tiers,
    build_reply_tiers,
    resolve_sender_profile_label,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Minimum samples required before making a prediction
MIN_SAMPLES_FOLDER = 3
MIN_SAMPLES_REPLY = 5
MIN_CONFIDENCE_FOLDER = 0.5
MIN_CONFIDENCE_REPLY = 0.3


def generate_predictions(db: Session, email: ProcessedEmail) -> List[EmailPrediction]:
    """Generate and persist internal predictions for an email.

    Creates predictions for:
      - target_folder: likely folder based on sender/domain/category patterns
      - reply_needed: whether this email is likely to need a reply
      - importance_boost: whether historical behavior suggests higher importance

    Args:
        db: Database session
        email: The email to predict for

    Returns:
        List of EmailPrediction objects created.
    """
    predictions = []

    # 1. Folder prediction
    folder_pred = _predict_folder(db, email)
    if folder_pred:
        predictions.append(folder_pred)

    # 2. Reply-needed prediction
    reply_pred = _predict_reply_needed(db, email)
    if reply_pred:
        predictions.append(reply_pred)

    # 3. Importance boost prediction
    importance_pred = _predict_importance_boost(db, email)
    if importance_pred:
        predictions.append(importance_pred)

    # Persist all predictions
    for pred in predictions:
        db.add(pred)

    if predictions:
        logger.debug(
            "predictions_generated email_id=%s count=%s types=%s",
            email.id,
            len(predictions),
            [p.prediction_type for p in predictions],
        )

    return predictions


def _predict_folder(db: Session, email: ProcessedEmail) -> Optional[EmailPrediction]:
    """Predict the likely target folder for an email.

    Uses FolderPlacementAggregate patterns in strict priority order
    (via ``build_folder_tiers``):
      1. sender_address (most specific — always wins when sufficient)
      2. sender_domain
      3. category
      4. subject_keyword (least specific)

    The first tier that meets thresholds wins.  More specific evidence
    always takes precedence over less specific evidence regardless of
    raw confidence scores.
    """
    sender = email.sender or ""
    category = email.category or ""
    from src.services.folder_classifier import extract_subject_keywords
    keywords = extract_subject_keywords(email.subject or "")

    # Build strict-precedence tiers via the shared helper
    tiers = build_folder_tiers(sender, category, keywords)

    _EXPLAIN = {
        "sender_address": lambda v: f"Emails from {v} were placed in folder",
        "sender_domain": lambda v: f"Sender domain {v} historically moved to folder",
        "category": lambda v: f"Emails with category '{v}' were placed in folder",
        "subject_keyword": lambda v: f"Subject keyword '{v}' historically associated with folder",
    }

    for pattern_type, pattern_value in tiers:
        agg = _get_best_aggregate(db, pattern_type, pattern_value)
        if (agg
                and agg.confidence >= MIN_CONFIDENCE_FOLDER
                and agg.occurrence_count >= MIN_SAMPLES_FOLDER):
            explain_fn = _EXPLAIN.get(pattern_type, lambda v: v)
            explanation = (
                f"{explain_fn(pattern_value)} "
                f"'{agg.target_folder}' in {agg.occurrence_count}/{agg.total_for_pattern} cases "
                f"({agg.confidence:.0%} confidence)"
            )
            return EmailPrediction(
                email_id=email.id,
                prediction_type="target_folder",
                predicted_value=agg.target_folder,
                confidence=agg.confidence,
                explanation=explanation,
                source_aggregate="folder_placement_aggregate",
                source_data={
                    "pattern_type": pattern_type,
                    "pattern_value": pattern_value,
                    "occurrence": agg.occurrence_count,
                    "total": agg.total_for_pattern,
                },
                created_at=datetime.now(timezone.utc),
            )

    return None


def _predict_reply_needed(db: Session, email: ProcessedEmail) -> Optional[EmailPrediction]:
    """Predict whether an email is likely to need a reply.

    Uses ReplyPattern aggregates in strict priority order
    (via ``build_reply_tiers``):
      1. sender_address (most specific — always wins when sufficient)
      2. sender_domain
      3. category

    The first tier that meets thresholds wins.
    """
    sender = email.sender or ""
    category = email.category or ""

    # Build tiers via the shared helper
    tiers = build_reply_tiers(sender, category)

    _EXPLAIN = {
        "sender_address": lambda v: f"Emails from {v}",
        "sender_domain": lambda v: f"Emails from domain {v}",
        "category": lambda v: f"Emails in category '{v}'",
    }

    for pattern_type, pattern_value in tiers:
        pattern = (
            db.query(ReplyPattern)
            .filter(ReplyPattern.pattern_type == pattern_type,
                    ReplyPattern.pattern_value == pattern_value)
            .first()
        )
        if pattern and (pattern.total_received or 0) >= MIN_SAMPLES_REPLY:
            prob = pattern.reply_probability or 0.0
            if prob >= MIN_CONFIDENCE_REPLY:
                delay_info = ""
                if pattern.avg_reply_delay_seconds:
                    hours = pattern.avg_reply_delay_seconds / 3600
                    delay_info = f", avg reply delay {hours:.1f}h"
                explain_fn = _EXPLAIN.get(pattern_type, lambda v: v)
                explanation = (
                    f"{explain_fn(pattern_value)} were replied to in "
                    f"{pattern.total_replied}/{pattern.total_received} historical cases "
                    f"({prob:.0%}){delay_info}"
                )
                return EmailPrediction(
                    email_id=email.id,
                    prediction_type="reply_needed",
                    predicted_value=f"{prob:.2f}",
                    confidence=prob,
                    explanation=explanation,
                    source_aggregate="reply_pattern",
                    source_data={
                        "pattern_type": pattern_type,
                        "pattern_value": pattern_value,
                        "total_received": pattern.total_received,
                        "total_replied": pattern.total_replied,
                        "reply_probability": prob,
                        "avg_delay_seconds": pattern.avg_reply_delay_seconds,
                    },
                    created_at=datetime.now(timezone.utc),
                )

    return None


def _predict_importance_boost(db: Session, email: ProcessedEmail) -> Optional[EmailPrediction]:
    """Predict whether an email should get an importance boost.

    Uses SenderProfile importance_tendency, reply_rate, and spam_tendency.
    Resolves the best matching profile via the centralized
    ``resolve_sender_profile_label`` helper (address > domain).
    """
    sender = email.sender or ""
    domain = extract_sender_domain(sender)

    if not domain:
        return None

    # Resolve best sender profile via centralized precedence helper
    profile, profile_label = resolve_sender_profile_label(
        db, sender, min_support=MIN_SAMPLES_FOLDER
    )
    if not profile:
        return None

    # Calculate boost based on multiple signals
    boost = 0.0
    reasons = []

    # High reply rate suggests importance
    if (profile.reply_rate or 0) > 0.5:
        boost += 0.3
        reasons.append(
            f"reply rate {profile.reply_rate:.0%} ({profile.total_replies}/{profile.total_emails})"
        )

    # Importance markings
    if (profile.importance_tendency or 0) > 0.1:
        boost += 0.2
        reasons.append(
            f"marked important {profile.marked_important_count} times"
        )

    # Low spam tendency is a positive signal
    if (profile.spam_tendency or 0) < 0.05 and (profile.total_emails or 0) >= 10:
        boost += 0.1
        reasons.append("rarely flagged as spam")

    # Mostly kept in inbox (not immediately archived) suggests engagement
    total = profile.total_emails or 1
    inbox_rate = (profile.kept_in_inbox_count or 0) / total
    if inbox_rate > 0.5:
        boost += 0.1
        reasons.append(f"kept in inbox {inbox_rate:.0%} of the time")

    if boost < 0.2 or not reasons:
        return None

    confidence = min(1.0, boost)
    explanation = (
        f"Sender {profile_label} has historical importance signals: "
        + "; ".join(reasons)
    )

    return EmailPrediction(
        email_id=email.id,
        prediction_type="importance_boost",
        predicted_value=f"{confidence:.2f}",
        confidence=confidence,
        explanation=explanation,
        source_aggregate="sender_profile",
        source_data={
            "profile_key": profile_label,
            "domain": domain,
            "total_emails": profile.total_emails,
            "reply_rate": profile.reply_rate,
            "importance_tendency": profile.importance_tendency,
            "spam_tendency": profile.spam_tendency,
            "boost": boost,
        },
        created_at=datetime.now(timezone.utc),
    )


def _get_best_aggregate(
    db: Session, pattern_type: str, pattern_value: str
) -> Optional[FolderPlacementAggregate]:
    """Get the highest-confidence FolderPlacementAggregate for a pattern."""
    return (
        db.query(FolderPlacementAggregate)
        .filter(
            FolderPlacementAggregate.pattern_type == pattern_type,
            FolderPlacementAggregate.pattern_value == pattern_value,
        )
        .order_by(FolderPlacementAggregate.confidence.desc())
        .first()
    )
