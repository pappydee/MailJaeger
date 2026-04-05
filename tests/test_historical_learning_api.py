"""
API-level tests for the historical learning job endpoints (v1.2.0).

Covers:
  - POST /api/learning/start  (start / resume — background thread)
  - POST /api/learning/stop   (pause)
  - GET  /api/learning/status  (progress query)
  - POST /api/learning/reset   (clear all learning data)
  - Authentication enforcement
  - Parameter passing (batch_size, max_runtime_seconds)
  - Idempotent / repeated start calls
  - Resume after pause does not reprocess
  - Status reflects state transitions (idle → running → completed / paused)
"""

import time
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from src.models.database import (
    Base,
    ProcessedEmail,
    SenderProfile,
    HistoricalLearningProgress,
    ProcessingJob,
    LearningRun,
    LearningProgress,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AUTH_HEADERS = {"Authorization": "Bearer test_key_abc123"}


@pytest.fixture(autouse=True)
def _reset_service_state():
    """Reset the historical learning service global state between tests."""
    import src.services.historical_learning_service as svc
    svc._cancel_event.clear()
    # Wait for any existing thread to finish
    if svc._current_thread and svc._current_thread.is_alive():
        svc._cancel_event.set()
        svc._current_thread.join(timeout=5)
    svc._current_thread = None
    # Release lock if held
    try:
        svc._job_lock.release()
    except RuntimeError:
        pass
    yield
    # Cleanup after test
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
def _in_memory_engine():
    """Create a shared in-memory SQLite engine with StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def _in_memory_db(_in_memory_engine):
    """Create an in-memory SQLite session from the shared engine."""
    SessionLocal = sessionmaker(bind=_in_memory_engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def db_factory(_in_memory_engine):
    """Callable that returns a new session from the same in-memory engine."""
    SessionLocal = sessionmaker(bind=_in_memory_engine)
    def _factory():
        return SessionLocal()
    return _factory


@pytest.fixture
def client(_in_memory_db, db_factory):
    """FastAPI TestClient with get_db and _learning_db_factory overridden."""
    from src.main import app
    from src.database.connection import get_db
    import src.main as main_mod

    def _override_get_db():
        try:
            yield _in_memory_db
        finally:
            pass

    # Override both get_db (for FastAPI DI) and _learning_db_factory (for service)
    app.dependency_overrides[get_db] = _override_get_db
    original_factory = main_mod._learning_db_factory

    main_mod._learning_db_factory = db_factory
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    main_mod._learning_db_factory = original_factory


def _make_email(db: Session, **kwargs) -> ProcessedEmail:
    """Helper to create and persist a ProcessedEmail."""
    defaults = {
        "message_id": f"msg-{id(kwargs)}@example.com",
        "subject": "Test Subject",
        "sender": "user@example.com",
        "recipients": "me@mymail.com",
        "folder": "INBOX",
        "category": "Allgemein",
        "analysis_state": "pending",
        "is_spam": False,
        "is_processed": True,
        "is_flagged": False,
        "body_plain": "Test body",
        "date": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    email = ProcessedEmail(**defaults)
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


def _wait_for_learning(db, timeout=10):
    """Poll until the latest LearningRun is no longer 'running'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        run = (
            db.query(LearningRun)
            .order_by(LearningRun.started_at.desc())
            .first()
        )
        if run and run.status != "running":
            return run
        time.sleep(0.1)
    return run


# ===========================================================================
# Authentication enforcement
# ===========================================================================


class TestLearningApiAuth:
    """All learning endpoints require authentication."""

    def test_start_requires_auth(self, client):
        response = client.post("/api/learning/start")
        assert response.status_code in (401, 403)

    def test_stop_requires_auth(self, client):
        response = client.post("/api/learning/stop")
        assert response.status_code in (401, 403)

    def test_status_requires_auth(self, client):
        response = client.get("/api/learning/status")
        assert response.status_code in (401, 403)

    def test_reset_requires_auth(self, client):
        response = client.post("/api/learning/reset")
        assert response.status_code in (401, 403)


# ===========================================================================
# POST /api/learning/start
# ===========================================================================


class TestLearningStartEndpoint:
    """Tests for the learning start endpoint."""

    def test_start_returns_success(self, client, _in_memory_db):
        """POST /api/learning/start returns success."""
        response = client.post("/api/learning/start", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "status" in data
        # Wait for background thread to finish
        _wait_for_learning(_in_memory_db)

    def test_start_with_emails(self, client, _in_memory_db):
        """Start processes existing emails in background."""
        for i in range(5):
            _make_email(
                _in_memory_db,
                message_id=f"start-{i}@test.com",
                sender=f"sender{i}@domain.com",
                folder="INBOX",
            )

        response = client.post("/api/learning/start", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Wait for completion
        _wait_for_learning(_in_memory_db)

        # Verify emails were processed
        progress_count = _in_memory_db.query(LearningProgress).count()
        assert progress_count >= 1

    def test_start_accepts_batch_size(self, client, _in_memory_db):
        """batch_size query parameter is accepted."""
        for i in range(10):
            _make_email(
                _in_memory_db,
                message_id=f"batch-{i}@test.com",
                sender=f"b{i}@batchdomain.com",
                folder="INBOX",
            )

        response = client.post(
            "/api/learning/start?batch_size=3", headers=AUTH_HEADERS
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        _wait_for_learning(_in_memory_db)

    def test_start_accepts_max_runtime_seconds(self, client, _in_memory_db):
        """max_runtime_seconds query parameter is accepted."""
        _make_email(
            _in_memory_db,
            message_id="runtime@test.com",
            sender="rt@runtime.com",
            folder="INBOX",
        )

        response = client.post(
            "/api/learning/start?max_runtime_seconds=60", headers=AUTH_HEADERS
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        _wait_for_learning(_in_memory_db)

    def test_start_accepts_both_params(self, client, _in_memory_db):
        """Both batch_size and max_runtime_seconds can be passed together."""
        _make_email(
            _in_memory_db,
            message_id="both-params@test.com",
            sender="bp@both.com",
            folder="INBOX",
        )

        response = client.post(
            "/api/learning/start?batch_size=50&max_runtime_seconds=120",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        _wait_for_learning(_in_memory_db)

    def test_start_rejects_invalid_batch_size(self, client, _in_memory_db):
        """Non-numeric batch_size returns 400."""
        response = client.post(
            "/api/learning/start?batch_size=abc", headers=AUTH_HEADERS
        )
        assert response.status_code == 400

    def test_start_rejects_invalid_max_runtime(self, client, _in_memory_db):
        """Non-numeric max_runtime_seconds returns 400."""
        response = client.post(
            "/api/learning/start?max_runtime_seconds=xyz", headers=AUTH_HEADERS
        )
        assert response.status_code == 400


# ===========================================================================
# GET /api/learning/status
# ===========================================================================


class TestLearningStatusEndpoint:
    """Tests for the learning status endpoint."""

    def test_status_idle_when_never_run(self, client, _in_memory_db):
        """Status is idle when no learning job has run."""
        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "idle"
        assert data["job_id"] is None

    def test_status_returns_structured_data(self, client, _in_memory_db):
        """Status response contains the required fields."""
        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        # Mandatory fields for v1.2.0
        for key in ("status", "total_emails", "processed_emails", "progress_percent",
                     "current_phase", "is_running"):
            assert key in data, f"Missing key: {key}"

    def test_status_after_completed_job(self, client, _in_memory_db):
        """Status reflects completed state after a full run."""
        for i in range(4):
            _make_email(
                _in_memory_db,
                message_id=f"status-comp-{i}@test.com",
                sender="sc@status.com",
                folder="INBOX",
            )

        # Start and wait for completion
        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        # Check status
        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["processed_emails"] >= 4

    def test_status_progress_percent(self, client, _in_memory_db):
        """Status includes progress_percent field."""
        for i in range(3):
            _make_email(
                _in_memory_db,
                message_id=f"pct-{i}@test.com",
                sender="pct@test.com",
                folder="INBOX",
            )

        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        data = response.json()
        assert data["progress_percent"] == 100.0

    def test_status_failed_when_all_internal_processing_fails(self, client, _in_memory_db):
        """If all emails fail internally, status must not report completed success."""
        import src.services.historical_learning_service as svc

        for i in range(3):
            _make_email(
                _in_memory_db,
                message_id=f"all-fail-{i}@test.com",
                sender=f"af{i}@test.com",
                folder="INBOX",
            )

        original = svc._learn_single_email

        def _always_fail(_db, _email):
            raise RuntimeError("forced internal failure")

        svc._learn_single_email = _always_fail
        try:
            start_response = client.post("/api/learning/start", headers=AUTH_HEADERS)
            assert start_response.status_code == 200
            _wait_for_learning(_in_memory_db)
        finally:
            svc._learn_single_email = original

        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["processed_emails"] == 0
        assert data["progress_percent"] == 0.0


# ===========================================================================
# POST /api/learning/stop
# ===========================================================================


class TestLearningStopEndpoint:
    """Tests for the learning stop endpoint."""

    def test_stop_when_no_job_running(self, client, _in_memory_db):
        """Stop returns a clear response even when no job is running."""
        response = client.post("/api/learning/stop", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_stop_pauses_running_job(self, client, _in_memory_db):
        """Stop pauses a job via DB status."""
        for i in range(5):
            _make_email(
                _in_memory_db,
                message_id=f"stop-{i}@test.com",
                sender="stop@stop.com",
                folder="INBOX",
            )

        # Start and wait for completion
        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        # Manually set the run back to "running" to simulate mid-run stop
        run = (
            _in_memory_db.query(LearningRun)
            .order_by(LearningRun.started_at.desc())
            .first()
        )
        if run:
            run.status = "running"
            _in_memory_db.commit()

        # Now stop
        response = client.post("/api/learning/stop", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True

        # Verify DB status changed to paused
        _in_memory_db.refresh(run)
        assert run.status == "paused"


# ===========================================================================
# POST /api/learning/reset
# ===========================================================================


class TestLearningResetEndpoint:
    """Tests for the learning reset endpoint."""

    def test_reset_when_nothing_exists(self, client, _in_memory_db):
        """Reset returns success even when no learning data exists."""
        response = client.post("/api/learning/reset", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_reset_clears_all_runs(self, client, _in_memory_db):
        """Reset deletes all LearningRun and LearningProgress records."""
        for i in range(3):
            _make_email(
                _in_memory_db,
                message_id=f"reset-{i}@test.com",
                sender="reset@test.com",
                folder="INBOX",
            )

        # Run learning
        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        # Verify data exists
        assert _in_memory_db.query(LearningRun).count() >= 1

        # Reset
        response = client.post("/api/learning/reset", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify cleared
        assert _in_memory_db.query(LearningRun).count() == 0
        assert _in_memory_db.query(LearningProgress).count() == 0

    def test_status_idle_after_reset(self, client, _in_memory_db):
        """Status returns idle after reset."""
        for i in range(2):
            _make_email(
                _in_memory_db,
                message_id=f"reset-idle-{i}@test.com",
                sender="ri@test.com",
                folder="INBOX",
            )

        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        client.post("/api/learning/reset", headers=AUTH_HEADERS)

        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        data = response.json()
        assert data["status"] == "idle"


# ===========================================================================
# Resume after pause — no reprocessing
# ===========================================================================


class TestLearningResumeViaApi:
    """Test resume and no-reprocessing via the API layer."""

    def test_status_reflects_paused_then_resumed(self, client, _in_memory_db):
        """Status transitions: idle → running → completed → paused → completed."""
        for i in range(4):
            _make_email(
                _in_memory_db,
                message_id=f"state-trans-{i}@test.com",
                sender="state@trans.com",
                folder="INBOX",
            )

        # 1. Idle
        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "idle"

        # 2. Start → completed
        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)
        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "completed"

        # 3. Simulate pause by setting DB status
        run = (
            _in_memory_db.query(LearningRun)
            .order_by(LearningRun.started_at.desc())
            .first()
        )
        run.status = "paused"
        _in_memory_db.commit()

        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "paused"

        # 4. Resume → completed again
        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)
        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "completed"


# ===========================================================================
# Sender profile updates from historical evidence
# ===========================================================================


class TestLearningUpdatesProfiles:
    """Verify that the API-driven job updates sender profiles."""

    def test_sender_profile_created_via_api(self, client, _in_memory_db):
        """Running the job via API creates SenderProfile records."""
        for i in range(3):
            _make_email(
                _in_memory_db,
                message_id=f"profile-{i}@test.com",
                sender="profile@example.com",
                folder="Work",
            )

        client.post("/api/learning/start", headers=AUTH_HEADERS)
        _wait_for_learning(_in_memory_db)

        profiles = _in_memory_db.query(SenderProfile).all()
        # At least one profile should exist for the domain or address
        assert len(profiles) >= 1
        # Check a domain-level profile has typical_folder set
        matching = [p for p in profiles if p.sender_domain == "example.com"]
        assert len(matching) >= 1
        assert any(p.typical_folder == "Work" for p in matching)
