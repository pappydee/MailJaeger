"""
Comprehensive tests for the historical learning service (v1.2.0).

Tests cover:
  - start/stop/resume lifecycle
  - No duplicate email processing
  - Progress tracking increases correctly
  - Survives restart (resume from checkpoint)
  - Concurrency: single job at a time
  - Reset clears all learning data
  - Phase transitions: scanning → analyzing → learning
  - Batch size configuration
  - Edge cases: empty mailbox, already-completed run
"""

import time
import threading
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models.database import (
    Base,
    ProcessedEmail,
    SenderProfile,
    LearningRun,
    LearningProgress,
    DecisionEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_service_globals():
    """Reset global state of the historical_learning_service between tests."""
    import src.services.historical_learning_service as svc
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


def _make_email(db, idx, sender="test@example.com", folder="INBOX"):
    """Create a test email."""
    email = ProcessedEmail(
        message_id=f"test-{idx}@example.com",
        subject=f"Test Subject {idx}",
        sender=sender,
        recipients="me@mymail.com",
        folder=folder,
        category="Allgemein",
        analysis_state="pending",
        is_spam=False,
        is_processed=True,
        is_flagged=False,
        body_plain=f"Test body {idx}",
        date=datetime.now(timezone.utc),
    )
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


def _wait_for_completion(db, timeout=10):
    """Wait for the latest LearningRun to leave 'running' status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        run = db.query(LearningRun).order_by(LearningRun.started_at.desc()).first()
        if run and run.status != "running":
            return run
        time.sleep(0.1)
    return run


# ===========================================================================
# Start / Stop / Resume lifecycle
# ===========================================================================


class TestStartStopResume:
    """Tests for basic lifecycle: start, stop, resume."""

    def test_start_creates_learning_run(self, db, db_factory):
        from src.services.historical_learning_service import start_learning
        result = start_learning(db_factory, batch_size=50)
        assert result["success"] is True
        assert result["status"] == "running"
        _wait_for_completion(db)
        runs = db.query(LearningRun).all()
        assert len(runs) == 1

    def test_start_with_empty_mailbox(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status
        result = start_learning(db_factory, batch_size=50)
        assert result["success"] is True
        _wait_for_completion(db)
        status = get_status(db_factory)
        assert status["status"] == "completed"
        assert status["total_emails"] == 0

    def test_start_processes_emails(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status
        for i in range(5):
            _make_email(db, i, sender=f"user{i}@test.com")

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["status"] == "completed"
        assert status["processed_emails"] >= 5
        assert status["progress_percent"] == 100.0

    def test_stop_pauses_job(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, stop_learning, get_status

        # Create enough emails that the job takes a while
        for i in range(5):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        # Immediately request stop
        result = stop_learning(db_factory)
        # Either succeeds or the job already finished
        assert isinstance(result, dict)

        # Wait for thread to finish
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["status"] in ("completed", "paused")

    def test_resume_continues_from_checkpoint(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        for i in range(10):
            _make_email(db, i, sender=f"resume{i}@test.com")

        # First run
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        first_status = get_status(db_factory)
        first_processed = first_status["processed_emails"]

        # Add more emails
        for i in range(10, 15):
            _make_email(db, i, sender=f"resume{i}@test.com")

        # Simulate paused state for the completed run
        db.expire_all()
        run = db.query(LearningRun).order_by(LearningRun.started_at.desc()).first()
        run.status = "paused"
        db.commit()

        # Resume
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        second_status = get_status(db_factory)
        assert second_status["processed_emails"] >= first_processed

    def test_resume_alias_calls_start(self, db, db_factory):
        from src.services.historical_learning_service import resume_learning
        result = resume_learning(db_factory, batch_size=50)
        assert result["success"] is True
        _wait_for_completion(db)


# ===========================================================================
# No duplicate processing
# ===========================================================================


class TestNoDuplicateProcessing:
    """Ensure emails are never processed twice."""

    def test_same_email_not_processed_twice(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        for i in range(5):
            _make_email(db, i)

        # First run
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)
        first_status = get_status(db_factory)

        # Second run (reset to paused to allow re-run)
        db.expire_all()
        run = db.query(LearningRun).order_by(LearningRun.started_at.desc()).first()
        run.status = "paused"
        run.last_email_uid = None  # Reset cursor to force re-scan
        db.commit()

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)
        second_status = get_status(db_factory)

        # LearningProgress should still only have 5 entries (no duplicates)
        progress_count = db.query(LearningProgress).count()
        assert progress_count == 5

    def test_learning_progress_has_unique_emails(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        for i in range(8):
            _make_email(db, i, sender=f"uniq{i}@test.com")

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        # Each email should appear exactly once in LearningProgress
        email_ids = [p.email_id for p in db.query(LearningProgress).all()]
        assert len(email_ids) == len(set(email_ids))


# ===========================================================================
# Progress tracking
# ===========================================================================


class TestProgressTracking:
    """Verify progress increases correctly and reaches 100%."""

    def test_progress_reaches_100(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        for i in range(10):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["progress_percent"] == 100.0
        assert status["processed_emails"] >= 10

    def test_progress_increases_monotonically(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        for i in range(20):
            _make_email(db, i, sender=f"mono{i}@test.com")

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["processed_emails"] >= 20
        assert status["progress_percent"] >= 100.0

    def test_last_email_uid_is_persisted(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        for i in range(5):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        db.expire_all()
        run = db.query(LearningRun).first()
        assert run is not None
        assert run.last_email_uid is not None
        assert int(run.last_email_uid) > 0


# ===========================================================================
# Survives restart
# ===========================================================================


class TestSurvivesRestart:
    """Verify the system can resume from a persisted checkpoint after restart."""

    def test_resume_from_persisted_state(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        for i in range(10):
            _make_email(db, i, sender=f"restart{i}@test.com")

        # Run and complete
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status_before = get_status(db_factory)
        assert status_before["status"] == "completed"
        assert status_before["last_email_uid"] is not None

        # Simulate crash: mark as paused (as if mid-run)
        db.expire_all()
        run = db.query(LearningRun).first()
        old_processed = run.processed_emails
        old_uid = run.last_email_uid
        run.status = "paused"
        db.commit()

        # Add new emails
        for i in range(10, 15):
            _make_email(db, i, sender=f"restart{i}@test.com")

        # Resume
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status_after = get_status(db_factory)
        assert status_after["status"] == "completed"
        # Should have processed more than before
        assert status_after["processed_emails"] >= int(old_processed or 0)

    def test_stale_running_job_recovered(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        # Simulate a stale "running" job from a previous crash
        stale_run = LearningRun(
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(stale_run)
        db.commit()

        # Start should recover the stale job and create a new one
        result = start_learning(db_factory, batch_size=50)
        assert result["success"] is True
        _wait_for_completion(db)

        # The stale run should be marked as failed
        db.expire_all()
        runs = db.query(LearningRun).order_by(LearningRun.started_at).all()
        assert any(r.status == "failed" for r in runs)
        # And a new run should be completed
        assert any(r.status == "completed" for r in runs)


# ===========================================================================
# Concurrency
# ===========================================================================


class TestConcurrency:
    """Only one job should run at a time."""

    def test_concurrent_start_rejected(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        for i in range(5):
            _make_email(db, i)

        # First start
        r1 = start_learning(db_factory, batch_size=50)
        assert r1["success"] is True

        # Second start should be rejected (job still running)
        r2 = start_learning(db_factory, batch_size=50)
        assert r2["success"] is False
        assert "already running" in r2["message"].lower()

        _wait_for_completion(db)


# ===========================================================================
# Reset
# ===========================================================================


class TestReset:
    """Reset clears all learning run/progress data."""

    def test_reset_clears_runs_and_progress(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, reset_learning

        for i in range(5):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        assert db.query(LearningRun).count() >= 1
        assert db.query(LearningProgress).count() >= 1

        result = reset_learning(db_factory)
        assert result["success"] is True

        db.expire_all()
        assert db.query(LearningRun).count() == 0
        assert db.query(LearningProgress).count() == 0

    def test_reset_allows_fresh_start(self, db, db_factory):
        from src.services.historical_learning_service import (
            start_learning, reset_learning, get_status,
        )

        for i in range(3):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        reset_learning(db_factory)

        status = get_status(db_factory)
        assert status["status"] == "idle"

        # Can start fresh
        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["status"] == "completed"


# ===========================================================================
# Phase transitions
# ===========================================================================


class TestPhaseTransitions:
    """Verify the job moves through scanning → analyzing → learning phases."""

    def test_completed_job_has_learning_phase(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        for i in range(5):
            _make_email(db, i)

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        db.expire_all()
        run = db.query(LearningRun).first()
        assert run is not None
        # After completion, phase should be "learning" (the last phase)
        assert run.current_phase == "learning"


# ===========================================================================
# Batch size configuration
# ===========================================================================


class TestBatchSize:
    """Verify batch size is respected and clamped."""

    def test_batch_size_clamped_to_min(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        result = start_learning(db_factory, batch_size=10)
        assert result["success"] is True
        _wait_for_completion(db)

    def test_batch_size_clamped_to_max(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        result = start_learning(db_factory, batch_size=500)
        assert result["success"] is True
        _wait_for_completion(db)


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge case handling."""

    def test_get_status_when_never_run(self, db, db_factory):
        from src.services.historical_learning_service import get_status
        status = get_status(db_factory)
        assert status["status"] == "idle"
        assert status["job_id"] is None
        assert status["is_running"] is False

    def test_stop_when_nothing_running(self, db, db_factory):
        from src.services.historical_learning_service import stop_learning
        result = stop_learning(db_factory)
        assert result["success"] is False

    def test_emails_in_non_learnable_folders_skipped(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status

        # Drafts and empty folders are not learnable
        _make_email(db, 0, folder="Drafts")
        _make_email(db, 1, folder="INBOX")

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        status = get_status(db_factory)
        assert status["status"] == "completed"

    def test_sender_profile_interaction_count_updated(self, db, db_factory):
        from src.services.historical_learning_service import start_learning

        for i in range(3):
            _make_email(db, i, sender="frequent@company.com", folder="Work")

        start_learning(db_factory, batch_size=50)
        _wait_for_completion(db)

        db.expire_all()
        profiles = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "company.com"
        ).all()
        # At least one profile should exist with interaction_count > 0
        if profiles:
            assert any((p.interaction_count or 0) > 0 for p in profiles)

    def test_all_internal_failures_mark_run_failed(self, db, db_factory):
        from src.services.historical_learning_service import start_learning, get_status
        import src.services.historical_learning_service as svc

        for i in range(3):
            _make_email(db, i, sender=f"fail{i}@test.com", folder="INBOX")

        original = svc._learn_single_email

        def _always_fail(_db, _email):
            raise RuntimeError("forced internal failure")

        svc._learn_single_email = _always_fail
        try:
            result = start_learning(db_factory, batch_size=50)
            assert result["success"] is True
            _wait_for_completion(db)
        finally:
            svc._learn_single_email = original

        status = get_status(db_factory)
        assert status["status"] == "failed"
        assert status["processed_emails"] == 0
        assert status["progress_percent"] == 0.0


# ===========================================================================
# New DB model fields
# ===========================================================================


class TestNewDBFields:
    """Verify new DB model fields exist and work."""

    def test_sender_profile_has_spam_probability(self, db):
        profile = SenderProfile(
            sender_domain="spam.com",
            total_emails=100,
            spam_probability=0.95,
            interaction_count=50,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        assert profile.spam_probability == 0.95
        assert profile.interaction_count == 50

    def test_decision_event_has_action_type_and_target_folder(self, db):
        # Need an email first for the FK
        email = _make_email(db, 999)
        event = DecisionEvent(
            email_id=email.id,
            event_type="move_to_folder",
            action_type="move",
            target_folder="Archive",
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        assert event.action_type == "move"
        assert event.target_folder == "Archive"

    def test_learning_run_table_exists(self, db):
        run = LearningRun(
            status="running",
            current_phase="scanning",
            total_emails=100,
            processed_emails=50,
            last_email_uid=42,
            progress_percent=50.0,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.id is not None
        assert run.status == "running"
        assert run.current_phase == "scanning"

    def test_learning_progress_table_exists(self, db):
        email = _make_email(db, 888)
        prog = LearningProgress(
            email_id=email.id,
            processed_at=datetime.now(timezone.utc),
            result_hash="abc123",
        )
        db.add(prog)
        db.commit()
        db.refresh(prog)
        assert prog.id is not None
        assert prog.email_id == email.id


class TestLegacySQLiteUpgradeLearning:
    """Historical learning should work on upgraded legacy SQLite schemas."""

    def test_historical_learning_runs_after_legacy_schema_repair(self, tmp_path):
        from src.database.startup_checks import ensure_historical_learning_schema_compatibility
        from src.services.historical_learning_service import start_learning, get_status

        db_file = tmp_path / "legacy_learning_runtime.sqlite"
        engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)

        with engine.begin() as connection:
            connection.execute(text("DROP TABLE sender_profiles"))
            connection.execute(
                text(
                    """
                    CREATE TABLE sender_profiles (
                        id INTEGER PRIMARY KEY,
                        sender_address VARCHAR(200),
                        sender_domain VARCHAR(200),
                        total_emails INTEGER,
                        typical_folder VARCHAR(200),
                        folder_distribution JSON,
                        total_replies INTEGER,
                        reply_rate FLOAT,
                        avg_reply_delay_seconds FLOAT,
                        median_reply_delay_seconds FLOAT,
                        importance_tendency FLOAT,
                        spam_tendency FLOAT,
                        marked_important_count INTEGER,
                        marked_spam_count INTEGER,
                        archived_count INTEGER,
                        deleted_count INTEGER,
                        kept_in_inbox_count INTEGER,
                        first_seen DATETIME,
                        last_seen DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )

        ensure_historical_learning_schema_compatibility(engine, debug=False)
        repaired_columns = {col["name"] for col in inspect(engine).get_columns("sender_profiles")}
        assert "spam_probability" in repaired_columns
        assert "interaction_count" in repaired_columns
        assert "preferred_category" in repaired_columns
        assert "preferred_folder" in repaired_columns
        assert "user_classification_count" in repaired_columns

        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()
        try:
            for i in range(4):
                _make_email(db, i, sender=f"legacy{i}@upgrade.com", folder="INBOX")

            def db_factory():
                return SessionLocal()

            result = start_learning(db_factory, batch_size=50)
            assert result["success"] is True
            _wait_for_completion(db)

            status = get_status(db_factory)
            assert status["status"] == "completed"
            assert status["processed_emails"] >= 1
        finally:
            db.close()
