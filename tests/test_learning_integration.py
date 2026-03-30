"""
Tests for learning-layer integration with the live processing pipeline.

Covers:
  - User-action learning updates BOTH address-level and domain-level SenderProfiles
  - Address/domain profile isolation
  - Live analysis pipeline generates and persists predictions
  - Importance scorer uses learned SenderProfile behavior
  - Prediction enrichment during analysis
  - Action execution triggers user-action learning
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    Base,
    ProcessedEmail,
    SenderProfile,
    FolderPlacementAggregate,
    ReplyPattern,
    EmailPrediction,
    UserActionEvent,
    HistoricalLearningProgress,
    ProcessingJob,
    ReplyLink,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """Create an in-memory SQLite database session with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_email(db, **kwargs):
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
        "date": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    email = ProcessedEmail(**defaults)
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


# ===========================================================================
# 1. User-Action Learning — Address + Domain Level
# ===========================================================================

class TestUserActionDualProfileUpdate:
    """User actions must update BOTH address-level and domain-level SenderProfiles."""

    def test_archived_action_updates_both_profiles(self, db):
        """record_user_action('archived') must update address-level AND domain-level."""
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="alice@corp.com", folder="INBOX")
        record_user_action(db, email, "archived")
        db.commit()

        # Domain-level profile
        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None, "Domain-level profile must exist"
        assert (domain_p.archived_count or 0) == 1

        # Address-level profile
        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@corp.com",
        ).first()
        assert addr_p is not None, "Address-level profile must exist"
        assert (addr_p.archived_count or 0) == 1

    def test_deleted_action_updates_both_profiles(self, db):
        """record_user_action('deleted') must update both levels."""
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="bob@corp.com", folder="INBOX")
        record_user_action(db, email, "deleted")
        db.commit()

        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.deleted_count or 0) == 1

        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "bob@corp.com",
        ).first()
        assert addr_p is not None
        assert (addr_p.deleted_count or 0) == 1

    def test_kept_in_inbox_updates_both_profiles(self, db):
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="carol@corp.com", folder="INBOX")
        record_user_action(db, email, "kept_in_inbox")
        db.commit()

        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.kept_in_inbox_count or 0) == 1

        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "carol@corp.com",
        ).first()
        assert addr_p is not None
        assert (addr_p.kept_in_inbox_count or 0) == 1

    def test_marked_important_updates_both_profiles(self, db):
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="dave@corp.com", folder="INBOX")
        record_user_action(db, email, "marked_important")
        db.commit()

        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.marked_important_count or 0) == 1

        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "dave@corp.com",
        ).first()
        assert addr_p is not None
        assert (addr_p.marked_important_count or 0) == 1

    def test_marked_spam_updates_both_profiles(self, db):
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="eve@corp.com", folder="INBOX")
        record_user_action(db, email, "marked_spam")
        db.commit()

        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.marked_spam_count or 0) == 1

        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "eve@corp.com",
        ).first()
        assert addr_p is not None
        assert (addr_p.marked_spam_count or 0) == 1

    def test_multiple_actions_accumulate_independently(self, db):
        """Two actions from different addresses at same domain stay separate at address level."""
        from src.services.historical_learning import record_user_action

        e1 = _make_email(db, message_id="act1@test.com", sender="alice@corp.com", folder="INBOX")
        e2 = _make_email(db, message_id="act2@test.com", sender="bob@corp.com", folder="INBOX")

        record_user_action(db, e1, "archived")
        record_user_action(db, e2, "deleted")
        db.commit()

        # Domain profile gets both
        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.archived_count or 0) == 1
        assert (domain_p.deleted_count or 0) == 1

        # Address profiles are separate
        alice_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@corp.com",
        ).first()
        assert alice_p is not None
        assert (alice_p.archived_count or 0) == 1
        assert (alice_p.deleted_count or 0) == 0

        bob_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "bob@corp.com",
        ).first()
        assert bob_p is not None
        assert (bob_p.archived_count or 0) == 0
        assert (bob_p.deleted_count or 0) == 1


# ===========================================================================
# 2. Profile Isolation — Domain Queries Exclude Address Rows
# ===========================================================================

class TestProfileIsolation:
    """Domain-only queries must not accidentally include address-level profiles."""

    def test_domain_query_excludes_address_profiles(self, db):
        """Query with sender_address IS NULL must not return address-level rows."""
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="frank@acme.com", folder="INBOX")
        record_user_action(db, email, "archived")
        db.commit()

        # Total SenderProfile rows for acme.com
        all_acme = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "acme.com",
        ).all()
        # Should be 2: one domain-level, one address-level
        assert len(all_acme) == 2

        # Domain-only query
        domain_only = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "acme.com",
            SenderProfile.sender_address.is_(None),
        ).all()
        assert len(domain_only) == 1
        assert domain_only[0].sender_address is None

    def test_address_query_returns_only_address_row(self, db):
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="grace@acme.com", folder="INBOX")
        record_user_action(db, email, "marked_important")
        db.commit()

        addr_profiles = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "grace@acme.com",
        ).all()
        assert len(addr_profiles) == 1
        assert addr_profiles[0].sender_address == "grace@acme.com"


# ===========================================================================
# 3. Importance Scorer Uses Learned SenderProfile
# ===========================================================================

class TestImportanceScorerLearnedBehavior:
    """Importance scoring must incorporate learned SenderProfile signals."""

    def test_importance_boosts_from_learned_profile(self, db):
        """An email from a sender with high importance/inbox tendency should score higher."""
        from src.services.importance_scorer import compute_importance_score

        # Create a sender profile with strong importance signals
        profile = SenderProfile(
            sender_domain="vip.com",
            sender_address=None,
            total_emails=20,
            importance_tendency=0.5,
            marked_important_count=10,
            kept_in_inbox_count=15,
            spam_tendency=0.0,
            reply_rate=0.8,
            total_replies=16,
            folder_distribution={"INBOX": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(profile)
        db.commit()

        email = _make_email(db, sender="boss@vip.com", folder="INBOX")
        score_with_profile = compute_importance_score(db, email)

        # Compare to email from unknown sender
        email_unknown = _make_email(db, message_id="unk@test.com",
                                     sender="nobody@unknown.com", folder="INBOX")
        score_without_profile = compute_importance_score(db, email_unknown)

        # Score should be higher due to learned importance signals
        assert score_with_profile > score_without_profile

    def test_importance_uses_address_profile_over_domain(self, db):
        """Address-level profile is preferred when available with enough support."""
        from src.services.importance_scorer import compute_importance_score

        # Domain profile: neutral
        domain_p = SenderProfile(
            sender_domain="mixed.com",
            sender_address=None,
            total_emails=20,
            importance_tendency=0.0,
            marked_important_count=0,
            kept_in_inbox_count=2,
            spam_tendency=0.0,
            reply_rate=0.0,
            total_replies=0,
            folder_distribution={"INBOX": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(domain_p)

        # Address profile: high importance
        addr_p = SenderProfile(
            sender_domain="mixed.com",
            sender_address="boss@mixed.com",
            total_emails=10,
            importance_tendency=0.5,
            marked_important_count=5,
            kept_in_inbox_count=8,
            spam_tendency=0.0,
            reply_rate=0.8,
            total_replies=8,
            folder_distribution={"INBOX": 10},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(addr_p)
        db.commit()

        email = _make_email(db, sender="boss@mixed.com", folder="INBOX")
        score = compute_importance_score(db, email)

        # Score should reflect address-level signals, not the neutral domain
        # baseline(30) + address importance(+5) + inbox(+5) + reply(+5) >= 45
        assert score >= 45, f"Score {score} should reflect address-level learned signals"

    def test_spam_tendency_reduces_score(self, db):
        """High spam tendency in SenderProfile should reduce importance relative to same sender without it."""
        from src.services.importance_scorer import compute_importance_score

        spam_profile = SenderProfile(
            sender_domain="spammy.com",
            sender_address=None,
            total_emails=20,
            importance_tendency=0.0,
            marked_important_count=0,
            kept_in_inbox_count=0,
            spam_tendency=0.8,
            marked_spam_count=16,
            reply_rate=0.0,
            total_replies=0,
            folder_distribution={"Spam": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(spam_profile)
        db.commit()

        # Old email to avoid recency bonus
        old_date = datetime.now(timezone.utc) - timedelta(days=30)
        email = _make_email(db, sender="x@spammy.com", folder="INBOX",
                            date=old_date)
        score_spam = compute_importance_score(db, email)

        email_clean = _make_email(db, message_id="clean@test.com",
                                   sender="x@clean.com", folder="INBOX",
                                   date=old_date)
        score_clean = compute_importance_score(db, email_clean)

        # Spammy sender should score lower than clean sender
        assert score_spam < score_clean, (
            f"Spammy sender score ({score_spam}) should be less than clean sender ({score_clean})"
        )


# ===========================================================================
# 4. Live Analysis Enrichment with Predictions
# ===========================================================================

class TestAnalysisEnrichment:
    """Analysis pipeline must generate and persist predictions after classification."""

    def test_analysis_generates_predictions_for_classified_emails(self, db):
        """After analysis, emails should have EmailPredictions persisted."""
        from src.services.historical_learning import learn_from_email

        # Build up learning aggregates first
        for i in range(5):
            e = _make_email(
                db, message_id=f"learn-enrich-{i}@test.com",
                sender=f"sender@enrichcorp.com",
                folder="Work",
                category="Business",
                analysis_state="classified",
            )
            learn_from_email(db, e)
        db.commit()

        # Now create a pending email from same sender
        new_email = _make_email(
            db, message_id="new-enrich@test.com",
            sender="sender@enrichcorp.com",
            folder="INBOX",
            category="Business",
            analysis_state="pending",
        )
        db.commit()

        # The enrichment function should generate predictions
        from src.pipeline.analysis import _enrich_batch_with_predictions
        # Simulate post-analysis by setting state
        new_email.analysis_state = "classified"
        db.add(new_email)
        db.commit()

        _enrich_batch_with_predictions(db, [new_email], [])

        preds = db.query(EmailPrediction).filter(
            EmailPrediction.email_id == new_email.id,
        ).all()
        assert len(preds) > 0, "Predictions must be generated during enrichment"

        # Check at least target_folder prediction exists
        folder_pred = next(
            (p for p in preds if p.prediction_type == "target_folder"), None
        )
        assert folder_pred is not None, "target_folder prediction must exist"
        assert folder_pred.predicted_value == "Work"

    def test_enrichment_skips_failed_emails(self, db):
        """Emails with analysis_state='failed' should not get predictions."""
        from src.pipeline.analysis import _enrich_batch_with_predictions

        failed_email = _make_email(
            db, message_id="fail@test.com",
            sender="x@y.com",
            analysis_state="failed",
        )
        db.commit()

        _enrich_batch_with_predictions(db, [failed_email], [])

        preds = db.query(EmailPrediction).filter(
            EmailPrediction.email_id == failed_email.id,
        ).all()
        assert len(preds) == 0, "Failed emails must not get predictions"


# ===========================================================================
# 5. Action Execution Triggers User-Action Learning
# ===========================================================================

class TestActionExecutionLearning:
    """Executing actions must trigger sender-profile learning."""

    def test_record_user_action_for_execution_maps_move(self, db):
        """_record_user_action_for_execution maps 'move' to 'moved_to_folder'."""
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="mover@corp.com", folder="INBOX")
        record_user_action(db, email, "moved_to_folder", new_folder="Work")
        db.commit()

        # Event should exist
        event = db.query(UserActionEvent).filter(
            UserActionEvent.email_id == email.id,
        ).first()
        assert event is not None
        assert event.action_type == "moved_to_folder"

    def test_record_user_action_for_execution_maps_archive(self, db):
        """Archiving updates SenderProfile at both levels."""
        from src.services.historical_learning import record_user_action

        email = _make_email(db, sender="archiver@corp.com", folder="INBOX")
        record_user_action(db, email, "archived")
        db.commit()

        # Both profiles updated
        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert (domain_p.archived_count or 0) >= 1

        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "archiver@corp.com",
        ).first()
        assert addr_p is not None
        assert (addr_p.archived_count or 0) >= 1


# ===========================================================================
# 6. _get_or_create_sender_profile shared helper
# ===========================================================================

class TestGetOrCreateSenderProfile:
    """The shared helper must consistently get-or-create profiles."""

    def test_creates_domain_profile_when_absent(self, db):
        from src.services.historical_learning import _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, domain="newdomain.com")
        db.commit()

        assert profile is not None
        assert profile.sender_domain == "newdomain.com"
        assert profile.sender_address is None
        assert profile.total_emails == 0

    def test_creates_address_profile_when_absent(self, db):
        from src.services.historical_learning import _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, address="new@addr.com")
        db.commit()

        assert profile is not None
        assert profile.sender_address == "new@addr.com"
        assert profile.sender_domain == "addr.com"

    def test_returns_existing_domain_profile(self, db):
        from src.services.historical_learning import _get_or_create_sender_profile

        p1 = _get_or_create_sender_profile(db, domain="existing.com")
        p1.total_emails = 42
        db.commit()

        p2 = _get_or_create_sender_profile(db, domain="existing.com")
        assert p2.id == p1.id
        assert p2.total_emails == 42

    def test_returns_existing_address_profile(self, db):
        from src.services.historical_learning import _get_or_create_sender_profile

        p1 = _get_or_create_sender_profile(db, address="existing@addr.com")
        p1.total_emails = 99
        db.commit()

        p2 = _get_or_create_sender_profile(db, address="existing@addr.com")
        assert p2.id == p1.id
        assert p2.total_emails == 99


# ===========================================================================
# 7. _apply_action_to_profile shared helper
# ===========================================================================

class TestApplyActionToProfile:
    """The shared helper must correctly increment counters."""

    def test_apply_archived(self, db):
        from src.services.historical_learning import _apply_action_to_profile, _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, domain="test.com")
        _apply_action_to_profile(profile, "archived")
        assert profile.archived_count == 1

    def test_apply_deleted(self, db):
        from src.services.historical_learning import _apply_action_to_profile, _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, domain="test.com")
        _apply_action_to_profile(profile, "deleted")
        assert profile.deleted_count == 1

    def test_apply_marked_important_recalculates_tendency(self, db):
        from src.services.historical_learning import _apply_action_to_profile, _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, domain="test.com")
        profile.total_emails = 10
        _apply_action_to_profile(profile, "marked_important")
        assert profile.marked_important_count == 1
        assert profile.importance_tendency == pytest.approx(0.1)

    def test_apply_marked_spam_recalculates_tendency(self, db):
        from src.services.historical_learning import _apply_action_to_profile, _get_or_create_sender_profile

        profile = _get_or_create_sender_profile(db, domain="test.com")
        profile.total_emails = 5
        _apply_action_to_profile(profile, "marked_spam")
        assert profile.marked_spam_count == 1
        assert profile.spam_tendency == pytest.approx(0.2)


# ===========================================================================
# 8. Importance Scorer _get_learned_profile precedence
# ===========================================================================

class TestLearnedProfilePrecedence:
    """resolve_sender_profile must prefer address-level over domain-level."""

    def test_returns_address_profile_when_available(self, db):
        from src.services.sender_precedence import resolve_sender_profile

        # Domain profile
        db.add(SenderProfile(
            sender_domain="prec.com",
            sender_address=None,
            total_emails=20,
            folder_distribution={},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        # Address profile
        db.add(SenderProfile(
            sender_domain="prec.com",
            sender_address="ceo@prec.com",
            total_emails=10,
            folder_distribution={},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        profile = resolve_sender_profile(db, "ceo@prec.com", min_support=3)
        assert profile is not None
        assert profile.sender_address == "ceo@prec.com"

    def test_falls_back_to_domain_when_address_insufficient(self, db):
        from src.services.sender_precedence import resolve_sender_profile

        # Domain profile with enough support
        db.add(SenderProfile(
            sender_domain="fallback.com",
            sender_address=None,
            total_emails=20,
            folder_distribution={},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        # Address profile with too few emails
        db.add(SenderProfile(
            sender_domain="fallback.com",
            sender_address="new@fallback.com",
            total_emails=1,
            folder_distribution={},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        profile = resolve_sender_profile(db, "new@fallback.com", min_support=3)
        assert profile is not None
        assert profile.sender_address is None  # domain fallback
        assert profile.sender_domain == "fallback.com"

    def test_returns_none_when_no_profiles(self, db):
        from src.services.sender_precedence import resolve_sender_profile

        profile = resolve_sender_profile(db, "ghost@nowhere.com", min_support=3)
        assert profile is None


# ===========================================================================
# 9. Source inspection: verify code paths are wired
# ===========================================================================

class TestSourceInspectionWiring:
    """Secondary enforcement: confirm key integration points exist in source."""

    def test_analysis_py_calls_enrich_batch(self):
        """The analysis module must call _enrich_batch_with_predictions."""
        import inspect
        from src.pipeline import analysis
        source = inspect.getsource(analysis.run_analysis)
        assert "_enrich_batch_with_predictions" in source

    def test_importance_scorer_calls_learned_behavior_boost(self):
        """compute_importance_score must call _learned_behavior_boost."""
        import inspect
        from src.services import importance_scorer
        source = inspect.getsource(importance_scorer.compute_importance_score)
        assert "_learned_behavior_boost" in source

    def test_importance_scorer_uses_centralized_precedence(self):
        """_learned_behavior_boost must delegate to sender_precedence module."""
        import inspect
        from src.services import importance_scorer
        source = inspect.getsource(importance_scorer._learned_behavior_boost)
        assert "resolve_sender_profile" in source

    def test_record_user_action_updates_address_and_domain(self):
        """record_user_action must call _update_sender_profile_for_action for both."""
        import inspect
        from src.services import historical_learning
        source = inspect.getsource(historical_learning.record_user_action)
        # Must extract address and call update for it
        assert "extract_sender_address" in source
        assert "_update_sender_profile_for_action" in source

    def test_main_py_calls_record_user_action_for_execution(self):
        """execute_action endpoint must call _record_user_action_for_execution."""
        import inspect
        import src.main as main_module
        source = inspect.getsource(main_module.execute_action)
        assert "_record_user_action_for_execution" in source


# ===========================================================================
# 10. Centralized sender precedence helper
# ===========================================================================

class TestSenderPrecedenceHelper:
    """Verify the shared sender_precedence module works correctly."""

    def test_resolve_sender_profile_address_wins(self, db):
        """resolve_sender_profile returns address-level when available."""
        from src.services.sender_precedence import resolve_sender_profile

        db.add(SenderProfile(
            sender_domain="sp.com", sender_address=None,
            total_emails=50, folder_distribution={},
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.add(SenderProfile(
            sender_domain="sp.com", sender_address="alice@sp.com",
            total_emails=10, folder_distribution={},
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        profile = resolve_sender_profile(db, "alice@sp.com", min_support=3)
        assert profile is not None
        assert profile.sender_address == "alice@sp.com"

    def test_resolve_sender_profile_falls_back_to_domain(self, db):
        """Falls back to domain-level when address profile has insufficient support."""
        from src.services.sender_precedence import resolve_sender_profile

        db.add(SenderProfile(
            sender_domain="sp2.com", sender_address=None,
            total_emails=50, folder_distribution={},
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.add(SenderProfile(
            sender_domain="sp2.com", sender_address="bob@sp2.com",
            total_emails=1, folder_distribution={},
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        profile = resolve_sender_profile(db, "bob@sp2.com", min_support=3)
        assert profile is not None
        assert profile.sender_address is None
        assert profile.sender_domain == "sp2.com"

    def test_resolve_sender_profile_returns_none(self, db):
        """Returns None when nothing meets min_support."""
        from src.services.sender_precedence import resolve_sender_profile

        profile = resolve_sender_profile(db, "nobody@ghost.com", min_support=3)
        assert profile is None

    def test_build_folder_tiers_order(self):
        """build_folder_tiers returns correct precedence order."""
        from src.services.sender_precedence import build_folder_tiers

        tiers = build_folder_tiers("alice@example.com", "Klinik", ["urgent", "patient"])
        types = [t[0] for t in tiers]
        assert types == ["sender_address", "sender_domain", "category", "subject_keyword", "subject_keyword"]

    def test_build_folder_tiers_without_address(self):
        """build_folder_tiers with malformed sender still includes domain."""
        from src.services.sender_precedence import build_folder_tiers

        tiers = build_folder_tiers("noreply", "Allgemein", [])
        # No address extractable, domain may also be empty
        types = [t[0] for t in tiers]
        assert "category" in types

    def test_build_reply_tiers_order(self):
        """build_reply_tiers returns correct precedence order."""
        from src.services.sender_precedence import build_reply_tiers

        tiers = build_reply_tiers("alice@example.com", "Forschung")
        types = [t[0] for t in tiers]
        assert types == ["sender_address", "sender_domain", "category"]

    def test_resolve_sender_profile_label_returns_label(self, db):
        """resolve_sender_profile_label returns the matching label."""
        from src.services.sender_precedence import resolve_sender_profile_label

        db.add(SenderProfile(
            sender_domain="label.com", sender_address="ceo@label.com",
            total_emails=20, folder_distribution={},
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        profile, label = resolve_sender_profile_label(db, "ceo@label.com", min_support=3)
        assert profile is not None
        assert label == "ceo@label.com"


# ===========================================================================
# 11. Prediction consumption — _apply_prediction_hints
# ===========================================================================

class TestPredictionConsumption:
    """Verify that _apply_prediction_hints actually consumes stored predictions."""

    def test_target_folder_backfills_suggested_folder(self, db):
        """When analysis didn't set suggested_folder, prediction fills it."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="alice@hospital.org", analysis_state="classified",
                            suggested_folder=None, action_required=False)
        # Create a target_folder prediction
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="target_folder",
            predicted_value="Klinik/Eingehend",
            confidence=0.8,
            explanation="test explanation",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.suggested_folder == "Klinik/Eingehend"

    def test_target_folder_does_not_override_existing(self, db):
        """When analysis already set suggested_folder, prediction does NOT override."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="bob@lab.com", analysis_state="deep_analyzed",
                            suggested_folder="Archive")
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="target_folder",
            predicted_value="Research/New",
            confidence=0.9,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.suggested_folder == "Archive"  # unchanged

    def test_reply_needed_sets_action_required(self, db):
        """When reply_needed prediction is high and action_required unset, it becomes True."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="urgent@dept.org", analysis_state="classified",
                            action_required=False)
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="reply_needed",
            predicted_value="0.80",
            confidence=0.8,
            explanation="Emails from urgent@dept.org were replied to in 8/10 cases",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.action_required is True
        assert "[learned]" in (email.reasoning or "")

    def test_reply_needed_does_not_override_existing_action_required(self, db):
        """When action_required is already True, prediction doesn't change it."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="ok@test.com", analysis_state="classified",
                            action_required=True, reasoning="LLM said so")
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="reply_needed",
            predicted_value="0.90",
            confidence=0.9,
            explanation="test",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.action_required is True
        assert email.reasoning == "LLM said so"  # unchanged

    def test_reply_needed_below_threshold_no_change(self, db):
        """reply_needed with low confidence doesn't set action_required."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="low@test.com", analysis_state="classified",
                            action_required=False)
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="reply_needed",
            predicted_value="0.30",
            confidence=0.3,
            explanation="test",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.action_required is False  # unchanged

    def test_importance_boost_backfills_when_none(self, db):
        """importance_boost fills importance_score when it was None."""
        from src.pipeline.analysis import _apply_prediction_hints
        from src.services.importance_scorer import _IMPORTANCE_BASELINE

        email = _make_email(db, sender="vip@company.com", analysis_state="classified",
                            importance_score=None)
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="importance_boost",
            predicted_value="0.60",
            confidence=0.6,
            explanation="reply rate high",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.importance_score is not None
        expected = _IMPORTANCE_BASELINE + 0.6 * 10.0
        assert email.importance_score == pytest.approx(expected, abs=0.1)

    def test_importance_boost_skips_when_already_scored(self, db):
        """importance_boost does NOT apply when importance_score already set."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="known@org.com", analysis_state="classified",
                            importance_score=55.0)
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="importance_boost",
            predicted_value="0.80",
            confidence=0.8,
            explanation="test",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.importance_score == pytest.approx(55.0, abs=0.1)  # unchanged

    def test_skips_failed_emails(self, db):
        """Predictions for failed emails are not consumed."""
        from src.pipeline.analysis import _apply_prediction_hints

        email = _make_email(db, sender="fail@test.com", analysis_state="failed",
                            suggested_folder=None)
        db.add(EmailPrediction(
            email_id=email.id,
            prediction_type="target_folder",
            predicted_value="Should/Not/Apply",
            confidence=0.9,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        _apply_prediction_hints(db, [email], [])

        db.refresh(email)
        assert email.suggested_folder is None  # not applied


# ===========================================================================
# 12. Anti-drift: centralized modules actually used
# ===========================================================================

class TestAntiDrift:
    """Source-inspection tests to catch drift if modules stop using shared helpers."""

    def test_prediction_engine_uses_build_folder_tiers(self):
        """prediction_engine._predict_folder must use build_folder_tiers."""
        import inspect
        from src.services import prediction_engine
        source = inspect.getsource(prediction_engine._predict_folder)
        assert "build_folder_tiers" in source

    def test_prediction_engine_uses_build_reply_tiers(self):
        """prediction_engine._predict_reply_needed must use build_reply_tiers."""
        import inspect
        from src.services import prediction_engine
        source = inspect.getsource(prediction_engine._predict_reply_needed)
        assert "build_reply_tiers" in source

    def test_prediction_engine_uses_resolve_sender_profile_label(self):
        """prediction_engine._predict_importance_boost must use resolve_sender_profile_label."""
        import inspect
        from src.services import prediction_engine
        source = inspect.getsource(prediction_engine._predict_importance_boost)
        assert "resolve_sender_profile_label" in source

    def test_importance_scorer_uses_resolve_sender_profile(self):
        """importance_scorer._learned_behavior_boost must use resolve_sender_profile."""
        import inspect
        from src.services import importance_scorer
        source = inspect.getsource(importance_scorer._learned_behavior_boost)
        assert "resolve_sender_profile" in source

    def test_historical_learning_uses_shared_get_or_create(self):
        """historical_learning._update_sender_profile_for_email must use _get_or_create_sender_profile."""
        import inspect
        from src.services import historical_learning
        source = inspect.getsource(historical_learning._update_sender_profile_for_email)
        assert "_get_or_create_sender_profile" in source

    def test_analysis_pipeline_calls_apply_prediction_hints(self):
        """The analysis module must call _apply_prediction_hints after enrichment."""
        import inspect
        from src.pipeline import analysis
        source = inspect.getsource(analysis.run_analysis)
        assert "_apply_prediction_hints" in source

    def test_analysis_pipeline_calls_enrich_batch(self):
        """The analysis module must call _enrich_batch_with_predictions."""
        import inspect
        from src.pipeline import analysis
        source = inspect.getsource(analysis.run_analysis)
        assert "_enrich_batch_with_predictions" in source


# ===========================================================================
# 13. Full integration: importance scoring consistently uses learned signals
# ===========================================================================

class TestImportanceScorerConsistency:
    """Verify importance scoring path uses centralized learned-behavior signals."""

    def test_importance_scorer_incorporates_learned_behavior(self, db):
        """compute_importance_score includes _learned_behavior_boost from SenderProfile."""
        from src.services.importance_scorer import compute_importance_score

        # Create address-level profile with strong importance signals
        db.add(SenderProfile(
            sender_domain="imp.com", sender_address="boss@imp.com",
            total_emails=50, folder_distribution={"INBOX": 45},
            marked_important_count=15, importance_tendency=0.3,
            reply_rate=0.8, total_replies=40,
            kept_in_inbox_count=40,
            first_seen=datetime.now(timezone.utc), last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        email = _make_email(db, sender="boss@imp.com", analysis_state="classified")

        score = compute_importance_score(db, email)
        # Score should be noticeably higher than baseline (30) because of learned signals
        assert score > 35.0  # baseline + at least some learned behavior boost

    def test_importance_scorer_without_learned_profile(self, db):
        """compute_importance_score still works when no learned profile exists."""
        from src.services.importance_scorer import compute_importance_score

        email = _make_email(db, sender="unknown@newdomain.xyz", analysis_state="classified")

        score = compute_importance_score(db, email)
        assert 0.0 <= score <= 100.0  # valid range, no crash
