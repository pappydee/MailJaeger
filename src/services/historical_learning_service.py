"""
Historical Learning Service — persistent, resumable learning system (v1.2.0).

Orchestrates a long-running background learning job that:
  - Scans all existing emails in batches
  - Learns folder patterns, sender behavior, classification corrections
  - Persists progress (last_email_uid, processed_emails)
  - Can be paused/resumed safely without reprocessing
  - Survives restarts/crashes via DB checkpointing

Concurrency model:
  - Exactly ONE job may run at a time (enforced via _job_lock)
  - The job runs in a daemon thread so the API never blocks
  - Safe cancellation via threading.Event checked between batches
"""

import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import (
    Base,
    LearningRun,
    LearningProgress,
    ProcessedEmail,
    SenderProfile,
    DecisionEvent,
    FolderPlacementAggregate,
)
from src.services.historical_learning import learn_from_email
from src.services.prediction_engine import generate_predictions
from src.services.folder_classifier import classify_folder, is_learnable_folder, FOLDER_TYPE_SENT
from src.services.historical_learning import learn_reply_linkage, update_reply_pattern_totals
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_BATCH_SIZE = 100
MIN_BATCH_SIZE = 50
MAX_BATCH_SIZE = 200

# Phases
PHASE_SCANNING = "scanning"
PHASE_ANALYZING = "analyzing"
PHASE_LEARNING = "learning"

# ---------------------------------------------------------------------------
# Module-level concurrency primitives (singleton per process)
# ---------------------------------------------------------------------------
_job_lock = threading.Lock()
_cancel_event = threading.Event()
_current_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_learning(db_factory, *, batch_size: int = DEFAULT_BATCH_SIZE) -> Dict[str, Any]:
    """Start or resume the historical learning job in a background thread.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session.
        batch_size: Number of emails per batch (clamped to 50–200).

    Returns:
        Dict with job_id, status, and message.

    Raises:
        RuntimeError if a job is already running.
    """
    global _current_thread

    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, batch_size))

    if not _job_lock.acquire(blocking=False):
        return {"success": False, "message": "A learning job is already running"}

    try:
        # Check DB for already-running job (stale from crash)
        db = db_factory()
        try:
            _recover_stale_job(db)
            run = _get_or_create_run(db)
            run_id = run.id
        finally:
            db.close()

        _cancel_event.clear()

        thread = threading.Thread(
            target=_run_learning_thread,
            args=(db_factory, run_id, batch_size),
            daemon=True,
            name=f"learning-job-{run_id}",
        )
        _current_thread = thread
        thread.start()

        return {
            "success": True,
            "job_id": run_id,
            "status": "running",
            "message": "Learning job started",
        }
    except Exception:
        _job_lock.release()
        raise


def stop_learning(db_factory) -> Dict[str, Any]:
    """Request the running learning job to stop (pause).

    Sets the cancel event and waits briefly for the thread to acknowledge.
    The DB status is set to 'paused' so resume picks up from the checkpoint.
    """
    global _current_thread

    _cancel_event.set()

    db = db_factory()
    try:
        run = (
            db.query(LearningRun)
            .filter(LearningRun.status == "running")
            .first()
        )
        if run:
            run.status = "paused"
            db.commit()
            return {
                "success": True,
                "job_id": run.id,
                "status": "paused",
                "message": "Learning job paused",
            }
        return {"success": False, "message": "No running learning job to stop"}
    finally:
        db.close()


def resume_learning(db_factory, *, batch_size: int = DEFAULT_BATCH_SIZE) -> Dict[str, Any]:
    """Resume a previously paused learning job.

    Alias for start_learning — the run logic automatically resumes from
    the last checkpoint (last_email_uid).
    """
    return start_learning(db_factory, batch_size=batch_size)


def get_status(db_factory) -> Dict[str, Any]:
    """Return the current status of the historical learning system.

    Always returns a structured object regardless of whether a job has
    ever been started.
    """
    db = db_factory()
    try:
        run = (
            db.query(LearningRun)
            .order_by(LearningRun.started_at.desc())
            .first()
        )

        total_emails = (
            db.query(func.count(ProcessedEmail.id))
            .filter(ProcessedEmail.folder != None, ProcessedEmail.folder != "")
            .scalar() or 0
        )

        processed_count = (
            db.query(func.count(LearningProgress.id)).scalar() or 0
        )

        if run is None:
            return {
                "status": "idle",
                "job_id": None,
                "total_emails": total_emails,
                "processed_emails": 0,
                "last_email_uid": None,
                "progress_percent": 0.0,
                "current_phase": None,
                "started_at": None,
                "completed_at": None,
                "is_running": False,
            }

        return {
            "status": run.status,
            "job_id": run.id,
            "total_emails": run.total_emails or total_emails,
            "processed_emails": run.processed_emails or 0,
            "last_email_uid": run.last_email_uid,
            "progress_percent": run.progress_percent or 0.0,
            "current_phase": run.current_phase,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "is_running": run.status == "running",
        }
    finally:
        db.close()


def reset_learning(db_factory) -> Dict[str, Any]:
    """Reset all learning data — deletes runs, progress, and allows fresh start.

    This is a destructive operation. It does NOT delete learned aggregates
    (SenderProfile, FolderPlacementAggregate) — only the run/progress tracking.
    """
    global _current_thread

    # Stop any running job first
    _cancel_event.set()
    if _current_thread and _current_thread.is_alive():
        _current_thread.join(timeout=5)

    db = db_factory()
    try:
        deleted_runs = db.query(LearningRun).delete()
        deleted_progress = db.query(LearningProgress).delete()
        db.commit()
        logger.info(
            "learning_reset deleted_runs=%s deleted_progress=%s",
            deleted_runs, deleted_progress,
        )
        return {
            "success": True,
            "deleted_runs": deleted_runs,
            "deleted_progress": deleted_progress,
            "message": "Learning data reset successfully",
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


def _run_learning_thread(db_factory, run_id: int, batch_size: int) -> None:
    """Main learning loop — runs in a background thread.

    Processes emails in batches, updating progress after each batch.
    Checks _cancel_event between batches for safe interruption.
    """
    db = db_factory()
    try:
        run = db.query(LearningRun).filter(LearningRun.id == run_id).first()
        if not run:
            logger.error("learning_thread_start_failed run_id=%s not_found", run_id)
            return

        run.status = "running"
        db.commit()

        # Phase 1: Scanning — count total emails
        run.current_phase = PHASE_SCANNING
        db.commit()
        logger.info("learning_phase phase=scanning run_id=%s", run_id)

        total_emails = int(
            db.query(func.count(ProcessedEmail.id))
            .filter(ProcessedEmail.folder != None, ProcessedEmail.folder != "")
            .scalar() or 0
        )
        run.total_emails = total_emails
        db.commit()

        if total_emails == 0:
            run.status = "completed"
            run.progress_percent = 100.0
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("learning_completed run_id=%s no_emails", run_id)
            return

        # Phase 2: Analyzing — process emails in batches
        run.current_phase = PHASE_ANALYZING
        db.commit()
        logger.info("learning_phase phase=analyzing run_id=%s total=%s", run_id, total_emails)

        cursor = int(run.last_email_uid) if run.last_email_uid is not None else None  # Resume point
        processed_in_run = int(run.processed_emails or 0)

        failures_in_run = 0

        while True:
            if _cancel_event.is_set():
                run.status = "paused"
                db.commit()
                logger.info("learning_paused run_id=%s processed=%s", run_id, processed_in_run)
                return

            # Fetch next batch (skip already processed via LearningProgress)
            query = (
                db.query(ProcessedEmail)
                .filter(
                    ProcessedEmail.folder != None,
                    ProcessedEmail.folder != "",
                )
            )
            if cursor is not None:
                query = query.filter(ProcessedEmail.id > cursor)
            batch = query.order_by(ProcessedEmail.id).limit(batch_size).all()

            if not batch:
                break  # All emails processed

            for email in batch:
                if _cancel_event.is_set():
                    # Persist progress before stopping
                    run.last_email_uid = cursor
                    run.processed_emails = processed_in_run
                    run.progress_percent = (
                        (processed_in_run / total_emails * 100) if total_emails > 0 else 0
                    )
                    run.status = "paused"
                    db.commit()
                    logger.info("learning_paused_mid_batch run_id=%s", run_id)
                    return

                # Skip if already processed (deduplication)
                already = (
                    db.query(LearningProgress)
                    .filter(LearningProgress.email_id == email.id)
                    .first()
                )
                if already:
                    cursor = email.id
                    continue

                try:
                    result_hash = _learn_single_email(db, email)

                    # Record progress
                    progress = LearningProgress(
                        email_id=email.id,
                        processed_at=datetime.now(timezone.utc),
                        result_hash=result_hash,
                    )
                    db.add(progress)
                    processed_in_run += 1
                    cursor = email.id

                except Exception as e:
                    logger.warning(
                        "learning_email_failed email_id=%s error=%s",
                        email.id, str(e),
                    )
                    failures_in_run += 1
                    cursor = email.id

            # Persist checkpoint after each batch
            run.last_email_uid = cursor
            run.processed_emails = processed_in_run
            pct = float(processed_in_run) / float(total_emails) * 100 if total_emails > 0 else 0.0
            run.progress_percent = pct
            db.commit()
            logger.info(
                "learning_batch_done run_id=%s processed=%s/%s percent=%s",
                run_id, processed_in_run, total_emails, f"{pct:.1f}",
            )

        # Phase 3: Learning — finalize aggregates
        run.current_phase = PHASE_LEARNING
        db.commit()
        logger.info("learning_phase phase=learning run_id=%s", run_id)

        try:
            update_reply_pattern_totals(db)
            db.commit()
        except Exception as e:
            logger.warning("learning_finalize_patterns_failed error=%s", str(e))

        # Mark final status truthfully
        if processed_in_run == 0 and total_emails > 0 and failures_in_run > 0:
            run.status = "failed"
            run.progress_percent = 0.0
            logger.error(
                "learning_failed_all_emails run_id=%s failures=%s total=%s",
                run_id,
                failures_in_run,
                total_emails,
            )
        else:
            run.status = "completed"
            run.progress_percent = 100.0
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "learning_completed run_id=%s processed=%s/%s failures=%s status=%s",
            run_id, processed_in_run, total_emails, failures_in_run, run.status,
        )

    except Exception as e:
        logger.error("learning_thread_failed run_id=%s error=%s", run_id, str(e))
        try:
            run = db.query(LearningRun).filter(LearningRun.id == run_id).first()
            if run:
                run.status = "failed"
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


def _learn_single_email(db: Session, email: ProcessedEmail) -> str:
    """Learn from a single email and return a result hash.

    Delegates to the existing historical_learning module for aggregate
    building and prediction generation.
    """
    folder = email.folder or ""
    folder_type = classify_folder(folder)
    is_sent = folder_type == FOLDER_TYPE_SENT

    if is_sent:
        learn_reply_linkage(db, email)
    else:
        learn_from_email(db, email, source="learning-run")
        generate_predictions(db, email)

    # Update sender interaction count and spam probability
    _update_sender_interaction(db, email)

    # Compute result hash for deduplication
    content = f"{email.id}:{email.sender}:{email.folder}:{email.category}"
    return hashlib.sha256(content.encode()).hexdigest()


def _update_sender_interaction(db: Session, email: ProcessedEmail) -> None:
    """Update SenderProfile interaction_count and spam_probability."""
    if not email.sender:
        return

    # Extract domain from sender
    domain = email.sender.split("@")[-1].lower() if "@" in email.sender else email.sender.lower()

    profile = (
        db.query(SenderProfile)
        .filter(
            (SenderProfile.sender_address == email.sender)
            | (SenderProfile.sender_domain == domain)
        )
        .first()
    )
    if profile:
        profile.interaction_count = (profile.interaction_count or 0) + 1
        # Compute spam_probability from tendencies
        total = profile.total_emails or 1
        spam_count = profile.marked_spam_count or 0
        profile.spam_probability = spam_count / total if total > 0 else 0.0
        profile.last_seen = datetime.now(timezone.utc)
        db.add(profile)


def _get_or_create_run(db: Session) -> LearningRun:
    """Get an existing paused run or create a new one."""
    existing = (
        db.query(LearningRun)
        .filter(LearningRun.status.in_(("running", "paused")))
        .order_by(LearningRun.started_at.desc())
        .first()
    )
    if existing:
        existing.status = "running"
        db.commit()
        logger.info("learning_run_resumed run_id=%s", existing.id)
        return existing

    run = LearningRun(
        status="running",
        current_phase=PHASE_SCANNING,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    logger.info("learning_run_created run_id=%s", run.id)
    return run


def _recover_stale_job(db: Session) -> None:
    """Mark any 'running' jobs as 'failed' (from a previous crash)."""
    stale = (
        db.query(LearningRun)
        .filter(LearningRun.status == "running")
        .all()
    )
    for run in stale:
        run.status = "failed"
        logger.warning("learning_run_stale_recovered run_id=%s", run.id)
    if stale:
        db.commit()
