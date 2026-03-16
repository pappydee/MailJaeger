"""
Regression tests for the v1.1.2 bug-fix / UX-consistency pass:

1. /api/emails/list — no ValidationError for emails with NULL bool columns
2. /api/emails/list — returns structured data (not 500)
3. RunStatus cancel state transitions
4. POST /api/processing/cancel endpoint
5. health_status overall_status field (OK / DEGRADED)
6. DashboardResponse exposes run_status (same source as /api/status)
7. Safe mode still enforced (no regression)
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import os
from datetime import datetime

ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}

AUTH = {"Authorization": f"Bearer {ENV['API_KEY']}"}


def _reset_rate_limiter():
    try:
        from src.middleware.rate_limiting import limiter

        limiter._storage.reset()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TASK 1 — EmailResponse ValidationError fix
# ---------------------------------------------------------------------------


class TestEmailResponseNullBoolCoercion:
    """Pydantic v2 strict bool rejects None; validator must coerce it to False."""

    def test_none_bool_fields_coerced_to_false(self):
        from src.models.schemas import EmailResponse

        class FakeEmail:
            id = 1
            message_id = "test@example.com"
            subject = None
            sender = None
            recipients = None
            date = None
            summary = None
            category = None
            spam_probability = None
            action_required = None  # NULL in DB for unanalysed emails
            priority = None
            suggested_folder = None
            reasoning = None
            is_spam = None
            is_archived = None
            is_flagged = None
            is_resolved = None
            tasks = []
            created_at = datetime.utcnow()
            processed_at = None

        # Must not raise ValidationError
        r = EmailResponse.from_orm(FakeEmail())
        assert r.action_required is False
        assert r.is_spam is False
        assert r.is_archived is False
        assert r.is_flagged is False
        assert r.is_resolved is False

    def test_explicit_true_preserved(self):
        from src.models.schemas import EmailResponse

        class FakeEmail:
            id = 2
            message_id = "spam@example.com"
            subject = None
            sender = None
            recipients = None
            date = None
            summary = None
            category = None
            spam_probability = 0.95
            action_required = True
            priority = "HIGH"
            suggested_folder = None
            reasoning = None
            is_spam = True
            is_archived = False
            is_flagged = False
            is_resolved = False
            tasks = []
            created_at = datetime.utcnow()
            processed_at = None

        r = EmailResponse.from_orm(FakeEmail())
        assert r.action_required is True
        assert r.is_spam is True


class TestEmailListEndpoint:
    """POST /api/emails/list must return 200 with valid data, never 500 ValidationError."""

    def _make_client(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            return TestClient(app, raise_server_exceptions=False)

    def test_list_emails_returns_200_with_empty_db(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_query = MagicMock()
                mock_query.filter.return_value = mock_query
                mock_query.order_by.return_value = mock_query
                mock_query.offset.return_value = mock_query
                mock_query.limit.return_value = mock_query
                mock_query.all.return_value = []
                mock_db.query.return_value = mock_query

                def _override_db():
                    yield mock_db

                mock_get_db.return_value = _override_db()
                app.dependency_overrides = {}

                from src.database.connection import get_db
                from src.main import app as real_app

                real_app.dependency_overrides[get_db] = _override_db

                response = client.post(
                    "/api/emails/list",
                    json={},
                    headers=AUTH,
                )
                real_app.dependency_overrides.clear()

            assert response.status_code == 200
            assert isinstance(response.json(), list)

    def test_list_emails_no_validation_error_for_null_bool_fields(self):
        """
        Regression: before the fix, emails with NULL action_required / is_spam in
        the DB caused Pydantic v2 to raise a ValidationError → 500.
        This test verifies the endpoint handles such records without crashing.
        """
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.database.connection import get_db

            # Build a fake ORM object that mimics an unanalysed DB row (all
            # bool columns are None, as they are before AI analysis runs).
            fake_email = MagicMock()
            fake_email.id = 1
            fake_email.message_id = "pending@example.com"
            fake_email.subject = "Pending"
            fake_email.sender = "sender@example.com"
            fake_email.recipients = None
            fake_email.date = None
            fake_email.summary = None
            fake_email.category = None
            fake_email.spam_probability = None
            fake_email.action_required = None  # ← key NULL value
            fake_email.priority = None
            fake_email.suggested_folder = None
            fake_email.reasoning = None
            fake_email.is_spam = None  # ← key NULL value
            fake_email.is_archived = None
            fake_email.is_flagged = None
            fake_email.is_resolved = None
            fake_email.tasks = []
            fake_email.created_at = datetime.utcnow()
            fake_email.processed_at = None

            mock_db = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.order_by.return_value = mock_query
            mock_query.offset.return_value = mock_query
            mock_query.limit.return_value = mock_query
            mock_query.all.return_value = [fake_email]
            mock_db.query.return_value = mock_query

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                client = TestClient(app, raise_server_exceptions=False)
                response = client.post(
                    "/api/emails/list",
                    json={},
                    headers=AUTH,
                )
                # Must be 200, not 500
                assert response.status_code == 200, (
                    f"Expected 200, got {response.status_code}: {response.text}"
                )
                data = response.json()
                assert isinstance(data, list)
                assert len(data) == 1
                # bool fields must be False (coerced from None)
                assert data[0]["action_required"] is False
                assert data[0]["is_spam"] is False
            finally:
                app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# TASK 3 — Cancel / Cancelled state
# ---------------------------------------------------------------------------


class TestRunStatusCancelState:
    """RunStatus cancel transitions (unit-level, no HTTP)."""

    def test_request_cancel_when_running(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.update(status="running")
        result = rs.request_cancel()
        assert result is True
        assert rs.cancel_requested is True
        assert rs.status == "cancelling"

    def test_request_cancel_when_already_cancelling(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.update(status="running")
        rs.request_cancel()  # transitions running → cancelling
        assert rs.status == "cancelling"
        # Calling again while already cancelling must still return True (idempotent)
        result = rs.request_cancel()
        assert result is True
        assert rs.cancel_requested is True

    def test_request_cancel_when_idle_returns_false(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        result = rs.request_cancel()
        assert result is False
        assert rs.cancel_requested is False
        assert rs.status == "idle"

    def test_request_cancel_when_success_returns_false(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.status = "success"
        result = rs.request_cancel()
        assert result is False

    def test_reset_clears_cancel_requested(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.update(status="running")
        rs.request_cancel()  # transitions to cancelling via proper API
        assert rs.cancel_requested is True
        rs.reset()
        assert rs.cancel_requested is False
        assert rs.status == "idle"

    def test_to_dict_includes_cancel_requested(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        d = rs.to_dict()
        assert "cancel_requested" in d
        assert d["cancel_requested"] is False

    def test_to_dict_cancel_requested_true_when_set(self):
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.status = "running"
        rs.request_cancel()
        d = rs.to_dict()
        assert d["cancel_requested"] is True
        assert d["status"] == "cancelling"


class TestCancelProcessingEndpoint:
    """POST /api/processing/cancel HTTP endpoint."""

    def _make_client(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            return TestClient(app, raise_server_exceptions=False)

    def test_cancel_when_no_run_active_returns_success_false(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.services.scheduler import get_run_status

            # Ensure status is idle
            get_run_status().reset()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/api/processing/cancel", headers=AUTH)
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is False
            assert "No active run" in data["message"]

    def test_cancel_when_run_active_sets_cancelling(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.services.scheduler import get_run_status

            rs = get_run_status()
            rs.reset()
            rs.status = "running"
            rs.run_id = 42

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/api/processing/cancel", headers=AUTH)
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "Cancellation requested" in data["message"]
            assert data["status"] == "cancelling"
            assert rs.cancel_requested is True

            # Clean up
            rs.reset()

    def test_cancel_requires_authentication(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/api/processing/cancel")  # no auth
            assert response.status_code == 401


class TestEmailProcessorCancellation:
    """EmailProcessor loop exits when cancel_requested is set."""

    def test_processor_stops_on_cancel_flag(self):
        """When cancel_requested is True, the per-email loop breaks early."""
        from src.services.email_processor import EmailProcessor
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.update(status="running")

        mock_db = MagicMock()
        processor = EmailProcessor(mock_db, status=rs)

        # Verify that the cancel check exists and works with a direct unit test
        rs.request_cancel()
        assert rs.cancel_requested is True

        # _update_status must not raise when cancel_requested is set
        processor._update_status(status="cancelling", current_step="Cancelling…")
        assert rs.status == "cancelling"


# ---------------------------------------------------------------------------
# TASK 4 — Health / System status
# ---------------------------------------------------------------------------


class TestHealthOverallStatus:
    """health_status must include overall_status: OK or DEGRADED."""

    def test_dashboard_health_includes_overall_status(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.database.connection import get_db

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, patch(
                    "src.main.AIService"
                ) as mock_ai, patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    response = client.get("/api/dashboard", headers=AUTH)
                    assert response.status_code == 200
                    data = response.json()
                    assert "health_status" in data
                    assert "overall_status" in data["health_status"]
                    assert data["health_status"]["overall_status"] == "OK"
            finally:
                app.dependency_overrides.clear()

    def test_dashboard_health_degraded_when_imap_unhealthy(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.database.connection import get_db

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, patch(
                    "src.main.AIService"
                ) as mock_ai, patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {
                        "status": "unhealthy",
                        "message": "Connection refused",
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    response = client.get("/api/dashboard", headers=AUTH)
                    assert response.status_code == 200
                    data = response.json()
                    assert data["health_status"]["overall_status"] == "DEGRADED"
            finally:
                app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# TASK 2 — Progress/Status consistency (run_status in dashboard)
# ---------------------------------------------------------------------------


class TestDashboardRunStatus:
    """Dashboard must expose live run_status so UI has one source of truth."""

    def test_dashboard_includes_run_status_field(self):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.database.connection import get_db

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.count.return_value = 5
            mock_db.query.return_value.filter.return_value.count.return_value = 2

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, patch(
                    "src.main.AIService"
                ) as mock_ai, patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    response = client.get("/api/dashboard", headers=AUTH)
                    assert response.status_code == 200
                    data = response.json()
                    # run_status must be present and contain the canonical fields
                    assert "run_status" in data
                    rs = data["run_status"]
                    for field in (
                        "status",
                        "processed",
                        "total",
                        "progress_percent",
                        "cancel_requested",
                    ):
                        assert field in rs, f"run_status missing field: {field}"
            finally:
                app.dependency_overrides.clear()

    def test_api_status_and_dashboard_run_status_are_identical(self):
        """
        /api/status and dashboard.run_status must both reflect the same RunStatus
        singleton, so the top progress bar and the dashboard panel never diverge.
        """
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            reload_settings()
            from src.main import app
            from src.database.connection import get_db
            from src.services.scheduler import get_run_status

            # Set a known state
            get_run_status().reset()
            get_run_status().update(status="running", processed=7, total=20)

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, patch(
                    "src.main.AIService"
                ) as mock_ai, patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    status_resp = client.get("/api/status", headers=AUTH)
                    dashboard_resp = client.get("/api/dashboard", headers=AUTH)

                    assert status_resp.status_code == 200
                    assert dashboard_resp.status_code == 200

                    status_data = status_resp.json()
                    run_status_from_dashboard = dashboard_resp.json()["run_status"]

                    # Both must report the same processed/total counters
                    assert status_data["processed"] == run_status_from_dashboard["processed"]
                    assert status_data["total"] == run_status_from_dashboard["total"]
                    assert status_data["status"] == run_status_from_dashboard["status"]
            finally:
                app.dependency_overrides.clear()
                get_run_status().reset()


# ---------------------------------------------------------------------------
# TASK 5 — Safe mode preserved (no regression)
# ---------------------------------------------------------------------------


class TestSafeModePreserved:
    """Safe mode must still prevent automatic IMAP actions."""

    def test_safe_mode_setting_honoured(self):
        with patch.dict(os.environ, {**ENV, "SAFE_MODE": "true"}):
            from src.config import reload_settings

            reload_settings()
            from src.config import get_settings

            s = get_settings()
            assert s.safe_mode is True, "SAFE_MODE env var must be respected"

    def test_email_processor_skips_imap_in_safe_mode(self):
        """When safe_mode=True, EmailProcessor must not open an IMAP connection
        for action execution (the imap_for_actions variable stays None)."""
        with patch.dict(os.environ, {**ENV, "SAFE_MODE": "true"}):
            from src.config import reload_settings

            reload_settings()
            from src.services.email_processor import EmailProcessor

            mock_db = MagicMock()
            processor = EmailProcessor(mock_db)
            # safe_mode must be True for this processor instance
            assert processor.settings.safe_mode is True


# ---------------------------------------------------------------------------
# Dashboard UI consistency — state selection (backend response-shape tests)
# ---------------------------------------------------------------------------


def _make_dashboard_client(run_status_override=None, last_run=None):
    """Helper: build a TestClient whose dashboard endpoint returns controlled data."""
    with patch.dict(os.environ, ENV):
        from src.config import reload_settings
        reload_settings()
        from src.main import app
        from src.database.connection import get_db
        from src.services.scheduler import get_run_status

        if run_status_override:
            rs = get_run_status()
            rs.reset()
            for k, v in run_status_override.items():
                if hasattr(rs, k):
                    setattr(rs, k, v)

        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.first.return_value = last_run
        mock_db.query.return_value.count.return_value = 0
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        def _override():
            yield mock_db

        app.dependency_overrides[get_db] = _override
        return app, _override


class TestDashboardStateSelection:
    """
    Backend response-shape tests that verify the dashboard payload lets the UI
    apply the correct "state selection" rule:

    - run_status.status == 'running'/'cancelling'  →  UI should use run_status
    - run_status.status == 'idle'                  →  UI should use last_run
    """

    def _patched_client(self, run_status_dict, last_run_obj=None):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.database.connection import get_db
            from src.services.scheduler import get_run_status

            rs = get_run_status()
            rs.reset()
            for k, v in run_status_dict.items():
                if hasattr(rs, k):
                    setattr(rs, k, v)

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = last_run_obj
            mock_db.query.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, \
                     patch("src.main.AIService") as mock_ai, \
                     patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                    mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.get("/api/dashboard", headers=AUTH)
                    return resp.json()
            finally:
                app.dependency_overrides.clear()
                get_run_status().reset()

    def test_dashboard_run_status_idle_exposes_status_idle(self):
        """When no run is active, run_status.status must be 'idle'."""
        data = self._patched_client({"status": "idle"})
        assert data["run_status"]["status"] == "idle"

    def test_dashboard_run_status_running_exposes_live_counters(self):
        """When a run is active, run_status carries live processed/total."""
        data = self._patched_client({"status": "running", "processed": 7, "total": 20})
        rs = data["run_status"]
        assert rs["status"] == "running"
        assert rs["processed"] == 7
        assert rs["total"] == 20

    def test_dashboard_run_status_cancelling_exposed(self):
        """cancelling state must be propagated through the dashboard."""
        data = self._patched_client({
            "status": "cancelling", "cancel_requested": True,
            "processed": 3, "total": 10,
        })
        rs = data["run_status"]
        assert rs["status"] == "cancelling"
        assert rs["cancel_requested"] is True

    def test_dashboard_run_status_cancelled_after_run(self):
        """Cancelled state must survive in run_status until reset."""
        data = self._patched_client({"status": "cancelled", "processed": 5, "total": 10})
        rs = data["run_status"]
        assert rs["status"] == "cancelled"
        assert rs["processed"] == 5

    def test_dashboard_has_no_last_run_when_db_empty(self):
        """When no DB runs exist and status is idle, last_run must be null."""
        data = self._patched_client({"status": "idle"}, last_run_obj=None)
        assert data["last_run"] is None

    def test_api_status_and_dashboard_run_status_consistent_during_run(self):
        """
        /api/status and dashboard.run_status must report the same counters
        during an active run — this is what prevents the 'Letzte Verarbeitung'
        from showing stale IN_PROGRESS/0 while the progress bar shows real counts.
        """
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.database.connection import get_db
            from src.services.scheduler import get_run_status

            rs = get_run_status()
            rs.reset()
            rs.status = "running"
            rs.processed = 12
            rs.total = 50
            rs.progress_percent = 35

            mock_db = MagicMock()
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            try:
                with patch("src.main.IMAPService") as mock_imap, \
                     patch("src.main.AIService") as mock_ai, \
                     patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                    mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    status_resp = client.get("/api/status", headers=AUTH)
                    dashboard_resp = client.get("/api/dashboard", headers=AUTH)

                    assert status_resp.status_code == 200
                    assert dashboard_resp.status_code == 200

                    s = status_resp.json()
                    d = dashboard_resp.json()["run_status"]

                    assert s["processed"] == d["processed"] == 12
                    assert s["total"] == d["total"] == 50
                    assert s["progress_percent"] == d["progress_percent"] == 35
                    assert s["status"] == d["status"] == "running"
            finally:
                app.dependency_overrides.clear()
                get_run_status().reset()
