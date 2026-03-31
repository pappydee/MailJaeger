"""
Shared prediction-signal helpers for consuming learned signals downstream.

This module is the **single integration point** where persisted
``EmailPrediction`` records are consumed as internal runtime signals.
Both the modern ``pipeline/analysis.py`` path and the legacy
``EmailProcessor.process_emails()`` path call these helpers to ensure
learned signals are applied consistently.

Integration points:
  - ``enrich_and_apply_hints(db, emails)`` — generate predictions then consume them
  - ``apply_prediction_hints(db, emails)`` — consume already-stored predictions
  - ``generate_email_predictions(db, emails)`` — create EmailPrediction rows
"""

from typing import List

from sqlalchemy.orm import Session

from src.models.database import ProcessedEmail
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Confidence threshold for reply-needed predictions to set action_required.
# Predictions with confidence below this value are not strong enough to flag.
_REPLY_CONFIDENCE_THRESHOLD = 0.5


def generate_email_predictions(
    db: Session,
    emails: List[ProcessedEmail],
) -> None:
    """Generate and persist learned-behavior predictions for classified emails.

    Creates ``EmailPrediction`` rows for each email that has been successfully
    analysed (not ``failed`` / ``pending``).  Predictions are internal — they
    do not change external behavior — but make learned signals available for
    downstream consumption via :func:`apply_prediction_hints`.
    """
    try:
        from src.services.prediction_engine import generate_predictions

        for email_record in emails:
            if email_record.analysis_state in ("failed", "pending"):
                continue
            generate_predictions(db, email_record)
        db.commit()
    except Exception as exc:
        logger.warning("Failed to generate predictions: %s", exc)


def apply_prediction_hints(
    db: Session,
    emails: List[ProcessedEmail],
) -> None:
    """Consume persisted ``EmailPrediction`` records as internal signals.

    Reads back stored predictions and applies them as soft hints on the
    ``ProcessedEmail`` rows:

      - **target_folder**: backfills ``suggested_folder`` when analysis did
        not set one (does NOT override explicit analysis results).
      - **reply_needed**: sets ``action_required = True`` when analysis left
        it unset and the learned signal shows high reply likelihood.
        Appends an explanation to ``reasoning``.
      - **importance_boost**: adjusts ``importance_score`` upward when
        learned behavior indicates importance (only backfills when score
        is absent).

    This is designed to be called from **every** active runtime path so
    that learned signals are consumed consistently regardless of which
    orchestrator drives the analysis.
    """
    try:
        from src.models.database import EmailPrediction

        for email_record in emails:
            if email_record.analysis_state in ("failed", "pending"):
                continue

            predictions = (
                db.query(EmailPrediction)
                .filter(EmailPrediction.email_id == email_record.id)
                .all()
            )
            for pred in predictions:
                if pred.prediction_type == "target_folder" and pred.predicted_value:
                    # Backfill suggested_folder only when analysis did not set one
                    if not email_record.suggested_folder:
                        email_record.suggested_folder = pred.predicted_value
                        _append_hint_reasoning(
                            email_record,
                            f"[learned] suggested folder: {pred.predicted_value}"
                            + (f" ({pred.explanation})" if pred.explanation else ""),
                        )
                        logger.debug(
                            "hint_applied email_id=%s type=target_folder value=%s",
                            email_record.id,
                            pred.predicted_value,
                        )

                elif pred.prediction_type == "reply_needed":
                    # If analysis did not flag action_required but learned
                    # signal indicates high reply probability, set it.
                    confidence = pred.confidence or 0.0
                    if not email_record.action_required and confidence >= _REPLY_CONFIDENCE_THRESHOLD:
                        email_record.action_required = True
                        hint_reason = (
                            f"[learned] {pred.explanation or 'reply likely'}"
                        )
                        _append_hint_reasoning(email_record, hint_reason)
                        logger.debug(
                            "hint_applied email_id=%s type=reply_needed conf=%.2f",
                            email_record.id,
                            confidence,
                        )

                elif pred.prediction_type == "importance_boost":
                    # Augment importance_score with learned signal only when
                    # importance scoring has not already incorporated it
                    boost_value = pred.confidence or 0.0
                    if boost_value > 0 and email_record.importance_score is None:
                        from src.services.importance_scorer import (
                            _IMPORTANCE_BASELINE,
                        )

                        adjustment = min(10.0, boost_value * 10.0)
                        email_record.importance_score = (
                            _IMPORTANCE_BASELINE + adjustment
                        )
                        _append_hint_reasoning(
                            email_record,
                            f"[learned] importance boost +{adjustment:.1f}"
                            + (f" ({pred.explanation})" if pred.explanation else ""),
                        )
                        logger.debug(
                            "hint_applied email_id=%s type=importance_boost adj=+%.1f",
                            email_record.id,
                            adjustment,
                        )

            db.add(email_record)

        db.commit()
    except Exception as exc:
        logger.warning("Failed to apply prediction hints: %s", exc)


def enrich_and_apply_hints(
    db: Session,
    emails: List[ProcessedEmail],
) -> None:
    """Generate predictions then immediately consume them as internal hints.

    Convenience wrapper that calls :func:`generate_email_predictions` followed
    by :func:`apply_prediction_hints`.  This is the recommended integration
    point for runtime paths that want to both produce and consume learned
    signals in a single call.
    """
    generate_email_predictions(db, emails)
    apply_prediction_hints(db, emails)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _append_hint_reasoning(
    email_record: ProcessedEmail,
    hint_text: str,
) -> None:
    """Append *hint_text* to the email's ``reasoning`` field without losing
    existing analysis reasoning.
    """
    if email_record.reasoning:
        email_record.reasoning += f"; {hint_text}"
    else:
        email_record.reasoning = hint_text
