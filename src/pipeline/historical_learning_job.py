"""
Pipeline: Historical Learning Job — resumable mailbox-wide learning scan.

Scans all indexed emails across all folders and builds learning aggregates:
  - SenderProfile per domain
  - FolderPlacementAggregate per sender/keyword/category → folder
  - ReplyPattern per sender/category (by linking sent mail to incoming)
  - EmailPrediction for each email (folder, reply, importance)

Resume semantics:
  The job tracks progress per folder in ``HistoricalLearningProgress``.
  Each folder row stores ``last_processed_email_id`` so the job can
  resume from exactly where it left off.  Already-learned emails are
  never reprocessed.

  The overall job also uses a ``ProcessingJob`` row (job_type="learning")
  for top-level status tracking.

Designed to be lightweight enough for Raspberry Pi / local systems:
  - Processes emails in configurable batch sizes (default 100)
  - Commits after each batch
  - Can be paused and resumed at any time
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from src.models.database import (
    ProcessedEmail,
    ProcessingJob,
    HistoricalLearningProgress,
)
from src.services.folder_classifier import (
    classify_folder,
    is_learnable_folder,
    FOLDER_TYPE_SENT,
)
from src.services.historical_learning import (
    learn_from_email,
    learn_reply_linkage,
    update_reply_pattern_totals,
)
from src.services.prediction_engine import generate_predictions
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Default batch size (tuned for Raspberry Pi)
DEFAULT_BATCH_SIZE = 100


def run_historical_learning_job(
    db: Session,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_emails: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a resumable historical learning scan across all indexed folders.

    This job:
      1. Discovers all distinct folders in the email index
      2. Processes each folder's emails in batches
      3. Builds/updates learning aggregates incrementally
      4. Links sent mail to incoming (reply learning)
      5. Generates predictions for each email
      6. Persists progress for resumability

    Args:
        db: Database session
        batch_size: Emails to process per batch (default 100)
        max_emails: Optional cap on total emails to process (for testing)

    Returns:
        Dict with job stats: {job_id, status, folders_processed, emails_learned,
        replies_linked, predictions_generated, ...}
    """
    # Start or resume the top-level job
    job = _start_learning_job(db)

    stats = {
        "job_id": job.id,
        "folders_processed": 0,
        "emails_learned": 0,
        "replies_linked": 0,
        "predictions_generated": 0,
        "failed": 0,
        "skipped": 0,
    }
    total_processed = 0

    try:
        # Discover all distinct folders
        folders = _discover_folders(db)
        logger.info("historical_learning_scan folders_found=%s", len(folders))

        for folder_name in folders:
            if max_emails and total_processed >= max_emails:
                break

            folder_stats = _process_folder(
                db, job, folder_name,
                batch_size=batch_size,
                remaining=max_emails - total_processed if max_emails else None,
            )
            stats["emails_learned"] += folder_stats.get("learned", 0)
            stats["replies_linked"] += folder_stats.get("replies_linked", 0)
            stats["predictions_generated"] += folder_stats.get("predictions", 0)
            stats["failed"] += folder_stats.get("failed", 0)
            stats["skipped"] += folder_stats.get("skipped", 0)
            stats["folders_processed"] += 1
            total_processed += folder_stats.get("learned", 0) + folder_stats.get("skipped", 0)

        # After processing all folders, update reply pattern totals
        updated_patterns = update_reply_pattern_totals(db)
        db.commit()
        logger.info("reply_pattern_totals_updated count=%s", updated_patterns)

        # Finalize job
        status = "completed" if stats["failed"] == 0 else "partial"
        _finish_learning_job(db, job, stats, status=status)
        stats["status"] = status
        return stats

    except Exception as e:
        logger.error("historical_learning_failed error=%s", str(e))
        _finish_learning_job(db, job, stats, status="failed", error_message=str(e))
        stats["status"] = "failed"
        return stats


def _discover_folders(db: Session) -> List[str]:
    """Discover all distinct folders from the email index."""
    rows = (
        db.query(distinct(ProcessedEmail.folder))
        .filter(ProcessedEmail.folder != None, ProcessedEmail.folder != "")
        .all()
    )
    folders = [r[0] for r in rows if r[0]]
    # Filter to learnable folders (exclude drafts)
    return [f for f in folders if is_learnable_folder(f)]


def _process_folder(
    db: Session,
    job: ProcessingJob,
    folder_name: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    remaining: Optional[int] = None,
) -> Dict[str, int]:
    """Process a single folder for historical learning.

    Resumes from the last processed email ID for this folder.
    Completed folders are still checked for newly arrived emails
    (id > last_processed_email_id) to support true incremental learning.
    """
    folder_type = classify_folder(folder_name)
    is_sent = folder_type == FOLDER_TYPE_SENT

    # Get or create folder progress
    progress = _get_or_create_folder_progress(db, folder_name, folder_type)

    # For completed folders, re-open to check for new emails only
    if progress.status == "completed":
        # Check if there are emails with id > last cursor
        cursor = progress.last_processed_email_id
        new_count_query = db.query(func.count(ProcessedEmail.id)).filter(
            ProcessedEmail.folder == folder_name
        )
        if cursor is not None:
            new_count_query = new_count_query.filter(ProcessedEmail.id > cursor)
        new_count = new_count_query.scalar() or 0
        if new_count == 0:
            logger.debug("historical_learning_folder_skip folder=%s (no new emails)", folder_name)
            return {"learned": 0, "replies_linked": 0, "predictions": 0, "failed": 0, "skipped": 0}
        # Re-open the folder for incremental processing
        logger.info("historical_learning_folder_incremental folder=%s new_emails=%s", folder_name, new_count)

    progress.status = "running"
    if not progress.started_at:
        progress.started_at = datetime.now(timezone.utc)
    else:
        progress.resumed_at = datetime.now(timezone.utc)
    db.add(progress)
    db.commit()

    folder_stats = {"learned": 0, "replies_linked": 0, "predictions": 0, "failed": 0, "skipped": 0}
    cursor = progress.last_processed_email_id

    while True:
        # Cap by remaining
        effective_batch = batch_size
        if remaining is not None:
            remaining_left = remaining - folder_stats["learned"] - folder_stats["skipped"]
            if remaining_left <= 0:
                break
            effective_batch = min(batch_size, remaining_left)

        # Fetch next batch
        query = db.query(ProcessedEmail).filter(ProcessedEmail.folder == folder_name)
        if cursor is not None:
            query = query.filter(ProcessedEmail.id > cursor)
        batch = query.order_by(ProcessedEmail.id).limit(effective_batch).all()

        if not batch:
            break

        for email in batch:
            try:
                if is_sent:
                    # For sent mail, try to link to incoming email
                    linkage = learn_reply_linkage(db, email)
                    if linkage:
                        folder_stats["replies_linked"] += 1
                else:
                    # For received mail, learn from folder placement
                    learn_from_email(db, email, source="imported-history")
                    folder_stats["learned"] += 1

                    # Generate predictions
                    preds = generate_predictions(db, email)
                    folder_stats["predictions"] += len(preds)

                cursor = email.id
            except Exception as e:
                logger.warning(
                    "historical_learning_email_failed email_id=%s error=%s",
                    email.id, str(e),
                )
                folder_stats["failed"] += 1
                cursor = email.id

        # Persist progress after each batch
        progress.last_processed_email_id = cursor
        progress.processed_count = (
            (progress.processed_count or 0) +
            folder_stats["learned"] + folder_stats["replies_linked"]
        )
        progress.failed_count = (progress.failed_count or 0) + folder_stats["failed"]
        db.add(progress)
        db.commit()

    # Mark folder as completed
    progress.status = "completed"
    progress.completed_at = datetime.now(timezone.utc)
    db.add(progress)

    # Update the top-level job cursor
    if cursor is not None:
        job.last_processed_email_id = cursor
        job.processed_count = (job.processed_count or 0) + folder_stats["learned"] + folder_stats["replies_linked"]
        db.add(job)

    db.commit()

    logger.info(
        "historical_learning_folder_done folder=%s type=%s learned=%s replies=%s predictions=%s failed=%s",
        folder_name, folder_type,
        folder_stats["learned"], folder_stats["replies_linked"],
        folder_stats["predictions"], folder_stats["failed"],
    )
    return folder_stats


def _get_or_create_folder_progress(
    db: Session, folder_name: str, folder_type: str
) -> HistoricalLearningProgress:
    """Get or create a HistoricalLearningProgress row for a folder."""
    progress = (
        db.query(HistoricalLearningProgress)
        .filter(HistoricalLearningProgress.folder_name == folder_name)
        .first()
    )
    if not progress:
        progress = HistoricalLearningProgress(
            folder_name=folder_name,
            folder_type=folder_type,
            status="pending",
            processed_count=0,
            failed_count=0,
        )
        db.add(progress)
        db.commit()
    return progress


def _start_learning_job(db: Session) -> ProcessingJob:
    """Start or resume a historical learning ProcessingJob."""
    existing = (
        db.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "learning",
            ProcessingJob.status.in_(("running", "paused")),
        )
        .first()
    )
    if existing:
        existing.status = "running"
        existing.resumed_at = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        logger.info("learning_job_resumed job_id=%s", existing.id)
        return existing

    job = ProcessingJob(
        job_type="learning",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    logger.info("learning_job_started job_id=%s", job.id)
    return job


def _finish_learning_job(
    db: Session,
    job: ProcessingJob,
    stats: Dict[str, Any],
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    """Finalize a historical learning job."""
    job.status = status
    job.completed_at = datetime.now(timezone.utc)
    job.result_stats = stats
    if error_message:
        job.error_message = error_message
    db.add(job)
    db.commit()
    logger.info(
        "learning_job_finished job_id=%s status=%s stats=%s",
        job.id, status, stats,
    )
