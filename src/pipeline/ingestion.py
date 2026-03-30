"""
Pipeline: Ingestion Phase

Responsibilities:
  - Fetch emails from IMAP into local index
  - Compute body_hash for deduplication
  - Resolve thread_id from In-Reply-To / References
  - No AI, no classification, no side effects beyond DB writes

Resume semantics:
  Ingestion is **idempotent by design**.  The underlying
  ``MailIngestionService`` uses a UID checkpoint (highest IMAP UID
  already stored for the target folder) to skip messages that are
  already in the local index.  Re-running ingestion will therefore
  only fetch genuinely new messages.

  The ``last_ingested_email_id`` returned in the stats dict is the
  DB primary key of the most recently ingested ``ProcessedEmail``
  row (if any were created).  The jobs layer persists this in
  ``ProcessingJob.last_processed_email_id`` for observability, but
  it is not needed for correctness — the UID checkpoint provides
  the actual deduplication guarantee.

Entry point: ``run_ingestion(db, folder, run_id)``
"""

from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc

from src.config import get_settings
from src.models.database import ProcessedEmail
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


def run_ingestion(
    db: Session,
    folder: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ingest emails from IMAP into the local mail index.

    This is a pure ingestion phase — no AI, no classification.

    Resume semantics:
      Ingestion is idempotent.  ``MailIngestionService`` internally
      uses an IMAP UID checkpoint so re-running never re-fetches
      messages that are already indexed.

    Returns stats: {new, skipped, failed, total, last_ingested_email_id}.
    """
    settings = get_settings()
    target_folder = folder or settings.inbox_folder

    try:
        from src.services.mail_ingestion_service import MailIngestionService

        service = MailIngestionService(db)
        stats = service.ingest_folder(
            folder=target_folder,
            run_id=run_id,
        )

        # Determine the DB id of the most recently created email for cursor tracking.
        # This is purely for observability — the UID checkpoint provides idempotency.
        last_email = (
            db.query(ProcessedEmail.id)
            .filter(ProcessedEmail.folder == target_folder)
            .order_by(desc(ProcessedEmail.id))
            .first()
        )
        stats["last_ingested_email_id"] = last_email[0] if last_email else None

        logger.info(
            "ingestion_complete folder=%s new=%s skipped=%s failed=%s",
            target_folder,
            stats.get("new", 0),
            stats.get("skipped", 0),
            stats.get("failed", 0),
        )
        return stats
    except Exception as e:
        sanitized = sanitize_error(e, debug=settings.debug)
        logger.error("ingestion_failed folder=%s error=%s", target_folder, sanitized)
        return {"new": 0, "skipped": 0, "failed": 0, "total": 0, "last_ingested_email_id": None}
