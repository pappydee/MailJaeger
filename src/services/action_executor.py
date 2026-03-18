"""
Action execution service for structured action_queue items.
"""

from datetime import datetime
from typing import Tuple
import copy

from src.models.database import ActionQueue, ProcessedEmail
from src.services.imap_service import IMAPService
from src.utils.error_handling import sanitize_error
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ActionExecutor:
    """Execute approved actions safely and idempotently."""

    SUPPORTED_ACTIONS = {"move", "mark_read", "delete", "mark_resolved", "reply_draft"}

    def __init__(self, imap: IMAPService):
        self.imap = imap

    def validate_payload(self, action: ActionQueue) -> Tuple[bool, str]:
        """Validate action payload before execution."""
        if action.action_type not in self.SUPPORTED_ACTIONS:
            return False, f"Unsupported action type: {action.action_type}"

        payload = action.payload or {}
        if not isinstance(payload, dict):
            return False, "payload must be a JSON object"

        if action.action_type == "move":
            target_folder = payload.get("target_folder")
            if not isinstance(target_folder, str) or not target_folder.strip():
                return False, "move action requires payload.target_folder"
        elif action.action_type == "reply_draft":
            draft_text = payload.get("draft_text")
            if not isinstance(draft_text, str) or not draft_text.strip():
                return False, "reply_draft action requires payload.draft_text"

        return True, ""

    def execute(self, action: ActionQueue, email: ProcessedEmail) -> bool:
        """
        Execute one approved action.

        Idempotency:
        - already executed -> no-op success
        - only approved actions can execute

        Compatibility:
        - accepts legacy state names approved_action/executed_action
          while writing normalized status values approved/executed/failed.
        """
        if action.status in ("executed", "executed_action"):
            return True

        if action.status not in ("approved", "approved_action"):
            previous_status = action.status
            action.status = "failed"
            action.error_message = (
                f"Action must be approved before execution (current={previous_status})"
            )
            logger.warning(
                "Action %s transition %s -> %s failed: not approved",
                getattr(action, "id", None),
                previous_status,
                action.status,
            )
            return False

        valid, validation_error = self.validate_payload(action)
        if not valid:
            previous_status = action.status
            action.status = "failed"
            action.error_message = validation_error
            logger.warning(
                "Action %s transition %s -> %s failed: %s",
                getattr(action, "id", None),
                previous_status,
                action.status,
                validation_error,
            )
            return False

        if not email or not email.uid:
            previous_status = action.status
            action.status = "failed"
            action.error_message = "Email or UID not found"
            logger.warning(
                "Action %s transition %s -> %s failed: email/uid missing",
                getattr(action, "id", None),
                previous_status,
                action.status,
            )
            return False

        try:
            uid = int(email.uid)
            payload = copy.deepcopy(action.payload or {})
            success = False

            if action.action_type == "move":
                success = self.imap.move_to_folder(uid, payload["target_folder"])
                if success:
                    email.is_archived = True
            elif action.action_type == "mark_read":
                success = self.imap.mark_as_read(uid)
            elif action.action_type == "delete":
                success = self.imap.delete_message(uid)
            elif action.action_type == "mark_resolved":
                email.is_resolved = True
                success = True
            elif action.action_type == "reply_draft":
                payload["draft_state"] = (
                    payload.get("draft_state") or "proposed_manual_send"
                )
                action.payload = payload
                success = True

            if success:
                previous_status = action.status
                action.status = "executed"
                action.executed_at = datetime.utcnow()
                action.error_message = None
                logger.info(
                    "Action %s transition %s -> %s",
                    getattr(action, "id", None),
                    previous_status,
                    action.status,
                )
                return True

            previous_status = action.status
            action.status = "failed"
            action.error_message = "IMAP operation failed"
            logger.warning(
                "Action %s transition %s -> %s failed: imap operation returned false",
                getattr(action, "id", None),
                previous_status,
                action.status,
            )
            return False
        except Exception as exc:
            previous_status = action.status
            action.status = "failed"
            action.error_message = sanitize_error(exc, debug=False)
            logger.error(
                "Action %s transition %s -> %s failed: %s",
                getattr(action, "id", None),
                previous_status,
                action.status,
                action.error_message,
            )
            return False
