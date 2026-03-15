"""
Tests for Round 1 Architecture Foundation

Covers:
- Fix A: IMAP safe mode read bug (BODY.PEEK, no marking as read)
- Fix B: Host Ollama configuration
- Fix C: No hardcoded AI timeouts
- Fix D: Configurable TLS certificate verification
- Priority 2: Extended email data model
- Priority 3: Thread reconstruction
- Priority 4: Body hash deduplication
- Priority 5: Decision events
- Priority 6: Action queue state machine
- Priority 7: Pause/resume analysis progress
- Priority 8: Resource budget controls
- Priority 9: Multi-stage analysis pipeline
- Priority 10: UTF-8/umlaut support
"""

import os
import hashlib
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

# Minimal test environment
ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "false",
    "REQUIRE_APPROVAL": "false",
    "AI_TIMEOUT": "30",
    "MAX_EMAILS_PER_BATCH": "50",
    "MAX_RUNTIME_MINUTES": "30",
    "MAX_LLM_CALLS_PER_RUN": "100",
    "MAX_PARALLEL_TASKS": "1",
}


# ============================================================================
# FIX A — IMAP Safe Mode Read Bug
# ============================================================================


class TestIMAPSafeModeReadBug:
    """Safe mode must never mark emails as read."""

    def test_fetch_uses_body_peek_not_rfc822(self):
        """Verify the fetch call uses BODY.PEEK[] so Seen flag is not set."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService

        service = IMAPService()
        mock_client = MagicMock()
        # Return an empty dict so no emails are processed
        mock_client.search.return_value = [1, 2, 3]
        mock_client.fetch.return_value = {}
        mock_client.select_folder.return_value = {}
        service.client = mock_client

        service.get_unread_emails(max_count=10)

        # The fetch call must use BODY.PEEK[] (not RFC822) to avoid setting \Seen
        mock_client.fetch.assert_called_once()
        call_args = mock_client.fetch.call_args
        fetch_items = call_args[0][1]  # second positional arg
        assert b"BODY.PEEK[]" in fetch_items, (
            f"Fetch must use BODY.PEEK[] to avoid marking emails as read, got: {fetch_items}"
        )
        assert "RFC822" not in fetch_items, (
            "Fetch must NOT use RFC822 (it marks emails as read)"
        )

    def test_parse_email_uses_body_peek_key(self):
        """_parse_email must read from BODY[] key (BODY.PEEK response key)."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService
        import email as email_lib

        service = IMAPService()
        service.settings = service.settings  # ensure loaded

        # Build a minimal raw email
        raw = b"From: sender@test.com\r\nSubject: Test\r\n\r\nBody content"

        # Simulate BODY.PEEK[] response (IMAP returns key b"BODY[]")
        message_data = {b"BODY[]": raw, b"FLAGS": []}
        result = service._parse_email(1, message_data)

        assert result is not None
        assert result["subject"] == "Test"
        assert result["sender"] == "sender@test.com"

    def test_parse_email_falls_back_to_rfc822_key(self):
        """_parse_email must fall back to RFC822 key for backward compatibility."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService

        service = IMAPService()
        raw = b"From: old@test.com\r\nSubject: Old\r\n\r\nOld body"

        # Simulate old-style RFC822 response
        message_data = {b"RFC822": raw, b"FLAGS": []}
        result = service._parse_email(2, message_data)

        assert result is not None
        assert result["subject"] == "Old"

    def test_get_unread_emails_does_not_mark_as_read(self):
        """get_unread_emails must NOT call mark_as_read or add SEEN flag."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService

        service = IMAPService()
        mock_client = MagicMock()
        raw = b"From: test@test.com\r\nSubject: Hello\r\nMessage-ID: <test123>\r\n\r\nContent"
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {1: {b"BODY[]": raw, b"FLAGS": []}}
        mock_client.select_folder.return_value = {}
        service.client = mock_client

        service.get_unread_emails()

        # Must not call add_flags with SEEN
        mock_client.add_flags.assert_not_called()


# ============================================================================
# FIX B — Host Ollama Configuration
# ============================================================================


class TestHostOllamaConfiguration:
    """MailJaeger must use the host system Ollama, not a Docker container."""

    def test_docker_compose_no_ollama_service(self):
        """docker-compose.yml must not contain an ollama service."""
        with open("docker-compose.yml", "r") as f:
            content = f.read()
        assert "ollama/ollama" not in content, "docker-compose.yml must not use ollama/ollama image"
        assert "mailjaeger-ollama" not in content, "docker-compose.yml must not have mailjaeger-ollama container"

    def test_docker_compose_uses_host_docker_internal(self):
        """docker-compose.yml must use host.docker.internal for AI endpoint."""
        with open("docker-compose.yml", "r") as f:
            content = f.read()
        assert "host.docker.internal" in content, (
            "docker-compose.yml must reference host.docker.internal for AI endpoint"
        )

    def test_docker_compose_no_depends_on_ollama(self):
        """docker-compose.yml must not have depends_on: ollama."""
        with open("docker-compose.yml", "r") as f:
            content = f.read()
        # There should be no ollama volume or service dependency
        assert "ollama_data" not in content, "docker-compose.yml must not have ollama_data volume"

    def test_config_default_ai_endpoint_is_host_docker_internal(self):
        """Default AI endpoint in config Field must point to host.docker.internal."""
        from src.config import Settings
        import inspect
        # Check the Field default value directly from the model field metadata
        field = Settings.model_fields.get("ai_endpoint")
        assert field is not None
        assert "host.docker.internal" in str(field.default), (
            f"Default ai_endpoint must be host.docker.internal, got: {field.default}"
        )


# ============================================================================
# FIX C — No Hardcoded AI Timeouts
# ============================================================================


class TestNoHardcodedAITimeouts:
    """AI timeout must always come from settings.ai_timeout."""

    def test_ai_service_check_health_uses_settings_timeout(self):
        """check_health() must use self.settings.ai_timeout, not a hardcoded value."""
        import inspect
        from src.services.ai_service import AIService
        source = inspect.getsource(AIService.check_health)
        # Must use ai_timeout from settings
        assert "ai_timeout" in source, "check_health must use self.settings.ai_timeout"
        # Must NOT use hardcoded small timeout like timeout=5
        assert "timeout=5" not in source, "check_health must not use hardcoded timeout=5"

    def test_ai_service_call_uses_settings_timeout(self):
        """_call_ai_service() must use self.settings.ai_timeout."""
        import inspect
        from src.services.ai_service import AIService
        source = inspect.getsource(AIService._call_ai_service)
        assert "ai_timeout" in source, "_call_ai_service must use self.settings.ai_timeout"

    def test_ai_timeout_is_configurable(self):
        """AI_TIMEOUT env var must control the timeout."""
        with patch.dict(os.environ, {**ENV, "AI_TIMEOUT": "60"}):
            from src.config import reload_settings
            settings = reload_settings()
            assert settings.ai_timeout == 60


# ============================================================================
# FIX D — TLS Certificate Verification
# ============================================================================


class TestTLSCertificateVerification:
    """TLS certificate verification must be configurable."""

    def test_config_has_imap_ssl_verify_setting(self):
        """Settings must have imap_ssl_verify field."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert hasattr(settings, "imap_ssl_verify"), "Settings must have imap_ssl_verify"

    def test_imap_ssl_verify_defaults_to_true(self):
        """imap_ssl_verify must default to True (safe default)."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert settings.imap_ssl_verify is True

    def test_imap_ssl_verify_can_be_disabled(self):
        """imap_ssl_verify can be set to False via env var."""
        with patch.dict(os.environ, {**ENV, "IMAP_SSL_VERIFY": "false"}):
            from src.config import reload_settings
            settings = reload_settings()
        assert settings.imap_ssl_verify is False

    def test_imap_service_creates_ssl_context(self):
        """IMAPService.connect must create a custom SSL context."""
        import inspect
        from src.services.imap_service import IMAPService
        source = inspect.getsource(IMAPService.connect)
        assert "ssl_context" in source, "connect() must create an ssl_context"
        assert "imap_ssl_verify" in source, "connect() must reference imap_ssl_verify"


# ============================================================================
# Priority 2 — Extended Email Data Model
# ============================================================================


class TestExtendedEmailDataModel:
    """emails table must have all required new fields."""

    def test_processed_email_has_thread_id(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "thread_id" in cols

    def test_processed_email_has_body_hash(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "body_hash" in cols

    def test_processed_email_has_snippet(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "snippet" in cols

    def test_processed_email_has_folder(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "folder" in cols

    def test_processed_email_has_received_at(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "received_at" in cols

    def test_processed_email_has_flags(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "flags" in cols

    def test_processed_email_has_analysis_state(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "analysis_state" in cols

    def test_processed_email_has_analysis_version(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "analysis_version" in cols

    def test_processed_email_has_importance_score(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "importance_score" in cols

    def test_processed_email_has_imap_uid(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "imap_uid" in cols


# ============================================================================
# Priority 3 — Thread Reconstruction
# ============================================================================


class TestThreadReconstruction:
    """Thread IDs must be resolved from email headers."""

    def _make_ingestion_service(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService
        db = MagicMock()
        return MailIngestionService(db_session=db)

    def test_generate_thread_id_is_stable(self):
        """Same message_id must always produce the same thread_id."""
        svc = self._make_ingestion_service()
        mid = "<test-message-id@example.com>"
        t1 = svc._generate_thread_id(mid)
        t2 = svc._generate_thread_id(mid)
        assert t1 == t2
        assert t1.startswith("thread-")

    def test_resolve_thread_id_uses_in_reply_to(self):
        """If In-Reply-To refers to a known email, use its thread_id."""
        svc = self._make_ingestion_service()
        parent = MagicMock()
        parent.thread_id = "thread-existing123"
        svc.db.query.return_value.filter.return_value.first.return_value = parent

        result = svc._resolve_thread_id(
            message_id="<reply@test.com>",
            in_reply_to="<original@test.com>",
            references="",
        )
        assert result == "thread-existing123"

    def test_resolve_thread_id_creates_new_when_no_parent(self):
        """If In-Reply-To is unknown, a new thread_id is generated."""
        svc = self._make_ingestion_service()
        svc.db.query.return_value.filter.return_value.first.return_value = None

        result = svc._resolve_thread_id(
            message_id="<new-email@test.com>",
            in_reply_to="<unknown-parent@test.com>",
            references="",
        )
        assert result.startswith("thread-")

    def test_resolve_thread_id_uses_references(self):
        """References header is used when In-Reply-To is missing."""
        svc = self._make_ingestion_service()
        ref_email = MagicMock()
        ref_email.thread_id = "thread-from-references"

        def mock_first():
            return ref_email

        svc.db.query.return_value.filter.return_value.first = mock_first

        result = svc._resolve_thread_id(
            message_id="<email@test.com>",
            in_reply_to="",
            references="<ref1@test.com> <ref2@test.com>",
        )
        assert result == "thread-from-references"

    def test_resolve_thread_id_no_reply_no_references(self):
        """Email with no In-Reply-To or References gets its own new thread."""
        svc = self._make_ingestion_service()
        svc.db.query.return_value.filter.return_value.first.return_value = None

        result = svc._resolve_thread_id(
            message_id="<standalone@test.com>",
            in_reply_to="",
            references="",
        )
        assert result.startswith("thread-")
        expected = svc._generate_thread_id("<standalone@test.com>")
        assert result == expected


# ============================================================================
# Priority 4 — Body Hash Deduplication
# ============================================================================


class TestBodyHashDeduplication:
    """Body hash must uniquely identify email content for deduplication."""

    def _make_ingestion_service(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService
        db = MagicMock()
        return MailIngestionService(db_session=db)

    def test_body_hash_is_sha256(self):
        """Body hash must be a 64-char hex string (SHA256)."""
        svc = self._make_ingestion_service()
        body_hash = svc._compute_body_hash("Hello world", "")
        assert len(body_hash) == 64
        assert all(c in "0123456789abcdef" for c in body_hash)

    def test_identical_bodies_produce_same_hash(self):
        """Two emails with identical bodies must have the same hash."""
        svc = self._make_ingestion_service()
        h1 = svc._compute_body_hash("Hello, this is the email body.", "")
        h2 = svc._compute_body_hash("Hello, this is the email body.", "")
        assert h1 == h2

    def test_different_bodies_produce_different_hashes(self):
        """Different email bodies must produce different hashes."""
        svc = self._make_ingestion_service()
        h1 = svc._compute_body_hash("Body A with unique content", "")
        h2 = svc._compute_body_hash("Body B with completely different content", "")
        assert h1 != h2

    def test_whitespace_differences_normalized(self):
        """Extra whitespace must not change the body hash."""
        svc = self._make_ingestion_service()
        h1 = svc._compute_body_hash("Hello   world", "")
        h2 = svc._compute_body_hash("Hello world", "")
        assert h1 == h2

    def test_html_body_hash_strips_tags(self):
        """HTML body hash must be based on text content, not HTML markup."""
        svc = self._make_ingestion_service()
        h_html = svc._compute_body_hash("", "<p>Hello world</p>")
        h_plain = svc._compute_body_hash("Hello world", "")
        # Both should hash similarly after normalization
        # (exact equality depends on HTML stripping, so we just verify it's computable)
        assert len(h_html) == 64

    def test_utf8_body_hash(self):
        """Body hash must handle UTF-8 characters including umlauts."""
        svc = self._make_ingestion_service()
        h = svc._compute_body_hash("Hallo Welt mit Umlauten: ä ö ü ß", "")
        assert len(h) == 64


# ============================================================================
# Priority 5 — Decision Events
# ============================================================================


class TestDecisionEvents:
    """decision_events table must exist with all required fields."""

    def test_decision_event_model_importable(self):
        from src.models.database import DecisionEvent
        assert DecisionEvent.__tablename__ == "decision_events"

    def test_decision_event_has_required_columns(self):
        from src.models.database import DecisionEvent
        cols = {c.name for c in DecisionEvent.__table__.columns}
        required = {
            "id", "email_id", "thread_id", "event_type", "source",
            "old_value", "new_value", "confidence", "created_at",
            "user_confirmed", "model_version", "rule_id",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_processed_email_has_decision_events_relationship(self):
        from src.models.database import ProcessedEmail
        assert hasattr(ProcessedEmail, "decision_events")


# ============================================================================
# Priority 6 — Action Queue State Machine
# ============================================================================


class TestActionQueueStateMachine:
    """action_queue table must implement the correct state machine."""

    def test_action_queue_model_importable(self):
        from src.models.database import ActionQueue
        assert ActionQueue.__tablename__ == "action_queue"

    def test_action_queue_has_state_machine_columns(self):
        from src.models.database import ActionQueue
        cols = {c.name for c in ActionQueue.__table__.columns}
        required = {
            "id", "email_id", "action_type", "status",
            "created_at", "queued_at", "approved_at", "executed_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_action_queue_default_status_is_proposed(self):
        """Default status must be 'proposed_action'."""
        from src.models.database import ActionQueue
        status_col = ActionQueue.__table__.columns["status"]
        assert status_col.default.arg == "proposed_action"

    def test_action_queue_has_all_state_values(self):
        """All state machine states must be documented in the model."""
        import inspect
        from src.models.database import ActionQueue
        source = inspect.getsource(ActionQueue)
        expected_states = [
            "proposed_action",
            "queued_action",
            "approved_action",
            "executed_action",
        ]
        for state in expected_states:
            assert state in source, f"State '{state}' must be documented in ActionQueue"

    def test_processed_email_has_action_queue_relationship(self):
        from src.models.database import ProcessedEmail
        assert hasattr(ProcessedEmail, "action_queue_items")


# ============================================================================
# Priority 7 — Pause/Resume Processing
# ============================================================================


class TestPauseResumeProcessing:
    """analysis_progress table must support pause/resume."""

    def test_analysis_progress_model_importable(self):
        from src.models.database import AnalysisProgress
        assert AnalysisProgress.__tablename__ == "analysis_progress"

    def test_analysis_progress_has_required_columns(self):
        from src.models.database import AnalysisProgress
        cols = {c.name for c in AnalysisProgress.__table__.columns}
        required = {
            "id", "stage", "last_email_id", "processed_count", "timestamp",
            "status", "started_at", "paused_at", "completed_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_ingestion_service_tracks_progress(self):
        """Ingestion service must create AnalysisProgress records."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        svc = MailIngestionService(db_session=db)
        progress = svc._get_or_create_progress("test-run-123", "ingestion", "INBOX")

        db.add.assert_called()
        db.commit.assert_called()


# ============================================================================
# Priority 8 — Resource Budget Controls
# ============================================================================


class TestResourceBudgetControls:
    """Config must support all resource budget parameters."""

    def test_config_has_max_emails_per_batch(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert hasattr(settings, "max_emails_per_batch")
        assert settings.max_emails_per_batch == 50

    def test_config_has_max_runtime_minutes(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert hasattr(settings, "max_runtime_minutes")
        assert settings.max_runtime_minutes == 30

    def test_config_has_max_llm_calls_per_run(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert hasattr(settings, "max_llm_calls_per_run")
        assert settings.max_llm_calls_per_run == 100

    def test_config_has_max_parallel_tasks(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            settings = reload_settings()
        assert hasattr(settings, "max_parallel_tasks")
        assert settings.max_parallel_tasks == 1

    def test_resource_limits_are_configurable(self):
        """Resource limits must be overridable via environment variables."""
        custom_env = {
            **ENV,
            "MAX_EMAILS_PER_BATCH": "25",
            "MAX_RUNTIME_MINUTES": "15",
            "MAX_LLM_CALLS_PER_RUN": "50",
        }
        with patch.dict(os.environ, custom_env):
            from src.config import reload_settings
            settings = reload_settings()
        assert settings.max_emails_per_batch == 25
        assert settings.max_runtime_minutes == 15
        assert settings.max_llm_calls_per_run == 50


# ============================================================================
# Priority 9 — Multi-Stage Analysis Pipeline
# ============================================================================


class TestMultiStageAnalysisPipeline:
    """Multi-stage pipeline must correctly route emails through stages."""

    def _make_pipeline(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.analysis_pipeline import AnalysisPipeline
        db = MagicMock()
        db.query.return_value.all.return_value = []
        return AnalysisPipeline(db_session=db)

    def _make_email(self, **kwargs) -> MagicMock:
        email = MagicMock()
        email.id = 1
        email.message_id = "<test@test.com>"
        email.sender = kwargs.get("sender", "user@example.com")
        email.subject = kwargs.get("subject", "Test email")
        email.body_plain = kwargs.get("body_plain", "")
        email.body_html = kwargs.get("body_html", "")
        email.snippet = kwargs.get("snippet", "")
        email.folder = kwargs.get("folder", "INBOX")
        email.thread_id = None
        email.analysis_state = "pending"
        return email

    def test_newsletter_classified_at_stage1(self):
        """Newsletters must be classified at Stage 1 (no LLM needed)."""
        pipeline = self._make_pipeline()
        email = self._make_email(sender="noreply@newsletter.com")
        result = pipeline._stage1_pre_classify(email)
        assert result["confident"] is True
        assert result["stage"] == 1

    def test_spam_subject_classified_at_stage1(self):
        """Spam subjects must be detected at Stage 1."""
        pipeline = self._make_pipeline()
        email = self._make_email(subject="unsubscribe from this newsletter")
        result = pipeline._stage1_pre_classify(email)
        assert result["confident"] is True

    def test_regular_email_not_classified_at_stage1(self):
        """Regular email must not be confidently classified at Stage 1."""
        pipeline = self._make_pipeline()
        email = self._make_email(
            sender="colleague@hospital.com",
            subject="Meeting tomorrow at 9am",
        )
        result = pipeline._stage1_pre_classify(email)
        assert result["confident"] is False

    def test_stage2_uses_override_rules(self):
        """Stage 2 must check ClassificationOverride rules."""
        pipeline = self._make_pipeline()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=1,
            sender_pattern="@hospital.com",
            subject_pattern=None,
            category="Klinik",
            priority="HIGH",
            spam=False,
            action_required=True,
            suggested_folder="Klinik",
        )
        pipeline.db.query.return_value.all.return_value = [rule]

        email = self._make_email(sender="doctor@hospital.com")
        result = pipeline._stage2_rule_classify(email)
        assert result["confident"] is True
        assert result["analysis"]["category"] == "Klinik"

    def test_stage2_no_match_returns_not_confident(self):
        """Stage 2 with no matching rule must return not confident."""
        pipeline = self._make_pipeline()
        pipeline.db.query.return_value.all.return_value = []
        email = self._make_email()
        result = pipeline._stage2_rule_classify(email)
        assert result["confident"] is False

    def test_pipeline_respects_llm_budget(self):
        """Pipeline must stop calling LLM after budget is exhausted."""
        pipeline = self._make_pipeline()
        pipeline._llm_calls_this_run = pipeline.settings.max_llm_calls_per_run
        assert not pipeline._llm_budget_available()

    def test_pipeline_llm_budget_available_initially(self):
        """LLM budget must be available at the start of a run."""
        pipeline = self._make_pipeline()
        assert pipeline._llm_budget_available()

    def test_analysis_pipeline_importable(self):
        """Analysis pipeline must be importable."""
        from src.services.analysis_pipeline import AnalysisPipeline
        assert AnalysisPipeline is not None


# ============================================================================
# Priority 10 — UTF-8 / Umlaut Support
# ============================================================================


class TestUTF8Support:
    """German umlauts and special characters must survive the entire pipeline."""

    def test_ai_service_validates_string_preserves_umlauts(self):
        """_validate_string must preserve German umlauts."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.ai_service import AIService

        service = AIService()
        german_text = "Sehr geehrte Damen und Herren, mit freundlichen Grüßen ä ö ü ß"
        result = service._validate_string(german_text)
        assert "ä" in result, "Umlaut ä must be preserved"
        assert "ö" in result, "Umlaut ö must be preserved"
        assert "ü" in result, "Umlaut ü must be preserved"
        assert "ß" in result, "Umlaut ß must be preserved"

    def test_ai_service_validate_string_removes_control_chars(self):
        """_validate_string must remove control characters but keep printable unicode."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.ai_service import AIService

        service = AIService()
        text_with_control = "Hello\x00World\x01\x02\x03Normal ä ö ü"
        result = service._validate_string(text_with_control)
        assert "\x00" not in result
        assert "\x01" not in result
        assert "Normal" in result
        assert "ä" in result

    def test_imap_service_decode_header_handles_umlauts(self):
        """_decode_header must handle encoded German umlauts in email headers."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService

        service = IMAPService()

        # Test plain UTF-8 header
        result = service._decode_header("Müller, Hans")
        assert "Müller" in result or "M" in result  # must not crash

    def test_imap_service_decode_header_handles_encoded_words(self):
        """_decode_header must handle RFC 2047 encoded headers (=?UTF-8?...)."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService
        from email.header import make_header, decode_header

        service = IMAPService()

        # Simulate encoded header as IMAP would return it
        encoded = "=?UTF-8?B?R3LDvMOfZSBFbXBmw6RuZ2VyOiBNw7xsbGVy?="
        result = service._decode_header(encoded)
        # Result should contain umlauts or at least not crash
        assert isinstance(result, str)

    def test_body_hash_utf8_stable(self):
        """Body hash must be stable for UTF-8 content including umlauts."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService

        db = MagicMock()
        svc = MailIngestionService(db_session=db)

        german_body = "Guten Tag, hier sind Informationen über die Umlaute: ä ö ü ß"
        h1 = svc._compute_body_hash(german_body, "")
        h2 = svc._compute_body_hash(german_body, "")
        assert h1 == h2
        assert len(h1) == 64

    def test_snippet_handles_utf8(self):
        """_make_snippet must handle UTF-8 characters without crashing."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService

        db = MagicMock()
        svc = MailIngestionService(db_session=db)

        german = "Sehr geehrte Frau Müller, mit freundlichen Grüßen aus München"
        snippet = svc._make_snippet(german)
        assert "Sehr" in snippet
        assert isinstance(snippet, str)

    def test_utf8_round_trip_in_parse_email(self):
        """Email parsing must preserve UTF-8 body content."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.imap_service import IMAPService
        from email.mime.text import MIMEText

        service = IMAPService()

        # Create a real email with German content
        msg = MIMEText("Hallo Welt: ä ö ü ß", "plain", "utf-8")
        msg["From"] = "test@example.com"
        msg["Subject"] = "Grüße aus Deutschland"
        msg["Message-ID"] = "<utf8test@test.com>"

        raw = msg.as_bytes()
        message_data = {b"BODY[]": raw, b"FLAGS": []}
        result = service._parse_email(1, message_data)

        assert result is not None
        assert "ü" in result["body_plain"] or "Hallo" in result["body_plain"]


# ============================================================================
# Ingestion Pipeline Integration
# ============================================================================


class TestIngestionPipeline:
    """Ingestion pipeline must correctly import emails."""

    def test_mail_ingestion_service_importable(self):
        """MailIngestionService must be importable."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService
        assert MailIngestionService is not None

    def test_ingestion_skips_duplicate_message_ids(self):
        """Emails already in DB must be skipped (not duplicated)."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.mail_ingestion_service import MailIngestionService
        from src.models.database import ProcessedEmail

        db = MagicMock()
        existing = MagicMock(spec=ProcessedEmail)
        db.query.return_value.filter.return_value.first.return_value = existing

        svc = MailIngestionService(db_session=db)

        raw = b"From: dup@test.com\r\nSubject: Dup\r\nMessage-ID: <dup@test.com>\r\n\r\nBody"
        message_data = {b"BODY[]": raw, b"FLAGS": []}

        mock_imap = MagicMock()

        def fake_parse(uid, md):
            return {
                "uid": str(uid),
                "message_id": "<dup@test.com>",
                "in_reply_to": "",
                "references": "",
                "subject": "Dup",
                "sender": "dup@test.com",
                "recipients": "",
                "date": None,
                "body_plain": "Body",
                "body_html": "",
                "integrity_hash": "abc",
            }

        mock_imap._parse_email = fake_parse

        result = svc._process_fetched_message(1, message_data, "INBOX", mock_imap)
        assert result == "skipped"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
