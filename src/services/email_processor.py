"""
Email processing service - orchestrates the email processing workflow
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import (
    ProcessedEmail,
    EmailTask,
    ProcessingRun,
    AuditLog,
    PendingAction
)
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.utils.logging import get_logger

logger = get_logger(__name__)


class EmailProcessor:
    """Main email processing orchestrator"""
    
    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
        self.imap_service = IMAPService()
        self.ai_service = AIService()
        self.stats = {
            'processed': 0,
            'spam': 0,
            'archived': 0,
            'action_required': 0,
            'failed': 0
        }
    
    def process_emails(self, trigger_type: str = "SCHEDULED") -> ProcessingRun:
        """
        Main processing workflow
        
        Steps:
        1. Retrieve unread emails
        2. AI analysis
        3. Spam classification
        4. Action/priority determination
        5. Mailbox actions
        6. Persistence
        """
        # Create processing run record
        run = ProcessingRun(
            started_at=datetime.utcnow(),
            trigger_type=trigger_type,
            status="IN_PROGRESS"
        )
        self.db.add(run)
        self.db.commit()
        
        logger.info(f"Starting email processing run (ID: {run.id}, Type: {trigger_type})")
        
        try:
            # Step 1: Retrieve emails
            with self.imap_service as imap:
                if not imap.client:
                    raise Exception("Failed to connect to IMAP server")
                
                emails = imap.get_unread_emails(
                    max_count=self.settings.max_emails_per_run
                )
                
                if not emails:
                    logger.info("No unread emails to process")
                    run.status = "SUCCESS"
                    run.completed_at = datetime.utcnow()
                    self.db.commit()
                    return run
                
                logger.info(f"Retrieved {len(emails)} emails for processing")
                
                # Process each email independently
                for email_data in emails:
                    try:
                        self._process_single_email(email_data, imap)
                    except Exception as e:
                        logger.error(f"Failed to process email {email_data.get('message_id')}: {e}")
                        self.stats['failed'] += 1
                        # Continue with next email (error isolation)
                        continue
            
            # Update run statistics
            run.emails_processed = self.stats['processed']
            run.emails_spam = self.stats['spam']
            run.emails_archived = self.stats['archived']
            run.emails_action_required = self.stats['action_required']
            run.emails_failed = self.stats['failed']
            run.status = "SUCCESS" if self.stats['failed'] == 0 else "PARTIAL"
            run.completed_at = datetime.utcnow()
            self.db.commit()
            
            logger.info(
                f"Processing run completed: {self.stats['processed']} processed, "
                f"{self.stats['spam']} spam, {self.stats['archived']} archived, "
                f"{self.stats['action_required']} action required, "
                f"{self.stats['failed']} failed"
            )
            
            return run
            
        except Exception as e:
            logger.error(f"Processing run failed: {e}")
            run.status = "FAILURE"
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            self.db.commit()
            return run
    
    def _process_single_email(self, email_data: Dict[str, Any], imap: IMAPService):
        """Process a single email through the complete workflow"""
        message_id = email_data.get('message_id')
        uid = int(email_data.get('uid'))
        
        logger.info(f"Processing email: {message_id}")
        
        # Check if already processed
        existing = self.db.query(ProcessedEmail).filter(
            ProcessedEmail.message_id == message_id
        ).first()
        
        if existing:
            logger.info(f"Email already processed: {message_id}")
            return
        
        # Step 2: AI Analysis with safe fallback
        try:
            analysis = self.ai_service.analyze_email(email_data)
        except Exception as e:
            logger.error(f"AI analysis failed for {message_id}: {e}")
            # Use fallback classification if AI fails
            analysis = self.ai_service._fallback_classification(email_data)
        
        # Step 3: Spam Classification
        is_spam = self._classify_spam(email_data, analysis)
        
        # Step 4: Action and Priority Determination
        action_required = analysis['action_required']
        priority = analysis['priority']
        
        # Create database record
        email_record = ProcessedEmail(
            message_id=message_id,
            uid=str(uid),
            subject=email_data.get('subject'),
            sender=email_data.get('sender'),
            recipients=email_data.get('recipients'),
            date=email_data.get('date'),
            body_plain=email_data.get('body_plain') if self.settings.store_email_body else None,
            body_html=email_data.get('body_html') if self.settings.store_email_body else None,
            summary=analysis['summary'],
            category=analysis['category'],
            spam_probability=analysis['spam_probability'],
            action_required=action_required,
            priority=priority,
            suggested_folder=analysis['suggested_folder'],
            reasoning=analysis['reasoning'],
            is_spam=is_spam,
            is_processed=True,
            integrity_hash=email_data.get('integrity_hash'),
            processed_at=datetime.utcnow()
        )
        
        # Add tasks
        for task_data in analysis.get('tasks', []):
            task = EmailTask(
                description=task_data['description'],
                due_date=task_data.get('due_date'),
                context=task_data.get('context'),
                confidence=task_data.get('confidence')
            )
            email_record.tasks.append(task)
        
        # Step 5: Persistence (add email_record first to get ID)
        self.db.add(email_record)
        self.db.flush()  # Flush to get email_record.id for PendingActions
        
        # Step 6: Mailbox Actions (with approval workflow or safe mode)
        actions_taken = []
        
        if self.settings.require_approval:
            # REQUIRE_APPROVAL=true: Queue actions as PendingActions
            logger.info(
                f"REQUIRE_APPROVAL enabled: Queuing actions for {message_id}"
            )
            actions_taken.append("queued_pending_actions")
            
            # Queue spam handling action
            if is_spam:
                if self.settings.delete_spam:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE",
                        target_folder=self.settings.spam_folder,
                        reason=f"Spam detected (probability: {spam_probability:.2f})",
                        status="PENDING",
                        proposed_by="AI"
                    )
                else:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE",
                        target_folder=self.settings.quarantine_folder,
                        reason=f"Spam quarantine (probability: {spam_probability:.2f})",
                        status="PENDING",
                        proposed_by="AI"
                    )
                self.db.add(pending_action)
                logger.info(
                    f"Queued MOVE action to {pending_action.target_folder}"
                )
            else:
                # Queue mark as read if enabled
                if self.settings.mark_as_read:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="MARK_READ",
                        reason="Mark processed email as read",
                        status="PENDING",
                        proposed_by="AI"
                    )
                    self.db.add(pending_action)
                    logger.info("Queued MARK_READ action")
                
                # Queue move to archive
                pending_action = PendingAction(
                    email_id=email_record.id,
                    action_type="MOVE",
                    target_folder=self.settings.archive_folder,
                    reason=f"Archive processed email (category: {analysis['category']})",
                    status="PENDING",
                    proposed_by="AI"
                )
                self.db.add(pending_action)
                logger.info(f"Queued MOVE action to {self.settings.archive_folder}")
                
                # Queue flag if action required
                if action_required:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="FLAG",
                        reason="Action required flag",
                        status="PENDING",
                        proposed_by="AI"
                    )
                    self.db.add(pending_action)
                    logger.info("Queued FLAG action")
                    self.stats['action_required'] += 1
        
        elif self.settings.safe_mode:
            # SAFE_MODE=true: Skip actions (dry-run)
            logger.info(f"SAFE MODE: Skipping IMAP actions for {message_id}")
            actions_taken.append("safe_mode_skip")
        
        else:
            # REQUIRE_APPROVAL=false, SAFE_MODE=false: Execute immediately
            if is_spam:
                # Handle spam based on configuration
                if self.settings.delete_spam:
                    # Move to spam folder (not actual deletion for safety)
                    if imap.move_to_folder(uid, self.settings.spam_folder):
                        actions_taken.append("moved_to_spam")
                        email_record.is_archived = True
                        self.stats['spam'] += 1
                        logger.info(
                            f"Moved spam email to {self.settings.spam_folder}"
                        )
                else:
                    # Move to quarantine folder for review
                    if imap.move_to_folder(uid, self.settings.quarantine_folder):
                        actions_taken.append("moved_to_quarantine")
                        email_record.is_archived = True
                        self.stats['spam'] += 1
                        logger.info(
                            f"Moved spam email to quarantine: "
                            f"{self.settings.quarantine_folder}"
                        )
            else:
                # Mark as read if configured
                if self.settings.mark_as_read:
                    if imap.mark_as_read(uid):
                        actions_taken.append("marked_as_read")
                
                # Move to archive
                if imap.move_to_folder(uid, self.settings.archive_folder):
                    actions_taken.append("moved_to_archive")
                    email_record.is_archived = True
                    self.stats['archived'] += 1
                
                # Flag if action required
                if action_required:
                    if imap.add_flag(uid):
                        actions_taken.append("flagged")
                        email_record.is_flagged = True
                    self.stats['action_required'] += 1
        
        email_record.actions_taken = {"actions": actions_taken}
        
        # Step 7: Add audit log
        audit = AuditLog(
            event_type="EMAIL_PROCESSED",
            email_message_id=message_id,
            description=f"Email processed: spam={is_spam}, action_required={action_required}, safe_mode={self.settings.safe_mode}",
            data={
                "category": analysis['category'],
                "priority": priority,
                "actions": actions_taken,
                "safe_mode": self.settings.safe_mode
            }
        )
        self.db.add(audit)
        
        self.db.commit()
        self.stats['processed'] += 1
        
        logger.info(
            f"Email processed successfully: spam={is_spam}, "
            f"action={action_required}, priority={priority}, "
            f"safe_mode={self.settings.safe_mode}"
        )
    
    def _classify_spam(self, email_data: Dict[str, Any], analysis: Dict[str, Any]) -> bool:
        """
        Classify email as spam
        
        Combines AI spam probability with heuristic indicators
        """
        spam_prob = analysis['spam_probability']
        
        # Heuristic checks
        subject = email_data.get('subject', '').lower()
        body = email_data.get('body_plain', '').lower()
        
        # Check for unsubscribe headers (common in newsletters/marketing)
        has_unsubscribe = 'unsubscribe' in body or 'abmelden' in body
        
        # Adjust probability based on heuristics
        if has_unsubscribe:
            spam_prob = max(spam_prob, 0.6)
        
        # Check against threshold
        return spam_prob >= self.settings.spam_threshold
