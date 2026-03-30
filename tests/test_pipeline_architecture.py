"""
Tests for the pipeline architecture: strict separation of processing phases,
resumable processing jobs, and learning hooks.

Covers:
- Pipeline module independence (ingestion, analysis, actions, learning)
- ProcessingJob model and job lifecycle
- Learning hooks record DecisionEvents
- Batch analysis through pipeline module
- No cross-layer side effects
"""

import os
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ENV = {
    "API_KEY": "test_key_pipeline",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "true",
    "REQUIRE_APPROVAL": "false",
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


# ==========================================================================
# 1. ProcessingJob model exists and has required fields
# ==========================================================================


class TestProcessingJobModel:
    """ProcessingJob model provides resumable job tracking."""

    def test_processing_job_table_exists(self):
        db = _make_db()
        from src.models.database import ProcessingJob

        # Table must exist (create_all succeeded)
        job = ProcessingJob(
            job_type="ingestion",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        assert job.id is not None
        db.close()

    def test_processing_job_has_required_fields(self):
        from src.models.database import ProcessingJob
        import inspect

        source = inspect.getsource(ProcessingJob)
        for field in [
            "job_type",
            "run_id",
            "status",
            "last_processed_email_id",
            "processed_count",
            "failed_count",
            "result_stats",
            "error_message",
            "started_at",
            "resumed_at",
            "completed_at",
        ]:
            assert field in source, f"ProcessingJob must have '{field}' column"

    def test_processing_job_lifecycle(self):
        db = _make_db()
        from src.models.database import ProcessingJob

        job = ProcessingJob(
            job_type="analysis",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        assert job.status == "running"

        # Simulate completion
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.processed_count = 10
        job.result_stats = {"analysed": 10, "failed": 0}
        db.commit()

        reloaded = db.query(ProcessingJob).filter(ProcessingJob.id == job.id).first()
        assert reloaded.status == "completed"
        assert reloaded.processed_count == 10
        assert reloaded.result_stats["analysed"] == 10
        db.close()

    def test_processing_job_resume(self):
        db = _make_db()
        from src.models.database import ProcessingJob

        # Create a paused job
        job = ProcessingJob(
            job_type="analysis",
            status="paused",
            started_at=datetime.now(timezone.utc),
            last_processed_email_id=42,
            processed_count=5,
        )
        db.add(job)
        db.commit()

        # Resume
        job.status = "running"
        job.resumed_at = datetime.now(timezone.utc)
        db.commit()

        reloaded = db.query(ProcessingJob).filter(ProcessingJob.id == job.id).first()
        assert reloaded.status == "running"
        assert reloaded.resumed_at is not None
        assert reloaded.last_processed_email_id == 42
        db.close()


# ==========================================================================
# 2. Pipeline modules are independently importable
# ==========================================================================


class TestPipelineModuleIndependence:
    """Each pipeline module can be imported and called independently."""

    def test_ingestion_module_importable(self):
        from src.pipeline.ingestion import run_ingestion
        assert callable(run_ingestion)

    def test_analysis_module_importable(self):
        from src.pipeline.analysis import run_analysis
        assert callable(run_analysis)

    def test_actions_module_importable(self):
        from src.pipeline.actions import run_actions
        assert callable(run_actions)

    def test_learning_module_importable(self):
        from src.pipeline.learning import (
            record_classification_context,
            record_user_feedback,
            aggregate_sender_stats,
            get_learning_summary,
        )
        assert callable(record_classification_context)
        assert callable(record_user_feedback)
        assert callable(aggregate_sender_stats)
        assert callable(get_learning_summary)

    def test_jobs_module_importable(self):
        from src.pipeline.jobs import (
            run_ingestion_job,
            run_analysis_job,
            run_action_job,
        )
        assert callable(run_ingestion_job)
        assert callable(run_analysis_job)
        assert callable(run_action_job)


# ==========================================================================
# 3. Pipeline ingestion phase — no AI
# ==========================================================================


class TestPipelineIngestion:
    """Ingestion phase must delegate to MailIngestionService without AI calls."""

    def test_ingestion_delegates_to_mail_ingestion_service(self):
        db = _make_db()
        mock_service = MagicMock()
        mock_service.ingest_folder.return_value = {
            "new": 5,
            "skipped": 3,
            "failed": 0,
            "total": 8,
        }

        with patch(
            "src.services.mail_ingestion_service.MailIngestionService",
            return_value=mock_service,
        ):
            from src.pipeline.ingestion import run_ingestion

            stats = run_ingestion(db, folder="INBOX", run_id="test-run")

        assert stats["new"] == 5
        assert stats["skipped"] == 3
        mock_service.ingest_folder.assert_called_once_with(
            folder="INBOX", run_id="test-run"
        )
        db.close()

    def test_ingestion_returns_empty_stats_on_error(self):
        db = _make_db()
        with patch(
            "src.services.mail_ingestion_service.MailIngestionService",
            side_effect=Exception("IMAP error"),
        ):
            from src.pipeline.ingestion import run_ingestion

            stats = run_ingestion(db)

        assert stats["new"] == 0
        assert stats["failed"] == 0
        db.close()


# ==========================================================================
# 4. Pipeline analysis phase — no IMAP side effects
# ==========================================================================


class TestPipelineAnalysis:
    """Analysis phase classifies emails without executing IMAP actions."""

    def test_analysis_with_no_pending_emails(self):
        db = _make_db()
        from src.pipeline.analysis import run_analysis

        stats = run_analysis(db)
        assert stats["analysed"] == 0
        db.close()

    def test_analysis_does_not_import_imap_service(self):
        """Analysis module must not directly use IMAPService."""
        import inspect
        from src.pipeline import analysis

        source = inspect.getsource(analysis)
        assert "IMAPService" not in source, (
            "Analysis pipeline module must not import or use IMAPService"
        )


# ==========================================================================
# 5. Pipeline action phase — only approved actions
# ==========================================================================


class TestPipelineActions:
    """Action execution phase only processes approved actions."""

    def test_actions_with_no_approved(self):
        db = _make_db()
        from src.pipeline.actions import run_actions

        stats = run_actions(db)
        assert stats["total"] == 0
        assert stats["executed"] == 0
        db.close()

    def test_actions_skips_non_approved(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, ActionQueue
        from src.pipeline.actions import run_actions

        email = ProcessedEmail(
            message_id="<test@example.com>",
            subject="Test",
            sender="test@example.com",
            is_processed=True,
            analysis_state="deep_analyzed",
        )
        db.add(email)
        db.flush()

        # Only proposed — should not execute
        action = ActionQueue(
            email_id=email.id,
            action_type="move",
            payload={"target_folder": "Archive"},
            status="proposed",
        )
        db.add(action)
        db.commit()

        stats = run_actions(db)
        assert stats["total"] == 0
        db.close()


# ==========================================================================
# 6. Learning module — structured logging and hooks
# ==========================================================================


class TestPipelineLearning:
    """Learning module captures decision events and classification context."""

    def test_record_classification_context(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, DecisionEvent
        from src.pipeline.learning import record_classification_context

        email = ProcessedEmail(
            message_id="<learn1@example.com>",
            subject="Test Learning",
            sender="user@example.com",
            is_processed=True,
            analysis_state="deep_analyzed",
        )
        db.add(email)
        db.flush()

        record_classification_context(
            db,
            email,
            {"category": "Privat", "spam_probability": 0.1},
            source="pipeline_stage3",
        )
        db.commit()

        events = (
            db.query(DecisionEvent)
            .filter(DecisionEvent.email_id == email.id)
            .all()
        )
        assert len(events) >= 1
        assert events[0].event_type == "classification"
        assert events[0].source == "pipeline_stage3"
        db.close()

    def test_record_user_feedback(self):
        db = _make_db()
        from src.models.database import ProcessedEmail, DecisionEvent
        from src.pipeline.learning import record_user_feedback

        email = ProcessedEmail(
            message_id="<learn2@example.com>",
            subject="Test Feedback",
            sender="user@example.com",
            is_processed=True,
            analysis_state="deep_analyzed",
        )
        db.add(email)
        db.flush()

        event = record_user_feedback(
            db,
            email_id=email.id,
            event_type="approve_suggestion",
            old_value="proposed",
            new_value="move",
            user_confirmed=True,
        )
        db.commit()

        assert event is not None
        assert event.source == "user"
        assert event.user_confirmed is True
        db.close()

    def test_get_learning_summary(self):
        db = _make_db()
        from src.pipeline.learning import get_learning_summary

        summary = get_learning_summary(db)
        assert "total_decision_events" in summary
        assert "user_feedback_events" in summary
        assert "learning_ready" in summary
        assert summary["learning_ready"] is False  # no events yet
        db.close()

    def test_aggregate_sender_stats_empty(self):
        db = _make_db()
        from src.pipeline.learning import aggregate_sender_stats

        stats = aggregate_sender_stats(db, "example.com")
        assert stats["domain"] == "example.com"
        assert stats["total"] == 0
        db.close()

    def test_aggregate_sender_stats_with_data(self):
        db = _make_db()
        from src.models.database import ProcessedEmail
        from src.pipeline.learning import aggregate_sender_stats

        for i in range(5):
            email = ProcessedEmail(
                message_id=f"<stats{i}@example.com>",
                subject="Test",
                sender=f"user{i}@example.com",
                is_processed=True,
                is_spam=(i == 0),
                action_required=(i < 2),
                category="Privat",
                analysis_state="deep_analyzed",
            )
            db.add(email)
        db.commit()

        stats = aggregate_sender_stats(db, "example.com")
        assert stats["total"] == 5
        assert stats["spam_rate"] == 0.2  # 1/5
        assert stats["action_rate"] == 0.4  # 2/5
        db.close()


# ==========================================================================
# 7. Processing jobs — lifecycle and tracking
# ==========================================================================


class TestProcessingJobs:
    """Processing jobs track pipeline phase execution with progress."""

    def test_ingestion_job_creates_and_completes(self):
        db = _make_db()
        from src.models.database import ProcessingJob

        mock_service = MagicMock()
        mock_service.ingest_folder.return_value = {
            "new": 3, "skipped": 1, "failed": 0, "total": 4
        }

        with patch(
            "src.services.mail_ingestion_service.MailIngestionService",
            return_value=mock_service,
        ):
            from src.pipeline.jobs import run_ingestion_job

            result = run_ingestion_job(db, folder="INBOX")

        assert result["status"] == "completed"
        assert result["stats"]["new"] == 3

        # Job record persisted
        job = db.query(ProcessingJob).filter(ProcessingJob.id == result["job_id"]).first()
        assert job is not None
        assert job.job_type == "ingestion"
        assert job.status == "completed"
        assert job.processed_count == 4
        db.close()

    def test_analysis_job_creates_and_completes(self):
        db = _make_db()
        from src.models.database import ProcessingJob
        from src.pipeline.jobs import run_analysis_job

        # No pending emails — should complete immediately
        result = run_analysis_job(db)
        assert result["status"] == "completed"

        job = db.query(ProcessingJob).filter(ProcessingJob.id == result["job_id"]).first()
        assert job is not None
        assert job.job_type == "analysis"
        assert job.status == "completed"
        db.close()

    def test_action_job_creates_and_completes(self):
        db = _make_db()
        from src.models.database import ProcessingJob
        from src.pipeline.jobs import run_action_job

        result = run_action_job(db)
        assert result["status"] == "completed"

        job = db.query(ProcessingJob).filter(ProcessingJob.id == result["job_id"]).first()
        assert job is not None
        assert job.job_type == "action"
        assert job.status == "completed"
        db.close()

    def test_failed_job_records_error(self):
        db = _make_db()
        from src.models.database import ProcessingJob
        from src.pipeline.jobs import run_ingestion_job, _start_job, _finish_job

        # Simulate a failure at the job level (not swallowed by run_ingestion)
        job = _start_job(db, "ingestion")
        _finish_job(db, job, {}, status="failed", error_message="IMAP connection refused")

        reloaded = db.query(ProcessingJob).filter(ProcessingJob.id == job.id).first()
        assert reloaded.status == "failed"
        assert reloaded.error_message == "IMAP connection refused"
        db.close()

    def test_job_resume_finds_existing_running_job(self):
        db = _make_db()
        from src.models.database import ProcessingJob

        # Create a "running" job manually (simulates interrupted job)
        existing = ProcessingJob(
            job_type="ingestion",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(existing)
        db.commit()
        existing_id = existing.id

        mock_service = MagicMock()
        mock_service.ingest_folder.return_value = {
            "new": 0, "skipped": 0, "failed": 0, "total": 0
        }
        with patch(
            "src.services.mail_ingestion_service.MailIngestionService",
            return_value=mock_service,
        ):
            from src.pipeline.jobs import run_ingestion_job

            result = run_ingestion_job(db)

        # Should resume the existing job, not create a new one
        assert result["job_id"] == existing_id
        db.close()


# ==========================================================================
# 8. Existing EmailProcessor still works (backward compatibility)
# ==========================================================================


class TestBackwardCompatibility:
    """EmailProcessor.process_emails() still works as before."""

    def test_email_processor_process_emails_still_exists(self):
        from src.services.email_processor import EmailProcessor

        assert hasattr(EmailProcessor, "process_emails")
        assert hasattr(EmailProcessor, "_run_ingestion")
        assert hasattr(EmailProcessor, "_process_indexed_email")
        assert hasattr(EmailProcessor, "compute_importance_score")

    def test_email_processor_two_phase_still_works(self):
        db = _make_db()
        from src.services.email_processor import EmailProcessor

        processor = EmailProcessor(db_session=db)
        with patch.object(
            processor,
            "_run_ingestion",
            return_value={"new": 0, "skipped": 0, "failed": 0},
        ):
            run = processor.process_emails(trigger_type="MANUAL")

        assert run.status == "SUCCESS"
        db.close()
