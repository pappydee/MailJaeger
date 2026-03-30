"""
Multi-Stage Analysis Pipeline — Priority 9

Implements a staged pipeline to avoid sending every email to the LLM.

Stage 1 — Fast pre-classification (no LLM)
  Uses: sender, domain, subject, folder, snippet, known patterns
  Detects: newsletters, automated messages, known senders

Stage 2 — Lightweight rule-based classification
  Uses: classification override rules, pattern matching
  Result: high-confidence classification without LLM

Stage 3 — Deep LLM analysis
  Only when Stages 1 and 2 are inconclusive
  Uses: full email content via Ollama
"""

import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import ProcessedEmail, DecisionEvent, AnalysisProgress
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)

PIPELINE_VERSION = "1.0.0"

# Known newsletter/automated sender patterns for Stage 1
_NEWSLETTER_PATTERNS = [
    r"newsletter",
    r"no[-_.]?reply",
    r"noreply",
    r"donotreply",
    r"do[-_.]not[-_.]reply",
    r"notifications?@",
    r"updates?@",
    r"alerts?@",
    r"news@",
    r"info@",
    r"mailer-daemon",
]

# Known spam keyword patterns for Stage 1 (subject + body)
_SPAM_SUBJECT_PATTERNS = [
    r"\bunsubscribe\b",
    r"\babmelden\b",
    r"\bnewsletter\b",
    r"\bclick here\b",
    r"\bklicken sie hier\b",
    r"\bcongratulations\b",
    r"\bgewonnen\b",
    r"\bfree\s+offer\b",
]


class AnalysisPipeline:
    """
    Multi-stage email analysis pipeline.

    Usage:
        pipeline = AnalysisPipeline(db_session)
        result = pipeline.analyse(email_record)
    """

    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
        self._llm_calls_this_run = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse(self, email_record: ProcessedEmail) -> Dict[str, Any]:
        """
        Run the multi-stage analysis pipeline on a single email record.

        Returns an analysis dict (same structure as AIService.analyze_email).
        Updates the email's analysis_state field.
        """
        try:
            # Stage 1: Fast pre-classification
            stage1_result = self.stage1_pre_classify(email_record)
            if stage1_result["confident"]:
                self.record_decision(email_record, "stage1_pre_classified", stage1_result)
                self.update_analysis_state(email_record, "pre_classified")
                return stage1_result["analysis"]

            # Stage 2: Rule-based classification
            stage2_result = self.stage2_rule_classify(email_record)
            if stage2_result["confident"]:
                self.record_decision(email_record, "stage2_classified", stage2_result)
                self.update_analysis_state(email_record, "classified")
                return stage2_result["analysis"]

            # Stage 3: Deep LLM analysis
            if self._llm_budget_available():
                stage3_result = self.stage3_llm_analyse(email_record)
                self.record_decision(email_record, "stage3_deep_analyzed", stage3_result)
                self.update_analysis_state(email_record, "deep_analyzed")
                self._llm_calls_this_run += 1
                return stage3_result["analysis"]
            else:
                # LLM budget exhausted — use Stage 2 result as best-effort
                logger.warning(
                    f"LLM budget exhausted ({self._llm_calls_this_run} calls). "
                    f"Using rule-based result for {email_record.message_id}"
                )
                self.update_analysis_state(email_record, "classified")
                return stage2_result["analysis"]

        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Pipeline analysis failed for {email_record.message_id}: {sanitized}")
            self.update_analysis_state(email_record, "failed")
            return self._fallback_analysis(email_record)

    def analyse_pending_batch(
        self,
        run_id: Optional[str] = None,
        max_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Analyse a batch of emails that are in 'pending' analysis state.

        Supports pause/resume via the analysis_progress table.
        """
        import uuid
        run_id = run_id or str(uuid.uuid4())
        effective_max = max_count or self.settings.max_emails_per_batch

        stats = {"analysed": 0, "skipped": 0, "failed": 0, "llm_calls": 0}

        # Get batch of pending emails
        pending = (
            self.db.query(ProcessedEmail)
            .filter(ProcessedEmail.analysis_state == "pending")
            .order_by(ProcessedEmail.received_at.asc())
            .limit(effective_max)
            .all()
        )

        if not pending:
            logger.info("No pending emails to analyse")
            return stats

        logger.info(f"Analysing batch of {len(pending)} emails")

        progress = self._get_or_create_progress(run_id, "analysis")
        progress.total_count = len(pending)
        self.db.commit()

        start_time = datetime.utcnow()

        for email_record in pending:
            # Check resource limits
            elapsed_minutes = (datetime.utcnow() - start_time).total_seconds() / 60
            if elapsed_minutes > self.settings.max_runtime_minutes:
                logger.info(f"Analysis paused: runtime limit ({self.settings.max_runtime_minutes}min) reached")
                self._mark_progress_paused(progress, stats, "runtime_limit")
                break

            if self._llm_calls_this_run >= self.settings.max_llm_calls_per_run:
                logger.info(f"Analysis paused: LLM call limit ({self.settings.max_llm_calls_per_run}) reached")
                self._mark_progress_paused(progress, stats, "llm_call_limit")
                break

            try:
                analysis = self.analyse(email_record)
                self.apply_analysis_to_record(email_record, analysis)
                stats["analysed"] += 1
                progress.last_email_id = email_record.id
                progress.last_message_id = email_record.message_id
            except Exception as e:
                sanitized = sanitize_error(e, debug=self.settings.debug)
                logger.error(f"Failed to analyse {email_record.message_id}: {sanitized}")
                stats["failed"] += 1

            progress.processed_count = stats["analysed"] + stats["skipped"]
            progress.llm_calls_used = self._llm_calls_this_run
            self.db.commit()

        stats["llm_calls"] = self._llm_calls_this_run
        self._mark_progress_complete(progress, stats)
        return stats

    # ------------------------------------------------------------------
    # Stage 1: Fast pre-classification (no LLM)
    # ------------------------------------------------------------------

    def stage1_pre_classify(self, email: ProcessedEmail) -> Dict[str, Any]:
        """
        Fast pattern-based pre-classification.

        Uses sender, domain, subject, folder, and known patterns.
        Returns a result dict with 'confident' flag and 'analysis'.
        """
        sender = (email.sender or "").lower()
        subject = (email.subject or "").lower()
        snippet = (email.snippet or "").lower()
        folder = (email.folder or "").lower()

        # Detect newsletter/automated senders
        is_newsletter = self._is_newsletter_sender(sender)
        is_spam_subject = self._has_spam_subject(subject)

        if is_newsletter or is_spam_subject:
            spam_prob = 0.85 if is_spam_subject else 0.65
            return {
                "confident": True,
                "stage": 1,
                "source": "newsletter_pattern",
                "analysis": {
                    "summary": f"Automatische Nachricht von {email.sender or 'Unbekannt'}",
                    "category": "Unklar",
                    "spam_probability": spam_prob,
                    "action_required": False,
                    "priority": "LOW",
                    "tasks": [],
                    "suggested_folder": "Archive",
                    "reasoning": "Automatisch erkannt: Newsletter/automatische Nachricht",
                },
            }

        # Detect already-processed folders (archived, spam, etc.)
        if folder in ("spam", "junk", "trash", "deleted"):
            return {
                "confident": True,
                "stage": 1,
                "source": "folder_classification",
                "analysis": {
                    "summary": f"E-Mail aus {folder}-Ordner",
                    "category": "Unklar",
                    "spam_probability": 0.9,
                    "action_required": False,
                    "priority": "LOW",
                    "tasks": [],
                    "suggested_folder": "Archive",
                    "reasoning": f"Erkannt als {folder} durch Ordnername",
                },
            }

        return {"confident": False, "stage": 1, "analysis": self._fallback_analysis(email)}

    def _is_newsletter_sender(self, sender: str) -> bool:
        """Check if sender matches newsletter/automated patterns."""
        return any(re.search(p, sender) for p in _NEWSLETTER_PATTERNS)

    def _has_spam_subject(self, subject: str) -> bool:
        """Check if subject matches known spam patterns."""
        return any(re.search(p, subject) for p in _SPAM_SUBJECT_PATTERNS)

    # ------------------------------------------------------------------
    # Stage 2: Rule-based classification
    # ------------------------------------------------------------------

    def stage2_rule_classify(self, email: ProcessedEmail) -> Dict[str, Any]:
        """
        Rule-based classification using ClassificationOverride rules.

        Returns a result dict with 'confident' flag and 'analysis'.
        """
        from src.models.database import ClassificationOverride

        sender = (email.sender or "").lower()
        subject = (email.subject or "").lower()

        rules = self.db.query(ClassificationOverride).all()
        matched_rule = None

        for rule in rules:
            if rule.sender_pattern:
                pattern = rule.sender_pattern.lower()
                # Ensure accurate domain matching: normalize to "@domain" form
                # so "@example.com" does not match "notexample.com"
                if not pattern.startswith("@"):
                    pattern = "@" + pattern
                if sender.endswith(pattern):
                    if rule.subject_pattern:
                        if rule.subject_pattern.lower() in subject:
                            matched_rule = rule
                            break
                    else:
                        matched_rule = rule
                        break
            elif rule.subject_pattern:
                if rule.subject_pattern.lower() in subject:
                    matched_rule = rule
                    break

        if not matched_rule:
            return {"confident": False, "stage": 2, "analysis": self._fallback_analysis(email)}

        # Build analysis from rule
        base_spam = 0.2
        analysis = {
            "summary": f"Klassifiziert per Regel von {email.sender or 'Unbekannt'}",
            "category": matched_rule.category or "Unklar",
            "spam_probability": 0.95 if matched_rule.spam is True else (0.05 if matched_rule.spam is False else base_spam),
            "action_required": matched_rule.action_required if matched_rule.action_required is not None else False,
            "priority": matched_rule.priority or "LOW",
            "tasks": [],
            "suggested_folder": matched_rule.suggested_folder or "Archive",
            "reasoning": f"Angewendete Override-Regel ID={matched_rule.id}",
        }
        return {
            "confident": True,
            "stage": 2,
            "source": f"override_rule:{matched_rule.id}",
            "analysis": analysis,
        }

    # ------------------------------------------------------------------
    # Stage 3: Deep LLM analysis
    # ------------------------------------------------------------------

    def stage3_llm_analyse(self, email: ProcessedEmail) -> Dict[str, Any]:
        """
        Deep LLM analysis via Ollama.

        Only called when Stages 1 and 2 are inconclusive.
        """
        from src.services.ai_service import AIService

        ai_service = AIService()
        email_data = {
            "subject": email.subject or "",
            "sender": email.sender or "",
            "body_plain": email.body_plain or "",
            "body_html": email.body_html or "",
        }

        try:
            analysis = ai_service.analyze_email(email_data)
        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"LLM analysis failed: {sanitized}")
            analysis = ai_service.fallback_classification(email_data)

        return {
            "confident": True,
            "stage": 3,
            "source": "llm",
            "analysis": analysis,
        }

    def _llm_budget_available(self) -> bool:
        """Check if we have remaining LLM call budget."""
        return self._llm_calls_this_run < self.settings.max_llm_calls_per_run

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fallback_analysis(self, email: ProcessedEmail) -> Dict[str, Any]:
        """Minimal fallback analysis when all stages fail."""
        return {
            "summary": f"E-Mail von {email.sender or 'Unbekannt'}: {email.subject or 'Kein Betreff'}",
            "category": "Unklar",
            "spam_probability": 0.2,
            "action_required": False,
            "priority": "LOW",
            "tasks": [],
            "suggested_folder": "Archive",
            "reasoning": "Automatische Fallback-Klassifizierung",
        }

    def apply_analysis_to_record(
        self, email_record: ProcessedEmail, analysis: Dict[str, Any]
    ) -> None:
        """Apply analysis results back to the email record."""
        email_record.summary = analysis.get("summary")
        email_record.category = analysis.get("category")
        email_record.spam_probability = analysis.get("spam_probability")
        email_record.action_required = analysis.get("action_required")
        email_record.priority = analysis.get("priority")
        email_record.suggested_folder = analysis.get("suggested_folder")
        email_record.reasoning = analysis.get("reasoning")
        email_record.is_spam = (analysis.get("spam_probability", 0.0) >= self.settings.spam_threshold)
        email_record.is_processed = True
        email_record.processed_at = datetime.utcnow()
        email_record.analysis_version = PIPELINE_VERSION
        self.db.add(email_record)

    def update_analysis_state(self, email: ProcessedEmail, state: str) -> None:
        """Update the analysis_state of an email record."""
        email.analysis_state = state
        self.db.add(email)

    def record_decision(
        self,
        email: ProcessedEmail,
        event_type: str,
        result: Dict[str, Any],
    ) -> None:
        """Record a decision event for learning/audit purposes."""
        try:
            analysis = result.get("analysis", {})
            stage = result.get("stage", 0)
            # Use a stage-appropriate confidence: stage 1/2 use fixed values, stage 3 uses spam_probability
            if stage == 1:
                confidence = 0.8
            elif stage == 2:
                confidence = 0.9
            else:
                confidence = analysis.get("spam_probability", 0.5)
            event = DecisionEvent(
                email_id=email.id,
                thread_id=email.thread_id,
                event_type=event_type,
                source=result.get("source", f"pipeline_stage{stage}"),
                new_value=analysis.get("category"),
                confidence=confidence,
                model_version=PIPELINE_VERSION,
                created_at=datetime.utcnow(),
            )
            self.db.add(event)

            # Learning hook: store classification context for future training
            try:
                from src.pipeline.learning import record_classification_context

                record_classification_context(
                    self.db,
                    email,
                    analysis,
                    source=result.get("source", f"pipeline_stage{stage}"),
                )
            except Exception:
                pass  # learning hooks must never break the pipeline
        except Exception as e:
            # Decision recording must never break the pipeline
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.warning(f"Failed to record decision event: {sanitized}")

    # ------------------------------------------------------------------
    # Progress tracking (Priority 7)
    # ------------------------------------------------------------------

    def _get_or_create_progress(self, run_id: str, stage: str) -> AnalysisProgress:
        progress = (
            self.db.query(AnalysisProgress)
            .filter(
                AnalysisProgress.run_id == run_id,
                AnalysisProgress.stage == stage,
            )
            .first()
        )
        if not progress:
            progress = AnalysisProgress(
                run_id=run_id,
                stage=stage,
                status="running",
                started_at=datetime.utcnow(),
            )
            self.db.add(progress)
            self.db.commit()
        return progress

    def _mark_progress_paused(
        self,
        progress: AnalysisProgress,
        stats: Dict[str, int],
        reason: str,
    ) -> None:
        progress.status = "paused"
        progress.paused_at = datetime.utcnow()
        progress.paused_reason = reason
        progress.processed_count = stats.get("analysed", 0) + stats.get("skipped", 0)
        self.db.commit()

    def _mark_progress_complete(
        self, progress: AnalysisProgress, stats: Dict[str, Any]
    ) -> None:
        progress.status = "completed"
        progress.completed_at = datetime.utcnow()
        progress.processed_count = stats.get("analysed", 0) + stats.get("skipped", 0)
        progress.failed_count = stats.get("failed", 0)
        progress.llm_calls_used = stats.get("llm_calls", 0)
        self.db.commit()
