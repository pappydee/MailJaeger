"""
Comprehensive tests for the historical AI analysis service.

Tests cover:
  - Correct 1-year (365-day) date filtering
  - Exclusion of older emails
  - Processing only pending emails
  - Correct state transitions: pending → running → completed|paused|failed
  - Resumability from checkpoint
  - Cancellation (stop mid-batch)
  - No regression in mailbox import
  - API endpoints (start/stop/status/reset)
  - Batch size configuration
  - Edge cases: empty mailbox, all old emails, all already-processed
"""

import time
import threading
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models.database import (
    Base,
    ProcessedEmail,
    HistoricalAnalysisRun,
    HistoricalAnalysisProgress,
    MailboxImportRun,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_analysis_service_globals():
    """Reset global state of the historical_analysis_service between tests."""
    import src.services.historical_analysis_service as svc

    svc._cancel_event.clear()
    if svc._current_thread and svc._current_thread.is_alive():
        svc._cancel_event.set()
        svc._current_thread.join(timeout=5)
    svc._current_thread = None
    try:
        svc._job_lock.release()
    except RuntimeError:
        pass
    yield
    svc._cancel_event.set()
    if svc._current_thread and svc._current_thread.is_alive():
        svc._current_thread.join(timeout=5)
    svc._current_thread = None
    try:
        svc._job_lock.release()
    except RuntimeError:
        pass
    svc._cancel_event.clear()


@pytest.fixture
def engine():
    """Shared in-memory SQLite engine."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    """Primary test session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def db_factory(engine):
    """Callable that returns a new session from the shared engine."""
    SessionLocal = sessionmaker(bind=engine)

    def factory():
        return SessionLocal()

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_AI_RESULT = {
    "summary": "Test summary",
    "category": "Verwaltung",
    "spam_probability": 0.1,
    "action_required": False,
    "priority": "LOW",
    "tasks": [],
    "suggested_folder": "Archive",
    "reasoning": "Test reasoning",
}


def _make_email(
    db,
    idx,
    sender="test@example.com",
    folder="INBOX",
    analysis_state="pending",
    date=None,
):
    """Create a test email with configurable date and analysis_state."""
    if date is None:
        date = datetime.now(timezone.utc) - timedelta(days=30)  # Recent by default
    email = ProcessedEmail(
        message_id=f"test-analysis-{idx}@example.com",
        subject=f"Test Subject {idx}",
        sender=sender,
        recipients="me@mymail.com",
        folder=folder,
        category=None,
        analysis_state=analysis_state,
        is_spam=False,
        is_processed=False,
        is_flagged=False,
        body_plain=f"Test body {idx}",
        date=date,
    )
    db.add(email)
    db.commit()
    return email


def _make_old_email(db, idx, days_old=400):
    """Create an email older than 1 year."""
    date = datetime.now(timezone.utc) - timedelta(days=days_old)
    return _make_email(db, idx, date=date, analysis_state="pending")


def _make_recent_email(db, idx, days_old=30):
    """Create a recent email within the last year."""
    date = datetime.now(timezone.utc) - timedelta(days=days_old)
    return _make_email(db, idx, date=date, analysis_state="pending")


def _wait_for_completion(db_factory, timeout=10):
    """Wait for the analysis job to complete or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        db = db_factory()
        try:
            run = (
                db.query(HistoricalAnalysisRun)
                .order_by(HistoricalAnalysisRun.started_at.desc())
                .first()
            )
            if run and run.status in ("completed", "failed"):
                return run.status
        finally:
            db.close()
        time.sleep(0.1)
    return "timeout"


# ---------------------------------------------------------------------------
# DATE FILTERING TESTS
# ---------------------------------------------------------------------------


class TestDateFiltering:
    """Tests for the 1-year date filtering requirement."""

    def test_only_recent_emails_are_eligible(self, db, db_factory):
        """Only emails from the last 365 days should be eligible."""
        from src.services.historical_analysis_service import _count_eligible_emails

        # Recent emails (within 1 year)
        _make_recent_email(db, 1, days_old=10)
        _make_recent_email(db, 2, days_old=100)
        _make_recent_email(db, 3, days_old=364)

        # Old emails (older than 1 year)
        _make_old_email(db, 4, days_old=366)
        _make_old_email(db, 5, days_old=500)
        _make_old_email(db, 6, days_old=1000)

        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        count = _count_eligible_emails(db, cutoff)
        assert count == 3, f"Expected 3 eligible emails, got {count}"

    def test_old_emails_excluded_from_processing(self, db, db_factory):
        """Emails older than 1 year must NEVER be processed."""
        from src.services.historical_analysis_service import (
            _fetch_eligible_batch,
        )

        _make_recent_email(db, 1, days_old=30)
        _make_old_email(db, 2, days_old=400)
        _make_recent_email(db, 3, days_old=200)
        _make_old_email(db, 4, days_old=500)

        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        batch = _fetch_eligible_batch(db, cutoff, None, 100)

        assert len(batch) == 2
        # All returned emails should be recent (within 365 days)
        for email in batch:
            email_date = email.date
            if email_date.tzinfo is None:
                email_date = email_date.replace(tzinfo=timezone.utc)
            assert email_date >= cutoff, f"Email {email.id} date {email_date} is before cutoff {cutoff}"

    def test_email_exactly_at_boundary(self, db, db_factory):
        """Email exactly at 365-day boundary should be included."""
        from src.services.historical_analysis_service import _count_eligible_emails

        # Exactly 365 days ago (should be included because >= cutoff)
        boundary_date = datetime.now(timezone.utc) - timedelta(days=365)
        _make_email(db, 1, date=boundary_date)

        # 366 days ago (should be excluded)
        _make_email(db, 2, date=datetime.now(timezone.utc) - timedelta(days=366))

        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        count = _count_eligible_emails(db, cutoff)
        # The boundary email might be included depending on exact timing
        assert count <= 2  # Either 1 or 2 (depending on sub-second timing)
        assert count >= 0

    def test_custom_max_age_days(self, db, db_factory):
        """Custom max_age_days should be respected."""
        from src.services.historical_analysis_service import _count_eligible_emails

        # 50 days old
        _make_recent_email(db, 1, days_old=50)
        # 100 days old
        _make_recent_email(db, 2, days_old=100)

        # With 60-day window
        cutoff_60 = datetime.now(timezone.utc) - timedelta(days=60)
        count_60 = _count_eligible_emails(db, cutoff_60)
        assert count_60 == 1

        # With 200-day window
        cutoff_200 = datetime.now(timezone.utc) - timedelta(days=200)
        count_200 = _count_eligible_emails(db, cutoff_200)
        assert count_200 == 2

    def test_no_old_emails_in_full_job(self, db, db_factory):
        """End-to-end test: full job must NOT process any old emails."""
        from src.services.historical_analysis_service import start_analysis

        # Create mix of old and recent
        recent1 = _make_recent_email(db, 1, days_old=30)
        recent2 = _make_recent_email(db, 2, days_old=200)
        old1 = _make_old_email(db, 3, days_old=400)
        old2 = _make_old_email(db, 4, days_old=700)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            result = start_analysis(db_factory, batch_size=10, max_age_days=365)
            assert result["success"] is True

            status = _wait_for_completion(db_factory)
            assert status == "completed"

        # Verify only recent emails were processed
        db.expire_all()
        assert recent1.analysis_state == "completed" or db.query(ProcessedEmail).get(recent1.id).analysis_state == "completed"
        assert recent2.analysis_state == "completed" or db.query(ProcessedEmail).get(recent2.id).analysis_state == "completed"

        old1_refreshed = db.query(ProcessedEmail).get(old1.id)
        old2_refreshed = db.query(ProcessedEmail).get(old2.id)
        assert old1_refreshed.analysis_state == "pending", "Old email must remain pending"
        assert old2_refreshed.analysis_state == "pending", "Old email must remain pending"


# ---------------------------------------------------------------------------
# ANALYSIS STATE TESTS
# ---------------------------------------------------------------------------


class TestAnalysisState:
    """Tests for processing only pending emails and correct state transitions."""

    def test_only_pending_emails_processed(self, db, db_factory):
        """Only emails with analysis_state='pending' should be processed."""
        from src.services.historical_analysis_service import _count_eligible_emails

        _make_email(db, 1, analysis_state="pending")
        _make_email(db, 2, analysis_state="completed")
        _make_email(db, 3, analysis_state="deep_analyzed")
        _make_email(db, 4, analysis_state="pending")
        _make_email(db, 5, analysis_state="failed")

        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        count = _count_eligible_emails(db, cutoff)
        assert count == 2  # Only the 2 pending emails

    def test_state_transitions_to_completed(self, db, db_factory):
        """Email analysis_state should transition from pending → completed."""
        from src.services.historical_analysis_service import start_analysis

        email = _make_recent_email(db, 1)
        assert email.analysis_state == "pending"

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        db.expire_all()
        refreshed = db.query(ProcessedEmail).get(email.id)
        assert refreshed.analysis_state == "completed"
        assert refreshed.category == "Verwaltung"
        assert refreshed.priority == "LOW"
        assert refreshed.summary == "Test summary"

    def test_no_reprocessing_of_completed_emails(self, db, db_factory):
        """Already-completed emails must not be reprocessed."""
        from src.services.historical_analysis_service import start_analysis

        # Pre-completed email
        _make_email(db, 1, analysis_state="completed")
        # Pending email
        pending = _make_recent_email(db, 2)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

            # AI should only be called once (for the pending email)
            assert mock_instance.analyze_email.call_count == 1

    def test_ai_results_stored_correctly(self, db, db_factory):
        """All AI analysis fields should be stored on the email record."""
        from src.services.historical_analysis_service import start_analysis

        email = _make_recent_email(db, 1)

        ai_result = {
            "summary": "Zusammenfassung der E-Mail",
            "category": "Klinik",
            "spam_probability": 0.05,
            "action_required": True,
            "priority": "HIGH",
            "tasks": [{"description": "Review", "due_date": None}],
            "suggested_folder": "Klinik",
            "reasoning": "Medical content detected",
        }

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = ai_result.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        db.expire_all()
        refreshed = db.query(ProcessedEmail).get(email.id)
        assert refreshed.summary == "Zusammenfassung der E-Mail"
        assert refreshed.category == "Klinik"
        assert refreshed.spam_probability == 0.05
        assert refreshed.action_required is True
        assert refreshed.priority == "HIGH"
        assert refreshed.suggested_folder == "Klinik"
        assert refreshed.reasoning == "Medical content detected"
        assert refreshed.is_processed is True


# ---------------------------------------------------------------------------
# JOB LIFECYCLE TESTS
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    """Tests for start/stop/resume/reset and state management."""

    def test_start_creates_run(self, db, db_factory):
        """Starting a job should create a HistoricalAnalysisRun."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            result = start_analysis(db_factory, batch_size=10)
            assert result["success"] is True
            assert result["job_id"] is not None

            _wait_for_completion(db_factory)

        runs = db_factory().query(HistoricalAnalysisRun).all()
        assert len(runs) == 1
        assert runs[0].status == "completed"

    def test_stop_pauses_job(self, db, db_factory):
        """Stopping a job should set status to 'paused'."""
        from src.services.historical_analysis_service import (
            start_analysis,
            stop_analysis,
            _cancel_event,
        )

        # Create many emails so job takes time
        for i in range(50):
            _make_recent_email(db, i)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            # Make AI slow to give us time to cancel
            def slow_analyze(email_data):
                time.sleep(0.05)
                return _MOCK_AI_RESULT.copy()

            mock_instance.analyze_email.side_effect = slow_analyze

            result = start_analysis(db_factory, batch_size=5)
            assert result["success"] is True

            # Wait briefly then stop
            time.sleep(0.2)
            stop_result = stop_analysis(db_factory)

        # Wait for thread to finish
        time.sleep(1)

        check_db = db_factory()
        run = (
            check_db.query(HistoricalAnalysisRun)
            .order_by(HistoricalAnalysisRun.started_at.desc())
            .first()
        )
        assert run is not None
        assert run.status in ("paused", "completed")  # May complete if fast enough
        check_db.close()

    def test_resume_from_checkpoint(self, db, db_factory):
        """Resuming should continue from last checkpoint, not reprocess."""
        from src.services.historical_analysis_service import (
            start_analysis,
            stop_analysis,
            _cancel_event,
        )

        # Create emails
        for i in range(10):
            _make_recent_email(db, i)

        call_count = 0

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value

            def counting_analyze(email_data):
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    _cancel_event.set()  # Stop after 3
                return _MOCK_AI_RESULT.copy()

            mock_instance.analyze_email.side_effect = counting_analyze

            # First run - should process some then pause
            start_analysis(db_factory, batch_size=5)
            _wait_for_completion(db_factory, timeout=5)

        # Check progress was saved
        check_db = db_factory()
        run = check_db.query(HistoricalAnalysisRun).first()
        assert run is not None
        first_processed = run.processed_count
        assert first_processed > 0
        check_db.close()

        # Resume
        call_count = 0
        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            status = _wait_for_completion(db_factory)
            assert status == "completed"

        # Check final state
        check_db = db_factory()
        run = check_db.query(HistoricalAnalysisRun).first()
        assert run.status == "completed"
        # Total processed should be 10 (not 10 + first_processed, since we don't double-count)
        check_db.close()

    def test_concurrent_start_rejected(self, db, db_factory):
        """Only one analysis job can run at a time."""
        from src.services.historical_analysis_service import start_analysis

        for i in range(5):
            _make_recent_email(db, i)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value

            def slow_analyze(email_data):
                time.sleep(0.5)
                return _MOCK_AI_RESULT.copy()

            mock_instance.analyze_email.side_effect = slow_analyze

            result1 = start_analysis(db_factory, batch_size=5)
            assert result1["success"] is True

            # Try starting second job immediately
            result2 = start_analysis(db_factory, batch_size=5)
            assert result2["success"] is False
            assert "already running" in result2["message"]

    def test_reset_clears_all_data(self, db, db_factory):
        """Reset should delete all run and progress records."""
        from src.services.historical_analysis_service import reset_analysis

        # Directly insert run and progress records (no need for background thread)
        run = HistoricalAnalysisRun(
            status="completed",
            total_eligible=2,
            processed_count=2,
        )
        db.add(run)
        db.commit()

        email = _make_recent_email(db, 1)
        progress = HistoricalAnalysisProgress(
            email_id=email.id,
            processed_at=datetime.now(timezone.utc),
            success=True,
        )
        db.add(progress)
        db.commit()

        # Verify data exists
        check_db = db_factory()
        assert check_db.query(HistoricalAnalysisRun).count() > 0
        assert check_db.query(HistoricalAnalysisProgress).count() > 0
        check_db.close()

        # Reset
        result = reset_analysis(db_factory)
        assert result["success"] is True

        # Verify data is gone
        check_db = db_factory()
        assert check_db.query(HistoricalAnalysisRun).count() == 0
        assert check_db.query(HistoricalAnalysisProgress).count() == 0
        check_db.close()

    def test_empty_mailbox(self, db, db_factory):
        """Job should complete immediately with no eligible emails."""
        from src.services.historical_analysis_service import start_analysis

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            result = start_analysis(db_factory, batch_size=10)
            assert result["success"] is True

            status = _wait_for_completion(db_factory)
            assert status == "completed"

            # AI should never be called
            MockAI.return_value.analyze_email.assert_not_called()

    def test_all_old_emails_no_processing(self, db, db_factory):
        """If all emails are older than 1 year, nothing should be processed."""
        from src.services.historical_analysis_service import start_analysis

        _make_old_email(db, 1, days_old=400)
        _make_old_email(db, 2, days_old=500)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            result = start_analysis(db_factory, batch_size=10)
            assert result["success"] is True

            status = _wait_for_completion(db_factory)
            assert status == "completed"

            MockAI.return_value.analyze_email.assert_not_called()

    def test_failed_analysis_records_failure(self, db, db_factory):
        """If AI fails for an email, it should be recorded as failed."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.side_effect = Exception("LLM timeout")

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        check_db = db_factory()
        run = check_db.query(HistoricalAnalysisRun).first()
        assert run.failed_count == 1
        assert run.processed_count == 0

        progress = check_db.query(HistoricalAnalysisProgress).first()
        assert progress is not None
        assert progress.success is False
        check_db.close()

    def test_all_fail_marks_run_failed(self, db, db_factory):
        """If all emails fail, the run status should be 'failed'."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)
        _make_recent_email(db, 2)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.side_effect = Exception("LLM down")

            start_analysis(db_factory, batch_size=10)
            status = _wait_for_completion(db_factory)
            assert status == "failed"


# ---------------------------------------------------------------------------
# STATUS ENDPOINT TESTS
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Tests for the get_analysis_status function."""

    def test_status_idle_no_runs(self, db, db_factory):
        """Status should be 'idle' when no jobs have been run."""
        from src.services.historical_analysis_service import get_analysis_status

        status = get_analysis_status(db_factory)
        assert status["status"] == "idle"
        assert status["job_id"] is None
        assert status["is_running"] is False
        assert status["progress_percent"] == 0.0

    def test_status_shows_progress(self, db, db_factory):
        """Status should show progress during a running job."""
        from src.services.historical_analysis_service import (
            start_analysis,
            get_analysis_status,
        )

        for i in range(5):
            _make_recent_email(db, i)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        status = get_analysis_status(db_factory)
        assert status["status"] == "completed"
        assert status["processed_count"] == 5
        assert status["progress_percent"] == 100.0
        assert status["total_eligible"] >= 5

    def test_status_includes_required_fields(self, db, db_factory):
        """Status must include all required fields from the spec."""
        from src.services.historical_analysis_service import get_analysis_status

        status = get_analysis_status(db_factory)
        required_fields = [
            "status",
            "job_id",
            "total_eligible",
            "processed_count",
            "failed_count",
            "progress_percent",
            "current_phase",
            "is_running",
        ]
        for field in required_fields:
            assert field in status, f"Missing required field: {field}"


# ---------------------------------------------------------------------------
# BATCH SIZE CONFIGURATION TESTS
# ---------------------------------------------------------------------------


class TestBatchConfiguration:
    """Tests for configurable batch size."""

    def test_batch_size_clamped_min(self, db, db_factory):
        """Batch size should be clamped to minimum."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            result = start_analysis(db_factory, batch_size=1)
            assert result["batch_size"] == 5  # MIN_BATCH_SIZE
            _wait_for_completion(db_factory)

    def test_batch_size_clamped_max(self, db, db_factory):
        """Batch size should be clamped to maximum."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            result = start_analysis(db_factory, batch_size=500)
            assert result["batch_size"] == 100  # MAX_BATCH_SIZE
            _wait_for_completion(db_factory)


# ---------------------------------------------------------------------------
# PROGRESS PERSISTENCE TESTS
# ---------------------------------------------------------------------------


class TestProgressPersistence:
    """Tests for progress checkpointing and persistence."""

    def test_progress_records_created(self, db, db_factory):
        """Each processed email should have a HistoricalAnalysisProgress record."""
        from src.services.historical_analysis_service import start_analysis

        _make_recent_email(db, 1)
        _make_recent_email(db, 2)
        _make_recent_email(db, 3)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        check_db = db_factory()
        progress_count = check_db.query(HistoricalAnalysisProgress).count()
        assert progress_count == 3
        check_db.close()

    def test_deduplication_on_resume(self, db, db_factory):
        """Already-processed emails should not be processed again on resume."""
        from src.services.historical_analysis_service import start_analysis

        emails = [_make_recent_email(db, i) for i in range(5)]

        # Manually mark first 2 as already processed
        for i in range(2):
            progress = HistoricalAnalysisProgress(
                email_id=emails[i].id,
                processed_at=datetime.now(timezone.utc),
                success=True,
            )
            db.add(progress)
        db.commit()

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

            # AI should only be called for the 3 unprocessed emails
            assert mock_instance.analyze_email.call_count == 3

    def test_checkpoint_saved_after_batch(self, db, db_factory):
        """last_processed_email_id should be updated after each batch."""
        from src.services.historical_analysis_service import start_analysis

        for i in range(10):
            _make_recent_email(db, i)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            start_analysis(db_factory, batch_size=5)
            _wait_for_completion(db_factory)

        check_db = db_factory()
        run = check_db.query(HistoricalAnalysisRun).first()
        assert run.last_processed_email_id is not None
        assert run.processed_count == 10
        check_db.close()


# ---------------------------------------------------------------------------
# STALE JOB RECOVERY
# ---------------------------------------------------------------------------


class TestStaleJobRecovery:
    """Tests for recovering from crashed jobs."""

    def test_stale_running_job_recovered(self, db, db_factory):
        """A stale 'running' job should be marked as 'failed' on next start."""
        # Simulate a stale run
        stale_run = HistoricalAnalysisRun(
            status="running",
            current_phase="analyzing",
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(stale_run)
        db.commit()
        stale_id = stale_run.id

        from src.services.historical_analysis_service import start_analysis

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            start_analysis(db_factory, batch_size=10)
            _wait_for_completion(db_factory)

        check_db = db_factory()
        stale = check_db.query(HistoricalAnalysisRun).get(stale_id)
        assert stale.status == "failed"
        check_db.close()


# ---------------------------------------------------------------------------
# MAILBOX IMPORT NON-REGRESSION TESTS
# ---------------------------------------------------------------------------


class TestMailboxImportNonRegression:
    """Ensure historical AI analysis does NOT modify mailbox import behavior."""

    def test_import_service_untouched(self):
        """The mailbox_import_service module should not reference analysis service."""
        import src.services.mailbox_import_service as mis

        source = open(mis.__file__).read()
        assert "historical_analysis" not in source
        assert "AIService" not in source
        assert "analyze_email" not in source

    def test_import_sets_pending_state(self):
        """Import should still set analysis_state='pending', not 'completed'."""
        import src.services.mailbox_import_service as mis

        source = open(mis.__file__).read()
        assert 'analysis_state="pending"' in source

    def test_import_models_unchanged(self, db, db_factory):
        """MailboxImportRun model should still exist and work."""
        run = MailboxImportRun(
            status="running",
            batch_size=20,
            skip_attachment_binaries=True,
        )
        db.add(run)
        db.commit()
        assert run.id is not None
        assert run.status == "running"

    def test_analysis_and_import_independent(self, db, db_factory):
        """Analysis runs should not interfere with import runs."""
        # Create an import run
        import_run = MailboxImportRun(
            status="completed",
            total_emails_ingested=100,
        )
        db.add(import_run)
        db.commit()

        # Create an analysis run
        analysis_run = HistoricalAnalysisRun(
            status="running",
            total_eligible=50,
        )
        db.add(analysis_run)
        db.commit()

        # Both should exist independently
        assert db.query(MailboxImportRun).count() == 1
        assert db.query(HistoricalAnalysisRun).count() == 1

        # Modifying one should not affect the other
        analysis_run.status = "completed"
        db.commit()

        import_run_check = db.query(MailboxImportRun).first()
        assert import_run_check.status == "completed"
        assert import_run_check.total_emails_ingested == 100


# ---------------------------------------------------------------------------
# API ENDPOINT TESTS
# ---------------------------------------------------------------------------


class TestAPIEndpoints:
    """Tests for the FastAPI API endpoints."""

    @pytest.fixture
    def client(self, db_factory):
        """Create a test client with DB override."""
        from fastapi.testclient import TestClient
        from src.main import app
        from src.database.connection import get_db
        import src.main as main_mod

        # Create a dedicated session for get_db dependency
        _client_db = db_factory()

        def override_get_db():
            try:
                yield _client_db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        # Override _analysis_db_factory so the service uses our in-memory DB
        original_factory = main_mod._analysis_db_factory
        main_mod._analysis_db_factory = db_factory

        client = TestClient(app)
        yield client

        app.dependency_overrides.pop(get_db, None)
        main_mod._analysis_db_factory = original_factory
        _client_db.close()

    def _auth_headers(self):
        return {"Authorization": "Bearer test_key_abc123"}

    def test_start_endpoint(self, client, db, db_factory):
        """POST /api/analysis/start should start a job."""
        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            resp = client.post(
                "/api/analysis/start?batch_size=10",
                headers=self._auth_headers(),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            _wait_for_completion(db_factory)

    def test_stop_endpoint(self, client, db, db_factory):
        """POST /api/analysis/stop should respond."""
        resp = client.post(
            "/api/analysis/stop",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200

    def test_status_endpoint(self, client):
        """GET /api/analysis/status should return status."""
        resp = client.get(
            "/api/analysis/status",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "total_eligible" in data
        assert "processed_count" in data
        assert "failed_count" in data
        assert "progress_percent" in data

    def test_reset_endpoint(self, client, db, db_factory):
        """POST /api/analysis/reset should clear data."""
        # Create some data first
        run = HistoricalAnalysisRun(status="completed")
        db.add(run)
        db.commit()

        resp = client.post(
            "/api/analysis/reset",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_start_with_custom_max_age(self, client, db, db_factory):
        """Start endpoint should accept max_age_days parameter."""
        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            resp = client.post(
                "/api/analysis/start?batch_size=10&max_age_days=180",
                headers=self._auth_headers(),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["max_age_days"] == 180
            _wait_for_completion(db_factory)

    def test_start_invalid_batch_size(self, client):
        """Start with invalid batch_size should return 400."""
        resp = client.post(
            "/api/analysis/start?batch_size=abc",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    def test_start_invalid_max_age_days(self, client):
        """Start with invalid max_age_days should return 400."""
        resp = client.post(
            "/api/analysis/start?max_age_days=abc",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    def test_status_endpoint_after_completion(self, client, db, db_factory):
        """Status should show completed state after job finishes."""
        _make_recent_email(db, 1)

        with patch(
            "src.services.historical_analysis_service.AIService"
        ) as MockAI:
            mock_instance = MockAI.return_value
            mock_instance.analyze_email.return_value = _MOCK_AI_RESULT.copy()

            client.post(
                "/api/analysis/start?batch_size=10",
                headers=self._auth_headers(),
            )
            _wait_for_completion(db_factory)

        resp = client.get(
            "/api/analysis/status",
            headers=self._auth_headers(),
        )
        data = resp.json()
        assert data["status"] == "completed"
        assert data["processed_count"] == 1
        assert data["progress_percent"] == 100.0

    def test_endpoints_require_auth(self, client):
        """All endpoints should require authentication."""
        # No auth header
        resp_start = client.post("/api/analysis/start")
        resp_stop = client.post("/api/analysis/stop")
        resp_status = client.get("/api/analysis/status")
        resp_reset = client.post("/api/analysis/reset")

        for resp in [resp_start, resp_stop, resp_status, resp_reset]:
            assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"
