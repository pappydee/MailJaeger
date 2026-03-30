"""
Email processing service - orchestrates the email processing workflow

Architecture (two-phase):
  Phase 1 — Ingestion: pull ALL emails (not just UNSEEN) from IMAP into local index
  Phase 2 — Analysis:  process pending emails from the local index through the
             multi-stage pipeline; execute IMAP actions only when needed

The local mail index is the primary data source.  Direct IMAP "UNSEEN" fetching
is only used for backward-compatible helpers such as _process_single_email.
"""

import logging
import json
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from datetime import datetime
from sqlalchemy import nullslast, desc as sa_desc
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import (
    ProcessedEmail,
    EmailTask,
    ProcessingRun,
    AuditLog,
    PendingAction,
    ActionQueue,
    ClassificationOverride,
)
from src.services.action_executor import ActionExecutor
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.services.thread_context import update_thread_state_for_thread
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

if TYPE_CHECKING:
    from src.services.scheduler import RunStatus

logger = get_logger(__name__)


class EmailProcessor:
    """Main email processing orchestrator"""

    # Keywords that indicate bulk / newsletter / promotional mail.
    # Used by compute_importance_score to penalise low-value mail so that
    # important messages are processed before newsletters regardless of recency.
    _BULK_INDICATORS: tuple = (
        "newsletter",
        "unsubscribe",
        "abmelden",
        "no-reply",
        "noreply",
        "do-not-reply",
        "donotreply",
        "list-unsubscribe",
        "bulk",
        "promo",
        "marketing",
        "digest",
        "weekly",
        "monthly",
        "angebot",
        "rabatt",
        "sale",
        "offer",
        "deal",
    )

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

    def _should_cancel(self) -> bool:
        """Return True when a cancellation has been requested for this run."""
        return self._status is not None and self._status.cancel_requested

    def _refresh_thread_state(self, thread_id: Optional[str]) -> str:
        """Infer and persist thread state for one thread id."""
        try:
            return update_thread_state_for_thread(
                self.db,
                thread_id=thread_id,
                user_address=getattr(self.settings, "imap_username", None),
            )
        except Exception as exc:
            logger.warning(
                "Failed to refresh thread_state for thread=%s: %s",
                thread_id,
                sanitize_error(exc, debug=self.settings.debug),
            )
            return "informational"

    def _execute_approved_actions(self) -> Dict[str, int]:
        """Execute all approved action_queue items before normal processing."""
        approved_actions = (
            self.db.query(ActionQueue)
            .filter(ActionQueue.status.in_(("approved", "approved_action")))
            # Deterministic execution order requirement: oldest queued action first.
            .order_by(ActionQueue.created_at.asc())
            .all()
        )
        if not approved_actions:
            return {"total": 0, "executed": 0, "failed": 0}

        imap = IMAPService()
        requires_imap = any(
            action.action_type in ("move", "mark_read", "delete")
            for action in approved_actions
        )
        if requires_imap and not imap.connect():
            logger.warning(
                "Could not connect to IMAP for approved action execution; "
                "IMAP-based approved actions may fail this run"
            )

        executor = ActionExecutor(imap)
        executed = 0
        failed = 0

        try:
            for action in approved_actions:
                try:
                    current_status = (action.status or "").lower()
                    if current_status in ("executed", "executed_action"):
                        logger.info(
                            "action_execution action_id=%s action_type=%s result=skipped_already_executed",
                            action.id,
                            action.action_type,
                        )
                        continue
                    if current_status in ("failed", "failed_action"):
                        logger.info(
                            "action_execution action_id=%s action_type=%s result=skipped_previously_failed",
                            action.id,
                            action.action_type,
                        )
                        continue
                    if current_status not in ("approved", "approved_action"):
                        logger.info(
                            "action_execution action_id=%s action_type=%s result=skipped_status_%s",
                            action.id,
                            action.action_type,
                            current_status or "unknown",
                        )
                        continue

                    email = (
                        self.db.query(ProcessedEmail)
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
                    self.db.add(action)
                    if email:
                        self.db.add(email)
                    thread_id = action.thread_id or (email.thread_id if email else None)
                    self._refresh_thread_state(thread_id)
                    logger.info(
                        "approved_action_run action_id=%s action_type=%s result=%s",
                        action.id,
                        action.action_type,
                        "success" if success else "failure",
                    )
                except Exception as exc:
                    failed += 1
                    action.status = "failed"
                    action.error_message = (
                        sanitize_error(exc, debug=self.settings.debug)
                        or "Execution failed"
                    )
                    action.updated_at = datetime.utcnow()
                    self.db.add(action)
                    self._refresh_thread_state(action.thread_id)
                    logger.error(
                        "approved_action_run action_id=%s action_type=%s result=failure error=%s",
                        action.id,
                        action.action_type,
                        action.error_message,
                    )

            self.db.commit()
        finally:
            try:
                imap.disconnect()
            except Exception:
                pass

        logger.info(
            "Approved action auto-execution complete: total=%s executed=%s failed=%s",
            len(approved_actions),
            executed,
            failed,
        )
        return {"total": len(approved_actions), "executed": executed, "failed": failed}

    # ------------------------------------------------------------------
    # Primary processing workflow (two-phase index-based)
    # ------------------------------------------------------------------

    def process_emails(self, trigger_type: str = "SCHEDULED") -> ProcessingRun:
        """
        Main processing workflow — two-phase approach:

        Phase 1 (Ingest):
          Connect to IMAP, fetch ALL messages (not just UNSEEN), and store
          metadata in the local mail index.  New messages are added;
          already-indexed messages are skipped.  Uses BODY.PEEK[] so no
          \Seen flag is set.

        Phase 2 (Analyse):
          Query the local index for emails in analysis_state='pending' and
          run them through the multi-stage analysis pipeline.  IMAP actions
          (move, flag) are only executed when safe_mode is disabled.

        This architecture means the local index is always the authoritative
        data source — not IMAP UNSEEN flags.
        """
        run = ProcessingRun(
            started_at=datetime.utcnow(),
            trigger_type=trigger_type,
            status="IN_PROGRESS",
        )
        self.db.add(run)
        self.db.commit()
        self._update_status(run_id=run.id)

        logger.info(
            f"Starting email processing run (ID: {run.id}, Type: {trigger_type})"
        )

        try:
            # Execute approved queue actions first (manual + scheduled runs),
            # but only when SAFE_MODE is disabled.
            approved_stats = {"total": 0, "executed": 0, "failed": 0}
            if self.settings.safe_mode:
                logger.info("SAFE MODE: skipping auto-execution of approved actions")
            else:
                self._update_status(
                    phase="ingestion",
                    current_step="Führe freigegebene Aktionen aus…",
                    progress_percent=2,
                )
                approved_stats = self._execute_approved_actions()
                if approved_stats["total"] > 0:
                    logger.info(
                        "Processed approved actions before ingestion: %s",
                        approved_stats,
                    )

            # ----------------------------------------------------------------
            # Phase 1: Ingest — import all emails from IMAP into local index
            # ----------------------------------------------------------------
            self._update_status(
                phase="ingestion",
                current_step="Phase 1: Ingesting emails from IMAP…",
                progress_percent=5,
            )
            ingestion_stats = self._run_ingestion(str(run.id))
            logger.info(
                f"Ingestion: {ingestion_stats.get('new', 0)} new, "
                f"{ingestion_stats.get('skipped', 0)} skipped, "
                f"{ingestion_stats.get('failed', 0)} failed"
            )

            # ----------------------------------------------------------------
            # Phase 2: Analyse — process pending emails from local index
            # ----------------------------------------------------------------
            self._update_status(
                phase="analysis",
                current_step="Phase 2: Querying local index for pending emails…",
                progress_percent=20,
            )

            # Compute importance scores for any pending emails that lack one,
            # then retrieve pending emails sorted by importance (high → low)
            # so the most critical messages are processed first.
            self._compute_pending_importance_scores()

            pending_emails = (
                self.db.query(ProcessedEmail)
                .filter(ProcessedEmail.analysis_state == "pending")
                .order_by(
                    nullslast(sa_desc(ProcessedEmail.importance_score)),
                )
                .limit(self.settings.max_emails_per_run)
                .all()
            )

            if not pending_emails:
                logger.info("No pending emails to analyse in local index")
                self._update_status(
                    phase=None,
                    current_step=None,
                    progress_percent=100,
                    total=0,
                    message="No new emails to analyse",
                )
                run.status = "SUCCESS"
                run.completed_at = datetime.utcnow()
                self.db.commit()
                return run

            total = len(pending_emails)
            logger.info(f"Found {total} pending email(s) in local index")
            self._update_status(
                current_step=f"Analysing {total} email(s) from local index…",
                progress_percent=25,
                total=total,
            )

            # Open IMAP connection for action execution only when needed
            imap_for_actions: Optional[IMAPService] = None
            if not self.settings.safe_mode and not self.settings.require_approval:
                imap_for_actions = IMAPService()
                if not imap_for_actions.connect():
                    logger.warning(
                        "Could not connect to IMAP for action execution; "
                        "actions will be skipped this run"
                    )
                    imap_for_actions = None
                elif imap_for_actions.client:
                    try:
                        imap_for_actions.client.select_folder(
                            self.settings.inbox_folder
                        )
                    except Exception as e:
                        sanitized = sanitize_error(e, debug=self.settings.debug)
                        logger.warning(
                            f"Could not select IMAP inbox for actions: {sanitized}"
                        )

            # Warn prominently if IMAP actions will be silently skipped
            if (
                imap_for_actions is None
                and not self.settings.safe_mode
                and not self.settings.require_approval
            ):
                logger.warning(
                    "IMAP connection unavailable: email analysis will run but "
                    "IMAP actions (move, flag) will be skipped for this entire run. "
                    "Check IMAP connectivity and logs above for the connection error."
                )

            try:
                # ---- Batch-aware analysis loop --------------------------------
                # We process emails in groups of ai_batch_size.  Within each
                # group, Stage-1/2 emails are handled individually (fast, no LLM),
                # and Stage-3 emails (those that need LLM) are sent in a single
                # batch AI request to reduce round-trips.
                batch_size = max(1, self.settings.ai_batch_size)
                idx = 0  # global email index across all batches

                for batch_start in range(0, total, batch_size):
                    batch = pending_emails[batch_start : batch_start + batch_size]

                    # --- cancellation checkpoint (per batch) ---
                    if self._should_cancel():
                        logger.info(
                            f"Cancellation requested — stopping before batch "
                            f"starting at {batch_start}/{total} "
                            f"({idx} completed so far)"
                        )
                        self._update_status(
                            status="cancelling",
                            current_step="Cancelling…",
                        )
                        break

                    # Run Stage 1 & 2 for every email in the batch
                    needs_llm: List = []  # (email_record,) tuples for Stage-3
                    for email_record in batch:
                        idx += 1
                        pct = 25 + int((idx / total) * 65)  # 25 → 90 %
                        self._update_status(
                            current_step=f"Analysing {idx}/{total}…",
                            progress_percent=pct,
                            processed=self.stats["processed"],
                            spam=self.stats["spam"],
                            action_required=self.stats["action_required"],
                            failed=self.stats["failed"],
                        )
                        try:
                            went_to_llm = self._process_indexed_email_stages12(
                                email_record, imap_for_actions
                            )
                            if went_to_llm:
                                needs_llm.append(email_record)
                        except Exception as e:
                            sanitized_error = sanitize_error(
                                e, debug=self.settings.debug
                            )
                            logger.error(
                                f"Failed to pre-classify indexed email "
                                f"{email_record.message_id}: {sanitized_error}"
                            )
                            self.stats["failed"] += 1
                            try:
                                email_record.analysis_state = "failed"
                                self.db.add(email_record)
                                self.db.commit()
                            except Exception:
                                pass

                    # Stage 3: batch LLM analysis for emails that need it
                    if needs_llm:
                        self._process_batch_llm(needs_llm, imap_for_actions)

            finally:
                if imap_for_actions:
                    try:
                        imap_for_actions.disconnect()
                    except Exception:
                        pass

            # Determine whether the run was cancelled mid-flight
            was_cancelled = self._should_cancel()

            # Save run statistics
            self._update_status(
                phase=None, current_step="Saving results…", progress_percent=95
            )
            run.emails_processed = self.stats["processed"]
            run.emails_spam = self.stats["spam"]
            run.emails_archived = self.stats["archived"]
            run.emails_action_required = self.stats["action_required"]
            run.emails_failed = self.stats["failed"]
            run.completed_at = datetime.utcnow()

            if was_cancelled:
                run.status = "CANCELLED"
            elif self.stats["failed"] == 0:
                run.status = "SUCCESS"
            else:
                run.status = "PARTIAL"
            self.db.commit()

            logger.info(
                f"Processing run {'CANCELLED' if was_cancelled else 'completed'}: "
                f"{self.stats['processed']} processed, "
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

    # ------------------------------------------------------------------
    # Phase 1 helper: ingestion
    # ------------------------------------------------------------------

    def _run_ingestion(self, run_id: str) -> Dict[str, Any]:
        """
        Run IMAP ingestion into the local mail index.

        Fetches ALL messages from the inbox (not just UNSEEN) using BODY.PEEK[]
        so no \\Seen flag is set.  Already-indexed messages are skipped.
        """
        try:
            from src.services.mail_ingestion_service import MailIngestionService

            ingestion_service = MailIngestionService(self.db)
            stats = ingestion_service.ingest_folder(
                folder=self.settings.inbox_folder,
                run_id=run_id,
            )
            return stats
        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Ingestion phase failed: {sanitized}")
            return {"new": 0, "skipped": 0, "failed": 0, "total": 0}

    # ------------------------------------------------------------------
    # Phase 2 helper: process a single indexed email record
    # ------------------------------------------------------------------

    def _process_indexed_email(
        self,
        email_record: ProcessedEmail,
        imap: Optional[IMAPService],
    ) -> None:
        """
        Analyse and act on a single email from the local index.

        Uses the multi-stage analysis pipeline (Stage 1: patterns,
        Stage 2: override rules, Stage 3: LLM).  IMAP actions are only
        executed when ``imap`` is provided (non-safe, non-approval mode).
        """
        from src.services.analysis_pipeline import AnalysisPipeline

        message_id = email_record.message_id
        logger.info(f"Processing indexed email: {message_id}")

        # Build a minimal email_data dict for helpers that still expect it
        email_data = {
            "message_id": message_id,
            "uid": email_record.imap_uid or email_record.uid,
            "subject": email_record.subject or "",
            "sender": email_record.sender or "",
            "recipients": email_record.recipients or "",
            "body_plain": email_record.body_plain or "",
            "body_html": email_record.body_html or "",
        }

        # Run multi-stage analysis pipeline
        pipeline = AnalysisPipeline(self.db)
        analysis = pipeline.analyse(email_record)

        # Apply analysis results to the record
        email_record.summary = analysis.get("summary")
        email_record.category = analysis.get("category")
        email_record.spam_probability = analysis.get("spam_probability")
        email_record.action_required = analysis.get("action_required")
        email_record.priority = analysis.get("priority")
        email_record.suggested_folder = analysis.get("suggested_folder")
        email_record.reasoning = analysis.get("reasoning")
        email_record.is_processed = True
        email_record.processed_at = datetime.utcnow()

        # Spam classification (same logic as legacy path)
        is_spam = self._classify_spam(email_data, analysis)
        email_record.is_spam = is_spam

        action_required = bool(analysis.get("action_required", False))
        priority = analysis.get("priority", "LOW")

        # -------------------------------------------------------------------
        # Mailbox actions (safe mode / approval / normal — same policy as before)
        # -------------------------------------------------------------------
        actions_taken = []
        uid_str = email_record.imap_uid or email_record.uid
        uid_int: Optional[int] = None
        try:
            uid_int = int(uid_str) if uid_str else None
        except (ValueError, TypeError):
            uid_int = None

        if self.settings.safe_mode:
            logger.info(f"SAFE MODE: Skipping IMAP actions for {message_id}")
            actions_taken.append("safe_mode_skip")

        elif self.settings.require_approval:
            logger.info(f"REQUIRE_APPROVAL: Enqueuing pending actions for {message_id}")
            actions_taken.append("queued_pending_actions")
            self.db.add(email_record)
            self.db.flush()

            if is_spam:
                self.db.add(
                    PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE_FOLDER",
                        target_folder=self.settings.quarantine_folder,
                        status="PENDING",
                    )
                )
                self.stats["spam"] += 1
            else:
                if self.settings.mark_as_read:
                    self.db.add(
                        PendingAction(
                            email_id=email_record.id,
                            action_type="MARK_READ",
                            status="PENDING",
                        )
                    )
                self.db.add(
                    PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE_FOLDER",
                        target_folder=self.settings.archive_folder,
                        status="PENDING",
                    )
                )
                self.stats["archived"] += 1
                if action_required:
                    self.db.add(
                        PendingAction(
                            email_id=email_record.id,
                            action_type="ADD_FLAG",
                            status="PENDING",
                        )
                    )
                    self.stats["action_required"] += 1

        elif imap and uid_int:
            # Normal mode — execute IMAP actions immediately
            if is_spam:
                target = (
                    self.settings.spam_folder
                    if self.settings.delete_spam
                    else self.settings.quarantine_folder
                )
                if imap.move_to_folder(uid_int, target):
                    actions_taken.append(
                        "moved_to_spam"
                        if self.settings.delete_spam
                        else "moved_to_quarantine"
                    )
                    email_record.is_archived = True
                    self.stats["spam"] += 1
            else:
                if self.settings.mark_as_read:
                    if imap.mark_as_read(uid_int):
                        actions_taken.append("marked_as_read")

                if imap.move_to_folder(uid_int, self.settings.archive_folder):
                    actions_taken.append("moved_to_archive")
                    email_record.is_archived = True
                    self.stats["archived"] += 1

                if action_required:
                    if imap.add_flag(uid_int):
                        actions_taken.append("flagged")
                        email_record.is_flagged = True
                    self.stats["action_required"] += 1

        email_record.actions_taken = {"actions": actions_taken}

        # Audit log
        self.db.add(email_record)
        email_record.thread_state = self._refresh_thread_state(email_record.thread_id)
        self.db.add(
            AuditLog(
                event_type="EMAIL_PROCESSED",
                email_message_id=message_id,
                description=(
                    f"Email processed (indexed): spam={is_spam}, "
                    f"action_required={action_required}, "
                    f"safe_mode={self.settings.safe_mode}, "
                    f"require_approval={self.settings.require_approval}"
                ),
                data={
                    "category": analysis.get("category"),
                    "priority": priority,
                    "actions": actions_taken,
                    "safe_mode": self.settings.safe_mode,
                    "require_approval": self.settings.require_approval,
                },
            )
        )
        self.db.commit()
        self.stats["processed"] += 1

        logger.info(
            f"Indexed email processed: spam={is_spam}, "
            f"action={action_required}, priority={priority}"
        )

    # ------------------------------------------------------------------
    # New helpers: importance scoring, batch processing
    # ------------------------------------------------------------------

    def compute_importance_score(self, email_record: ProcessedEmail) -> float:
        """
        Compute an importance score in the range 0–100 for a single email.

        Delegates to ``src.services.importance_scorer.compute_importance_score``
        so the logic is reusable without instantiating EmailProcessor.
        """
        from src.services.importance_scorer import compute_importance_score as _score
        return _score(self.db, email_record)

    def _compute_pending_importance_scores(self) -> None:
        """Compute and persist importance_score for all 'pending' emails that lack one."""
        try:
            unscored = (
                self.db.query(ProcessedEmail)
                .filter(
                    ProcessedEmail.analysis_state == "pending",
                    ProcessedEmail.importance_score == None,  # noqa: E711
                )
                .all()
            )
            if not unscored:
                return
            logger.info(f"Computing importance scores for {len(unscored)} emails")
            for email_record in unscored:
                email_record.importance_score = self.compute_importance_score(
                    email_record
                )
                self.db.add(email_record)
            self.db.commit()
        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.warning(f"Failed to compute importance scores: {sanitized}")

    def _process_indexed_email_stages12(
        self,
        email_record: ProcessedEmail,
        imap: Optional[IMAPService],
    ) -> bool:
        """
        Run Stage 1 and Stage 2 of the analysis pipeline on a single indexed email.

        If Stages 1 and 2 produce a confident classification the email is
        fully handled here and ``False`` is returned.
        If Stage 3 (LLM) is required, the email is left in its current
        analysis_state and ``True`` is returned so the caller can batch it.
        """
        from src.services.analysis_pipeline import AnalysisPipeline

        pipeline = AnalysisPipeline(self.db)

        # Stage 1
        stage1 = pipeline._stage1_pre_classify(email_record)
        if stage1["confident"]:
            pipeline._record_decision(email_record, "stage1_pre_classified", stage1)
            pipeline._update_analysis_state(email_record, "pre_classified")
            self._apply_analysis_and_act(email_record, stage1["analysis"], imap)
            return False

        # Stage 2
        stage2 = pipeline._stage2_rule_classify(email_record)
        if stage2["confident"]:
            pipeline._record_decision(email_record, "stage2_classified", stage2)
            pipeline._update_analysis_state(email_record, "classified")
            self._apply_analysis_and_act(email_record, stage2["analysis"], imap)
            return False

        # Neither stage produced a confident result — needs LLM
        return True

    def _process_batch_llm(
        self,
        email_records: List[ProcessedEmail],
        imap: Optional[IMAPService],
    ) -> None:
        """
        Send a batch of emails to the AI service in one LLM call
        (Stage 3 — batch path).  Applies results and IMAP actions.
        """
        from src.services.analysis_pipeline import AnalysisPipeline, PIPELINE_VERSION

        pipeline = AnalysisPipeline(self.db)

        # Build the lightweight dicts the AI service expects
        email_data_list = []
        for rec in email_records:
            email_data_list.append(
                {
                    "id": rec.id,
                    "subject": rec.subject or "",
                    "sender": rec.sender or "",
                    "body_plain": rec.body_plain or "",
                    "body_html": rec.body_html or "",
                }
            )

        try:
            results = self.ai_service.analyze_emails_batch(email_data_list)
        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Batch LLM analysis failed: {sanitized}")
            results = [
                self.ai_service.fallback_classification(ed) for ed in email_data_list
            ]

        for email_record, analysis in zip(email_records, results):
            try:
                pipeline._update_analysis_state(email_record, "deep_analyzed")
                pipeline._record_decision(
                    email_record,
                    "stage3_deep_analyzed",
                    {"stage": 3, "source": "llm_batch", "analysis": analysis},
                )
                self._apply_analysis_and_act(email_record, analysis, imap)
                email_record.analysis_version = PIPELINE_VERSION
                self.db.add(email_record)
            except Exception as e:
                sanitized = sanitize_error(e, debug=self.settings.debug)
                logger.error(
                    f"Failed to apply batch LLM result for "
                    f"{email_record.message_id}: {sanitized}"
                )
                self.stats["failed"] += 1
                try:
                    email_record.analysis_state = "failed"
                    self.db.add(email_record)
                except Exception:
                    pass

        self.db.commit()

    def _apply_analysis_and_act(
        self,
        email_record: ProcessedEmail,
        analysis: Dict[str, Any],
        imap: Optional[IMAPService],
    ) -> None:
        """
        Apply analysis results to the email record and execute IMAP actions.

        This is a refactored version of the logic that was previously inlined
        in ``_process_indexed_email``.  Both the Stage-1/2 path and the Stage-3
        batch path call this helper.
        """
        from src.models.database import AuditLog, PendingAction

        message_id = email_record.message_id

        email_data = {
            "message_id": message_id,
            "uid": email_record.imap_uid or email_record.uid,
            "subject": email_record.subject or "",
            "sender": email_record.sender or "",
            "body_plain": email_record.body_plain or "",
        }

        # Apply classification
        email_record.summary = analysis.get("summary")
        email_record.category = analysis.get("category")
        email_record.spam_probability = analysis.get("spam_probability")
        email_record.action_required = analysis.get("action_required")
        email_record.priority = analysis.get("priority")
        email_record.suggested_folder = analysis.get("suggested_folder")
        email_record.reasoning = analysis.get("reasoning")
        email_record.is_processed = True
        email_record.processed_at = datetime.utcnow()

        is_spam = self._classify_spam(email_data, analysis)
        email_record.is_spam = is_spam
        action_required = bool(analysis.get("action_required", False))
        priority = analysis.get("priority", "LOW")

        actions_taken = []
        uid_str = email_record.imap_uid or email_record.uid
        uid_int: Optional[int] = None
        try:
            uid_int = int(uid_str) if uid_str else None
        except (ValueError, TypeError):
            uid_int = None

        if self.settings.safe_mode:
            actions_taken.append("safe_mode_skip")

        elif self.settings.require_approval:
            actions_taken.append("queued_pending_actions")
            self.db.add(email_record)
            self.db.flush()
            if is_spam:
                self.db.add(
                    PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE_FOLDER",
                        target_folder=self.settings.quarantine_folder,
                        status="PENDING",
                    )
                )
                self.stats["spam"] += 1
            else:
                if self.settings.mark_as_read:
                    self.db.add(
                        PendingAction(
                            email_id=email_record.id,
                            action_type="MARK_READ",
                            status="PENDING",
                        )
                    )
                self.db.add(
                    PendingAction(
                        email_id=email_record.id,
                        action_type="MOVE_FOLDER",
                        target_folder=self.settings.archive_folder,
                        status="PENDING",
                    )
                )
                self.stats["archived"] += 1
                if action_required:
                    self.db.add(
                        PendingAction(
                            email_id=email_record.id,
                            action_type="ADD_FLAG",
                            status="PENDING",
                        )
                    )
                    self.stats["action_required"] += 1

        elif imap and uid_int:
            if is_spam:
                target = (
                    self.settings.spam_folder
                    if self.settings.delete_spam
                    else self.settings.quarantine_folder
                )
                if imap.move_to_folder(uid_int, target):
                    actions_taken.append(
                        "moved_to_spam"
                        if self.settings.delete_spam
                        else "moved_to_quarantine"
                    )
                    email_record.is_archived = True
                    self.stats["spam"] += 1
            else:
                if self.settings.mark_as_read:
                    if imap.mark_as_read(uid_int):
                        actions_taken.append("marked_as_read")
                if imap.move_to_folder(uid_int, self.settings.archive_folder):
                    actions_taken.append("moved_to_archive")
                    email_record.is_archived = True
                    self.stats["archived"] += 1
                if action_required:
                    if imap.add_flag(uid_int):
                        actions_taken.append("flagged")
                        email_record.is_flagged = True
                    self.stats["action_required"] += 1

        email_record.actions_taken = {"actions": actions_taken}
        self.db.add(email_record)
        self.db.flush()
        self._queue_action_proposals(email_record, analysis)
        email_record.thread_state = self._refresh_thread_state(email_record.thread_id)
        self.db.add(
            AuditLog(
                event_type="EMAIL_PROCESSED",
                email_message_id=message_id,
                description=(
                    f"Email processed (indexed): spam={is_spam}, "
                    f"action_required={action_required}, "
                    f"safe_mode={self.settings.safe_mode}, "
                    f"require_approval={self.settings.require_approval}"
                ),
                data={
                    "category": analysis.get("category"),
                    "priority": priority,
                    "actions": actions_taken,
                    "safe_mode": self.settings.safe_mode,
                    "require_approval": self.settings.require_approval,
                },
            )
        )
        self.db.commit()
        self.stats["processed"] += 1

    def _queue_action_proposals(
        self,
        email_record: ProcessedEmail,
        analysis: Dict[str, Any],
    ) -> None:
        """Create structured action proposals (status=proposed) from analysis output."""
        from src.models.database import ActionQueue

        try:
            proposed_actions = analysis.get("proposed_actions")
            candidates = []

            if isinstance(proposed_actions, list):
                for item in proposed_actions:
                    if not isinstance(item, dict):
                        continue
                    action_type = str(item.get("action_type", "")).strip().lower()
                    payload = item.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    if action_type:
                        candidates.append(
                            {"action_type": action_type, "payload": payload}
                        )
            else:
                suggested_folder = analysis.get("suggested_folder")
                if suggested_folder:
                    candidates.append(
                        {
                            "action_type": "move",
                            "payload": {"target_folder": suggested_folder},
                        }
                    )

            if not candidates:
                return

            existing = (
                self.db.query(ActionQueue)
                .filter(ActionQueue.email_id == email_record.id)
                .all()
            )
            existing_pairs = {
                (
                    row.action_type,
                    json.dumps(row.payload or {}, sort_keys=True),
                )
                for row in existing
            }

            for item in candidates:
                payload_key = json.dumps(item["payload"], sort_keys=True)
                pair = (item["action_type"], payload_key)
                if pair in existing_pairs:
                    continue
                self.db.add(
                    ActionQueue(
                        email_id=email_record.id,
                        thread_id=email_record.thread_id,
                        action_type=item["action_type"],
                        payload=item["payload"],
                        status="proposed",
                    )
                )
                existing_pairs.add(pair)
        except Exception as exc:
            logger.warning(
                "Skipping action proposal enqueue for email %s: %s",
                email_record.message_id,
                sanitize_error(exc, debug=self.settings.debug),
            )

    # ------------------------------------------------------------------
    # Legacy method — kept for backward compatibility with tests and
    # direct callers that pass raw email_data dicts + an IMAP handle.
    # Not called by the primary process_emails() flow any more.
    # ------------------------------------------------------------------

    def _process_single_email(self, email_data: Dict[str, Any], imap: IMAPService):
        """
        Process a single email from a raw email_data dict.

        This is the legacy path used by tests and any callers that pass
        raw IMAP-fetched email dicts.  The primary processing flow now uses
        _process_indexed_email() via the local mail index.
        """
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

        # Check for a matching classification override rule first.
        override_rule = self._find_matching_override(email_data)
        applied_override = override_rule is not None

        if applied_override:
            logger.info(
                f"Override rule {override_rule.id} matched for {message_id} "
                f"(pattern: {override_rule.sender_pattern!r})"
            )
            analysis = self._build_analysis_from_override(override_rule, email_data)
        else:
            try:
                analysis = self.ai_service.analyze_email(email_data)
            except Exception as e:
                sanitized_error = sanitize_error(e, debug=self.settings.debug)
                logger.error(f"AI analysis failed for {message_id}: {sanitized_error}")
                analysis = self.ai_service.fallback_classification(email_data)

        is_spam = self._classify_spam(email_data, analysis)
        action_required = analysis["action_required"]
        priority = analysis["priority"]

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

        for task_data in analysis.get("tasks", []):
            task = EmailTask(
                description=task_data["description"],
                due_date=task_data.get("due_date"),
                context=task_data.get("context"),
                confidence=task_data.get("confidence"),
            )
            email_record.tasks.append(task)

        actions_taken = []

        if self.settings.safe_mode:
            logger.info(f"SAFE MODE: Skipping IMAP actions for {message_id}")
            actions_taken.append("safe_mode_skip")

        elif self.settings.require_approval:
            logger.info(f"REQUIRE_APPROVAL: Enqueuing pending actions for {message_id}")
            actions_taken.append("queued_pending_actions")

            self.db.add(email_record)
            self.db.flush()

            if is_spam:
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
                if self.settings.mark_as_read:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="MARK_READ",
                        status="PENDING",
                    )
                    self.db.add(pending_action)

                pending_action = PendingAction(
                    email_id=email_record.id,
                    action_type="MOVE_FOLDER",
                    target_folder=self.settings.archive_folder,
                    status="PENDING",
                )
                self.db.add(pending_action)
                self.stats["archived"] += 1

                if action_required:
                    pending_action = PendingAction(
                        email_id=email_record.id,
                        action_type="ADD_FLAG",
                        status="PENDING",
                    )
                    self.db.add(pending_action)
                    self.stats["action_required"] += 1

        else:
            if is_spam:
                if self.settings.delete_spam:
                    if imap.move_to_folder(uid, self.settings.spam_folder):
                        actions_taken.append("moved_to_spam")
                        email_record.is_archived = True
                        self.stats["spam"] += 1
                else:
                    if imap.move_to_folder(uid, self.settings.quarantine_folder):
                        actions_taken.append("moved_to_quarantine")
                        email_record.is_archived = True
                        self.stats["spam"] += 1
            else:
                if self.settings.mark_as_read:
                    if imap.mark_as_read(uid):
                        actions_taken.append("marked_as_read")

                if imap.move_to_folder(uid, self.settings.archive_folder):
                    actions_taken.append("moved_to_archive")
                    email_record.is_archived = True
                    self.stats["archived"] += 1

                if action_required:
                    if imap.add_flag(uid):
                        actions_taken.append("flagged")
                        email_record.is_flagged = True
                    self.stats["action_required"] += 1

        self.db.add(email_record)
        self.db.flush()
        email_record.actions_taken = {"actions": actions_taken}
        self._queue_action_proposals(email_record, analysis)
        email_record.thread_state = self._refresh_thread_state(email_record.thread_id)

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

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _classify_spam(
        self, email_data: Dict[str, Any], analysis: Dict[str, Any]
    ) -> bool:
        """
        Classify email as spam.

        Combines AI spam probability with heuristic indicators.
        """
        spam_prob = analysis["spam_probability"]

        body = email_data.get("body_plain", "").lower()

        has_unsubscribe = "unsubscribe" in body or "abmelden" in body
        if has_unsubscribe:
            spam_prob = max(spam_prob, 0.6)

        return spam_prob >= self.settings.spam_threshold

    def _find_matching_override(
        self, email_data: Dict[str, Any]
    ) -> Optional["ClassificationOverride"]:
        """Look up a ClassificationOverride rule that matches this email."""
        sender = (email_data.get("sender") or "").lower()
        subject = (email_data.get("subject") or "").lower()

        rules = self.db.query(ClassificationOverride).all()
        for rule in rules:
            if rule.sender_pattern:
                pattern = rule.sender_pattern.lower()
                if sender.endswith(pattern) or sender == pattern.lstrip("@"):
                    if rule.subject_pattern:
                        if rule.subject_pattern.lower() in subject:
                            return rule
                    else:
                        return rule
            elif rule.subject_pattern:
                if rule.subject_pattern.lower() in subject:
                    return rule
        return None

    def _build_analysis_from_override(
        self,
        rule: "ClassificationOverride",
        email_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build an AI-style analysis dict from a ClassificationOverride rule."""
        fallback = self.ai_service.fallback_classification(email_data)
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
