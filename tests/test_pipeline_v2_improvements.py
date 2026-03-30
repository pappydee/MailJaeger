"""
Tests for pipeline architecture improvements:

- STEP 1: No private method access from pipeline/analysis.py
- STEP 2: No EmailProcessor dependency from pipeline/analysis.py
- STEP 3: Real job resume behavior (last_processed_email_id used)
- STEP 4: Override re-application on future matching emails
- Additional: importance_scorer module
"""

import os
import inspect
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ENV = {
    "API_KEY": "test_key_pipeline_v2",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "true",
    "REQUIRE_APPROVAL": "false",
    "LEARNING_ENABLED": "true",
}


def _make_db():
    """Create an in-memory SQLite DB with all tables."""
    with patch.dict(os.environ, ENV):
        from src.config import reload_settings

        reload_settings()
    from src.models.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


# =====================================================================
# STEP 1: No private method access from pipeline/analysis.py
# =====================================================================


class TestNoPrivateMethodAccess:
    """pipeline/analysis.py must only call public methods."""

    def test_analysis_module_has_no_underscore_method_calls_on_pipeline(self):
        """pipeline/analysis.py must not call _private methods on pipeline object."""
        from src.pipeline import analysis

        source = inspect.getsource(analysis)
        # Look for patterns like pipeline._something(
        import re

        private_calls = re.findall(r"pipeline\._\w+\(", source)
        assert private_calls == [], (
            f"pipeline/analysis.py still calls private methods: {private_calls}"
        )

    def test_analysis_module_has_no_underscore_method_calls_on_ai_service(self):
        """pipeline/analysis.py must not call _private methods on ai_service."""
        from src.pipeline import analysis

        source = inspect.getsource(analysis)
        import re

        private_calls = re.findall(r"ai_service\._\w+\(", source)
        assert private_calls == [], (
            f"pipeline/analysis.py still calls private ai_service methods: {private_calls}"
        )

    def test_analysis_pipeline_has_public_stage1(self):
        """AnalysisPipeline.stage1_pre_classify must be a public method."""
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "stage1_pre_classify")
        assert callable(getattr(AnalysisPipeline, "stage1_pre_classify"))

    def test_analysis_pipeline_has_public_stage2(self):
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "stage2_rule_classify")
        assert callable(getattr(AnalysisPipeline, "stage2_rule_classify"))

    def test_analysis_pipeline_has_public_record_decision(self):
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "record_decision")
        assert callable(getattr(AnalysisPipeline, "record_decision"))

    def test_analysis_pipeline_has_public_update_analysis_state(self):
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "update_analysis_state")
        assert callable(getattr(AnalysisPipeline, "update_analysis_state"))

    def test_analysis_pipeline_has_public_apply_analysis_to_record(self):
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "apply_analysis_to_record")
        assert callable(getattr(AnalysisPipeline, "apply_analysis_to_record"))

    def test_backward_compat_aliases_still_exist(self):
        """Private aliases must still exist for backward compat."""
        from src.services.analysis_pipeline import AnalysisPipeline

        assert hasattr(AnalysisPipeline, "_stage1_pre_classify")
        assert hasattr(AnalysisPipeline, "_stage2_rule_classify")
        assert hasattr(AnalysisPipeline, "_record_decision")
        assert hasattr(AnalysisPipeline, "_update_analysis_state")
        assert hasattr(AnalysisPipeline, "_apply_analysis_to_record")

    def test_ai_service_has_public_fallback(self):
        """AIService must have a public fallback_classification method."""
        from src.services.ai_service import AIService

        assert hasattr(AIService, "fallback_classification")
        assert callable(getattr(AIService, "fallback_classification"))


# =====================================================================
# STEP 2: No EmailProcessor dependency from pipeline/analysis.py
# =====================================================================


class TestNoEmailProcessorDependency:
    """pipeline/analysis.py must not import or instantiate EmailProcessor."""

    def test_analysis_module_does_not_import_email_processor(self):
        from src.pipeline import analysis

        source = inspect.getsource(analysis)
        assert "EmailProcessor" not in source, (
            "pipeline/analysis.py must not reference EmailProcessor"
        )

    def test_importance_scorer_module_exists(self):
        from src.services.importance_scorer import compute_importance_score
        from src.services.importance_scorer import compute_pending_importance_scores

        assert callable(compute_importance_score)
        assert callable(compute_pending_importance_scores)

    def test_importance_scorer_computes_score(self):
        db = _make_db()
        from src.models.database import ProcessedEmail
        from src.services.importance_scorer import compute_importance_score

        email = ProcessedEmail(
            message_id="<score-test@example.com>",
            subject="Urgent: Deadline tomorrow",
            sender="boss@company.com",
            analysis_state="pending",
        )
        db.add(email)
        db.flush()

        score = compute_importance_score(db, email)
        # Should get urgent keyword bonus
        assert score > 30.0
        assert 0.0 <= score <= 100.0
        db.close()

    def test_importance_scorer_penalizes_newsletter(self):
        db = _make_db()
        from src.models.database import ProcessedEmail
        from src.services.importance_scorer import compute_importance_score

        email = ProcessedEmail(
            message_id="<newsletter-score@example.com>",
            subject="Weekly newsletter digest",
            sender="noreply@spam.com",
            analysis_state="pending",
        )
        db.add(email)
        db.flush()

        score = compute_importance_score(db, email)
        # Should get newsletter penalty
        assert score < 30.0
        db.close()

    def test_email_processor_delegates_to_importance_scorer(self):
        """EmailProcessor.compute_importance_score should delegate to the service."""
        db = _make_db()
        from src.services.email_processor import EmailProcessor
        from src.models.database import ProcessedEmail

        processor = EmailProcessor(db_session=db)
        email = ProcessedEmail(
            message_id="<delegate-test@example.com>",
            subject="Test",
            sender="user@example.com",
            analysis_state="pending",
        )
        db.add(email)
        db.flush()

        score = processor.compute_importance_score(email)
        assert 0.0 <= score <= 100.0
        db.close()


# =====================================================================
# STEP 3: Real job resume behavior
# =====================================================================


class TestRealJobResume:
    """Analysis jobs must use last_processed_email_id for resume filtering."""

    def test_run_analysis_accepts_resume_after_id(self):
        """run_analysis must accept a resume_after_id parameter."""
        from src.pipeline.analysis import run_analysis

        sig = inspect.signature(run_analysis)
        assert "resume_after_id" in sig.parameters

    def test_run_analysis_skips_emails_before_cursor(self):
        """Emails with id <= resume_after_id should be skipped."""
        db = _make_db()
        from src.models.database import ProcessedEmail
        from src.pipeline.analysis import run_analysis

        # Create 3 emails: IDs 1, 2, 3
        emails = []
        for i in range(3):
            email = ProcessedEmail(
                message_id=f"<resume-{i}@example.com>",
                subject=f"Email {i}",
                sender=f"noreply@newsletter{i}.com",  # Will match newsletter pattern
                analysis_state="pending",
            )
            db.add(email)
            emails.append(email)
        db.flush()

        # Resume after id of first email — should only process emails 2 and 3
        resume_after = emails[0].id
        stats = run_analysis(db, resume_after_id=resume_after)

        # All 3 are newsletters → stage1 pre-classification
        # But only 2 should be processed (emails[1] and emails[2])
        assert stats["analysed"] == 2
        db.close()

    def test_run_analysis_returns_last_email_id(self):
        """Stats should include last_email_id for cursor tracking."""
        db = _make_db()
        from src.models.database import ProcessedEmail
        from src.pipeline.analysis import run_analysis

        email = ProcessedEmail(
            message_id="<last-id-test@example.com>",
            subject="Newsletter test",
            sender="noreply@example.com",
            analysis_state="pending",
        )
        db.add(email)
        db.flush()
        email_id = email.id

        stats = run_analysis(db)

        assert "last_email_id" in stats
        assert stats["last_email_id"] == email_id
        db.close()

    def test_analysis_job_persists_cursor_and_uses_it(self):
        """run_analysis_job should persist and reuse the cursor."""
        db = _make_db()
        from src.models.database import ProcessedEmail, ProcessingJob
        from src.pipeline.jobs import run_analysis_job

        # Create 3 emails
        for i in range(3):
            email = ProcessedEmail(
                message_id=f"<job-resume-{i}@example.com>",
                subject=f"Newsletter {i}",
                sender="noreply@bulk.com",
                analysis_state="pending",
            )
            db.add(email)
        db.commit()

        # First job: process all 3
        result1 = run_analysis_job(db)
        assert result1["stats"]["analysed"] == 3
        job1_id = result1["job_id"]

        # Check the job has a cursor
        job1 = db.query(ProcessingJob).filter(ProcessingJob.id == job1_id).first()
        assert job1.last_processed_email_id is not None
        cursor_after_first = job1.last_processed_email_id

        # Create 2 more emails
        for i in range(3, 5):
            email = ProcessedEmail(
                message_id=f"<job-resume-{i}@example.com>",
                subject=f"Newsletter {i}",
                sender="noreply@bulk.com",
                analysis_state="pending",
            )
            db.add(email)
        db.commit()

        # Simulate a resumed job by creating one with "running" status and cursor
        existing_job = ProcessingJob(
            job_type="analysis",
            status="running",
            started_at=datetime.now(timezone.utc),
            last_processed_email_id=cursor_after_first,
            processed_count=3,
        )
        db.add(existing_job)
        db.commit()

        # Second job: should resume and only process the 2 new emails
        result2 = run_analysis_job(db)
        assert result2["stats"]["analysed"] == 2  # Only the 2 new ones
        assert result2["job_id"] == existing_job.id  # Resumed the existing job
        db.close()


# =====================================================================
# STEP 4: Override re-application
# =====================================================================


class TestOverrideReapplication:
    """Override rules must be re-applicable to matching emails."""

    def test_apply_override_to_matching_emails_exists(self):
        from src.pipeline.learning import apply_override_to_matching_emails

        assert callable(apply_override_to_matching_emails)

    def test_override_reapplies_to_matching_sender(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, ClassificationOverride
        from src.pipeline.learning import apply_override_to_matching_emails

        # Create override rule for @spam.com
        rule = ClassificationOverride(
            sender_pattern="@spam.com",
            category="Spam",
            spam=True,
            priority="LOW",
        )
        db.add(rule)
        db.flush()

        # Create emails from matching domain — not yet overridden
        for i in range(3):
            email = ProcessedEmail(
                message_id=f"<reapply-{i}@spam.com>",
                subject=f"Spam {i}",
                sender=f"user{i}@spam.com",
                is_processed=True,
                analysis_state="deep_analyzed",
                category="Unklar",
            )
            db.add(email)
        # And one from non-matching domain
        other = ProcessedEmail(
            message_id="<safe@good.com>",
            subject="Important",
            sender="boss@good.com",
            is_processed=True,
            analysis_state="deep_analyzed",
            category="Privat",
        )
        db.add(other)
        db.commit()

        # Apply the override
        stats = apply_override_to_matching_emails(db, rule.id)

        assert stats["matched"] == 3
        assert stats["updated"] == 3

        # Verify the matching emails were updated
        spam_emails = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.sender.ilike("%@spam.com"))
            .all()
        )
        for email in spam_emails:
            assert email.category == "Spam"
            assert email.is_spam is True
            assert email.override_rule_id == rule.id

        # Verify non-matching email was NOT changed
        safe_email = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.sender == "boss@good.com")
            .first()
        )
        assert safe_email.category == "Privat"
        assert safe_email.is_spam is not True
        db.close()

    def test_override_skips_already_overridden_emails(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, ClassificationOverride
        from src.pipeline.learning import apply_override_to_matching_emails

        rule = ClassificationOverride(
            sender_pattern="@example.com",
            category="Marketing",
        )
        db.add(rule)
        db.flush()

        # Email manually overridden by user
        manual = ProcessedEmail(
            message_id="<manual@example.com>",
            subject="Test",
            sender="user@example.com",
            is_processed=True,
            analysis_state="deep_analyzed",
            overridden=True,  # Manual override
            category="Privat",
        )
        db.add(manual)
        db.commit()

        stats = apply_override_to_matching_emails(db, rule.id)

        assert stats["updated"] == 0
        # Manual override should be preserved
        reloaded = db.query(ProcessedEmail).filter(ProcessedEmail.id == manual.id).first()
        assert reloaded.category == "Privat"
        db.close()

    def test_override_records_decision_events(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, ClassificationOverride, DecisionEvent
        from src.pipeline.learning import apply_override_to_matching_emails

        rule = ClassificationOverride(
            sender_pattern="@audit.com",
            category="Verwaltung",
        )
        db.add(rule)
        db.flush()

        email = ProcessedEmail(
            message_id="<audit@audit.com>",
            subject="Test",
            sender="admin@audit.com",
            is_processed=True,
            analysis_state="classified",
        )
        db.add(email)
        db.commit()

        apply_override_to_matching_emails(db, rule.id)

        # Should have recorded a decision event
        events = (
            db.query(DecisionEvent)
            .filter(
                DecisionEvent.email_id == email.id,
                DecisionEvent.event_type == "override_reapplication",
            )
            .all()
        )
        assert len(events) == 1
        assert "override_rule:" in events[0].source
        db.close()

    def test_override_with_nonexistent_rule(self):
        db = _make_db()
        from src.pipeline.learning import apply_override_to_matching_emails

        stats = apply_override_to_matching_emails(db, 9999)
        assert stats["matched"] == 0
        assert stats["updated"] == 0
        db.close()

    def test_override_with_subject_pattern(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, ClassificationOverride
        from src.pipeline.learning import apply_override_to_matching_emails

        rule = ClassificationOverride(
            sender_pattern="@mixed.com",
            subject_pattern="INVOICE",
            category="Verwaltung",
            action_required=True,
        )
        db.add(rule)
        db.flush()

        # Matching email
        match = ProcessedEmail(
            message_id="<invoice@mixed.com>",
            subject="INVOICE #12345",
            sender="billing@mixed.com",
            is_processed=True,
            analysis_state="classified",
        )
        # Non-matching subject
        no_match = ProcessedEmail(
            message_id="<promo@mixed.com>",
            subject="Special Offer",
            sender="promo@mixed.com",
            is_processed=True,
            analysis_state="classified",
        )
        db.add_all([match, no_match])
        db.commit()

        stats = apply_override_to_matching_emails(db, rule.id)

        # SQL filters both sender and subject → only the INVOICE email matches
        assert stats["updated"] == 1
        assert stats["skipped"] == 0

        reloaded_match = db.query(ProcessedEmail).filter(ProcessedEmail.id == match.id).first()
        assert reloaded_match.category == "Verwaltung"
        assert reloaded_match.action_required is True

        reloaded_no = db.query(ProcessedEmail).filter(ProcessedEmail.id == no_match.id).first()
        assert reloaded_no.category != "Verwaltung"
        db.close()
