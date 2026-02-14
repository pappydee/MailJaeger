"""
Service for managing pending IMAP actions requiring approval
"""
import logging
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from src.config import get_settings
from src.models.database import PendingAction, ProcessedEmail, AuditLog
from src.services.imap_service import IMAPService
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Global lock for applying actions (prevents concurrent execution)
_apply_lock = threading.Lock()


class PendingActionsService:
    """Service for managing pending actions approval workflow"""
    
    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
    
    def enqueue_action(
        self,
        email_id: int,
        action_type: str,
        reason: str,
        proposed_by: str = "system",
        target_folder: Optional[str] = None
    ) -> Optional[PendingAction]:
        """
        Enqueue a pending action for approval
        
        Args:
            email_id: Database ID of the email
            action_type: MARK_READ, MOVE_FOLDER, FLAG, DELETE
            reason: spam, action_required, archive_policy, ai_suggestion, etc.
            proposed_by: system or user
            target_folder: Only for MOVE_FOLDER actions
        
        Returns:
            PendingAction if created, None if duplicate found
        """
        # Validate action type
        valid_actions = ["MARK_READ", "MOVE_FOLDER", "FLAG", "DELETE"]
        if action_type not in valid_actions:
            logger.error(f"Invalid action type: {action_type}")
            return None
        
        # Validate folder allowlist for MOVE_FOLDER
        if action_type == "MOVE_FOLDER":
            if not target_folder:
                logger.error("MOVE_FOLDER action requires target_folder")
                return None
            
            allowed_folders = self.settings.get_allowed_folders()
            if target_folder not in allowed_folders:
                logger.warning(f"Folder '{target_folder}' not in allowlist: {allowed_folders}")
                # Create a FAILED action record instead
                action = PendingAction(
                    email_id=email_id,
                    action_type=action_type,
                    target_folder=target_folder,
                    reason=reason,
                    proposed_by=proposed_by,
                    status="FAILED",
                    error_code="FOLDER_NOT_ALLOWED",
                    error_message=f"Folder not in allowed list: {allowed_folders}"
                )
                self.db.add(action)
                self.db.commit()
                return action
        
        # Check for duplicates (same email + action type + status=PENDING/APPROVED)
        duplicate = self.db.query(PendingAction).filter(
            and_(
                PendingAction.email_id == email_id,
                PendingAction.action_type == action_type,
                or_(
                    PendingAction.status == "PENDING",
                    PendingAction.status == "APPROVED"
                )
            )
        ).first()
        
        if duplicate:
            logger.info(f"Duplicate pending action found for email {email_id}: {action_type}")
            return None
        
        # Create pending action
        action = PendingAction(
            email_id=email_id,
            action_type=action_type,
            target_folder=target_folder,
            reason=reason,
            proposed_by=proposed_by,
            status="PENDING"
        )
        
        self.db.add(action)
        self.db.commit()
        
        logger.info(f"Enqueued {action_type} action for email {email_id} (reason: {reason})")
        
        # Add audit log
        self._add_audit_log(
            "ACTION_ENQUEUED",
            action.id,
            email_id,
            f"Action enqueued: {action_type} (reason: {reason})"
        )
        
        return action
    
    def list_pending_actions(
        self,
        status: Optional[str] = None,
        action_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        List pending actions with filters and pagination
        
        Returns:
            Dictionary with results, total count, and pagination info
        """
        query = self.db.query(PendingAction)
        
        # Apply filters
        if status:
            query = query.filter(PendingAction.status == status)
        if action_type:
            query = query.filter(PendingAction.action_type == action_type)
        
        # Get total count
        total = query.count()
        
        # Apply pagination
        offset = (page - 1) * page_size
        actions = query.order_by(
            PendingAction.created_at.desc()
        ).offset(offset).limit(page_size).all()
        
        return {
            "results": actions,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics of pending actions"""
        summary = {}
        
        # Count by status
        for status in ["PENDING", "APPROVED", "REJECTED", "APPLIED", "FAILED"]:
            count = self.db.query(PendingAction).filter(
                PendingAction.status == status
            ).count()
            summary[f"status_{status.lower()}"] = count
        
        # Count by action type (for pending only)
        for action_type in ["MARK_READ", "MOVE_FOLDER", "FLAG", "DELETE"]:
            count = self.db.query(PendingAction).filter(
                and_(
                    PendingAction.action_type == action_type,
                    PendingAction.status == "PENDING"
                )
            ).count()
            summary[f"type_{action_type.lower()}"] = count
        
        return summary
    
    def approve_action(self, action_id: int, approved_by: str = "admin") -> bool:
        """Approve a pending action"""
        action = self.db.query(PendingAction).filter(
            PendingAction.id == action_id
        ).first()
        
        if not action:
            logger.error(f"Action {action_id} not found")
            return False
        
        if action.status != "PENDING":
            logger.warning(f"Action {action_id} is not pending (status: {action.status})")
            return False
        
        action.status = "APPROVED"
        action.approved_at = datetime.utcnow()
        action.approved_by = approved_by
        
        self.db.commit()
        
        logger.info(f"Approved action {action_id}: {action.action_type}")
        
        # Add audit log
        self._add_audit_log(
            "ACTION_APPROVED",
            action_id,
            action.email_id,
            f"Action approved: {action.action_type}"
        )
        
        return True
    
    def reject_action(self, action_id: int, rejected_by: str = "admin") -> bool:
        """Reject a pending action"""
        action = self.db.query(PendingAction).filter(
            PendingAction.id == action_id
        ).first()
        
        if not action:
            logger.error(f"Action {action_id} not found")
            return False
        
        if action.status != "PENDING":
            logger.warning(f"Action {action_id} is not pending (status: {action.status})")
            return False
        
        action.status = "REJECTED"
        # Store rejection time and user in approved_at/approved_by for now
        # (Consider renaming to reviewed_at/reviewed_by in future schema update)
        action.approved_at = datetime.utcnow()
        action.approved_by = f"rejected_by:{rejected_by}"
        
        self.db.commit()
        
        logger.info(f"Rejected action {action_id}: {action.action_type}")
        
        # Add audit log
        self._add_audit_log(
            "ACTION_REJECTED",
            action_id,
            action.email_id,
            f"Action rejected: {action.action_type}"
        )
        
        return True
    
    def apply_action(self, action_id: int) -> Dict[str, Any]:
        """
        Apply a single approved action
        
        Returns:
            Result dictionary with success status and message
        """
        action = self.db.query(PendingAction).filter(
            PendingAction.id == action_id
        ).first()
        
        if not action:
            return {"success": False, "error": "Action not found"}
        
        if action.status != "APPROVED":
            return {"success": False, "error": f"Action not approved (status: {action.status})"}
        
        # Get email record
        email = self.db.query(ProcessedEmail).filter(
            ProcessedEmail.id == action.email_id
        ).first()
        
        if not email:
            action.status = "FAILED"
            action.error_code = "EMAIL_NOT_FOUND"
            action.error_message = "Email record not found"
            self.db.commit()
            return {"success": False, "error": "Email not found"}
        
        # Validate folder allowlist again at apply time
        if action.action_type == "MOVE_FOLDER":
            allowed_folders = self.settings.get_allowed_folders()
            if action.target_folder not in allowed_folders:
                action.status = "FAILED"
                action.error_code = "FOLDER_NOT_ALLOWED"
                action.error_message = f"Folder not in allowed list: {allowed_folders}"
                self.db.commit()
                return {"success": False, "error": "Folder not allowed"}
        
        # Apply the action via IMAP
        result = self._apply_imap_action(email, action)
        
        if result["success"]:
            action.status = "APPLIED"
            action.applied_at = datetime.utcnow()
            logger.info(f"Applied action {action_id}: {action.action_type}")
            
            # Add audit log
            self._add_audit_log(
                "ACTION_APPLIED",
                action_id,
                action.email_id,
                f"Action applied successfully: {action.action_type}",
                result.get("data")
            )
        else:
            action.status = "FAILED"
            action.error_code = result.get("error_code", "UNKNOWN_ERROR")
            # Sanitize error message (never include credentials or server banners)
            action.error_message = self._sanitize_error_message(result.get("error", "Unknown error"))
            logger.error(f"Failed to apply action {action_id}: {action.error_message}")
            
            # Add audit log
            self._add_audit_log(
                "ACTION_FAILED",
                action_id,
                action.email_id,
                f"Action failed: {action.action_type}",
                {"error": action.error_message}
            )
        
        self.db.commit()
        return result
    
    def apply_batch(self, max_count: Optional[int] = None) -> Dict[str, Any]:
        """
        Apply multiple approved actions in batch
        
        Args:
            max_count: Maximum number of actions to apply (uses config default if None)
        
        Returns:
            Summary of batch application
        """
        # Acquire lock to prevent concurrent batch applies
        acquired = _apply_lock.acquire(blocking=False)
        if not acquired:
            return {
                "success": False,
                "error": "Another batch apply is in progress",
                "error_code": "CONCURRENT_APPLY"
            }
        
        try:
            if max_count is None:
                max_count = self.settings.max_pending_actions_per_run
            
            # Get approved actions
            approved_actions = self.db.query(PendingAction).filter(
                PendingAction.status == "APPROVED"
            ).order_by(PendingAction.created_at.asc()).limit(max_count).all()
            
            if not approved_actions:
                return {
                    "success": True,
                    "message": "No approved actions to apply",
                    "applied": 0,
                    "failed": 0
                }
            
            applied = 0
            failed = 0
            
            for action in approved_actions:
                result = self.apply_action(action.id)
                if result["success"]:
                    applied += 1
                else:
                    failed += 1
            
            return {
                "success": True,
                "message": f"Batch apply completed: {applied} applied, {failed} failed",
                "applied": applied,
                "failed": failed,
                "total": len(approved_actions)
            }
        
        finally:
            _apply_lock.release()
    
    def _apply_imap_action(
        self,
        email: ProcessedEmail,
        action: PendingAction
    ) -> Dict[str, Any]:
        """
        Apply IMAP action for an email
        
        Returns:
            Result dictionary with success status
        """
        if not email.uid:
            return {
                "success": False,
                "error": "Email UID not available",
                "error_code": "MISSING_UID"
            }
        
        try:
            uid = int(email.uid)
        except (ValueError, TypeError):
            return {
                "success": False,
                "error": "Invalid email UID",
                "error_code": "INVALID_UID"
            }
        
        try:
            with IMAPService() as imap:
                if not imap.client:
                    return {
                        "success": False,
                        "error": "Failed to connect to IMAP server",
                        "error_code": "IMAP_CONNECTION_FAILED"
                    }
                
                if action.action_type == "MARK_READ":
                    success = imap.mark_as_read(uid)
                    if success:
                        return {"success": True, "data": {"action": "marked_read"}}
                    else:
                        return {
                            "success": False,
                            "error": "Failed to mark as read",
                            "error_code": "IMAP_MARK_READ_FAILED"
                        }
                
                elif action.action_type == "MOVE_FOLDER":
                    success = imap.move_to_folder(uid, action.target_folder)
                    if success:
                        return {
                            "success": True,
                            "data": {"action": "moved", "folder": action.target_folder}
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to move to folder: {action.target_folder}",
                            "error_code": "IMAP_MOVE_FAILED"
                        }
                
                elif action.action_type == "FLAG":
                    success = imap.add_flag(uid)
                    if success:
                        return {"success": True, "data": {"action": "flagged"}}
                    else:
                        return {
                            "success": False,
                            "error": "Failed to add flag",
                            "error_code": "IMAP_FLAG_FAILED"
                        }
                
                elif action.action_type == "DELETE":
                    # Move to spam folder instead of actual deletion (safety)
                    success = imap.move_to_folder(uid, self.settings.spam_folder)
                    if success:
                        return {
                            "success": True,
                            "data": {"action": "moved_to_spam"}
                        }
                    else:
                        return {
                            "success": False,
                            "error": "Failed to move to spam folder",
                            "error_code": "IMAP_DELETE_FAILED"
                        }
                
                else:
                    return {
                        "success": False,
                        "error": f"Unknown action type: {action.action_type}",
                        "error_code": "UNKNOWN_ACTION_TYPE"
                    }
        
        except Exception as e:
            logger.error(f"IMAP action failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": self._sanitize_error_message(str(e)),
                "error_code": "IMAP_ERROR"
            }
    
    def _sanitize_error_message(self, message: str) -> str:
        """
        Sanitize error message to prevent leaking sensitive information
        
        Removes:
        - Credentials
        - Server banners
        - File paths with secrets
        - Stack traces
        """
        # Truncate long messages
        if len(message) > 500:
            message = message[:500] + "..."
        
        # Check for common credential patterns (more specific)
        import re
        
        # Patterns for actual credentials (not just words)
        sensitive_patterns = [
            r'password["\s:=]+[^\s]+',  # password="xxx" or password: xxx
            r'passwd["\s:=]+[^\s]+',
            r'secret["\s:=]+[^\s]+',
            r'token["\s:=]+[^\s]+',
            r'key["\s:=]+[^\s]+',
            r'authorization["\s:=]+[^\s]+',
            r'bearer\s+[a-zA-Z0-9_-]+',  # bearer tokens
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',  # email addresses
        ]
        
        lower_msg = message.lower()
        for pattern in sensitive_patterns:
            if re.search(pattern, lower_msg):
                return "Operation failed (details omitted for security)"
        
        return message
    
    def _add_audit_log(
        self,
        event_type: str,
        action_id: int,
        email_id: int,
        description: str,
        data: Optional[Dict[str, Any]] = None
    ):
        """Add audit log entry"""
        # Get email message_id for audit trail
        email = self.db.query(ProcessedEmail).filter(
            ProcessedEmail.id == email_id
        ).first()
        
        message_id = email.message_id if email else None
        
        audit = AuditLog(
            event_type=event_type,
            email_message_id=message_id,
            description=description,
            data={
                "pending_action_id": action_id,
                "email_id": email_id,
                **(data or {})
            }
        )
        
        self.db.add(audit)
        # Commit handled by caller
