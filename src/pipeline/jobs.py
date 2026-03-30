"""
Pipeline: Processing Jobs — resumable job tracking.

Provides three independently callable jobs:
  - ``run_ingestion_job(db)`` — IMAP fetch into local index
  - ``run_analysis_job(db)`` — classify pending emails
  - ``run_action_job(db)``  — execute approved actions

Each job persists its state in the ``processing_jobs`` table so it can
be resumed after interruption.  Progress is tracked via
``last_processed_email_id`` to avoid reprocessing.
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import ProcessingJob
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error
from src.pipeline.ingestion import run_ingestion
from src.pipeline.analysis import run_analysis
from src.pipeline.actions import run_actions

logger = get_logger(__name__)


def _start_job(db: Session, job_type: str, run_id: Optional[str] = None) -> ProcessingJob:
    """Create or resume a ProcessingJob record."""
    # Check for an existing incomplete job of this type
    existing = (
        db.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == job_type,
            ProcessingJob.status.in_(("running", "paused")),
        )
        .first()
    )
    if existing:
        existing.status = "running"
        existing.resumed_at = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        logger.info("job_resumed job_id=%s job_type=%s", existing.id, job_type)
        return existing

    job = ProcessingJob(
        job_type=job_type,
        run_id=run_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    logger.info("job_started job_id=%s job_type=%s", job.id, job_type)
    return job


def _finish_job(
    db: Session,
    job: ProcessingJob,
    stats: Dict[str, Any],
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    """Mark a ProcessingJob as completed or failed."""
    job.status = status
    job.completed_at = datetime.now(timezone.utc)
    job.result_stats = stats
    if error_message:
        job.error_message = error_message
    db.add(job)
    db.commit()
    logger.info(
        "job_finished job_id=%s job_type=%s status=%s stats=%s",
        job.id,
        job.job_type,
        status,
        stats,
    )


def run_ingestion_job(
    db: Session,
    folder: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run an ingestion job with progress tracking.

    Returns: {job_id, stats, status}
    """
    job = _start_job(db, "ingestion", run_id)
    try:
        stats = run_ingestion(db, folder=folder, run_id=run_id or str(job.id))

        # Track progress
        job.processed_count = stats.get("new", 0) + stats.get("skipped", 0)
        job.failed_count = stats.get("failed", 0)

        status = "completed" if stats.get("failed", 0) == 0 else "partial"
        _finish_job(db, job, stats, status=status)
        return {"job_id": job.id, "stats": stats, "status": status}
    except Exception as e:
        settings = get_settings()
        error_msg = sanitize_error(e, debug=settings.debug)
        _finish_job(db, job, {}, status="failed", error_message=error_msg)
        return {"job_id": job.id, "stats": {}, "status": "failed"}


def run_analysis_job(
    db: Session,
    max_count: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run an analysis job with progress tracking.

    Returns: {job_id, stats, status}
    """
    job = _start_job(db, "analysis", run_id)
    try:
        stats = run_analysis(db, max_count=max_count, run_id=run_id or str(job.id))

        job.processed_count = stats.get("analysed", 0)
        job.failed_count = stats.get("failed", 0)
        if stats.get("analysed", 0) > 0:
            # Record last processed email for resume capability
            from src.models.database import ProcessedEmail

            last = (
                db.query(ProcessedEmail)
                .filter(
                    ProcessedEmail.analysis_state.in_(
                        ("pre_classified", "classified", "deep_analyzed")
                    )
                )
                .order_by(ProcessedEmail.processed_at.desc())
                .first()
            )
            if last:
                job.last_processed_email_id = last.id

        status = "completed" if stats.get("failed", 0) == 0 else "partial"
        _finish_job(db, job, stats, status=status)
        return {"job_id": job.id, "stats": stats, "status": status}
    except Exception as e:
        settings = get_settings()
        error_msg = sanitize_error(e, debug=settings.debug)
        _finish_job(db, job, {}, status="failed", error_message=error_msg)
        return {"job_id": job.id, "stats": {}, "status": "failed"}


def run_action_job(db: Session) -> Dict[str, Any]:
    """
    Run an action execution job with progress tracking.

    Returns: {job_id, stats, status}
    """
    job = _start_job(db, "action")
    try:
        stats = run_actions(db)

        job.processed_count = stats.get("executed", 0)
        job.failed_count = stats.get("failed", 0)

        status = "completed" if stats.get("failed", 0) == 0 else "partial"
        _finish_job(db, job, stats, status=status)
        return {"job_id": job.id, "stats": stats, "status": status}
    except Exception as e:
        settings = get_settings()
        error_msg = sanitize_error(e, debug=settings.debug)
        _finish_job(db, job, {}, status="failed", error_message=error_msg)
        return {"job_id": job.id, "stats": {}, "status": "failed"}
