"""
Pipeline: Ingestion Phase

Responsibilities:
  - Fetch emails from IMAP into local index
  - Compute body_hash for deduplication
  - Resolve thread_id from In-Reply-To / References
  - No AI, no classification, no side effects beyond DB writes

Entry point: ``run_ingestion(db, folder, run_id)``
"""

from typing import Dict, Any, Optional
from sqlalchemy.orm import Session

from src.config import get_settings
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
    Returns stats: {new, skipped, failed, total}.
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
        return {"new": 0, "skipped": 0, "failed": 0, "total": 0}
