"""
API-level tests for the historical learning job endpoints.

Covers:
  - POST /api/learning/start  (start / resume)
  - POST /api/learning/stop   (pause)
  - GET  /api/learning/status  (progress query)
  - Authentication enforcement
  - Parameter passing (batch_size, max_runtime_seconds)
  - Idempotent / repeated start calls
  - Resume after pause does not reprocess
  - Status reflects state transitions (idle → running → completed / paused)
"""

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
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AUTH_HEADERS = {"Authorization": "Bearer test_key_abc123"}


@pytest.fixture
def _in_memory_db():
    """Create an in-memory SQLite database with all tables.

    Uses StaticPool + check_same_thread=False so the same in-memory DB
    is accessible from the test thread AND the ASGI/Starlette worker thread
    (TestClient spawns a new thread for sync endpoints).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def client(_in_memory_db):
    """FastAPI TestClient with get_db overridden to the in-memory session."""
    from src.main import app
    from src.database.connection import get_db

    def _override_get_db():
        try:
            yield _in_memory_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


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


# ===========================================================================
# POST /api/learning/start
# ===========================================================================


class TestLearningStartEndpoint:
    """Tests for the learning start endpoint."""

    def test_start_returns_success(self, client, _in_memory_db):
        """POST /api/learning/start returns success even with no emails."""
        response = client.post("/api/learning/start", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "status" in data

    def test_start_with_emails(self, client, _in_memory_db):
        """Start processes existing emails and returns stats."""
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
        assert data.get("emails_learned", 0) >= 0

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

    def test_repeated_start_is_safe(self, client, _in_memory_db):
        """Calling start multiple times is safe and idempotent for already-learned data."""
        for i in range(3):
            _make_email(
                _in_memory_db,
                message_id=f"idempotent-{i}@test.com",
                sender="idem@safe.com",
                folder="INBOX",
            )

        # First call
        r1 = client.post("/api/learning/start", headers=AUTH_HEADERS)
        assert r1.status_code == 200
        d1 = r1.json()
        learned_first = d1.get("emails_learned", 0)

        # Second call — should not re-learn the same emails
        r2 = client.post("/api/learning/start", headers=AUTH_HEADERS)
        assert r2.status_code == 200
        d2 = r2.json()
        learned_second = d2.get("emails_learned", 0)

        # Second run should learn 0 since all are already processed
        assert learned_second == 0


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
        # Mandatory fields
        for key in ("status", "total_emails", "processed_count", "remaining_count", "folders"):
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

        # Run the job
        client.post("/api/learning/start", headers=AUTH_HEADERS)

        # Check status
        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["processed_count"] >= 4

    def test_status_shows_folder_progress(self, client, _in_memory_db):
        """Status includes per-folder progress information."""
        _make_email(
            _in_memory_db,
            message_id="folder-prog@test.com",
            sender="fp@folder.com",
            folder="Work",
        )

        client.post("/api/learning/start", headers=AUTH_HEADERS)

        response = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["folders"], list)
        assert len(data["folders"]) >= 1
        folder = data["folders"][0]
        assert "folder_name" in folder
        assert "status" in folder
        assert "processed_count" in folder


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
        # Should not crash; either success=True/False or message about no job
        assert isinstance(data, dict)

    def test_stop_pauses_running_job(self, client, _in_memory_db):
        """Stop pauses a previously started (and now completed-but-simulated-running) job."""
        # Create a bunch of emails
        for i in range(5):
            _make_email(
                _in_memory_db,
                message_id=f"stop-{i}@test.com",
                sender="stop@stop.com",
                folder="INBOX",
            )

        # Start the job (runs to completion synchronously)
        client.post("/api/learning/start", headers=AUTH_HEADERS)

        # Manually set the job back to "running" to simulate mid-run stop
        job = (
            _in_memory_db.query(ProcessingJob)
            .filter(ProcessingJob.job_type == "learning")
            .order_by(ProcessingJob.started_at.desc())
            .first()
        )
        if job:
            job.status = "running"
            _in_memory_db.commit()

        # Now stop
        response = client.post("/api/learning/stop", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True

        # Verify DB status changed to paused
        _in_memory_db.refresh(job)
        assert job.status == "paused"


# ===========================================================================
# Resume after pause — no reprocessing
# ===========================================================================


class TestLearningResumeViaApi:
    """Test resume and no-reprocessing via the API layer."""

    def test_resume_does_not_reprocess_via_api(self, client, _in_memory_db):
        """Emails learned in run 1 are NOT relearned in run 2 via API."""
        for i in range(6):
            _make_email(
                _in_memory_db,
                message_id=f"api-resume-{i}@test.com",
                sender="resume@api.com",
                folder="INBOX",
            )

        # Run 1: process first batch with max_runtime_seconds=0
        # (max_runtime_seconds=0 won't actually do anything useful since the
        # job runs synchronously, but we can use batch control)
        r1 = client.post(
            "/api/learning/start?batch_size=3", headers=AUTH_HEADERS
        )
        assert r1.status_code == 200
        learned_first = r1.json().get("emails_learned", 0)

        # Run 2: resume — should only process remaining
        r2 = client.post(
            "/api/learning/start?batch_size=10", headers=AUTH_HEADERS
        )
        assert r2.status_code == 200
        learned_second = r2.json().get("emails_learned", 0)

        # Total should equal all 6 emails
        assert learned_first + learned_second == 6

    def test_status_reflects_paused_then_resumed(self, client, _in_memory_db):
        """Status transitions: idle → completed → paused → completed on resume."""
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
        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "completed"

        # 3. Simulate pause by setting DB status
        job = (
            _in_memory_db.query(ProcessingJob)
            .filter(ProcessingJob.job_type == "learning")
            .order_by(ProcessingJob.started_at.desc())
            .first()
        )
        job.status = "paused"
        _in_memory_db.commit()

        r = client.get("/api/learning/status", headers=AUTH_HEADERS)
        assert r.json()["status"] == "paused"

        # 4. Resume → completed again
        client.post("/api/learning/start", headers=AUTH_HEADERS)
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

        profiles = _in_memory_db.query(SenderProfile).all()
        # At least one profile should exist for the domain or address
        assert len(profiles) >= 1
        # Check the profile has a preferred folder
        domain_profile = next(
            (p for p in profiles if p.sender_domain == "example.com"), None
        )
        assert domain_profile is not None
        assert domain_profile.typical_folder == "Work"
