"""
Historical AI Analysis Service — persistent, resumable AI analysis system.

Orchestrates a long-running background job that:
  - Scans stored emails from the last N days (default 365) with analysis_state='pending'
  - Runs each email through the existing AI analysis pipeline (LLM)
  - Updates analysis_state and stores AI results (summary, category, priority, etc.)
  - Persists progress (last_processed_email_id, processed_count)
  - Can be paused/resumed safely without reprocessing
  - Survives restarts/crashes via DB checkpointing

Production hardening (v2):
  - Structured logging per batch and per email (AI_ANALYZED / AI_FAILED)
  - LLM call counters and timing for observability
  - Configurable max_emails_per_run, sleep_between_batches, max_runtime_seconds
  - Defensive re-check of analysis_state == pending before each email
  - Log warning if any email older than cutoff is encountered (belt-and-suspenders)
  - Status endpoint includes emails_remaining, estimated_time_remaining, processing_speed

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
from dataclasses import dataclass
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
DEFAULT_MAX_EMAILS_PER_RUN = 5000
DEFAULT_SLEEP_BETWEEN_BATCHES = 0.5  # seconds
DEFAULT_MAX_RUNTIME_SECONDS = 7200  # 2 hours

# Phases
PHASE_SCANNING = "scanning"
PHASE_ANALYZING = "analyzing"
PHASE_FINALIZING = "finalizing"


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class HistoricalAnalysisConfig:
    """Configuration for the historical AI analysis job."""
    batch_size: int = DEFAULT_BATCH_SIZE
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    max_emails_per_run: int = DEFAULT_MAX_EMAILS_PER_RUN
    sleep_between_batches: float = DEFAULT_SLEEP_BETWEEN_BATCHES
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS

    def __post_init__(self):
        self.batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, self.batch_size))
        self.max_age_days = max(1, self.max_age_days)
        self.max_emails_per_run = max(1, self.max_emails_per_run)
        self.sleep_between_batches = max(0.0, self.sleep_between_batches)
        self.max_runtime_seconds = max(60, self.max_runtime_seconds)


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
    max_emails_per_run: int = DEFAULT_MAX_EMAILS_PER_RUN,
    sleep_between_batches: float = DEFAULT_SLEEP_BETWEEN_BATCHES,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
) -> Dict[str, Any]:
    """Start or resume the historical AI analysis job in a background thread.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session.
        batch_size: Number of emails per batch (clamped to MIN–MAX).
        max_age_days: Only analyze emails from the last N days (default 365).
        max_emails_per_run: Stop after processing this many emails (default 5000).
        sleep_between_batches: Seconds to sleep between batches (default 0.5).
        max_runtime_seconds: Max wall-clock seconds before auto-pause (default 7200).

    Returns:
        Dict with success, job_id, status, and message.
    """
    global _current_thread

    config = HistoricalAnalysisConfig(
        batch_size=batch_size,
        max_age_days=max_age_days,
        max_emails_per_run=max_emails_per_run,
        sleep_between_batches=sleep_between_batches,
        max_runtime_seconds=max_runtime_seconds,
    )

    if not _job_lock.acquire(blocking=False):
        return {"success": False, "message": "An analysis job is already running"}

    try:
        db = db_factory()
        try:
            _recover_stale_job(db)
            run = _get_or_create_run(db, config)
            run_id = run.id
        finally:
            db.close()

        _cancel_event.clear()

        thread = threading.Thread(
            target=_run_analysis_thread,
            args=(db_factory, run_id, config),
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
            "batch_size": config.batch_size,
            "max_age_days": config.max_age_days,
            "max_emails_per_run": config.max_emails_per_run,
            "sleep_between_batches": config.sleep_between_batches,
            "max_runtime_seconds": config.max_runtime_seconds,
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
    ever been started.  Includes progress quality metrics:
      - emails_remaining
      - estimated_time_remaining (seconds)
      - processing_speed (emails/sec)
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
            return _build_status_response(
                status="idle",
                total_eligible=total_eligible,
            )

        processed = run.processed_count or 0
        failed = run.failed_count or 0
        total = run.total_eligible or total_eligible
        llm_calls = run.total_llm_calls or 0
        llm_time = run.total_llm_time_seconds or 0.0

        # Compute progress quality metrics
        emails_remaining = max(0, total - processed - failed)
        processing_speed = 0.0
        estimated_time_remaining = None
        if llm_calls > 0 and llm_time > 0:
            processing_speed = round(llm_calls / llm_time, 3)
            if processing_speed > 0 and emails_remaining > 0:
                estimated_time_remaining = round(emails_remaining / processing_speed, 1)

        return _build_status_response(
            status=run.status,
            job_id=run.id,
            total_eligible=total,
            processed_count=processed,
            failed_count=failed,
            last_processed_email_id=run.last_processed_email_id,
            progress_percent=run.progress_percent or 0.0,
            current_phase=run.current_phase,
            started_at=(run.started_at.isoformat() if run.started_at else None),
            completed_at=(run.completed_at.isoformat() if run.completed_at else None),
            is_running=run.status == "running",
            batch_size=run.batch_size or DEFAULT_BATCH_SIZE,
            max_age_days=run.max_age_days or DEFAULT_MAX_AGE_DAYS,
            total_llm_calls=llm_calls,
            total_llm_time_seconds=round(llm_time, 3),
            avg_time_per_email=round(llm_time / llm_calls, 3) if llm_calls > 0 else 0.0,
            emails_remaining=emails_remaining,
            estimated_time_remaining=estimated_time_remaining,
            processing_speed=processing_speed,
        )
    finally:
        db.close()


def _build_status_response(**kwargs) -> Dict[str, Any]:
    """Build a consistent status response dict with all required fields."""
    defaults = {
        "status": "idle",
        "job_id": None,
        "total_eligible": 0,
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
        # Observability fields
        "total_llm_calls": 0,
        "total_llm_time_seconds": 0.0,
        "avg_time_per_email": 0.0,
        # Progress quality fields
        "emails_remaining": 0,
        "estimated_time_remaining": None,
        "processing_speed": 0.0,
    }
    defaults.update(kwargs)

    # Aliases for minimal UI contract
    defaults["processed"] = defaults["processed_count"]
    defaults["total"] = defaults["total_eligible"]
    defaults["llm_calls"] = defaults["total_llm_calls"]
    defaults["failures"] = defaults["failed_count"]
    defaults["current_batch_size"] = defaults["batch_size"]

    return defaults


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
    config: HistoricalAnalysisConfig,
) -> None:
    """Main analysis loop — runs in a background thread.

    Processes eligible emails in batches, updating progress after each batch.
    Checks _cancel_event between batches for safe interruption.

    Resource controls:
      - Sleeps between batches (config.sleep_between_batches)
      - Stops after max_emails_per_run emails
      - Stops after max_runtime_seconds wall-clock time
    """
    db = db_factory()
    job_start_time = time.monotonic()
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
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.max_age_days)

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
            "analysis_phase phase=analyzing run_id=%s total=%s max_age_days=%s "
            "batch_size=%s max_emails=%s max_runtime=%s sleep=%s",
            run_id,
            total_eligible,
            config.max_age_days,
            config.batch_size,
            config.max_emails_per_run,
            config.max_runtime_seconds,
            config.sleep_between_batches,
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
        llm_calls_in_run = int(run.total_llm_calls or 0)
        llm_time_in_run = float(run.total_llm_time_seconds or 0.0)
        batch_number = 0

        while True:
            # --- Cancellation check ---
            if _cancel_event.is_set():
                _persist_checkpoint(
                    run, cursor, processed_in_run, failures_in_run,
                    llm_calls_in_run, llm_time_in_run, total_eligible, "paused",
                )
                db.commit()
                logger.info(
                    "analysis_paused run_id=%s processed=%s reason=cancel_event",
                    run_id, processed_in_run,
                )
                return

            # --- Max emails per run check ---
            if processed_in_run >= config.max_emails_per_run:
                _persist_checkpoint(
                    run, cursor, processed_in_run, failures_in_run,
                    llm_calls_in_run, llm_time_in_run, total_eligible, "paused",
                )
                db.commit()
                logger.info(
                    "analysis_paused run_id=%s processed=%s reason=max_emails_per_run limit=%s",
                    run_id, processed_in_run, config.max_emails_per_run,
                )
                return

            # --- Max runtime check ---
            elapsed = time.monotonic() - job_start_time
            if elapsed >= config.max_runtime_seconds:
                _persist_checkpoint(
                    run, cursor, processed_in_run, failures_in_run,
                    llm_calls_in_run, llm_time_in_run, total_eligible, "paused",
                )
                db.commit()
                logger.info(
                    "analysis_paused run_id=%s processed=%s reason=max_runtime elapsed=%s limit=%s",
                    run_id, processed_in_run, f"{elapsed:.1f}", config.max_runtime_seconds,
                )
                return

            # Fetch next batch of eligible emails
            batch = _fetch_eligible_batch(db, cutoff, cursor, config.batch_size)

            if not batch:
                break  # All eligible emails processed

            batch_number += 1
            batch_start = time.monotonic()
            batch_processed = 0
            batch_failed = 0

            logger.info(
                "analysis_batch_start run_id=%s batch=%s size=%s cursor_after=%s",
                run_id, batch_number, len(batch),
                cursor,
            )

            for email in batch:
                if _cancel_event.is_set():
                    # Persist progress before stopping
                    _persist_checkpoint(
                        run, cursor, processed_in_run, failures_in_run,
                        llm_calls_in_run, llm_time_in_run, total_eligible, "paused",
                    )
                    db.commit()
                    logger.info(
                        "analysis_paused_mid_batch run_id=%s batch=%s",
                        run_id, batch_number,
                    )
                    return

                # Skip if already processed (deduplication via progress table)
                already = (
                    db.query(HistoricalAnalysisProgress)
                    .filter(HistoricalAnalysisProgress.email_id == email.id)
                    .first()
                )
                if already:
                    cursor = email.id
                    continue

                # --- Defensive re-check: analysis_state must be 'pending' ---
                db.refresh(email)
                if email.analysis_state != "pending":
                    logger.info(
                        "analysis_skip_not_pending email_id=%s state=%s",
                        email.id, email.analysis_state,
                    )
                    cursor = email.id
                    continue

                # --- Defensive guard: email must be within cutoff ---
                email_date = email.date
                if email_date is not None:
                    if email_date.tzinfo is None:
                        email_date = email_date.replace(tzinfo=timezone.utc)
                    if email_date < cutoff:
                        logger.warning(
                            "analysis_SKIP_OLD_EMAIL email_id=%s date=%s cutoff=%s "
                            "SAFETY: refusing to analyze email older than %s days",
                            email.id, email_date.isoformat(), cutoff.isoformat(),
                            config.max_age_days,
                        )
                        cursor = email.id
                        continue

                try:
                    email_start = time.monotonic()
                    _analyze_single_email(db, email, ai_service)
                    email_elapsed = time.monotonic() - email_start

                    llm_calls_in_run += 1
                    llm_time_in_run += email_elapsed

                    # Record progress
                    progress = HistoricalAnalysisProgress(
                        email_id=email.id,
                        processed_at=datetime.now(timezone.utc),
                        analysis_result=_snapshot_analysis(email),
                        success=True,
                    )
                    db.add(progress)
                    processed_in_run += 1
                    batch_processed += 1
                    cursor = email.id

                    logger.info(
                        "analysis_AI_ANALYZED email_id=%s elapsed=%ss run_id=%s",
                        email.id, f"{email_elapsed:.3f}", run_id,
                    )

                except Exception as e:
                    logger.warning(
                        "analysis_AI_FAILED email_id=%s error=%s run_id=%s",
                        email.id, str(e)[:200], run_id,
                    )
                    # Rollback any partial changes from the failed analysis
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    # Re-fetch the run after rollback (it was detached)
                    run = (
                        db.query(HistoricalAnalysisRun)
                        .filter(HistoricalAnalysisRun.id == run_id)
                        .first()
                    )
                    # Mark the email so it's not retried endlessly
                    try:
                        fresh_email = db.query(ProcessedEmail).get(email.id)
                        if fresh_email:
                            fresh_email.analysis_state = "ai_failed"
                    except Exception:
                        pass
                    # Record failure in progress table
                    try:
                        progress = HistoricalAnalysisProgress(
                            email_id=email.id,
                            processed_at=datetime.now(timezone.utc),
                            success=False,
                        )
                        db.add(progress)
                        db.flush()
                    except Exception:
                        # If progress insert also fails (e.g. UNIQUE),
                        # rollback and continue
                        try:
                            db.rollback()
                            run = (
                                db.query(HistoricalAnalysisRun)
                                .filter(HistoricalAnalysisRun.id == run_id)
                                .first()
                            )
                        except Exception:
                            pass
                    failures_in_run += 1
                    batch_failed += 1
                    cursor = email.id

            # Persist checkpoint after each batch
            _persist_checkpoint(
                run, cursor, processed_in_run, failures_in_run,
                llm_calls_in_run, llm_time_in_run, total_eligible, "running",
            )
            db.commit()

            batch_elapsed = time.monotonic() - batch_start
            logger.info(
                "analysis_batch_end run_id=%s batch=%s processed=%s failed=%s "
                "elapsed=%ss total_processed=%s/%s percent=%s",
                run_id, batch_number, batch_processed, batch_failed,
                f"{batch_elapsed:.2f}", processed_in_run, total_eligible,
                f"{run.progress_percent:.1f}",
            )

            # --- Sleep between batches to avoid overloading CPU ---
            if config.sleep_between_batches > 0 and not _cancel_event.is_set():
                time.sleep(config.sleep_between_batches)

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
        run.total_llm_calls = llm_calls_in_run
        run.total_llm_time_seconds = llm_time_in_run
        db.commit()

        total_elapsed = time.monotonic() - job_start_time
        logger.info(
            "analysis_completed run_id=%s processed=%s/%s failures=%s "
            "llm_calls=%s llm_time=%ss wall_time=%ss status=%s",
            run_id, processed_in_run, total_eligible, failures_in_run,
            llm_calls_in_run, f"{llm_time_in_run:.2f}", f"{total_elapsed:.2f}", run.status,
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


def _persist_checkpoint(
    run: HistoricalAnalysisRun,
    cursor: Optional[int],
    processed: int,
    failed: int,
    llm_calls: int,
    llm_time: float,
    total: int,
    status: str,
) -> None:
    """Update the run record with current checkpoint data."""
    run.last_processed_email_id = cursor
    run.processed_count = processed
    run.failed_count = failed
    run.total_llm_calls = llm_calls
    run.total_llm_time_seconds = llm_time
    pct = (
        float(processed) / float(total) * 100
        if total > 0
        else 0.0
    )
    run.progress_percent = pct
    run.status = status


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
    config: HistoricalAnalysisConfig,
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
        batch_size=config.batch_size,
        max_age_days=config.max_age_days,
        max_emails_per_run=config.max_emails_per_run,
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
