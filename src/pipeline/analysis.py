"""
Pipeline: Analysis Phase

Responsibilities:
  - Classify pending emails via rules + LLM
  - Compute importance scores
  - Record DecisionEvents for audit/learning
  - No IMAP side effects (pure analysis)

Entry point: ``run_analysis(db, max_count, run_id)``
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from sqlalchemy import nullslast, desc as sa_desc
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import ProcessedEmail
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


def _compute_pending_importance_scores(db: Session) -> None:
    """Compute and persist importance_score for all 'pending' emails that lack one."""
    from src.services.importance_scorer import compute_pending_importance_scores
    compute_pending_importance_scores(db)


def run_analysis(
    db: Session,
    max_count: Optional[int] = None,
    run_id: Optional[str] = None,
    resume_after_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Analyse pending emails from the local index.

    This is a pure analysis phase — no IMAP side effects.
    Classification results and DecisionEvents are persisted to the DB.

    Args:
        resume_after_id: If set, only emails with ``id > resume_after_id``
            are considered.  Used by the job layer for real resumability.

    Returns stats: {analysed, skipped, failed, llm_calls, last_email_id}.
    """
    settings = get_settings()
    effective_max = max_count or settings.max_emails_per_run

    # Ensure importance scores exist for ordering
    _compute_pending_importance_scores(db)

    # Fetch pending emails ordered by importance (highest first)
    query = db.query(ProcessedEmail).filter(
        ProcessedEmail.analysis_state == "pending"
    )
    if resume_after_id is not None:
        query = query.filter(ProcessedEmail.id > resume_after_id)
    pending = (
        query
        .order_by(nullslast(sa_desc(ProcessedEmail.importance_score)))
        .limit(effective_max)
        .all()
    )

    if not pending:
        logger.info("No pending emails to analyse")
        return {"analysed": 0, "skipped": 0, "failed": 0, "llm_calls": 0, "last_email_id": resume_after_id}

    logger.info("Analysing %s pending email(s)", len(pending))

    from src.services.analysis_pipeline import AnalysisPipeline

    pipeline = AnalysisPipeline(db)
    from src.services.ai_service import AIService

    ai_service = AIService()

    stats: Dict[str, Any] = {"analysed": 0, "skipped": 0, "failed": 0, "llm_calls": 0, "last_email_id": resume_after_id}
    batch_size = max(1, settings.ai_batch_size)

    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        needs_llm: List[ProcessedEmail] = []

        for email_record in batch:
            try:
                # Stage 0: Learned rule-based classification
                learned = pipeline.stage_learned_classify(email_record)
                if learned["confident"]:
                    pipeline.record_decision(
                        email_record, "learned_classified", learned
                    )
                    pipeline.update_analysis_state(email_record, "learned_classified")
                    pipeline.apply_analysis_to_record(
                        email_record, learned["analysis"]
                    )
                    stats["analysed"] += 1
                    stats["last_email_id"] = email_record.id
                    continue

                # Stage 1: Fast pre-classification
                stage1 = pipeline.stage1_pre_classify(email_record)
                if stage1["confident"]:
                    pipeline.record_decision(
                        email_record, "stage1_pre_classified", stage1
                    )
                    pipeline.update_analysis_state(email_record, "pre_classified")
                    pipeline.apply_analysis_to_record(
                        email_record, stage1["analysis"]
                    )
                    stats["analysed"] += 1
                    stats["last_email_id"] = email_record.id
                    continue

                # Stage 2: Rule-based classification
                stage2 = pipeline.stage2_rule_classify(email_record)
                if stage2["confident"]:
                    pipeline.record_decision(
                        email_record, "stage2_classified", stage2
                    )
                    pipeline.update_analysis_state(email_record, "classified")
                    pipeline.apply_analysis_to_record(
                        email_record, stage2["analysis"]
                    )
                    stats["analysed"] += 1
                    stats["last_email_id"] = email_record.id
                    continue

                # Needs LLM — collect for batch
                needs_llm.append(email_record)
            except Exception as e:
                sanitized = sanitize_error(e, debug=settings.debug)
                logger.error(
                    "Failed to pre-classify %s: %s",
                    email_record.message_id,
                    sanitized,
                )
                stats["failed"] += 1
                try:
                    email_record.analysis_state = "failed"
                    db.add(email_record)
                    db.commit()
                except Exception:
                    pass

        # Batch LLM analysis for remaining emails
        if needs_llm:
            from src.services.analysis_pipeline import PIPELINE_VERSION

            email_data_list = [
                {
                    "id": rec.id,
                    "subject": rec.subject or "",
                    "sender": rec.sender or "",
                    "body_plain": rec.body_plain or "",
                    "body_html": rec.body_html or "",
                }
                for rec in needs_llm
            ]
            try:
                results = ai_service.analyze_emails_batch(email_data_list)
            except Exception as e:
                sanitized = sanitize_error(e, debug=settings.debug)
                logger.error("Batch LLM analysis failed: %s", sanitized)
                results = [
                    ai_service.fallback_classification(ed) for ed in email_data_list
                ]

            stats["llm_calls"] += 1

            for email_record, analysis in zip(needs_llm, results):
                try:
                    pipeline.update_analysis_state(email_record, "deep_analyzed")
                    pipeline.record_decision(
                        email_record,
                        "stage3_deep_analyzed",
                        {"stage": 3, "source": "llm_batch", "analysis": analysis},
                    )
                    pipeline.apply_analysis_to_record(email_record, analysis)
                    email_record.analysis_version = PIPELINE_VERSION
                    db.add(email_record)
                    stats["analysed"] += 1
                    stats["last_email_id"] = email_record.id
                except Exception as e:
                    sanitized = sanitize_error(e, debug=settings.debug)
                    logger.error(
                        "Failed to apply batch result for %s: %s",
                        email_record.message_id,
                        sanitized,
                    )
                    stats["failed"] += 1
                    try:
                        email_record.analysis_state = "failed"
                        db.add(email_record)
                    except Exception:
                        pass

        db.commit()

        # Post-analysis enrichment: generate + consume learned predictions
        # (shared helper ensures all runtime paths behave identically)
        from src.services.prediction_signals import enrich_and_apply_hints
        all_in_batch = list(set(batch) | set(needs_llm))
        enrich_and_apply_hints(db, all_in_batch)

    logger.info(
        "analysis_complete analysed=%s failed=%s llm_calls=%s",
        stats["analysed"],
        stats["failed"],
        stats["llm_calls"],
    )
    return stats



# NOTE: All prediction generation and hint consumption is handled by the
# shared ``prediction_signals`` module.  No inline logic should exist here.
# See ``enrich_and_apply_hints`` call in ``run_analysis()``.
