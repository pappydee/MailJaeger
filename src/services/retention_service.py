"""
Data retention and purge service
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_

from src.config import get_settings
from src.models.database import ProcessedEmail, PendingAction, AuditLog
from src.utils.logging import get_logger

logger = get_logger(__name__)


class RetentionService:
    """Service for managing data retention and purge"""
    
    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
    
    def purge_old_data(self) -> Dict[str, Any]:
        """
        Purge old data according to retention policies
        
        Returns:
            Summary of purge operation
        """
        logger.info("Starting data purge job")
        
        results = {
            "success": True,
            "emails_purged": 0,
            "actions_purged": 0,
            "errors": []
        }
        
        try:
            # Purge old emails
            if self.settings.retention_days_emails > 0:
                emails_purged = self._purge_old_emails()
                results["emails_purged"] = emails_purged
                logger.info(f"Purged {emails_purged} old emails")
            
            # Purge old completed/failed actions
            if self.settings.retention_days_actions > 0:
                actions_purged = self._purge_old_actions()
                results["actions_purged"] = actions_purged
                logger.info(f"Purged {actions_purged} old actions")
            
            # Add audit log
            audit = AuditLog(
                event_type="DATA_PURGE",
                email_message_id=None,
                description=f"Data purge completed: {results['emails_purged']} emails, {results['actions_purged']} actions",
                data=results
            )
            self.db.add(audit)
            self.db.commit()
            
        except Exception as e:
            logger.error(f"Data purge failed: {e}", exc_info=True)
            results["success"] = False
            results["errors"].append(str(e))
            self.db.rollback()
        
        return results
    
    def _purge_old_emails(self) -> int:
        """
        Purge old processed emails
        
        Note: Only purges if STORE_EMAIL_BODY is enabled,
        otherwise we keep metadata for analysis
        """
        if self.settings.retention_days_emails == 0:
            # Never purge when set to 0
            logger.info("Email purge disabled (RETENTION_DAYS_EMAILS=0)")
            return 0
        
        if not self.settings.store_email_body:
            # Don't purge if we're not storing bodies (minimal footprint)
            logger.info("Skipping email purge (STORE_EMAIL_BODY=false)")
            return 0
        
        cutoff_date = datetime.utcnow() - timedelta(days=self.settings.retention_days_emails)
        
        # Find old emails
        old_emails = self.db.query(ProcessedEmail).filter(
            ProcessedEmail.created_at < cutoff_date
        ).all()
        
        count = len(old_emails)
        
        if count > 0:
            # Delete old emails (cascade will delete related tasks and learning signals)
            for email in old_emails:
                self.db.delete(email)
            
            self.db.commit()
            logger.info(f"Purged {count} emails older than {cutoff_date}")
        
        return count
    
    def _purge_old_actions(self) -> int:
        """
        Purge old completed/failed/rejected pending actions
        
        Note: Never purges PENDING or APPROVED actions automatically
        """
        if self.settings.retention_days_actions == 0:
            # Never purge when set to 0
            logger.info("Action purge disabled (RETENTION_DAYS_ACTIONS=0)")
            return 0
        
        cutoff_date = datetime.utcnow() - timedelta(days=self.settings.retention_days_actions)
        
        # Find old completed/failed/rejected actions
        old_actions = self.db.query(PendingAction).filter(
            and_(
                PendingAction.created_at < cutoff_date,
                PendingAction.status.in_(["APPLIED", "FAILED", "REJECTED"])
            )
        ).all()
        
        count = len(old_actions)
        
        if count > 0:
            for action in old_actions:
                self.db.delete(action)
            
            self.db.commit()
            logger.info(f"Purged {count} actions older than {cutoff_date}")
        
        return count
