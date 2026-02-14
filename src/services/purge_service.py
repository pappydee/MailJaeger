"""
Retention and purge service for MailJaeger
"""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from typing import Dict, Any

from src.config import get_settings
from src.models.database import ProcessedEmail, PendingAction, AuditLog
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PurgeService:
    """Service for purging old data based on retention policies"""
    
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
    
    def execute_purge(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute data purge based on retention policies
        
        Args:
            dry_run: If True, only count records without deleting
            
        Returns:
            Dictionary with purge statistics
        """
        stats = {
            'dry_run': dry_run,
            'emails_deleted': 0,
            'actions_deleted': 0,
            'audit_logs_deleted': 0,
            'errors': []
        }
        
        try:
            # Purge old emails
            if self.settings.retention_days_emails > 0:
                stats['emails_deleted'] = self._purge_old_emails(
                    self.settings.retention_days_emails,
                    dry_run
                )
            
            # Purge old completed/rejected pending actions
            if self.settings.retention_days_actions > 0:
                stats['actions_deleted'] = self._purge_old_actions(
                    self.settings.retention_days_actions,
                    dry_run
                )
            
            # Purge old audit logs
            if self.settings.retention_days_audit > 0:
                stats['audit_logs_deleted'] = self._purge_old_audit_logs(
                    self.settings.retention_days_audit,
                    dry_run
                )
            
            if not dry_run:
                self.db.commit()
                logger.info(
                    f"Purge completed: {stats['emails_deleted']} emails, "
                    f"{stats['actions_deleted']} actions, "
                    f"{stats['audit_logs_deleted']} audit logs deleted"
                )
            else:
                logger.info(
                    f"Purge dry-run: would delete {stats['emails_deleted']} emails, "
                    f"{stats['actions_deleted']} actions, "
                    f"{stats['audit_logs_deleted']} audit logs"
                )
        
        except Exception as e:
            self.db.rollback()
            error_msg = f"Purge failed: {type(e).__name__}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            stats['errors'].append(error_msg)
        
        return stats
    
    def _purge_old_emails(self, retention_days: int, dry_run: bool) -> int:
        """Purge emails older than retention period"""
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        query = self.db.query(ProcessedEmail).filter(
            ProcessedEmail.created_at < cutoff_date
        )
        
        count = query.count()
        
        if not dry_run and count > 0:
            # Deleting related records is handled by cascade
            query.delete(synchronize_session=False)
            logger.info(f"Deleted {count} emails older than {retention_days} days")
        
        return count
    
    def _purge_old_actions(self, retention_days: int, dry_run: bool) -> int:
        """
        Purge completed/rejected/failed pending actions older than retention period
        
        NOTE: Does NOT delete PENDING or APPROVED actions
        """
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        # Only delete actions that are in terminal states
        terminal_states = ['APPLIED', 'REJECTED', 'FAILED']
        
        query = self.db.query(PendingAction).filter(
            PendingAction.created_at < cutoff_date,
            PendingAction.status.in_(terminal_states)
        )
        
        count = query.count()
        
        if not dry_run and count > 0:
            query.delete(synchronize_session=False)
            logger.info(
                f"Deleted {count} completed/rejected actions older than {retention_days} days"
            )
        
        return count
    
    def _purge_old_audit_logs(self, retention_days: int, dry_run: bool) -> int:
        """Purge audit logs older than retention period"""
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        query = self.db.query(AuditLog).filter(
            AuditLog.created_at < cutoff_date
        )
        
        count = query.count()
        
        if not dry_run and count > 0:
            query.delete(synchronize_session=False)
            logger.info(f"Deleted {count} audit logs older than {retention_days} days")
        
        return count


def get_purge_service(db: Session) -> PurgeService:
    """Get purge service instance"""
    return PurgeService(db)
