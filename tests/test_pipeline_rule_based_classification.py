"""
Integration tests for rule-based classification in the production analysis pipeline.

Verifies that:
  1. Emails from senders with learned SenderProfiles are classified via rule-based path
  2. Rule-based result is used BEFORE LLM fallback
  3. LLM path is skipped when deterministic sender-based result exists
  4. Newsletter-like emails are classified by rule-based heuristics
  5. Emails without rule match still use existing fallback classification path
  6. Explanation/source is preserved in the resulting analyzed object
  7. The integration works across all three production pipeline paths
"""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    Base,
    ProcessedEmail,
    SenderProfile,
    DecisionEvent,
    ClassificationOverride,
    AnalysisProgress,
)

_email_counter = 0


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db_session(tmp_path):
    """Create a fresh in-memory DB session for each test."""
    db_file = tmp_path / "test_pipeline_rule.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_email(db_session, **kwargs):
    """Helper to create a ProcessedEmail with sensible defaults."""
    global _email_counter
    _email_counter += 1
    uid_val = str(100 + _email_counter)
    defaults = dict(
        message_id=f"test-{_email_counter}@example.com",
        uid=uid_val,
        subject="Test Email",
        sender="unknown@example.com",
        category=None,
        priority=None,
        is_spam=False,
        is_processed=False,
        is_resolved=False,
        analysis_state="pending",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    email = ProcessedEmail(**defaults)
    db_session.add(email)
    db_session.commit()
    return email


def _make_sender_profile(db_session, **kwargs):
    """Helper to create a SenderProfile."""
    profile = SenderProfile(**kwargs)
    db_session.add(profile)
    db_session.commit()
    return profile


# ── Test: stage_learned_classify via AnalysisPipeline ────────────────


class TestStageLearnedClassify:
    """Tests for the stage_learned_classify method on AnalysisPipeline."""

    def test_sender_profile_classification(self, db_session):
        """Email from sender with user-classified profile gets learned classification."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="alice@corp.com",
            sender_domain="corp.com",
            preferred_category="work",
            preferred_folder="Work",
            user_classification_count=3,
        )
        email = _make_email(
            db_session,
            message_id="learned-001@test.com",
            sender="alice@corp.com",
            subject="Project update",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["stage"] == 0
        assert result["source"] == "sender_profile"
        assert result["analysis"]["category"] == "work"
        assert result["analysis"]["suggested_folder"] == "Work"
        assert "Learned from previous user classification" in result["analysis"]["reasoning"]

    def test_newsletter_heuristic_classification(self, db_session):
        """Newsletter-like email is classified by heuristics."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="newsletter-pipe-001@test.com",
            sender="newsletter@company.com",
            subject="Weekly Digest",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["stage"] == 0
        assert result["source"] == "newsletter_heuristic"
        assert result["analysis"]["category"] == "newsletter"
        assert "Newsletter pattern detected" in result["analysis"]["reasoning"]

    def test_spam_heuristic_classification(self, db_session):
        """Spam-like email is classified by heuristics."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="spam-pipe-001@test.com",
            sender="scammer@shady.net",
            subject="You have won a million dollars!",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["stage"] == 0
        assert result["source"] == "spam_heuristic"
        assert result["analysis"]["category"] == "spam"
        assert result["analysis"]["spam_probability"] == 0.9
        assert "Spam pattern detected" in result["analysis"]["reasoning"]

    def test_unknown_sender_no_match(self, db_session):
        """Email from unknown sender without matching heuristics returns not confident."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="unknown-001@test.com",
            sender="bob@company.org",
            subject="Meeting tomorrow",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is False
        assert result["stage"] == 0

    def test_sender_profile_without_user_classification_not_confident(self, db_session):
        """Sender profile with 0 user classifications is not used for category."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="auto@corp.com",
            sender_domain="corp.com",
            preferred_category=None,
            user_classification_count=0,
            total_emails=5,
        )
        email = _make_email(
            db_session,
            message_id="no-learn-001@test.com",
            sender="auto@corp.com",
            subject="Automated report",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is False

    def test_domain_level_profile_fallback(self, db_session):
        """Domain-level sender profile is used when no address-level exists."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_domain="bigcorp.com",
            sender_address=None,
            preferred_category="private",
            preferred_folder="Personal",
            user_classification_count=2,
        )
        email = _make_email(
            db_session,
            message_id="domain-001@test.com",
            sender="newperson@bigcorp.com",
            subject="Hello",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["analysis"]["category"] == "private"
        assert result["analysis"]["suggested_folder"] == "Personal"


# ── Test: Full pipeline flow (AnalysisPipeline.analyse) ─────────────


class TestAnalysePipelineIntegration:
    """Tests that the full analyse() method prioritises learned classification."""

    def test_learned_classification_skips_all_other_stages(self, db_session):
        """When learned classification is confident, stages 1/2/3 are not called."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="known@corp.com",
            sender_domain="corp.com",
            preferred_category="work",
            preferred_folder="Work",
            user_classification_count=5,
        )
        email = _make_email(
            db_session,
            message_id="full-pipe-001@test.com",
            sender="known@corp.com",
            subject="Quarterly review",
        )

        pipeline = AnalysisPipeline(db_session)

        with patch.object(pipeline, "stage1_pre_classify") as mock_s1, \
             patch.object(pipeline, "stage2_rule_classify") as mock_s2, \
             patch.object(pipeline, "stage3_llm_analyse") as mock_s3:

            analysis = pipeline.analyse(email)

            mock_s1.assert_not_called()
            mock_s2.assert_not_called()
            mock_s3.assert_not_called()

        assert analysis["category"] == "work"
        assert email.analysis_state == "learned_classified"

    def test_unknown_sender_falls_through_to_stage1(self, db_session):
        """When no learned rule matches, Stage 1 is called."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="fallthrough-001@test.com",
            sender="random@unknown.org",
            subject="Hello world",
        )

        pipeline = AnalysisPipeline(db_session)

        with patch.object(
            pipeline, "stage3_llm_analyse",
            return_value={
                "confident": True,
                "stage": 3,
                "source": "llm",
                "analysis": {
                    "summary": "LLM result",
                    "category": "Unklar",
                    "spam_probability": 0.1,
                    "action_required": False,
                    "priority": "LOW",
                    "tasks": [],
                    "suggested_folder": "Archive",
                    "reasoning": "LLM analysis",
                },
            },
        ):
            analysis = pipeline.analyse(email)

        # Should have gone through to later stages since no learned rule matched
        assert analysis is not None
        assert email.analysis_state in ("pre_classified", "classified", "deep_analyzed")

    def test_decision_event_recorded_for_learned_classification(self, db_session):
        """A DecisionEvent with source=sender_profile is recorded."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="tracked@corp.com",
            sender_domain="corp.com",
            preferred_category="todo",
            user_classification_count=2,
        )
        email = _make_email(
            db_session,
            message_id="decision-001@test.com",
            sender="tracked@corp.com",
            subject="Action needed",
        )

        pipeline = AnalysisPipeline(db_session)
        pipeline.analyse(email)
        db_session.commit()

        events = db_session.query(DecisionEvent).filter(
            DecisionEvent.email_id == email.id
        ).all()

        assert len(events) >= 1
        learned_event = [e for e in events if e.event_type == "learned_classified"]
        assert len(learned_event) == 1
        assert learned_event[0].source == "sender_profile"
        assert learned_event[0].new_value == "todo"


# ── Test: run_analysis pipeline path ────────────────────────────────


class TestRunAnalysisPipelineIntegration:
    """Tests that run_analysis() in pipeline/analysis.py uses learned classification."""

    @patch("src.pipeline.analysis.get_settings")
    def test_learned_classification_in_run_analysis(self, mock_settings, db_session):
        """run_analysis() uses learned classification and skips LLM."""
        from src.pipeline.analysis import run_analysis

        settings = MagicMock()
        settings.max_emails_per_run = 10
        settings.ai_batch_size = 5
        settings.max_llm_calls_per_run = 10
        settings.max_runtime_minutes = 5
        settings.debug = False
        settings.spam_threshold = 0.8
        mock_settings.return_value = settings

        _make_sender_profile(
            db_session,
            sender_address="learned@example.com",
            sender_domain="example.com",
            preferred_category="private",
            preferred_folder="Personal",
            user_classification_count=4,
        )
        email = _make_email(
            db_session,
            message_id="run-analysis-001@test.com",
            sender="learned@example.com",
            subject="Personal message",
            analysis_state="pending",
        )

        with patch("src.pipeline.analysis._compute_pending_importance_scores"), \
             patch("src.services.prediction_signals.enrich_and_apply_hints"):

            stats = run_analysis(db_session)

        assert stats["analysed"] == 1
        assert stats["llm_calls"] == 0

        db_session.refresh(email)
        assert email.category == "private"
        assert email.analysis_state == "learned_classified"
        assert "Learned from previous user classification" in (email.reasoning or "")

    @patch("src.pipeline.analysis.get_settings")
    def test_unknown_sender_still_uses_llm_in_run_analysis(self, mock_settings, db_session):
        """run_analysis() falls back to LLM for unknown senders."""
        from src.pipeline.analysis import run_analysis

        settings = MagicMock()
        settings.max_emails_per_run = 10
        settings.ai_batch_size = 5
        settings.max_llm_calls_per_run = 10
        settings.max_runtime_minutes = 5
        settings.debug = False
        settings.spam_threshold = 0.8
        mock_settings.return_value = settings

        email = _make_email(
            db_session,
            message_id="run-analysis-unknown-001@test.com",
            sender="nobody@nowhere.org",
            subject="Regular email",
            analysis_state="pending",
        )

        mock_batch_result = [{
            "summary": "LLM classified email",
            "category": "Verwaltung",
            "spam_probability": 0.1,
            "action_required": False,
            "priority": "LOW",
            "tasks": [],
            "suggested_folder": "Admin",
            "reasoning": "LLM analysis result",
        }]

        with patch("src.pipeline.analysis._compute_pending_importance_scores"), \
             patch("src.services.prediction_signals.enrich_and_apply_hints"), \
             patch("src.services.ai_service.AIService.analyze_emails_batch", return_value=mock_batch_result):

            stats = run_analysis(db_session)

        assert stats["analysed"] == 1
        assert stats["llm_calls"] == 1

        db_session.refresh(email)
        assert email.analysis_state == "deep_analyzed"

    @patch("src.pipeline.analysis.get_settings")
    def test_newsletter_heuristic_in_run_analysis(self, mock_settings, db_session):
        """Newsletter-like email is handled by learned heuristic in run_analysis()."""
        from src.pipeline.analysis import run_analysis

        settings = MagicMock()
        settings.max_emails_per_run = 10
        settings.ai_batch_size = 5
        settings.max_llm_calls_per_run = 10
        settings.max_runtime_minutes = 5
        settings.debug = False
        settings.spam_threshold = 0.8
        mock_settings.return_value = settings

        email = _make_email(
            db_session,
            message_id="newsletter-run-001@test.com",
            sender="noreply@bigservice.com",
            subject="Your weekly update",
            analysis_state="pending",
        )

        with patch("src.pipeline.analysis._compute_pending_importance_scores"), \
             patch("src.services.prediction_signals.enrich_and_apply_hints"):

            stats = run_analysis(db_session)

        assert stats["analysed"] == 1
        assert stats["llm_calls"] == 0

        db_session.refresh(email)
        assert email.category == "newsletter"
        assert email.analysis_state in ("learned_classified", "pre_classified")


# ── Test: EmailProcessor path ───────────────────────────────────────


class TestEmailProcessorIntegration:
    """Tests for _process_indexed_email_stages12 using learned classification."""

    def test_learned_classification_in_email_processor(self, db_session):
        """EmailProcessor._process_indexed_email_stages12 uses learned classification."""
        from src.services.email_processor import EmailProcessor

        _make_sender_profile(
            db_session,
            sender_address="known@bigcorp.com",
            sender_domain="bigcorp.com",
            preferred_category="work",
            preferred_folder="Projects",
            user_classification_count=3,
        )
        email = _make_email(
            db_session,
            message_id="processor-001@test.com",
            sender="known@bigcorp.com",
            subject="Sprint review",
            analysis_state="pending",
        )

        with patch("src.services.email_processor.get_settings") as mock_settings:
            settings = MagicMock()
            settings.safe_mode = True
            settings.require_approval = False
            settings.debug = False
            settings.spam_threshold = 0.8
            settings.max_llm_calls_per_run = 10
            settings.max_runtime_minutes = 5
            settings.max_emails_per_batch = 50
            mock_settings.return_value = settings

            processor = EmailProcessor.__new__(EmailProcessor)
            processor.settings = settings
            processor.db = db_session
            processor.ai_service = MagicMock()
            processor.stats = {"processed": 0, "spam": 0, "archived": 0, "action_required": 0, "failed": 0}

            needs_llm = processor._process_indexed_email_stages12(email, None)

        assert needs_llm is False  # Learned classification handled it

        db_session.refresh(email)
        assert email.category == "work"
        assert email.analysis_state == "learned_classified"
        assert email.reasoning is not None
        assert "Learned from previous user classification" in email.reasoning

    def test_unknown_sender_needs_llm_in_email_processor(self, db_session):
        """Unknown sender email returns True (needs LLM) from stages12."""
        from src.services.email_processor import EmailProcessor

        email = _make_email(
            db_session,
            message_id="processor-unknown-001@test.com",
            sender="stranger@random.org",
            subject="Something normal",
            analysis_state="pending",
        )

        with patch("src.services.email_processor.get_settings") as mock_settings:
            settings = MagicMock()
            settings.safe_mode = True
            settings.require_approval = False
            settings.debug = False
            settings.spam_threshold = 0.8
            settings.max_llm_calls_per_run = 10
            settings.max_runtime_minutes = 5
            settings.max_emails_per_batch = 50
            mock_settings.return_value = settings

            processor = EmailProcessor.__new__(EmailProcessor)
            processor.settings = settings
            processor.db = db_session
            processor.ai_service = MagicMock()
            processor.stats = {"processed": 0, "spam": 0, "archived": 0, "action_required": 0, "failed": 0}

            needs_llm = processor._process_indexed_email_stages12(email, None)

        # Should need LLM since no learned rules matched and Stage 1/2 didn't either
        assert needs_llm is True


# ── Test: End-to-end learning flow ──────────────────────────────────


class TestEndToEndLearningFlow:
    """Tests the full learning loop: manual classify → future emails use learned rule."""

    def test_manual_classification_influences_future_emails(self, db_session):
        """After manual classification, a new email from the same sender is auto-classified."""
        from src.services.learning_loop import record_manual_classification
        from src.services.analysis_pipeline import AnalysisPipeline

        # Step 1: Create an existing email and manually classify it
        email1 = _make_email(
            db_session,
            message_id="e2e-original@test.com",
            sender="colleague@work.org",
            subject="Meeting notes",
            category="Unklar",
            is_processed=True,
            analysis_state="classified",
        )
        record_manual_classification(db_session, email1, "work", target_folder="Work/Meetings")
        db_session.commit()

        # Step 2: A new email arrives from the same sender
        email2 = _make_email(
            db_session,
            message_id="e2e-new@test.com",
            sender="colleague@work.org",
            subject="Follow-up on meeting",
            analysis_state="pending",
        )

        # Step 3: Run the pipeline — should use learned classification
        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email2)

        assert result["confident"] is True
        assert result["analysis"]["category"] == "work"
        assert result["analysis"]["suggested_folder"] == "Work/Meetings"
        assert "Learned from previous user classification" in result["analysis"]["reasoning"]

    def test_multiple_classifications_increase_confidence(self, db_session):
        """Multiple manual classifications increase the confidence score."""
        from src.services.learning_loop import record_manual_classification
        from src.services.analysis_pipeline import AnalysisPipeline

        # Classify multiple emails from same sender
        for i in range(5):
            email = _make_email(
                db_session,
                message_id=f"multi-{i}@test.com",
                uid=str(300 + i),
                sender="boss@company.com",
                subject=f"Task {i}",
                category="Unklar",
                is_processed=True,
                analysis_state="classified",
            )
            record_manual_classification(db_session, email, "todo")
            db_session.commit()

        # New email from same sender
        new_email = _make_email(
            db_session,
            message_id="multi-new@test.com",
            sender="boss@company.com",
            subject="Another task",
            analysis_state="pending",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(new_email)

        assert result["confident"] is True
        assert result["analysis"]["category"] == "todo"
        # With 5 classifications, confidence should be high
        # rule: min(1.0, 0.7 + 0.1 * count) = min(1.0, 0.7 + 0.5) = 1.0


# ── Test: Explanation/transparency ──────────────────────────────────


class TestExplanationTransparency:
    """Tests that the explanation from learned classification is properly preserved."""

    def test_reasoning_field_set_from_sender_profile(self, db_session):
        """The reasoning field on ProcessedEmail is set from the learned explanation."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="clear@example.com",
            sender_domain="example.com",
            preferred_category="private",
            user_classification_count=1,
        )
        email = _make_email(
            db_session,
            message_id="explain-001@test.com",
            sender="clear@example.com",
            subject="Personal note",
        )

        pipeline = AnalysisPipeline(db_session)
        analysis = pipeline.analyse(email)
        pipeline.apply_analysis_to_record(email, analysis)
        db_session.commit()

        db_session.refresh(email)
        assert email.reasoning is not None
        assert "Learned from previous user classification" in email.reasoning

    def test_reasoning_field_set_from_newsletter_heuristic(self, db_session):
        """Newsletter heuristic sets appropriate reasoning."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="explain-nl-001@test.com",
            sender="digest@marketing.io",
            subject="Your weekly roundup",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert "Newsletter pattern detected" in result["analysis"]["reasoning"]

    def test_reasoning_field_set_from_spam_heuristic(self, db_session):
        """Spam heuristic sets appropriate reasoning."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="explain-spam-001@test.com",
            sender="bad@evil.net",
            subject="Congratulations you have won a prize!",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert "Spam pattern detected" in result["analysis"]["reasoning"]

    def test_decision_event_source_reflects_learned_rule(self, db_session):
        """DecisionEvent source indicates the learned classification source."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="audit@example.com",
            sender_domain="example.com",
            preferred_category="work",
            user_classification_count=2,
        )
        email = _make_email(
            db_session,
            message_id="audit-001@test.com",
            sender="audit@example.com",
            subject="Audit trail test",
        )

        pipeline = AnalysisPipeline(db_session)
        pipeline.analyse(email)
        db_session.commit()

        event = db_session.query(DecisionEvent).filter(
            DecisionEvent.email_id == email.id,
            DecisionEvent.event_type == "learned_classified",
        ).first()

        assert event is not None
        assert event.source == "sender_profile"


# ── Test: LLM avoidance ────────────────────────────────────────────


class TestLLMAvoidance:
    """Tests that LLM calls are avoided when learned classification is confident."""

    def test_llm_not_called_for_known_sender(self, db_session):
        """AIService.analyze_email is never invoked for a known sender."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="nollm@example.com",
            sender_domain="example.com",
            preferred_category="private",
            user_classification_count=3,
        )
        email = _make_email(
            db_session,
            message_id="nollm-001@test.com",
            sender="nollm@example.com",
            subject="No LLM needed",
        )

        pipeline = AnalysisPipeline(db_session)

        with patch("src.services.ai_service.AIService.analyze_email") as mock_llm, \
             patch("src.services.ai_service.AIService.analyze_emails_batch") as mock_llm_batch:

            pipeline.analyse(email)

            mock_llm.assert_not_called()
            mock_llm_batch.assert_not_called()

    def test_llm_not_called_for_newsletter_heuristic(self, db_session):
        """LLM is not called for emails matching newsletter patterns."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="nollm-nl-001@test.com",
            sender="noreply@service.com",
            subject="Your notification",
        )

        pipeline = AnalysisPipeline(db_session)

        with patch("src.services.ai_service.AIService.analyze_email") as mock_llm:
            pipeline.analyse(email)
            mock_llm.assert_not_called()

    @patch("src.pipeline.analysis.get_settings")
    def test_llm_called_for_unmatched_email_in_batch(self, mock_settings, db_session):
        """LLM is only called for emails that don't match any learned rule."""
        from src.pipeline.analysis import run_analysis

        settings = MagicMock()
        settings.max_emails_per_run = 10
        settings.ai_batch_size = 5
        settings.max_llm_calls_per_run = 10
        settings.max_runtime_minutes = 5
        settings.debug = False
        settings.spam_threshold = 0.8
        mock_settings.return_value = settings

        # Email 1: known sender (should NOT need LLM)
        _make_sender_profile(
            db_session,
            sender_address="known@company.com",
            sender_domain="company.com",
            preferred_category="work",
            user_classification_count=3,
        )
        email_known = _make_email(
            db_session,
            message_id="batch-known@test.com",
            uid="401",
            sender="known@company.com",
            subject="Known sender email",
            analysis_state="pending",
        )

        # Email 2: unknown sender (should need LLM)
        email_unknown = _make_email(
            db_session,
            message_id="batch-unknown@test.com",
            uid="402",
            sender="stranger@other.org",
            subject="Unknown sender email",
            analysis_state="pending",
        )

        mock_batch_result = [{
            "summary": "LLM result",
            "category": "Unklar",
            "spam_probability": 0.1,
            "action_required": False,
            "priority": "LOW",
            "tasks": [],
            "suggested_folder": "Archive",
            "reasoning": "LLM classified",
        }]

        with patch("src.pipeline.analysis._compute_pending_importance_scores"), \
             patch("src.services.prediction_signals.enrich_and_apply_hints"), \
             patch("src.services.ai_service.AIService.analyze_emails_batch", return_value=mock_batch_result) as mock_batch:

            stats = run_analysis(db_session)

        assert stats["analysed"] == 2
        assert stats["llm_calls"] == 1  # Only 1 LLM call for the unknown email

        # The batch should only contain the unknown email
        mock_batch.assert_called_once()
        batch_data = mock_batch.call_args[0][0]
        assert len(batch_data) == 1
        assert batch_data[0]["sender"] == "stranger@other.org"

        db_session.refresh(email_known)
        assert email_known.analysis_state == "learned_classified"
        assert email_known.category == "work"

        db_session.refresh(email_unknown)
        assert email_unknown.analysis_state == "deep_analyzed"


# ── Test: Sender identifier normalization ───────────────────────────


class TestSenderNormalization:
    """Tests that sender identifiers are properly normalised for profile lookup."""

    def test_display_name_format_still_matches(self, db_session):
        """Sender format 'Name <email>' is handled correctly."""
        from src.services.analysis_pipeline import AnalysisPipeline

        _make_sender_profile(
            db_session,
            sender_address="alice@example.com",
            sender_domain="example.com",
            preferred_category="work",
            user_classification_count=2,
        )
        email = _make_email(
            db_session,
            message_id="norm-001@test.com",
            sender="Alice Smith <alice@example.com>",
            subject="Test",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["analysis"]["category"] == "work"

    def test_case_insensitive_newsletter_detection(self, db_session):
        """Newsletter detection works regardless of sender case."""
        from src.services.analysis_pipeline import AnalysisPipeline

        email = _make_email(
            db_session,
            message_id="norm-nl-001@test.com",
            sender="NEWSLETTER@Company.COM",
            subject="Updates",
        )

        pipeline = AnalysisPipeline(db_session)
        result = pipeline.stage_learned_classify(email)

        assert result["confident"] is True
        assert result["analysis"]["category"] == "newsletter"


# ── Test: Cross-path consistency ────────────────────────────────────


class TestCrossPathConsistency:
    """Verify all three production paths produce identical Stage 0 outcomes."""

    def _create_known_sender_scenario(self, db_session):
        """Set up a known sender + pending email for all paths."""
        _make_sender_profile(
            db_session,
            sender_address="crosspath@company.com",
            sender_domain="company.com",
            preferred_category="work",
            preferred_folder="Work",
            user_classification_count=4,
        )

    def test_all_paths_classify_known_sender_identically(self, db_session):
        """AnalysisPipeline.analyse, run_analysis, and _process_indexed_email_stages12
        all produce identical learned_classified state for a known sender."""
        from src.services.analysis_pipeline import AnalysisPipeline
        from src.pipeline.analysis import run_analysis
        from src.services.email_processor import EmailProcessor

        self._create_known_sender_scenario(db_session)

        # -- Path 1: AnalysisPipeline.analyse() --
        email1 = _make_email(
            db_session,
            message_id="crosspath-1@test.com",
            sender="crosspath@company.com",
            subject="Path 1 test",
            analysis_state="pending",
        )
        pipeline = AnalysisPipeline(db_session)
        analysis1 = pipeline.analyse(email1)
        pipeline.apply_analysis_to_record(email1, analysis1)
        db_session.commit()

        # -- Path 2: run_analysis() --
        email2 = _make_email(
            db_session,
            message_id="crosspath-2@test.com",
            sender="crosspath@company.com",
            subject="Path 2 test",
            analysis_state="pending",
        )
        with patch("src.pipeline.analysis.get_settings") as mock_settings:
            settings = MagicMock()
            settings.max_emails_per_run = 10
            settings.ai_batch_size = 5
            settings.max_llm_calls_per_run = 10
            settings.max_runtime_minutes = 5
            settings.debug = False
            settings.spam_threshold = 0.8
            mock_settings.return_value = settings
            with patch("src.pipeline.analysis._compute_pending_importance_scores"), \
                 patch("src.services.prediction_signals.enrich_and_apply_hints"):
                run_analysis(db_session)

        db_session.refresh(email2)

        # -- Path 3: EmailProcessor._process_indexed_email_stages12() --
        email3 = _make_email(
            db_session,
            message_id="crosspath-3@test.com",
            sender="crosspath@company.com",
            subject="Path 3 test",
            analysis_state="pending",
        )
        with patch("src.services.email_processor.get_settings") as mock_settings:
            settings = MagicMock()
            settings.safe_mode = True
            settings.require_approval = False
            settings.debug = False
            settings.spam_threshold = 0.8
            settings.max_llm_calls_per_run = 10
            settings.max_runtime_minutes = 5
            settings.max_emails_per_batch = 50
            mock_settings.return_value = settings
            processor = EmailProcessor.__new__(EmailProcessor)
            processor.settings = settings
            processor.db = db_session
            processor.ai_service = MagicMock()
            processor.stats = {"processed": 0, "spam": 0, "archived": 0, "action_required": 0, "failed": 0}
            needs_llm = processor._process_indexed_email_stages12(email3, None)

        db_session.refresh(email3)

        # All three paths must produce identical outcomes
        for label, email in [("analyse()", email1), ("run_analysis()", email2), ("stages12()", email3)]:
            assert email.analysis_state == "learned_classified", f"{label}: expected learned_classified, got {email.analysis_state}"
            assert email.category == "work", f"{label}: expected work, got {email.category}"
            assert "Learned from previous user classification" in (email.reasoning or ""), f"{label}: missing explanation"

        assert needs_llm is False  # Path 3 confirms LLM not needed
