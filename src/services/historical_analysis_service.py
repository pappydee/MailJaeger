"""
Historical AI Analysis Service — persistent, resumable AI analysis system.

Orchestrates a long-running background job that:
  - Scans stored emails from the last N days (default 365) with analysis_state='pending'
  - Runs each email through the existing AI analysis pipeline (LLM)
  - Updates analysis_state and stores AI results (summary, category, priority, etc.)
  - Persists progress (last_processed_email_id, processed_count)
  - Can be paused/resumed safely without reprocessing
  - Survives restarts/crashes via DB checkpointing

Concurrency model:
  - Exactly ONE job may run at a time (enforced via _job_lock)
  - The job runs in a daemon thread so the API never blocks
  - Safe cancellation via threading.Event checked between batches

SAFETY RULES:
  - Does NOT modify mailbox import logic
  - Does NOT analyze emails older than max_age_days
  - Does NOT introduce attachment analysis
  - Reuses existing AIService for LLM calls
"""

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import (
    Base,
    ProcessedEmail,
    HistoricalAnalysisRun,
    HistoricalAnalysisProgress,
)
from src.services.ai_service import AIService
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_BATCH_SIZE = 20
MIN_BATCH_SIZE = 5
MAX_BATCH_SIZE = 100
DEFAULT_MAX_AGE_DAYS = 365

# Phases
PHASE_SCANNING = "scanning"
PHASE_ANALYZING = "analyzing"
PHASE_FINALIZING = "finalizing"

# ---------------------------------------------------------------------------
# Module-level concurrency primitives (singleton per process)
# ---------------------------------------------------------------------------
_job_lock = threading.Lock()
_cancel_event = threading.Event()
_current_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_analysis(
    db_factory: Callable[[], Session],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Dict[str, Any]:
    """Start or resume the historical AI analysis job in a background thread.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session.
        batch_size: Number of emails per batch (clamped to MIN–MAX).
        max_age_days: Only analyze emails from the last N days (default 365).

    Returns:
        Dict with success, job_id, status, and message.
    """
    global _current_thread

    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, batch_size))
    max_age_days = max(1, max_age_days)

    if not _job_lock.acquire(blocking=False):
        return {"success": False, "message": "An analysis job is already running"}

    try:
        db = db_factory()
        try:
            _recover_stale_job(db)
            run = _get_or_create_run(db, batch_size, max_age_days)
            run_id = run.id
        finally:
            db.close()

        _cancel_event.clear()

        thread = threading.Thread(
            target=_run_analysis_thread,
            args=(db_factory, run_id, batch_size, max_age_days),
            daemon=True,
            name=f"analysis-job-{run_id}",
        )
        _current_thread = thread
        thread.start()

        return {
            "success": True,
            "job_id": run_id,
            "status": "running",
            "message": "Historical AI analysis job started",
            "batch_size": batch_size,
            "max_age_days": max_age_days,
        }
    except Exception:
        _job_lock.release()
        raise


def stop_analysis(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Request the running analysis job to stop (pause).

    Sets the cancel event and updates the DB status to 'paused' so
    resume picks up from the checkpoint.
    """
    global _current_thread

    _cancel_event.set()

    db = db_factory()
    try:
        run = (
            db.query(HistoricalAnalysisRun)
            .filter(HistoricalAnalysisRun.status == "running")
            .first()
        )
        if run:
            run.status = "paused"
            db.commit()
            return {
                "success": True,
                "job_id": run.id,
                "status": "paused",
                "message": "Historical AI analysis job paused",
            }
        return {"success": False, "message": "No running analysis job to stop"}
    finally:
        db.close()


def get_analysis_status(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Return the current status of the historical AI analysis system.

    Always returns a structured object regardless of whether a job has
    ever been started.
    """
    db = db_factory()
    try:
        run = (
            db.query(HistoricalAnalysisRun)
            .order_by(HistoricalAnalysisRun.started_at.desc())
            .first()
        )

        # Count eligible emails (last N days, pending)
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_MAX_AGE_DAYS)
        total_eligible = _count_eligible_emails(db, cutoff)

        if run is None:
            return {
                "status": "idle",
                "job_id": None,
                "total_eligible": total_eligible,
                "processed_count": 0,
                "failed_count": 0,
                "last_processed_email_id": None,
                "progress_percent": 0.0,
                "current_phase": None,
                "started_at": None,
                "completed_at": None,
                "is_running": False,
                "batch_size": DEFAULT_BATCH_SIZE,
                "max_age_days": DEFAULT_MAX_AGE_DAYS,
            }

        return {
            "status": run.status,
            "job_id": run.id,
            "total_eligible": run.total_eligible or total_eligible,
            "processed_count": run.processed_count or 0,
            "failed_count": run.failed_count or 0,
            "last_processed_email_id": run.last_processed_email_id,
            "progress_percent": run.progress_percent or 0.0,
            "current_phase": run.current_phase,
            "started_at": (
                run.started_at.isoformat() if run.started_at else None
            ),
            "completed_at": (
                run.completed_at.isoformat() if run.completed_at else None
            ),
            "is_running": run.status == "running",
            "batch_size": run.batch_size or DEFAULT_BATCH_SIZE,
            "max_age_days": run.max_age_days or DEFAULT_MAX_AGE_DAYS,
        }
    finally:
        db.close()


def reset_analysis(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Reset all analysis run/progress data.

    Stops any running job and deletes all HistoricalAnalysisRun and
    HistoricalAnalysisProgress records. Does NOT revert AI analysis
    results already written to ProcessedEmail rows.
    """
    global _current_thread

    # Stop any running job first
    _cancel_event.set()
    if _current_thread and _current_thread.is_alive():
        _current_thread.join(timeout=5)

    db = db_factory()
    try:
        deleted_runs = db.query(HistoricalAnalysisRun).delete()
        deleted_progress = db.query(HistoricalAnalysisProgress).delete()
        db.commit()
        logger.info(
            "analysis_reset deleted_runs=%s deleted_progress=%s",
            deleted_runs,
            deleted_progress,
        )
        return {
            "success": True,
            "deleted_runs": deleted_runs,
            "deleted_progress": deleted_progress,
            "message": "Analysis data reset successfully",
        }
    finally:
        db.close()

    # Release lock if it was held
    try:
        _job_lock.release()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Background thread entry point
# ---------------------------------------------------------------------------


def _run_analysis_thread(
    db_factory: Callable[[], Session],
    run_id: int,
    batch_size: int,
    max_age_days: int,
) -> None:
    """Main analysis loop — runs in a background thread.

    Processes eligible emails in batches, updating progress after each batch.
    Checks _cancel_event between batches for safe interruption.
    """
    db = db_factory()
    try:
        run = (
            db.query(HistoricalAnalysisRun)
            .filter(HistoricalAnalysisRun.id == run_id)
            .first()
        )
        if not run:
            logger.error(
                "analysis_thread_start_failed run_id=%s not_found", run_id
            )
            return

        run.status = "running"
        db.commit()

        # Compute cutoff date: only emails from last max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        # Phase 1: Scanning — count eligible emails
        run.current_phase = PHASE_SCANNING
        db.commit()
        logger.info("analysis_phase phase=scanning run_id=%s", run_id)

        total_eligible = _count_eligible_emails(db, cutoff)
        run.total_eligible = total_eligible
        db.commit()

        if total_eligible == 0:
            run.status = "completed"
            run.progress_percent = 100.0
            run.completed_at = datetime.now(timezone.utc)
            run.current_phase = PHASE_FINALIZING
            db.commit()
            logger.info("analysis_completed run_id=%s no_eligible_emails", run_id)
            return

        # Phase 2: Analyzing — process emails in batches via AI
        run.current_phase = PHASE_ANALYZING
        db.commit()
        logger.info(
            "analysis_phase phase=analyzing run_id=%s total=%s max_age_days=%s",
            run_id,
            total_eligible,
            max_age_days,
        )

        # Initialize AI service
        ai_service = AIService()

        cursor = (
            int(run.last_processed_email_id)
            if run.last_processed_email_id is not None
            else None
        )
        processed_in_run = int(run.processed_count or 0)
        failures_in_run = int(run.failed_count or 0)

        while True:
            if _cancel_event.is_set():
                run.status = "paused"
                db.commit()
                logger.info(
                    "analysis_paused run_id=%s processed=%s",
                    run_id,
                    processed_in_run,
                )
                return

            # Fetch next batch of eligible emails
            batch = _fetch_eligible_batch(db, cutoff, cursor, batch_size)

            if not batch:
                break  # All eligible emails processed

            for email in batch:
                if _cancel_event.is_set():
                    # Persist progress before stopping
                    run.last_processed_email_id = cursor
                    run.processed_count = processed_in_run
                    run.failed_count = failures_in_run
                    run.progress_percent = (
                        (processed_in_run / total_eligible * 100)
                        if total_eligible > 0
                        else 0
                    )
                    run.status = "paused"
                    db.commit()
                    logger.info(
                        "analysis_paused_mid_batch run_id=%s", run_id
                    )
                    return

                # Skip if already processed (deduplication)
                already = (
                    db.query(HistoricalAnalysisProgress)
                    .filter(HistoricalAnalysisProgress.email_id == email.id)
                    .first()
                )
                if already:
                    cursor = email.id
                    continue

                try:
                    _analyze_single_email(db, email, ai_service)

                    # Record progress
                    progress = HistoricalAnalysisProgress(
                        email_id=email.id,
                        processed_at=datetime.now(timezone.utc),
                        analysis_result=_snapshot_analysis(email),
                        success=True,
                    )
                    db.add(progress)
                    processed_in_run += 1
                    cursor = email.id

                except Exception as e:
                    logger.warning(
                        "analysis_email_failed email_id=%s error=%s",
                        email.id,
                        str(e)[:200],
                    )
                    # Record failure
                    progress = HistoricalAnalysisProgress(
                        email_id=email.id,
                        processed_at=datetime.now(timezone.utc),
                        success=False,
                    )
                    db.add(progress)
                    failures_in_run += 1
                    cursor = email.id

            # Persist checkpoint after each batch
            run.last_processed_email_id = cursor
            run.processed_count = processed_in_run
            run.failed_count = failures_in_run
            pct = (
                float(processed_in_run) / float(total_eligible) * 100
                if total_eligible > 0
                else 0.0
            )
            run.progress_percent = pct
            db.commit()
            logger.info(
                "analysis_batch_done run_id=%s processed=%s/%s failed=%s percent=%s",
                run_id,
                processed_in_run,
                total_eligible,
                failures_in_run,
                f"{pct:.1f}",
            )

        # Phase 3: Finalizing
        run.current_phase = PHASE_FINALIZING
        db.commit()
        logger.info("analysis_phase phase=finalizing run_id=%s", run_id)

        # Mark final status truthfully
        if processed_in_run == 0 and total_eligible > 0 and failures_in_run > 0:
            run.status = "failed"
            run.progress_percent = 0.0
            run.error_message = (
                f"All {failures_in_run} eligible emails failed analysis"
            )
            logger.error(
                "analysis_failed_all_emails run_id=%s failures=%s total=%s",
                run_id,
                failures_in_run,
                total_eligible,
            )
        else:
            run.status = "completed"
            run.progress_percent = 100.0
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "analysis_completed run_id=%s processed=%s/%s failures=%s status=%s",
            run_id,
            processed_in_run,
            total_eligible,
            failures_in_run,
            run.status,
        )

    except Exception as e:
        logger.error(
            "analysis_thread_failed run_id=%s error=%s", run_id, str(e)[:200]
        )
        try:
            run = (
                db.query(HistoricalAnalysisRun)
                .filter(HistoricalAnalysisRun.id == run_id)
                .first()
            )
            if run:
                run.status = "failed"
                run.error_message = str(e)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            _job_lock.release()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_eligible_emails(db: Session, cutoff: datetime) -> int:
    """Count emails eligible for historical AI analysis.

    Eligible = analysis_state='pending' AND date >= cutoff.
    """
    return int(
        db.query(func.count(ProcessedEmail.id))
        .filter(
            ProcessedEmail.analysis_state == "pending",
            ProcessedEmail.date >= cutoff,
        )
        .scalar()
        or 0
    )


def _fetch_eligible_batch(
    db: Session,
    cutoff: datetime,
    cursor: Optional[int],
    batch_size: int,
) -> list:
    """Fetch the next batch of eligible emails for analysis.

    Filters:
      - analysis_state = 'pending'
      - date >= cutoff (last N days)
      - id > cursor (for pagination/resume)

    Orders by id ascending for deterministic processing.
    """
    query = db.query(ProcessedEmail).filter(
        ProcessedEmail.analysis_state == "pending",
        ProcessedEmail.date >= cutoff,
    )
    if cursor is not None:
        query = query.filter(ProcessedEmail.id > cursor)
    return query.order_by(ProcessedEmail.id).limit(batch_size).all()


def _analyze_single_email(
    db: Session,
    email: ProcessedEmail,
    ai_service: AIService,
) -> None:
    """Analyze a single email using the AI service and update the DB record.

    Uses the existing AIService.analyze_email() method — the same LLM
    pipeline used for real-time analysis.
    """
    email_data = {
        "id": email.id,
        "subject": email.subject or "",
        "sender": email.sender or "",
        "body_plain": email.body_plain or "",
        "body_html": email.body_html or "",
    }

    result = ai_service.analyze_email(email_data)

    # Update email with AI analysis results
    email.summary = result.get("summary")
    email.category = result.get("category")
    email.spam_probability = result.get("spam_probability")
    email.action_required = result.get("action_required")
    email.priority = result.get("priority")
    email.suggested_folder = result.get("suggested_folder")
    email.reasoning = result.get("reasoning")
    email.analysis_state = "completed"
    email.processed_at = datetime.now(timezone.utc)
    email.is_processed = True

    db.add(email)


def _snapshot_analysis(email: ProcessedEmail) -> dict:
    """Create a JSON-serializable snapshot of the analysis results."""
    return {
        "summary": email.summary,
        "category": email.category,
        "spam_probability": email.spam_probability,
        "action_required": email.action_required,
        "priority": email.priority,
        "suggested_folder": email.suggested_folder,
        "reasoning": email.reasoning,
    }


def _get_or_create_run(
    db: Session,
    batch_size: int,
    max_age_days: int,
) -> HistoricalAnalysisRun:
    """Get an existing paused run or create a new one."""
    existing = (
        db.query(HistoricalAnalysisRun)
        .filter(HistoricalAnalysisRun.status.in_(("running", "paused")))
        .order_by(HistoricalAnalysisRun.started_at.desc())
        .first()
    )
    if existing:
        existing.status = "running"
        db.commit()
        logger.info("analysis_run_resumed run_id=%s", existing.id)
        return existing

    run = HistoricalAnalysisRun(
        status="running",
        current_phase=PHASE_SCANNING,
        batch_size=batch_size,
        max_age_days=max_age_days,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    logger.info("analysis_run_created run_id=%s", run.id)
    return run


def _recover_stale_job(db: Session) -> None:
    """Mark any 'running' jobs as 'failed' (from a previous crash)."""
    stale = (
        db.query(HistoricalAnalysisRun)
        .filter(HistoricalAnalysisRun.status == "running")
        .all()
    )
    for run in stale:
        run.status = "failed"
        logger.warning("analysis_run_stale_recovered run_id=%s", run.id)
    if stale:
        db.commit()
