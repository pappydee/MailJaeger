"""
Tests for v1.2 performance and reporting features:

1. Batch AI analysis (AI_BATCH_SIZE, analyze_emails_batch)
2. Ingestion/analysis separation (phase field in RunStatus)
3. Priority-based analysis (importance scoring + ordering)
4. Default schedule time 02:00
5. Daily report endpoint (GET /api/reports/daily)
6. Scheduler fallback to 02:00
"""

import json
import pytest
from unittest.mock import patch, MagicMock, call
from fastapi.testclient import TestClient
import os
from datetime import datetime, timedelta

ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}

AUTH = {"Authorization": "Bearer " + ENV["API_KEY"]}


def _reset_rate_limiter():
    try:
        from src.middleware.rate_limiting import limiter
        limiter._storage.reset()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Feature 4 — Default schedule time 02:00
# ---------------------------------------------------------------------------


class TestDefaultScheduleTime:
    def test_default_schedule_time_is_0200(self):
        """New default schedule time must be 02:00, not 08:00."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            s = reload_settings()
            assert s.schedule_time == "02:00", (
                f"Expected default '02:00', got '{s.schedule_time}'"
            )

    def test_schedule_time_env_override(self):
        """SCHEDULE_TIME env var must still override the default."""
        with patch.dict(os.environ, {**ENV, "SCHEDULE_TIME": "06:30"}):
            from src.config import reload_settings

            s = reload_settings()
            assert s.schedule_time == "06:30"

    def test_scheduler_parse_fallback_returns_2_0(self):
        """If SCHEDULE_TIME is invalid, scheduler must fall back to 02:00."""
        with patch.dict(os.environ, {**ENV, "SCHEDULE_TIME": "not-a-time"}):
            from src.config import reload_settings
            reload_settings()
            from src.services.scheduler import SchedulerService

            svc = SchedulerService()
            h, m = svc._parse_schedule_time()
            assert (h, m) == (2, 0), f"Expected fallback (2, 0), got ({h}, {m})"


# ---------------------------------------------------------------------------
# Feature 1 — AI_BATCH_SIZE config
# ---------------------------------------------------------------------------


class TestBatchSizeConfig:
    def test_ai_batch_size_default_is_10(self):
        """AI_BATCH_SIZE must default to 10."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings

            s = reload_settings()
            assert s.ai_batch_size == 10

    def test_ai_batch_size_env_override(self):
        """AI_BATCH_SIZE env var must be honoured."""
        with patch.dict(os.environ, {**ENV, "AI_BATCH_SIZE": "25"}):
            from src.config import reload_settings

            s = reload_settings()
            assert s.ai_batch_size == 25


# ---------------------------------------------------------------------------
# Feature 1 — analyze_emails_batch() in AIService
# ---------------------------------------------------------------------------


class TestAnalyzeEmailsBatch:
    def _make_email(self, idx: int) -> dict:
        return {
            "id": idx,
            "subject": f"Test email {idx}",
            "sender": f"sender{idx}@example.com",
            "body_plain": f"Body of email {idx}",
            "body_html": "",
        }

    def test_batch_returns_same_count_as_input(self):
        """analyze_emails_batch must return exactly as many results as inputs."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(i) for i in range(5)]

            # Mock the raw AI call to return a valid array
            batch_response = json.dumps([
                {
                    "email_id": i,
                    "summary": f"Summary {i}",
                    "category": "Privat",
                    "spam_probability": 0.1,
                    "action_required": False,
                    "priority": "LOW",
                    "tasks": [],
                    "suggested_folder": "Archive",
                    "reasoning": "Test",
                }
                for i in range(5)
            ])
            with patch.object(ai, "_call_ai_service", return_value=batch_response):
                results = ai.analyze_emails_batch(emails)

            assert len(results) == 5

    def test_batch_empty_input_returns_empty_list(self):
        """Empty input list must return empty list without calling AI."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            with patch.object(ai, "_call_ai_service") as mock_call:
                result = ai.analyze_emails_batch([])
            assert result == []
            mock_call.assert_not_called()

    def test_batch_fallback_on_ai_failure(self):
        """If AI call fails, batch must fall back to per-email fallback classification."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(1), self._make_email(2)]

            with patch.object(ai, "_call_ai_service", side_effect=Exception("timeout")):
                results = ai.analyze_emails_batch(emails)

            assert len(results) == 2
            for r in results:
                assert "category" in r
                assert "spam_probability" in r

    def test_batch_parse_response_handles_id_lookup(self):
        """_parse_batch_response must match results by email_id."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [{"id": 10, "subject": "A", "sender": "a@b.com", "body_plain": "", "body_html": ""},
                      {"id": 20, "subject": "B", "sender": "b@c.com", "body_plain": "", "body_html": ""}]
            raw = json.dumps([
                {"email_id": 20, "summary": "B summary", "category": "Klinik",
                 "spam_probability": 0.0, "action_required": True, "priority": "HIGH",
                 "tasks": [], "suggested_folder": "Archive", "reasoning": "r"},
                {"email_id": 10, "summary": "A summary", "category": "Privat",
                 "spam_probability": 0.1, "action_required": False, "priority": "LOW",
                 "tasks": [], "suggested_folder": "Archive", "reasoning": "r"},
            ])
            results = ai._parse_batch_response(raw, emails)
            # email id=10 → index 0, should be A summary
            assert results[0]["category"] == "Privat"
            # email id=20 → index 1, should be B summary
            assert results[1]["category"] == "Klinik"


# ---------------------------------------------------------------------------
# Feature 2 — Phase field in RunStatus
# ---------------------------------------------------------------------------


class TestRunStatusPhase:
    def test_run_status_has_phase_field(self):
        """RunStatus must have a 'phase' field."""
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        assert hasattr(rs, "phase")
        assert rs.phase is None

    def test_run_status_to_dict_includes_phase(self):
        """to_dict() must include 'phase'."""
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.phase = "analysis"
        d = rs.to_dict()
        assert "phase" in d
        assert d["phase"] == "analysis"

    def test_reset_clears_phase(self):
        """reset() must set phase back to None."""
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.phase = "ingestion"
        rs.reset()
        assert rs.phase is None

    def test_update_sets_phase(self):
        """update() must accept phase kwarg."""
        from src.services.scheduler import RunStatus

        rs = RunStatus()
        rs.update(phase="ingestion")
        assert rs.phase == "ingestion"

    def test_api_status_includes_phase(self):
        """GET /api/status must include 'phase' in the response."""
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.services.scheduler import get_run_status

            get_run_status().reset()
            get_run_status().update(phase="analysis", status="running")
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/status", headers=AUTH)
            assert resp.status_code == 200
            data = resp.json()
            assert "phase" in data
            assert data["phase"] == "analysis"
            get_run_status().reset()


# ---------------------------------------------------------------------------
# Feature 3 — Importance scoring
# ---------------------------------------------------------------------------


class TestImportanceScoring:
    def test_compute_importance_score_returns_float_in_range(self):
        """compute_importance_score must return a float in [0, 100]."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.email_processor import EmailProcessor
            from src.models.database import ProcessedEmail

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            processor = EmailProcessor(mock_db)
            email = ProcessedEmail(
                subject="Dringende Anfrage",
                sender="boss@example.com",
                received_at=datetime.utcnow(),
                thread_id="thread-1",
            )
            score = processor.compute_importance_score(email)
            assert isinstance(score, float)
            assert 0.0 <= score <= 100.0

    def test_urgent_keywords_increase_score(self):
        """Emails with urgent keywords in subject must score higher."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.email_processor import EmailProcessor
            from src.models.database import ProcessedEmail

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            processor = EmailProcessor(mock_db)

            normal = ProcessedEmail(subject="Hello", sender="a@b.com")
            urgent = ProcessedEmail(subject="Dringend: Frist morgen", sender="a@b.com")

            score_normal = processor.compute_importance_score(normal)
            score_urgent = processor.compute_importance_score(urgent)
            assert score_urgent > score_normal, (
                f"Urgent email ({score_urgent}) should score higher than normal ({score_normal})"
            )

    def test_recent_emails_score_higher(self):
        """Emails received in last 24 h must score higher than old emails."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.email_processor import EmailProcessor
            from src.models.database import ProcessedEmail

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.count.return_value = 0

            processor = EmailProcessor(mock_db)

            recent = ProcessedEmail(
                subject="Test", sender="a@b.com",
                received_at=datetime.utcnow() - timedelta(hours=1),
            )
            old = ProcessedEmail(
                subject="Test", sender="a@b.com",
                received_at=datetime.utcnow() - timedelta(days=10),
            )
            assert processor.compute_importance_score(recent) > processor.compute_importance_score(old)


# ---------------------------------------------------------------------------
# Feature 5 — Daily report endpoint
# ---------------------------------------------------------------------------


class TestDailyReportEndpoint:
    def _client(self, emails_in_db=None):
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.database.connection import get_db

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
                emails_in_db or []
            )
            mock_db.query.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
            mock_db.query.return_value.filter.return_value.count.return_value = len(
                emails_in_db or []
            )

            def _override():
                yield mock_db

            app.dependency_overrides[get_db] = _override
            return app, mock_db

    def test_daily_report_requires_auth(self):
        """GET /api/reports/daily must reject unauthenticated requests."""
        _reset_rate_limiter()
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/reports/daily")
            assert resp.status_code == 401

    def test_daily_report_returns_200_when_no_emails(self):
        """GET /api/reports/daily must return 200 even when no emails processed."""
        app, _ = self._client(emails_in_db=[])
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/reports/daily", headers=AUTH)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "pending"
            assert "generated_at" in data
        finally:
            app.dependency_overrides.clear()

    def test_daily_report_response_shape(self):
        """Response must include all required fields."""
        app, _ = self._client(emails_in_db=[])
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/reports/daily", headers=AUTH)
            assert resp.status_code == 200
            d = resp.json()
            for key in ("status", "generated_at"):
                assert key in d, f"Missing key: {key}"
        finally:
            app.dependency_overrides.clear()

    def test_daily_report_fallback_when_ai_unavailable(self):
        """Endpoint should return async status quickly without blocking on AI generation."""
        app, _ = self._client(emails_in_db=[])
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/reports/daily", headers=AUTH)
            assert resp.status_code == 200
            d = resp.json()
            assert d["status"] == "pending"
        finally:
            app.dependency_overrides.clear()

    def test_dashboard_daily_report_available_field_present(self):
        """GET /api/dashboard must include 'daily_report_available' bool."""
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
                with patch("src.main.IMAPService") as mock_imap, \
                     patch("src.main.AIService") as mock_ai, \
                     patch("src.main.get_scheduler") as mock_sched:
                    mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                    mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                    mock_sched.return_value.get_next_run_time.return_value = None
                    mock_sched.return_value.get_status.return_value = {}

                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.get("/api/dashboard", headers=AUTH)
                assert resp.status_code == 200
                data = resp.json()
                assert "daily_report_available" in data
                assert isinstance(data["daily_report_available"], bool)
            finally:
                app.dependency_overrides.clear()
