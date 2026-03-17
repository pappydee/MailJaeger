"""
Action execution service for structured action_queue items.
"""

from datetime import datetime
from typing import Tuple

from src.models.database import ActionQueue, ProcessedEmail
from src.services.imap_service import IMAPService
from src.utils.error_handling import sanitize_error
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ActionExecutor:
    """Execute approved actions safely and idempotently."""

    SUPPORTED_ACTIONS = {"move", "mark_read", "delete"}

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
            action.status = "failed"
            action.error_message = f"Action must be approved before execution (current={action.status})"
            return False

        valid, validation_error = self.validate_payload(action)
        if not valid:
            action.status = "failed"
            action.error_message = validation_error
            return False

        if not email or not email.uid:
            action.status = "failed"
            action.error_message = "Email or UID not found"
            return False

        try:
            uid = int(email.uid)
            payload = action.payload or {}
            success = False

            if action.action_type == "move":
                success = self.imap.move_to_folder(uid, payload["target_folder"])
                if success:
                    email.is_archived = True
            elif action.action_type == "mark_read":
                success = self.imap.mark_as_read(uid)
            elif action.action_type == "delete":
                success = self.imap.delete_message(uid)

            if success:
                action.status = "executed"
                action.executed_at = datetime.utcnow()
                action.error_message = None
                return True

            action.status = "failed"
            action.error_message = "IMAP operation failed"
            return False
        except Exception as exc:
            action.status = "failed"
            action.error_message = sanitize_error(exc, debug=False)
            logger.error("Action execution failed: %s", action.error_message)
            return False
