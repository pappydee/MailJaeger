"""
Pipeline: Action Execution Phase

Responsibilities:
  - Execute approved ActionQueue items via IMAP
  - Only operates on approved actions
  - Updates action status and email state
  - Logs to AuditLog

Entry point: ``run_actions(db)``
"""

from typing import Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import ActionQueue, ProcessedEmail, AuditLog
from src.services.action_executor import ActionExecutor
from src.services.imap_service import IMAPService
from src.services.thread_context import update_thread_state_for_thread
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


def run_actions(db: Session) -> Dict[str, Any]:
    """
    Execute all approved ActionQueue items.

    Connects to IMAP only if there are IMAP-requiring actions.
    Returns stats: {total, executed, failed, skipped}.
    """
    settings = get_settings()

    approved = (
        db.query(ActionQueue)
        .filter(ActionQueue.status.in_(("approved", "approved_action")))
        .order_by(ActionQueue.created_at.asc())
        .all()
    )

    if not approved:
        return {"total": 0, "executed": 0, "failed": 0, "skipped": 0}

    imap = IMAPService()
    requires_imap = any(
        a.action_type in ("move", "mark_read", "delete") for a in approved
    )
    if requires_imap and not imap.connect():
        logger.warning(
            "Could not connect to IMAP for action execution; "
            "IMAP-based actions may fail"
        )

    executor = ActionExecutor(imap)
    executed = 0
    failed = 0
    skipped = 0

    try:
        for action in approved:
            current_status = (action.status or "").lower()
            if current_status in ("executed", "executed_action"):
                skipped += 1
                continue
            if current_status in ("failed", "failed_action"):
                skipped += 1
                continue
            if current_status not in ("approved", "approved_action"):
                skipped += 1
                continue

            email = (
                db.query(ProcessedEmail)
                .filter(ProcessedEmail.id == action.email_id)
                .first()
            )

            success = executor.execute(action, email)
            if success:
                executed += 1
            else:
                failed += 1
                if not action.error_message:
                    action.error_message = "Execution failed"

            action.updated_at = datetime.utcnow()
            db.add(action)
            if email:
                db.add(email)

            # Refresh thread state
            thread_id = action.thread_id or (email.thread_id if email else None)
            if thread_id:
                try:
                    update_thread_state_for_thread(
                        db,
                        thread_id=thread_id,
                        user_address=getattr(settings, "imap_username", None),
                    )
                except Exception:
                    pass

            logger.info(
                "action_execution action_id=%s action_type=%s result=%s",
                action.id,
                action.action_type,
                "success" if success else "failure",
            )

        db.commit()
    except Exception as exc:
        sanitized = sanitize_error(exc, debug=settings.debug)
        logger.error("Action execution batch failed: %s", sanitized)
    finally:
        try:
            imap.disconnect()
        except Exception:
            pass

    stats = {
        "total": len(approved),
        "executed": executed,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "action_execution_complete total=%d executed=%d failed=%d skipped=%d",
        stats["total"],
        stats["executed"],
        stats["failed"],
        stats["skipped"],
    )
    return stats
