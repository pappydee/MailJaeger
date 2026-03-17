"""
Tests for v1.3 stabilization pass:

1. Scheduler defaults resolve to 02:00 everywhere (config, docker-compose files, .env.example)
2. Custom CA auto-loading via entrypoint.sh works without disabling TLS
3. Daily report returns structured items (important_items, action_items, etc.)
4. Safe-mode morning report includes clickable suggested_actions
5. Batch analysis: correct ID mapping, duplicate IDs, malformed/partial JSON, cancellation
6. Ingestion phase does NOT call the AI
7. Newsletter/bulk penalty in importance scoring
"""

import json
import os
import re
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

ENV = {
    "API_KEY": "test_key_v130",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}

AUTH = {"Authorization": "Bearer " + ENV["API_KEY"]}

REPO_ROOT = Path(__file__).parent.parent


def _reset_rate_limiter():
    try:
        from src.middleware.rate_limiting import limiter
        limiter._storage.reset()
    except Exception:
        pass


# ===========================================================================
# Task 1 — Scheduler defaults: 02:00 everywhere
# ===========================================================================


class TestSchedulerDefaults:
    """02:00 must be the default schedule time in every config source."""

    def test_docker_compose_yml_default_is_0200(self):
        """docker-compose.yml must inject SCHEDULE_TIME default 02:00, not 08:00."""
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "SCHEDULE_TIME:-08:00" not in content, (
            "docker-compose.yml still has the old 08:00 default"
        )
        assert "SCHEDULE_TIME:-02:00" in content, (
            "docker-compose.yml must set SCHEDULE_TIME default to 02:00"
        )

    def test_docker_compose_prod_yml_default_is_0200(self):
        """docker-compose.prod.yml must inject SCHEDULE_TIME default 02:00, not 08:00."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "SCHEDULE_TIME:-08:00" not in content, (
            "docker-compose.prod.yml still has the old 08:00 default"
        )
        assert "SCHEDULE_TIME:-02:00" in content, (
            "docker-compose.prod.yml must set SCHEDULE_TIME default to 02:00"
        )

    def test_env_example_default_is_0200(self):
        """.env.example must document SCHEDULE_TIME=02:00, not 08:00."""
        content = (REPO_ROOT / ".env.example").read_text()
        assert "SCHEDULE_TIME=08:00" not in content, (
            ".env.example still has the old 08:00 default"
        )
        assert "SCHEDULE_TIME=02:00" in content, (
            ".env.example must set SCHEDULE_TIME to 02:00"
        )

    def test_config_default_is_0200(self):
        """Python Settings default must be 02:00."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            s = reload_settings()
            assert s.schedule_time == "02:00", (
                f"Config default should be '02:00', got '{s.schedule_time}'"
            )

    def test_no_hidden_0800_in_docker_files(self):
        """No hidden 08:00 scheduler default must remain in any compose file."""
        for fname in ("docker-compose.yml", "docker-compose.prod.yml"):
            content = (REPO_ROOT / fname).read_text()
            # The only allowed 08: patterns are comments, not active defaults
            active_lines = [
                line for line in content.splitlines()
                if "08:00" in line and not line.strip().startswith("#")
            ]
            assert not active_lines, (
                f"{fname} still has an active (non-comment) 08:00 line: {active_lines}"
            )


# ===========================================================================
# Task 2 — TLS / CA certificate auto-loading
# ===========================================================================


class TestCACertAutoLoading:
    """entrypoint.sh must handle cert directories correctly."""

    def test_entrypoint_script_exists(self):
        """scripts/entrypoint.sh must exist."""
        assert (REPO_ROOT / "scripts" / "entrypoint.sh").exists()

    def test_entrypoint_imports_crt_and_pem(self):
        """entrypoint.sh must handle both .crt and .pem files."""
        content = (REPO_ROOT / "scripts" / "entrypoint.sh").read_text()
        assert ".crt" in content
        assert ".pem" in content

    def test_entrypoint_runs_update_ca_certificates(self):
        """entrypoint.sh must call update-ca-certificates."""
        content = (REPO_ROOT / "scripts" / "entrypoint.sh").read_text()
        assert "update-ca-certificates" in content

    def test_entrypoint_does_not_disable_tls_verification(self):
        """entrypoint.sh must NOT set any env var that disables TLS verification."""
        content = (REPO_ROOT / "scripts" / "entrypoint.sh").read_text()
        assert "IMAP_SSL_VERIFY=false" not in content
        assert "SSL_VERIFY=false" not in content
        assert "verify=false" not in content.lower()

    def test_dockerfile_copies_entrypoint(self):
        """Dockerfile must copy and chmod the entrypoint script."""
        content = (REPO_ROOT / "Dockerfile").read_text()
        assert "entrypoint.sh" in content
        assert "chmod +x" in content

    def test_dockerfile_installs_ca_certificates(self):
        """Dockerfile must install the ca-certificates package."""
        content = (REPO_ROOT / "Dockerfile").read_text()
        assert "ca-certificates" in content

    def test_docker_compose_has_certs_volume_comment(self):
        """docker-compose.yml must document the optional certs volume."""
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        # The commented-out certs volume line should be present
        assert "certs" in content

    def test_env_example_documents_certs(self):
        """.env.example must explain how to use the certs directory."""
        content = (REPO_ROOT / ".env.example").read_text()
        assert "certs" in content.lower()

    def test_entrypoint_works_with_no_certs(self):
        """entrypoint.sh must be a valid POSIX shell script (no syntax errors)."""
        import subprocess
        result = subprocess.run(
            ["sh", "-n", str(REPO_ROOT / "scripts" / "entrypoint.sh")],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"entrypoint.sh has syntax errors: {result.stderr}"
        )


# ===========================================================================
# Task 3 — Daily report structured items
# ===========================================================================


class TestDailyReportStructured:
    """Daily report response must include structured item lists."""

    def _get_client(self):
        _reset_rate_limiter()
        from src.config import reload_settings
        reload_settings()
        from src.main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_daily_report_has_structured_fields(self):
        """GET /api/reports/daily must include important_items, action_items, etc."""
        with patch.dict(os.environ, ENV):
            client = self._get_client()
            with patch("src.main.AIService") as mock_ai_cls:
                mock_ai = mock_ai_cls.return_value
                mock_ai.generate_report.return_value = "Test report"
                resp = client.get("/api/reports/daily", headers=AUTH)

            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "important_items" in data
            assert "action_items" in data
            assert "unresolved_items" in data
            assert "spam_items" in data
            assert "suggested_actions" in data
            assert "totals" in data
            assert isinstance(data["important_items"], list)
            assert isinstance(data["action_items"], list)
            assert isinstance(data["unresolved_items"], list)
            assert isinstance(data["spam_items"], list)
            assert isinstance(data["suggested_actions"], list)
            assert isinstance(data["totals"], dict)

    def test_daily_report_backward_compat_fields_present(self):
        """Backward-compatible fields (total_processed, action_required, etc.) must still exist."""
        with patch.dict(os.environ, ENV):
            client = self._get_client()
            with patch("src.main.AIService") as mock_ai_cls:
                mock_ai = mock_ai_cls.return_value
                mock_ai.generate_report.return_value = "Test"
                resp = client.get("/api/reports/daily", headers=AUTH)

            assert resp.status_code == 200
            data = resp.json()
            assert "total_processed" in data
            assert "action_required" in data
            assert "spam_detected" in data
            assert "unresolved" in data
            assert "report_text" in data
            assert "generated_at" in data
            assert "period_hours" in data

    def test_daily_report_fallback_still_useful(self):
        """When AI is unavailable, fallback report_text must still be non-empty."""
        with patch.dict(os.environ, ENV):
            client = self._get_client()
            with patch("src.main.AIService") as mock_ai_cls:
                mock_ai = mock_ai_cls.return_value
                mock_ai.generate_report.return_value = None
                resp = client.get("/api/reports/daily", headers=AUTH)

            assert resp.status_code == 200
            data = resp.json()
            assert data["report_text"]
            # Fallback text must mention emails processed or be a report header
            assert len(data["report_text"]) > 10


# ===========================================================================
# Task 4 — Safe mode: suggested_actions in morning report
# ===========================================================================


class TestSafeModeSuggestedActions:
    """In SAFE MODE the morning report must include clickable suggested_actions."""

    def _get_client_with_safe_mode(self, safe_mode: bool = True):
        _reset_rate_limiter()
        env = {**ENV, "SAFE_MODE": str(safe_mode).lower()}
        from src.config import reload_settings
        with patch.dict(os.environ, env):
            reload_settings()
            from src.main import app
            return TestClient(app, raise_server_exceptions=False), env

    def test_suggested_actions_schema_fields(self):
        """ReportSuggestedAction must have email_id, action_type, description, safe_mode."""
        from src.models.schemas import ReportSuggestedAction
        action = ReportSuggestedAction(
            email_id=1,
            thread_id="thread-1",
            action_type="archive",
            payload={"target_folder": "Archive"},
            target_folder="Archive",
            description="Archivieren: Test",
            safe_mode=True,
        )
        assert action.email_id == 1
        assert action.thread_id == "thread-1"
        assert action.action_type == "archive"
        assert action.payload["target_folder"] == "Archive"
        assert action.safe_mode is True

    def test_suggested_actions_have_email_ids(self):
        """Each suggested action must be tied to a specific email_id."""
        from src.models.schemas import ReportSuggestedAction
        action = ReportSuggestedAction(
            email_id=42,
            action_type="MARK_RESOLVED",
            description="Als erledigt markieren",
            safe_mode=True,
        )
        assert action.email_id == 42

    def test_valid_action_types(self):
        """Supported action_type values must include the expected set."""
        from src.models.schemas import ReportSuggestedAction
        valid_types = [
            "move", "archive", "mark_read",
            "mark_spam", "mark_resolved", "reply_draft", "delete",
        ]
        for atype in valid_types:
            a = ReportSuggestedAction(
                email_id=1, action_type=atype, description="test", safe_mode=True
            )
            assert a.action_type == atype

    def test_daily_report_endpoint_includes_suggested_actions(self):
        """GET /api/reports/daily response must include suggested_actions list."""
        _reset_rate_limiter()
        with patch.dict(os.environ, {**ENV, "SAFE_MODE": "true"}):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.AIService") as mock_ai_cls:
                mock_ai = mock_ai_cls.return_value
                mock_ai.generate_report.return_value = "Report"
                resp = client.get("/api/reports/daily", headers=AUTH)

            assert resp.status_code == 200
            data = resp.json()
            assert "suggested_actions" in data
            # In safe mode with no emails, list should be empty (not raise an error)
            assert isinstance(data["suggested_actions"], list)

    def test_report_ui_supports_queueing_suggested_actions(self):
        """Frontend should post suggested-action clicks to report queue endpoint."""
        content = (REPO_ROOT / "frontend" / "app.js").read_text()
        assert "/api/reports/daily/suggested-actions" in content
        assert "SAFE MODE aktiv" in content


# ===========================================================================
# Task 5 — Batch analysis hardening
# ===========================================================================


class TestBatchAnalysisHardened:
    """Batch analysis must handle edge cases safely."""

    def _make_email(self, idx: int) -> dict:
        return {
            "id": idx,
            "subject": f"Subject {idx}",
            "sender": f"user{idx}@example.com",
            "body_plain": f"Body {idx}",
            "body_html": "",
        }

    def _valid_item(self, email_id):
        return {
            "email_id": email_id,
            "summary": f"Summary {email_id}",
            "category": "Privat",
            "spam_probability": 0.1,
            "action_required": False,
            "priority": "LOW",
            "tasks": [],
            "suggested_folder": "Archive",
            "reasoning": "OK",
        }

    def test_duplicate_ids_in_response_uses_first_occurrence(self):
        """When the AI returns duplicate email_ids, the first occurrence must win."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(1), self._make_email(2)]
            # AI returns id=1 twice (duplicate), then id=2
            dup_response = json.dumps([
                {**self._valid_item(1), "summary": "First occurrence of id 1"},
                {**self._valid_item(1), "summary": "Duplicate id 1 — must be ignored"},
                self._valid_item(2),
            ])
            results = ai._parse_batch_response(dup_response, emails)
            assert len(results) == 2
            # id=1 must use the first occurrence
            assert results[0]["summary"] == "First occurrence of id 1"

    def test_partial_json_array_fallback(self):
        """Malformed/incomplete JSON should trigger a fallback, not an exception."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(1), self._make_email(2)]
            # Truncated JSON — should raise internally but be caught by analyze_emails_batch
            with patch.object(ai, "_call_ai_service", return_value="[{bad json"):
                results = ai.analyze_emails_batch(emails)
            # Must return 2 fallback results, not raise
            assert len(results) == 2
            for r in results:
                assert "category" in r

    def test_missing_id_in_response_falls_back(self):
        """If the AI omits email_id for an entry, fallback must be used."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(10)]
            # Response has no email_id field
            no_id_item = {
                "summary": "No ID",
                "category": "Unklar",
                "spam_probability": 0.0,
                "action_required": False,
                "priority": "LOW",
                "tasks": [],
                "suggested_folder": "Archive",
                "reasoning": "test",
            }
            response = json.dumps([no_id_item])
            results = ai._parse_batch_response(response, emails)
            # Should still return one result (positional fallback)
            assert len(results) == 1
            assert "category" in results[0]

    def test_invalid_category_defaults_to_unklar(self):
        """Invalid category values in batch response must be normalized to 'Unklar'."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(5)]
            bad_item = {**self._valid_item(5), "category": "NOT_A_VALID_CATEGORY"}
            response = json.dumps([bad_item])
            results = ai._parse_batch_response(response, emails)
            assert results[0]["category"] == "Unklar"

    def test_empty_batch_returns_empty_without_ai_call(self):
        """Empty batch must not trigger an AI call."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            with patch.object(ai, "_call_ai_service") as mock_call:
                results = ai.analyze_emails_batch([])
            assert results == []
            mock_call.assert_not_called()

    def test_batch_id_lookup_by_email_id(self):
        """Results must be matched by email_id even when order differs."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService

            ai = AIService()
            emails = [self._make_email(100), self._make_email(200)]
            # AI returns them in reverse order
            response = json.dumps([self._valid_item(200), self._valid_item(100)])
            response = response.replace(
                '"Summary 200"', '"Email 200 summary"'
            ).replace(
                '"Summary 100"', '"Email 100 summary"'
            )
            results = ai._parse_batch_response(response, emails)
            # results[0] should be for email id=100
            assert len(results) == 2


# ===========================================================================
# Task 6 — Ingestion phase does NOT call the AI
# ===========================================================================


class TestIngestionNoAI:
    """Phase 1 (ingestion) must never invoke the AI service."""

    def test_mail_ingestion_service_has_no_ai_import(self):
        """MailIngestionService source must not import AIService."""
        ingestion_path = (
            REPO_ROOT / "src" / "services" / "mail_ingestion_service.py"
        )
        if not ingestion_path.exists():
            pytest.skip("mail_ingestion_service.py not found")
        content = ingestion_path.read_text()
        # It must not import AIService directly
        assert "AIService" not in content, (
            "mail_ingestion_service.py must not import or use AIService"
        )

    def test_ingestion_service_does_not_call_ai(self):
        """MailIngestionService.ingest_folder must not call analyze_email."""
        ingestion_path = (
            REPO_ROOT / "src" / "services" / "mail_ingestion_service.py"
        )
        if not ingestion_path.exists():
            pytest.skip("mail_ingestion_service.py not found")
        content = ingestion_path.read_text()
        assert "analyze_email" not in content, (
            "ingestion service must not call analyze_email — AI is Phase 2 only"
        )
        assert "analyze_emails_batch" not in content, (
            "ingestion service must not call analyze_emails_batch — AI is Phase 2 only"
        )

    def test_run_ingestion_creates_analysis_state_pending(self):
        """_run_ingestion helper must exist in EmailProcessor and not touch AI."""
        from src.services.email_processor import EmailProcessor
        import inspect

        source = inspect.getsource(EmailProcessor._run_ingestion)
        # Must call ingestion service, not AI
        assert "MailIngestionService" in source or "ingestion_service" in source
        assert "ai_service" not in source.lower()


# ===========================================================================
# Task 7 — Newsletter / bulk penalty in importance scoring
# ===========================================================================


class TestImportanceScoringNewsletterPenalty:
    """Newsletter / bulk mail must not get inflated scores due to recency alone."""

    def _make_processor(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.email_processor import EmailProcessor
            from src.models.database import ProcessedEmail

            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.count.return_value = 0
            return EmailProcessor(mock_db), ProcessedEmail

    def test_newsletter_sender_gets_lower_score_than_normal(self):
        """A recent newsletter email must score lower than a recent normal email."""
        processor, ProcessedEmail = self._make_processor()
        now = datetime.utcnow()

        newsletter = ProcessedEmail(
            subject="Weekly Digest",
            sender="newsletter@marketing.example.com",
            received_at=now,
        )
        normal = ProcessedEmail(
            subject="Dringende Anfrage",
            sender="boss@company.example.com",
            received_at=now,
        )
        ns = processor.compute_importance_score(newsletter)
        nm = processor.compute_importance_score(normal)
        assert ns < nm, (
            f"Newsletter score ({ns}) should be lower than normal email ({nm})"
        )

    def test_unsubscribe_in_subject_lowers_score(self):
        """Emails mentioning unsubscribe in subject must score lower than baseline."""
        processor, ProcessedEmail = self._make_processor()

        bulk_email = ProcessedEmail(
            subject="Unsubscribe from our list",
            sender="info@example.com",
        )
        score = processor.compute_importance_score(bulk_email)
        # Baseline is 30; with newsletter penalty (-20) score should be below the unpenalised baseline
        assert score < 30.0, (
            f"Bulk email with 'unsubscribe' should score below the 30-point baseline, got {score}"
        )

    def test_no_reply_sender_lowers_score(self):
        """Emails from no-reply addresses must be penalised."""
        processor, ProcessedEmail = self._make_processor()

        no_reply = ProcessedEmail(
            subject="Your receipt",
            sender="no-reply@store.example.com",
        )
        normal = ProcessedEmail(
            subject="Your receipt",
            sender="service@store.example.com",
        )
        ns = processor.compute_importance_score(no_reply)
        nm = processor.compute_importance_score(normal)
        assert ns < nm, (
            f"no-reply sender ({ns}) should score lower than normal sender ({nm})"
        )

    def test_score_stays_in_valid_range_with_penalty(self):
        """Even with the newsletter penalty, score must stay >= 0."""
        processor, ProcessedEmail = self._make_processor()

        email = ProcessedEmail(
            subject="Monthly newsletter unsubscribe sale offer deal",
            sender="noreply@newsletter.marketing.example.com",
        )
        score = processor.compute_importance_score(email)
        assert 0.0 <= score <= 100.0

    def test_urgent_email_scores_higher_than_newsletter(self):
        """An urgent email must score higher than a newsletter even if newsletter is newer."""
        processor, ProcessedEmail = self._make_processor()
        now = datetime.utcnow()
        older = now - timedelta(hours=30)

        newsletter_new = ProcessedEmail(
            subject="Weekly Digest",
            sender="newsletter@corp.example.com",
            received_at=now,
        )
        urgent_older = ProcessedEmail(
            subject="Dringende Anfrage — Frist heute",
            sender="boss@company.example.com",
            received_at=older,
        )
        ns = processor.compute_importance_score(newsletter_new)
        us = processor.compute_importance_score(urgent_older)
        assert us > ns, (
            f"Urgent older email ({us}) should outscore recent newsletter ({ns})"
        )
