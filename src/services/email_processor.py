"""
Email processing service - orchestrates the email processing workflow
"""

import logging
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from datetime import datetime
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import (
    ProcessedEmail,
    EmailTask,
    ProcessingRun,
    AuditLog,
    PendingAction,
    ClassificationOverride,
)
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

if TYPE_CHECKING:
    from src.services.scheduler import RunStatus

logger = get_logger(__name__)


class EmailProcessor:
    """Main email processing orchestrator"""

    def __init__(self, db_session: Session, status: Optional["RunStatus"] = None):
        self.settings = get_settings()
        self.db = db_session
        self.imap_service = IMAPService()
        self.ai_service = AIService()
        self._status = status  # optional shared RunStatus for live progress updates
        self.stats = {
            "processed": 0,
            "spam": 0,
            "archived": 0,
            "action_required": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # Internal helper to push progress updates without crashing
    # ------------------------------------------------------------------
    def _update_status(self, **kwargs) -> None:
        if self._status is not None:
            try:
                self._status.update(**kwargs)
            except Exception:
                pass  # never let status updates break the processor

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
            status="IN_PROGRESS",
        )
        self.db.add(run)
        self.db.commit()

        # Report run_id back to RunStatus as soon as we have it
        self._update_status(run_id=run.id)

        logger.info(
            f"Starting email processing run (ID: {run.id}, Type: {trigger_type})"
        )

        try:
            # Step 1: Retrieve emails
            self._update_status(current_step="Connecting to mail server…", progress_percent=5)
            try:
                with self.imap_service as imap:
                    self._update_status(current_step="Fetching unread emails…", progress_percent=10)
                    emails = imap.get_unread_emails(
                        max_count=self.settings.max_emails_per_run
                    )

                    if not emails:
                        logger.info("No unread emails to process")
                        self._update_status(
                            current_step=None,
                            progress_percent=100,
                            total=0,
                            message="No new emails",
                        )
                        run.status = "SUCCESS"
                        run.completed_at = datetime.utcnow()
                        self.db.commit()
                        return run

                    total = len(emails)
                    logger.info(f"Retrieved {total} emails for processing")
                    self._update_status(
                        current_step=f"Analysing {total} email(s)…",
                        progress_percent=15,
                        total=total,
                    )

                    # Process each email independently
                    for idx, email_data in enumerate(emails, start=1):
                        pct = 15 + int((idx / total) * 75)  # 15 → 90 %
                        self._update_status(
                            current_step=f"Analysing {idx}/{total}…",
                            progress_percent=pct,
                            processed=self.stats["processed"],
                            spam=self.stats["spam"],
                            action_required=self.stats["action_required"],
                            failed=self.stats["failed"],
                        )
                        try:
                            self._process_single_email(email_data, imap)
                        except Exception as e:
                            sanitized_error = sanitize_error(
                                e, debug=self.settings.debug
                            )
                            logger.error(
                                f"Failed to process email {email_data.get('message_id')}: {sanitized_error}"
                            )
                            self.stats["failed"] += 1
                            # Continue with next email (error isolation)
                            continue

            except RuntimeError as e:
                # IMAP connection failed - fail closed
                sanitized_error = sanitize_error(e, debug=self.settings.debug)
                logger.error(f"IMAP connection failed: {sanitized_error}")
                self._update_status(
                    status="failed",
                    current_step=None,
                    message="IMAP connection failed",
                )
                run.status = "FAILURE"
                run.error_message = sanitize_error(e, debug=self.settings.debug)
                run.completed_at = datetime.utcnow()
                self.db.commit()
                return run

            # Update run statistics
            self._update_status(current_step="Saving results…", progress_percent=95)
            run.emails_processed = self.stats["processed"]
            run.emails_spam = self.stats["spam"]
            run.emails_archived = self.stats["archived"]
            run.emails_action_required = self.stats["action_required"]
            run.emails_failed = self.stats["failed"]
            run.status = "SUCCESS" if self.stats["failed"] == 0 else "PARTIAL"
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
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Processing run failed: {sanitized_error}")
            run.status = "FAILURE"
            run.error_message = sanitize_error(e, debug=self.settings.debug)
            run.completed_at = datetime.utcnow()
            self.db.commit()
            return run

    def _process_single_email(self, email_data: Dict[str, Any], imap: IMAPService):
        """Process a single email through the complete workflow"""
        message_id = email_data.get("message_id")
        uid = int(email_data.get("uid"))

        logger.info(f"Processing email: {message_id}")

        # Check if already processed
        existing = (
            self.db.query(ProcessedEmail)
            .filter(ProcessedEmail.message_id == message_id)
            .first()
        )

        if existing:
            logger.info(f"Email already processed: {message_id}")
            return

        # Step 2: Check for a matching classification override rule first.
        # If a rule matches, skip the AI call entirely.
        override_rule = self._find_matching_override(email_data)
        applied_override = override_rule is not None

        if applied_override:
            logger.info(
                f"Override rule {override_rule.id} matched for {message_id} "
                f"(pattern: {override_rule.sender_pattern!r})"
            )
            analysis = self._build_analysis_from_override(override_rule, email_data)
        else:
            # Step 2b: AI Analysis with safe fallback
            try:
                analysis = self.ai_service.analyze_email(email_data)
            except Exception as e:
                sanitized_error = sanitize_error(e, debug=self.settings.debug)
                logger.error(f"AI analysis failed for {message_id}: {sanitized_error}")
                # Use fallback classification if AI fails
                analysis = self.ai_service._fallback_classification(email_data)

        # Step 3: Spam Classification
        is_spam = self._classify_spam(email_data, analysis)

        # Step 4: Action and Priority Determination
        action_required = analysis["action_required"]
        priority = analysis["priority"]

        # Create database record
        email_record = ProcessedEmail(
            message_id=message_id,
            uid=str(uid),
            subject=email_data.get("subject"),
            sender=email_data.get("sender"),
            recipients=email_data.get("recipients"),
            date=email_data.get("date"),
            body_plain=(
                email_data.get("body_plain") if self.settings.store_email_body else None
            ),
            body_html=(
                email_data.get("body_html") if self.settings.store_email_body else None
            ),
            summary=analysis["summary"],
            category=analysis["category"],
            spam_probability=analysis["spam_probability"],
            action_required=action_required,
            priority=priority,
            suggested_folder=analysis["suggested_folder"],
            reasoning=analysis["reasoning"],
            is_spam=is_spam,
            is_processed=True,
            integrity_hash=email_data.get("integrity_hash"),
            processed_at=datetime.utcnow(),
            overridden=applied_override,
            override_rule_id=override_rule.id if override_rule else None,
        )

        # Add tasks
        for task_data in analysis.get("tasks", []):
            task = EmailTask(
                description=task_data["description"],
                due_date=task_data.get("due_date"),
                context=task_data.get("context"),
                confidence=task_data.get("confidence"),
            )
            email_record.tasks.append(task)

        # Step 5: Mailbox Actions (with safe mode and approval checks)
        actions_taken = []

        # A) SAFE_MODE always wins - no IMAP actions taken
        if self.settings.safe_mode:
            logger.info(f"SAFE MODE: Skipping IMAP actions for {message_id}")
            actions_taken.append("safe_mode_skip")

        # B) REQUIRE_APPROVAL - enqueue PendingActions instead of executing
        elif self.settings.require_approval:
            logger.info(f"REQUIRE_APPROVAL: Enqueuing pending actions for {message_id}")
            actions_taken.append("queued_pending_actions")

            # Ensure email_record has an id before creating PendingActions
            self.db.add(email_record)
            self.db.flush()

            # Enqueue PendingAction rows based on what would have been done
            if is_spam:
                # Always enqueue a MOVE to quarantine_folder (never delete)
                pending_action = PendingAction(
                    email_id=email_record.id,
                    action_type="MOVE_FOLDER",
                    target_folder=self.settings.quarantine_folder,
                    status="PENDING",
                )
                self.db.add(pending_action)
                self.stats["spam"] += 1
                logger.info(
                    f"Enqueued MOVE to {self.settings.quarantine_folder} for spam email"
                )
            else:
                # Mark as read if configured
                if self.settings.mark_as_read:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="MARK_READ",
                        status="PENDING",
                    )
                    self.db.add(pending_action)

                # Move to archive
                pending_action = PendingAction(
                    email_id=email_record.id,
                    action_type="MOVE_FOLDER",
                    target_folder=self.settings.archive_folder,
                    status="PENDING",
                )
                self.db.add(pending_action)
                self.stats["archived"] += 1

                # Flag if action required
                if action_required:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="ADD_FLAG",
                        status="PENDING",
                    )
                    self.db.add(pending_action)
                    self.stats["action_required"] += 1

        # C) Normal mode - execute IMAP actions immediately
        else:
            if is_spam:
                # Handle spam based on configuration
                if self.settings.delete_spam:
                    # Move to spam folder (not actual deletion for safety)
                    if imap.move_to_folder(uid, self.settings.spam_folder):
                        actions_taken.append("moved_to_spam")
                        email_record.is_archived = True
                        self.stats["spam"] += 1
                        logger.info(f"Moved spam email to {self.settings.spam_folder}")
                else:
                    # Move to quarantine folder for review
                    if imap.move_to_folder(uid, self.settings.quarantine_folder):
                        actions_taken.append("moved_to_quarantine")
                        email_record.is_archived = True
                        self.stats["spam"] += 1
                        logger.info(
                            f"Moved spam email to quarantine: {self.settings.quarantine_folder}"
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
                    self.stats["archived"] += 1

                # Flag if action required
                if action_required:
                    if imap.add_flag(uid):
                        actions_taken.append("flagged")
                        email_record.is_flagged = True
                    self.stats["action_required"] += 1

        email_record.actions_taken = {"actions": actions_taken}

        # Step 6: Persistence
        self.db.add(email_record)

        # Add audit log
        audit = AuditLog(
            event_type="EMAIL_PROCESSED",
            email_message_id=message_id,
            description=f"Email processed: spam={is_spam}, action_required={action_required}, safe_mode={self.settings.safe_mode}, require_approval={self.settings.require_approval}",
            data={
                "category": analysis["category"],
                "priority": priority,
                "actions": actions_taken,
                "safe_mode": self.settings.safe_mode,
                "require_approval": self.settings.require_approval,
            },
        )
        self.db.add(audit)

        self.db.commit()
        self.stats["processed"] += 1

        logger.info(
            f"Email processed successfully: spam={is_spam}, "
            f"action={action_required}, priority={priority}, "
            f"safe_mode={self.settings.safe_mode}, "
            f"require_approval={self.settings.require_approval}"
        )

    def _classify_spam(
        self, email_data: Dict[str, Any], analysis: Dict[str, Any]
    ) -> bool:
        """
        Classify email as spam

        Combines AI spam probability with heuristic indicators
        """
        spam_prob = analysis["spam_probability"]

        # Heuristic checks
        subject = email_data.get("subject", "").lower()
        body = email_data.get("body_plain", "").lower()

        # Check for unsubscribe headers (common in newsletters/marketing)
        has_unsubscribe = "unsubscribe" in body or "abmelden" in body

        # Adjust probability based on heuristics
        if has_unsubscribe:
            spam_prob = max(spam_prob, 0.6)

        # Check against threshold
        return spam_prob >= self.settings.spam_threshold

    # ------------------------------------------------------------------
    # Override rule helpers
    # ------------------------------------------------------------------

    def _find_matching_override(
        self, email_data: Dict[str, Any]
    ) -> Optional["ClassificationOverride"]:
        """
        Look up a ClassificationOverride rule that matches this email.

        Matching priority:
        1. sender_pattern (domain match: sender ends with the pattern)
        2. subject_pattern (case-insensitive substring of subject)

        Returns the first matching rule, or None.
        """
        sender = (email_data.get("sender") or "").lower()
        subject = (email_data.get("subject") or "").lower()

        rules = self.db.query(ClassificationOverride).all()
        for rule in rules:
            # Check sender domain match
            if rule.sender_pattern:
                pattern = rule.sender_pattern.lower()
                if sender.endswith(pattern) or sender == pattern.lstrip("@"):
                    # Optionally also check subject if subject_pattern is set
                    if rule.subject_pattern:
                        if rule.subject_pattern.lower() in subject:
                            return rule
                    else:
                        return rule
            # Check subject-only rule (no sender_pattern)
            elif rule.subject_pattern:
                if rule.subject_pattern.lower() in subject:
                    return rule
        return None

    def _build_analysis_from_override(
        self,
        rule: "ClassificationOverride",
        email_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Build an AI-style analysis dict from a ClassificationOverride rule,
        falling back to defaults for unset fields.
        """
        fallback = self.ai_service._fallback_classification(email_data)
        return {
            "summary": fallback.get("summary", ""),
            "category": rule.category if rule.category else fallback["category"],
            "spam_probability": (
                0.95
                if rule.spam is True
                else (0.05 if rule.spam is False else fallback["spam_probability"])
            ),
            "action_required": (
                rule.action_required
                if rule.action_required is not None
                else fallback["action_required"]
            ),
            "priority": rule.priority if rule.priority else fallback["priority"],
            "tasks": fallback.get("tasks", []),
            "suggested_folder": (
                rule.suggested_folder
                if rule.suggested_folder
                else fallback["suggested_folder"]
            ),
            "reasoning": "Applied override rule",
        }
