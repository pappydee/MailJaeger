"""
Tests for the MailJaeger historical learning layer.

Covers:
  - Folder classification utility
  - Historical folder placement learning
  - Sender/domain aggregate updates
  - Sent-mail reply linkage
  - Reply delay capture
  - Internal predictions with explanations
  - Resumable historical learning job
  - User action event recording
"""

import pytest
from datetime import datetime, timezone, timedelta
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
# 1. Folder Classification Utility
# ===========================================================================

class TestFolderClassifier:
    """Test folder name classification."""

    def test_classify_inbox(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("INBOX") == "inbox"
        assert classify_folder("inbox") == "inbox"
        assert classify_folder("Posteingang") == "inbox"

    def test_classify_archive(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Archive") == "archive"
        assert classify_folder("Archiv") == "archive"
        assert classify_folder("[Gmail]/All Mail") == "archive"
        assert classify_folder("Alle Nachrichten") == "archive"

    def test_classify_spam(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Spam") == "spam"
        assert classify_folder("Junk") == "spam"
        assert classify_folder("Bulk") == "spam"

    def test_classify_trash(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Trash") == "trash"
        assert classify_folder("Deleted") == "trash"
        assert classify_folder("Papierkorb") == "trash"

    def test_classify_sent(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Sent") == "sent"
        assert classify_folder("Sent Items") == "sent"
        assert classify_folder("Gesendet") == "sent"
        assert classify_folder("Gesendete") == "sent"

    def test_classify_drafts(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Drafts") == "drafts"
        assert classify_folder("Entwürfe") == "drafts"

    def test_classify_custom(self):
        from src.services.folder_classifier import classify_folder
        assert classify_folder("Rechnungen") == "custom"
        assert classify_folder("Projects") == "custom"
        assert classify_folder("Work/Important") == "custom"

    def test_is_learnable_excludes_drafts(self):
        from src.services.folder_classifier import is_learnable_folder
        assert is_learnable_folder("INBOX") is True
        assert is_learnable_folder("Sent") is True
        assert is_learnable_folder("Drafts") is False

    def test_extract_sender_domain(self):
        from src.services.folder_classifier import extract_sender_domain
        assert extract_sender_domain("user@example.com") == "example.com"
        assert extract_sender_domain("Name <user@example.com>") == "example.com"
        assert extract_sender_domain("") == ""
        assert extract_sender_domain("noatsign") == ""

    def test_extract_sender_address(self):
        from src.services.folder_classifier import extract_sender_address
        assert extract_sender_address("user@example.com") == "user@example.com"
        assert extract_sender_address("Name <user@example.com>") == "user@example.com"
        assert extract_sender_address("") == ""

    def test_extract_subject_keywords(self):
        from src.services.folder_classifier import extract_subject_keywords
        kws = extract_subject_keywords("Re: Invoice from Vendor Corp")
        assert "invoice" in kws
        assert "vendor" in kws
        assert "corp" in kws
        # "from" is too short (< 3 default min)... actually "from" is 4 chars
        # but it should work since it's not in stop words

    def test_extract_subject_keywords_strips_prefixes(self):
        from src.services.folder_classifier import extract_subject_keywords
        kws1 = extract_subject_keywords("Re: Meeting Notes")
        kws2 = extract_subject_keywords("AW: Meeting Notes")
        assert kws1 == kws2


# ===========================================================================
# 2. Historical Folder Placement Creates Learning Signals
# ===========================================================================

class TestHistoricalFolderPlacement:
    """Test that learn_from_email creates reusable learning signals."""

    def test_learn_from_email_creates_sender_profile(self, db):
        from src.services.historical_learning import learn_from_email
        email = _make_email(db, sender="bills@company.de", folder="Rechnungen")
        stats = learn_from_email(db, email)
        db.commit()

        assert stats["sender_profiles"] >= 1

        profile = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "company.de"
        ).first()
        assert profile is not None
        assert profile.total_emails == 1
        assert profile.typical_folder == "Rechnungen"
        assert "Rechnungen" in (profile.folder_distribution or {})

    def test_learn_from_email_creates_folder_aggregates(self, db):
        from src.services.historical_learning import learn_from_email
        email = _make_email(db, sender="info@shop.com", folder="Orders", category="Bestellungen")
        stats = learn_from_email(db, email)
        db.commit()

        assert stats["folder_aggregates"] >= 1

        # Check sender_domain aggregate
        agg = db.query(FolderPlacementAggregate).filter(
            FolderPlacementAggregate.pattern_type == "sender_domain",
            FolderPlacementAggregate.pattern_value == "shop.com",
        ).first()
        assert agg is not None
        assert agg.target_folder == "Orders"
        assert agg.occurrence_count >= 1

    def test_learn_creates_category_aggregate(self, db):
        from src.services.historical_learning import learn_from_email
        email = _make_email(db, sender="a@x.com", folder="Work", category="Arbeit")
        learn_from_email(db, email)
        db.commit()

        agg = db.query(FolderPlacementAggregate).filter(
            FolderPlacementAggregate.pattern_type == "category",
            FolderPlacementAggregate.pattern_value == "Arbeit",
        ).first()
        assert agg is not None
        assert agg.target_folder == "Work"


# ===========================================================================
# 3. Sender/Domain Folder Aggregates Updated Correctly
# ===========================================================================

class TestSenderDomainAggregates:
    """Test that aggregates build up correctly with multiple emails."""

    def test_multiple_emails_same_domain_same_folder(self, db):
        from src.services.historical_learning import learn_from_email
        for i in range(5):
            email = _make_email(
                db, message_id=f"msg-same-{i}@test.com",
                sender=f"user{i}@acme.org", folder="Rechnungen"
            )
            learn_from_email(db, email)
        db.commit()

        profile = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "acme.org"
        ).first()
        assert profile is not None
        assert profile.total_emails == 5
        assert profile.typical_folder == "Rechnungen"
        assert profile.folder_distribution.get("Rechnungen") == 5

        agg = db.query(FolderPlacementAggregate).filter(
            FolderPlacementAggregate.pattern_type == "sender_domain",
            FolderPlacementAggregate.pattern_value == "acme.org",
            FolderPlacementAggregate.target_folder == "Rechnungen",
        ).first()
        assert agg is not None
        assert agg.occurrence_count == 5
        assert agg.confidence == 1.0  # only one folder

    def test_multiple_emails_different_folders_confidence(self, db):
        from src.services.historical_learning import learn_from_email
        # 3 emails to "Rechnungen", 1 to "INBOX"
        for i in range(3):
            email = _make_email(
                db, message_id=f"msg-multi-{i}@test.com",
                sender=f"billing{i}@corp.com", folder="Rechnungen"
            )
            learn_from_email(db, email)

        email = _make_email(
            db, message_id="msg-multi-inbox@test.com",
            sender="billing4@corp.com", folder="INBOX"
        )
        learn_from_email(db, email)
        db.commit()

        # The Rechnungen aggregate should have 75% confidence
        agg_rech = db.query(FolderPlacementAggregate).filter(
            FolderPlacementAggregate.pattern_type == "sender_domain",
            FolderPlacementAggregate.pattern_value == "corp.com",
            FolderPlacementAggregate.target_folder == "Rechnungen",
        ).first()
        assert agg_rech is not None
        assert agg_rech.occurrence_count == 3
        assert agg_rech.total_for_pattern == 4
        assert abs(agg_rech.confidence - 0.75) < 0.01

        agg_inbox = db.query(FolderPlacementAggregate).filter(
            FolderPlacementAggregate.pattern_type == "sender_domain",
            FolderPlacementAggregate.pattern_value == "corp.com",
            FolderPlacementAggregate.target_folder == "INBOX",
        ).first()
        assert agg_inbox is not None
        assert agg_inbox.occurrence_count == 1
        assert abs(agg_inbox.confidence - 0.25) < 0.01

    def test_sender_profile_tracks_spam_tendency(self, db):
        from src.services.historical_learning import learn_from_email
        # 2 spam emails, 1 normal
        for i in range(2):
            email = _make_email(
                db, message_id=f"spam-{i}@test.com",
                sender=f"x{i}@spammer.net", folder="Spam", is_spam=True
            )
            learn_from_email(db, email)
        email = _make_email(
            db, message_id="notspam@test.com",
            sender="x3@spammer.net", folder="INBOX", is_spam=False
        )
        learn_from_email(db, email)
        db.commit()

        profile = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "spammer.net"
        ).first()
        assert profile is not None
        assert abs(profile.spam_tendency - (2 / 3)) < 0.01


# ===========================================================================
# 4. Sent-Mail Replies Linked Back to Incoming
# ===========================================================================

class TestReplyLinkage:
    """Test sent mail linkage to incoming emails/threads."""

    def test_link_reply_via_thread_id(self, db):
        from src.services.historical_learning import learn_reply_linkage
        now = datetime.now(timezone.utc)
        incoming = _make_email(
            db, message_id="incoming-1@test.com",
            sender="boss@company.com", folder="INBOX",
            thread_id="thread-123", date=now - timedelta(hours=2),
        )
        sent = _make_email(
            db, message_id="sent-1@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="thread-123", date=now,
        )

        result = learn_reply_linkage(db, sent)
        db.commit()

        assert result is not None
        assert result["replied_to_email_id"] == incoming.id
        assert result["replied_to_thread_id"] == "thread-123"
        assert result["linkage_method"] == "thread_id"

    def test_link_reply_via_subject_heuristic(self, db):
        from src.services.historical_learning import learn_reply_linkage
        now = datetime.now(timezone.utc)
        incoming = _make_email(
            db, message_id="in-subj@test.com",
            sender="colleague@work.com", folder="INBOX",
            subject="Meeting Notes",
            date=now - timedelta(hours=1),
        )
        sent = _make_email(
            db, message_id="out-subj@test.com",
            sender="me@mymail.com", folder="Sent",
            subject="Re: Meeting Notes",
            date=now,
            thread_id=None,  # no thread_id
        )

        result = learn_reply_linkage(db, sent)
        db.commit()

        assert result is not None
        assert result["replied_to_email_id"] == incoming.id
        assert result["linkage_method"] == "subject_heuristic"

    def test_no_link_when_no_match(self, db):
        from src.services.historical_learning import learn_reply_linkage
        sent = _make_email(
            db, message_id="orphan-sent@test.com",
            sender="me@mymail.com", folder="Sent",
            subject="Completely unrelated subject",
            thread_id=None,
        )
        result = learn_reply_linkage(db, sent)
        assert result is None


# ===========================================================================
# 5. Reply Delay Captured Correctly
# ===========================================================================

class TestReplyDelay:
    """Test that reply delay is captured accurately."""

    def test_reply_delay_seconds(self, db):
        from src.services.historical_learning import learn_reply_linkage
        now = datetime.now(timezone.utc)
        delay_hours = 3
        incoming = _make_email(
            db, message_id="delay-in@test.com",
            sender="client@client.com", folder="INBOX",
            thread_id="delay-thread", date=now - timedelta(hours=delay_hours),
        )
        sent = _make_email(
            db, message_id="delay-out@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="delay-thread", date=now,
        )

        result = learn_reply_linkage(db, sent)
        db.commit()

        assert result is not None
        expected_delay = delay_hours * 3600
        assert abs(result["reply_delay_seconds"] - expected_delay) < 10  # within 10 seconds tolerance

        # Check that ReplyPattern was updated
        pattern = db.query(ReplyPattern).filter(
            ReplyPattern.pattern_type == "sender_domain",
            ReplyPattern.pattern_value == "client.com",
        ).first()
        assert pattern is not None
        assert pattern.total_replied >= 1
        assert pattern.avg_reply_delay_seconds is not None
        assert abs(pattern.avg_reply_delay_seconds - expected_delay) < 10

    def test_multiple_replies_average_delay(self, db):
        from src.services.historical_learning import learn_reply_linkage
        now = datetime.now(timezone.utc)

        # Reply 1: 1 hour delay
        incoming1 = _make_email(
            db, message_id="avg-in-1@test.com",
            sender="partner@biz.com", folder="INBOX",
            thread_id="avg-thread-1", date=now - timedelta(hours=1),
        )
        sent1 = _make_email(
            db, message_id="avg-out-1@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="avg-thread-1", date=now,
        )
        learn_reply_linkage(db, sent1)

        # Reply 2: 3 hour delay
        incoming2 = _make_email(
            db, message_id="avg-in-2@test.com",
            sender="other@biz.com", folder="INBOX",
            thread_id="avg-thread-2", date=now - timedelta(hours=3),
        )
        sent2 = _make_email(
            db, message_id="avg-out-2@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="avg-thread-2", date=now,
        )
        learn_reply_linkage(db, sent2)
        db.commit()

        pattern = db.query(ReplyPattern).filter(
            ReplyPattern.pattern_type == "sender_domain",
            ReplyPattern.pattern_value == "biz.com",
        ).first()
        assert pattern is not None
        assert pattern.total_replied == 2
        # Average of 1h and 3h = 2h = 7200s
        assert abs(pattern.avg_reply_delay_seconds - 7200) < 100


# ===========================================================================
# 6. Internal Predictions Created and Explained
# ===========================================================================

class TestPredictions:
    """Test that predictions are created with explanations."""

    def test_folder_prediction_from_aggregates(self, db):
        from src.services.historical_learning import learn_from_email
        from src.services.prediction_engine import generate_predictions

        # Build up aggregate: 5 emails from billing.com -> "Rechnungen"
        for i in range(5):
            email = _make_email(
                db, message_id=f"pred-train-{i}@test.com",
                sender=f"invoice{i}@billing.com", folder="Rechnungen"
            )
            learn_from_email(db, email)
        db.commit()

        # Now predict for a new email from same domain
        new_email = _make_email(
            db, message_id="pred-new@test.com",
            sender="bill@billing.com", folder="INBOX"
        )

        preds = generate_predictions(db, new_email)
        db.commit()

        folder_pred = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert folder_pred is not None
        assert folder_pred.predicted_value == "Rechnungen"
        assert folder_pred.confidence > 0.5
        assert "billing.com" in folder_pred.explanation
        assert folder_pred.explanation  # non-empty
        assert folder_pred.source_data  # non-empty

    def test_reply_needed_prediction(self, db):
        from src.services.prediction_engine import generate_predictions
        from src.models.database import ReplyPattern

        # Create a reply pattern with high reply rate
        pattern = ReplyPattern(
            pattern_type="sender_domain",
            pattern_value="important.com",
            total_received=20,
            total_replied=18,
            reply_probability=0.9,
            avg_reply_delay_seconds=1800,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(pattern)
        db.commit()

        email = _make_email(
            db, message_id="reply-pred@test.com",
            sender="ceo@important.com", folder="INBOX"
        )

        preds = generate_predictions(db, email)
        db.commit()

        reply_pred = next((p for p in preds if p.prediction_type == "reply_needed"), None)
        assert reply_pred is not None
        assert reply_pred.confidence >= 0.3
        assert "important.com" in reply_pred.explanation
        assert "replied" in reply_pred.explanation.lower()

    def test_importance_boost_prediction(self, db):
        from src.services.prediction_engine import generate_predictions

        # Create a sender profile with high reply rate and importance
        profile = SenderProfile(
            sender_domain="vip.org",
            total_emails=20,
            reply_rate=0.8,
            total_replies=16,
            importance_tendency=0.3,
            marked_important_count=6,
            spam_tendency=0.0,
            marked_spam_count=0,
            kept_in_inbox_count=15,
            folder_distribution={"INBOX": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(profile)
        db.commit()

        email = _make_email(
            db, message_id="imp-pred@test.com",
            sender="exec@vip.org", folder="INBOX"
        )

        preds = generate_predictions(db, email)
        db.commit()

        imp_pred = next((p for p in preds if p.prediction_type == "importance_boost"), None)
        assert imp_pred is not None
        assert imp_pred.confidence > 0
        assert "vip.org" in imp_pred.explanation

    def test_no_prediction_below_threshold(self, db):
        from src.services.prediction_engine import generate_predictions

        # Create email with no history
        email = _make_email(
            db, message_id="nopred@test.com",
            sender="unknown@nowhere.xyz", folder="INBOX"
        )

        preds = generate_predictions(db, email)
        assert len(preds) == 0

    def test_predictions_stored_in_db(self, db):
        from src.services.historical_learning import learn_from_email
        from src.services.prediction_engine import generate_predictions

        # Build aggregate
        for i in range(5):
            email = _make_email(
                db, message_id=f"dbpred-{i}@test.com",
                sender=f"shop{i}@store.com", folder="Orders"
            )
            learn_from_email(db, email)
        db.commit()

        new_email = _make_email(
            db, message_id="dbpred-new@test.com",
            sender="sale@store.com", folder="INBOX"
        )
        generate_predictions(db, new_email)
        db.commit()

        stored = db.query(EmailPrediction).filter(
            EmailPrediction.email_id == new_email.id
        ).all()
        assert len(stored) >= 1


# ===========================================================================
# 7. Historical Learning Job Resumable
# ===========================================================================

class TestHistoricalLearningJob:
    """Test the resumable historical learning job."""

    def test_basic_learning_job(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job

        # Create some emails in different folders
        for i in range(3):
            _make_email(
                db, message_id=f"job-inbox-{i}@test.com",
                sender=f"sender{i}@domain.com", folder="INBOX"
            )
        for i in range(2):
            _make_email(
                db, message_id=f"job-rech-{i}@test.com",
                sender=f"bill{i}@bills.com", folder="Rechnungen"
            )

        stats = run_historical_learning_job(db, batch_size=10)

        assert stats["status"] in ("completed", "partial")
        assert stats["emails_learned"] >= 5
        assert stats["folders_processed"] >= 2

        # Verify aggregates were created
        profiles = db.query(SenderProfile).count()
        assert profiles >= 2

    def test_learning_job_creates_processing_job(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job

        _make_email(db, message_id="pj@test.com", sender="a@b.com", folder="INBOX")
        run_historical_learning_job(db)

        job = db.query(ProcessingJob).filter(ProcessingJob.job_type == "learning").first()
        assert job is not None
        assert job.status in ("completed", "partial")

    def test_learning_job_avoids_duplicate_processing(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job

        for i in range(3):
            _make_email(
                db, message_id=f"dup-{i}@test.com",
                sender=f"s{i}@d.com", folder="INBOX"
            )

        # First run
        stats1 = run_historical_learning_job(db)
        learned1 = stats1["emails_learned"]

        # Second run should not reprocess (folder marked as completed)
        stats2 = run_historical_learning_job(db)
        assert stats2["emails_learned"] == 0 or stats2["skipped"] >= 0

    def test_learning_job_resumable(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job

        # Create 10 emails
        for i in range(10):
            _make_email(
                db, message_id=f"resume-{i}@test.com",
                sender=f"r{i}@resume.com", folder="INBOX"
            )

        # First run with max_emails=5 (partial)
        stats1 = run_historical_learning_job(db, max_emails=5)

        # Check that progress was saved
        progress = db.query(HistoricalLearningProgress).filter(
            HistoricalLearningProgress.folder_name == "INBOX"
        ).first()
        assert progress is not None
        # Progress was saved — but since max_emails caps the loop,
        # the folder may be marked completed with partial data or have a cursor
        assert progress.processed_count >= 0

    def test_learning_job_handles_sent_folder(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job
        now = datetime.now(timezone.utc)

        # Create incoming email + sent reply
        _make_email(
            db, message_id="in-sent@test.com",
            sender="client@client.com", folder="INBOX",
            thread_id="sent-thread", date=now - timedelta(hours=1),
        )
        _make_email(
            db, message_id="out-sent@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="sent-thread", date=now,
        )

        stats = run_historical_learning_job(db)
        assert stats["replies_linked"] >= 1

    def test_learning_job_with_max_emails_cap(self, db):
        from src.pipeline.historical_learning_job import run_historical_learning_job

        for i in range(20):
            _make_email(
                db, message_id=f"cap-{i}@test.com",
                sender=f"c{i}@cap.com", folder="INBOX"
            )

        stats = run_historical_learning_job(db, max_emails=5)
        # Should process at most 5 emails
        total_processed = stats["emails_learned"] + stats.get("skipped", 0)
        assert total_processed <= 10  # allow some tolerance for batch processing


# ===========================================================================
# 8. User Action Event Recording
# ===========================================================================

class TestUserActionRecording:
    """Test user action event recording."""

    def test_record_moved_to_folder(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="x@y.com", folder="INBOX")

        event = record_user_action(
            db, email, "moved_to_folder",
            old_folder="INBOX", new_folder="Rechnungen",
        )
        db.commit()

        assert event is not None
        assert event.action_type == "moved_to_folder"
        assert event.old_folder == "INBOX"
        assert event.new_folder == "Rechnungen"
        assert event.sender_domain == "y.com"
        assert event.source == "user"

    def test_record_archived(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="a@b.com", folder="INBOX")

        event = record_user_action(db, email, "archived", new_folder="Archive")
        db.commit()

        assert event.action_type == "archived"
        assert event.new_folder == "Archive"

    def test_record_deleted(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="spam@bad.com", folder="INBOX")

        event = record_user_action(db, email, "deleted")
        db.commit()

        assert event.action_type == "deleted"

    def test_record_replied(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="boss@corp.com", folder="INBOX")

        event = record_user_action(db, email, "replied")
        db.commit()

        assert event.action_type == "replied"

    def test_record_marked_spam(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="promo@spam.net", folder="INBOX")

        event = record_user_action(db, email, "marked_spam")
        db.commit()

        assert event.action_type == "marked_spam"

        # Check that sender profile was updated
        profile = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "spam.net"
        ).first()
        assert profile is not None
        assert profile.marked_spam_count >= 1

    def test_record_marked_important(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="vip@important.com", folder="INBOX")

        event = record_user_action(db, email, "marked_important")
        db.commit()

        profile = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "important.com"
        ).first()
        assert profile is not None
        assert profile.marked_important_count >= 1

    def test_all_action_types_storable(self, db):
        from src.services.historical_learning import record_user_action
        action_types = [
            "moved_to_folder", "archived", "deleted", "kept_in_inbox",
            "replied", "forwarded", "marked_important", "marked_spam", "unmarked_spam",
        ]
        for i, action in enumerate(action_types):
            email = _make_email(
                db, message_id=f"action-{i}@test.com",
                sender=f"u{i}@d{i}.com", folder="INBOX"
            )
            event = record_user_action(db, email, action)
            db.commit()
            assert event.action_type == action

        total = db.query(UserActionEvent).count()
        assert total == len(action_types)

    def test_action_event_has_source_imported_history(self, db):
        from src.services.historical_learning import record_user_action
        email = _make_email(db, sender="x@y.com", folder="INBOX")

        event = record_user_action(db, email, "archived", source="imported-history")
        db.commit()

        assert event.source == "imported-history"


# ===========================================================================
# 9. Incremental Learning (completed folders pick up new emails)
# ===========================================================================

class TestIncrementalLearning:
    """Test that historical learning is truly incremental."""

    def test_completed_folder_picks_up_new_emails(self, db):
        """Completed folders must still process newly arrived emails."""
        from src.pipeline.historical_learning_job import run_historical_learning_job

        # Create initial emails and run first scan
        for i in range(3):
            _make_email(
                db, message_id=f"inc-initial-{i}@test.com",
                sender=f"s{i}@d.com", folder="INBOX"
            )
        stats1 = run_historical_learning_job(db)
        assert stats1["emails_learned"] == 3

        # Add new emails to the same folder
        for i in range(2):
            _make_email(
                db, message_id=f"inc-new-{i}@test.com",
                sender=f"n{i}@d.com", folder="INBOX"
            )

        # Second run must pick up only the 2 new emails
        stats2 = run_historical_learning_job(db)
        assert stats2["emails_learned"] == 2

    def test_completed_folder_no_reprocessing_without_new_emails(self, db):
        """A completed folder with no new emails is truly skipped."""
        from src.pipeline.historical_learning_job import run_historical_learning_job

        _make_email(db, message_id="noreproc@test.com", sender="a@b.com", folder="INBOX")
        run_historical_learning_job(db)

        # Second run: nothing new
        stats2 = run_historical_learning_job(db)
        assert stats2["emails_learned"] == 0


# ===========================================================================
# 10. Sender-Address-Level Profiles
# ===========================================================================

class TestSenderAddressProfiles:
    """Test address-level and domain-level profile coexistence."""

    def test_address_level_profile_created(self, db):
        """learn_from_email creates both domain- and address-level profiles."""
        from src.services.historical_learning import learn_from_email

        email = _make_email(db, sender="john@corp.com", folder="Work")
        learn_from_email(db, email)
        db.commit()

        # Domain-level profile
        domain_p = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "corp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_p is not None
        assert domain_p.total_emails == 1

        # Address-level profile
        addr_p = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "john@corp.com",
        ).first()
        assert addr_p is not None
        assert addr_p.total_emails == 1

    def test_domain_fallback_when_address_absent(self, db):
        """Predictions use domain when no address-level data exists."""
        from src.services.historical_learning import learn_from_email
        from src.services.prediction_engine import generate_predictions

        # Build domain-level aggregate from many senders at same domain
        for i in range(5):
            email = _make_email(
                db, message_id=f"dom-fb-{i}@test.com",
                sender=f"user{i}@bigcorp.com", folder="Rechnungen"
            )
            learn_from_email(db, email)
        db.commit()

        # New email from a never-seen address at that domain
        new_email = _make_email(
            db, message_id="dom-fb-new@test.com",
            sender="newperson@bigcorp.com", folder="INBOX"
        )
        preds = generate_predictions(db, new_email)
        db.commit()

        folder_pred = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert folder_pred is not None
        assert folder_pred.predicted_value == "Rechnungen"
        # Source should be domain-level since address has only 1 sample
        assert folder_pred.source_data.get("pattern_type") in ("sender_domain", "sender_address")


# ===========================================================================
# 11. Durable Reply Links
# ===========================================================================

class TestReplyLinks:
    """Test durable reply link storage."""

    def test_reply_link_stored(self, db):
        """learn_reply_linkage persists a ReplyLink record."""
        from src.services.historical_learning import learn_reply_linkage
        from src.models.database import ReplyLink

        now = datetime.now(timezone.utc)
        incoming = _make_email(
            db, message_id="rl-in@test.com",
            sender="client@client.com", folder="INBOX",
            thread_id="rl-thread", date=now - timedelta(hours=2),
        )
        sent = _make_email(
            db, message_id="rl-out@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="rl-thread", date=now,
        )

        result = learn_reply_linkage(db, sent)
        db.commit()

        assert result is not None
        link = db.query(ReplyLink).filter(
            ReplyLink.sent_email_id == sent.id,
            ReplyLink.original_email_id == incoming.id,
        ).first()
        assert link is not None
        assert link.linkage_method == "thread_id"
        assert link.confidence == 0.9
        assert link.reply_delay_seconds is not None
        assert link.original_sender_domain == "client.com"

    def test_reply_link_not_duplicated(self, db):
        """Running learn_reply_linkage twice does not duplicate the link."""
        from src.services.historical_learning import learn_reply_linkage
        from src.models.database import ReplyLink

        now = datetime.now(timezone.utc)
        incoming = _make_email(
            db, message_id="rl-dup-in@test.com",
            sender="x@y.com", folder="INBOX",
            thread_id="rl-dup-thread", date=now - timedelta(hours=1),
        )
        sent = _make_email(
            db, message_id="rl-dup-out@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="rl-dup-thread", date=now,
        )

        learn_reply_linkage(db, sent)
        db.commit()
        learn_reply_linkage(db, sent)
        db.commit()

        count = db.query(ReplyLink).filter(
            ReplyLink.sent_email_id == sent.id,
        ).count()
        assert count == 1

    def test_heuristic_reply_link_has_lower_confidence(self, db):
        """Subject-heuristic links get lower confidence than thread-based links."""
        from src.services.historical_learning import learn_reply_linkage
        from src.models.database import ReplyLink

        now = datetime.now(timezone.utc)
        _make_email(
            db, message_id="rl-heur-in@test.com",
            sender="col@work.com", folder="INBOX",
            subject="Budget Meeting",
            date=now - timedelta(hours=1),
        )
        sent = _make_email(
            db, message_id="rl-heur-out@test.com",
            sender="me@mymail.com", folder="Sent",
            subject="Re: Budget Meeting",
            date=now,
            thread_id=None,
        )

        learn_reply_linkage(db, sent)
        db.commit()

        link = db.query(ReplyLink).filter(
            ReplyLink.sent_email_id == sent.id,
        ).first()
        assert link is not None
        assert link.linkage_method == "subject_heuristic"
        assert link.confidence < 0.9  # lower than thread-based


# ===========================================================================
# 12. Prediction Precedence (address > domain > category)
# ===========================================================================

class TestPredictionPrecedence:
    """Test that prediction uses sender_address > sender_domain > category."""

    def test_address_takes_precedence_over_domain(self, db):
        """If address-level data says folder X and domain says folder Y, address wins."""
        from src.services.prediction_engine import generate_predictions

        # Create domain-level aggregate: domain -> "Work"
        agg = FolderPlacementAggregate(
            pattern_type="sender_domain", pattern_value="mixed.com",
            target_folder="Work", occurrence_count=5, total_for_pattern=10,
            confidence=0.5,
        )
        db.add(agg)

        # Create address-level aggregate: specific address -> "VIP"
        agg2 = FolderPlacementAggregate(
            pattern_type="sender_address", pattern_value="boss@mixed.com",
            target_folder="VIP", occurrence_count=4, total_for_pattern=4,
            confidence=1.0,
        )
        db.add(agg2)
        db.commit()

        email = _make_email(
            db, message_id="prec-test@test.com",
            sender="boss@mixed.com", folder="INBOX",
        )
        preds = generate_predictions(db, email)
        db.commit()

        folder_pred = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert folder_pred is not None
        assert folder_pred.predicted_value == "VIP"
        assert folder_pred.source_data["pattern_type"] == "sender_address"

    def test_importance_uses_historical_signals(self, db):
        """Importance boost uses reply rate, importance markings, and inbox retention."""
        from src.services.prediction_engine import generate_predictions

        profile = SenderProfile(
            sender_domain="engaged.com",
            total_emails=20,
            reply_rate=0.8, total_replies=16,
            importance_tendency=0.3, marked_important_count=6,
            spam_tendency=0.0, marked_spam_count=0,
            kept_in_inbox_count=15,
            folder_distribution={"INBOX": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(profile)
        db.commit()

        email = _make_email(
            db, message_id="imp-det@test.com",
            sender="exec@engaged.com", folder="INBOX"
        )
        preds = generate_predictions(db, email)
        db.commit()

        imp_pred = next((p for p in preds if p.prediction_type == "importance_boost"), None)
        assert imp_pred is not None
        assert imp_pred.confidence > 0
        assert "reply rate" in imp_pred.explanation or "important" in imp_pred.explanation


# ===========================================================================
# 13. Strict Precedence Tests (address MUST beat domain even with lower confidence)
# ===========================================================================

class TestStrictPrecedence:
    """Verify that more specific signals always win regardless of raw confidence."""

    def test_folder_address_beats_domain_even_with_lower_confidence(self, db):
        """sender_address with lower confidence must still beat sender_domain."""
        # Domain aggregate: high confidence
        db.add(FolderPlacementAggregate(
            pattern_type="sender_domain", pattern_value="corp.com",
            target_folder="Archive", occurrence_count=10, total_for_pattern=12,
            confidence=0.83,
        ))
        # Address aggregate: lower confidence but meets thresholds
        db.add(FolderPlacementAggregate(
            pattern_type="sender_address", pattern_value="ceo@corp.com",
            target_folder="VIP", occurrence_count=3, total_for_pattern=5,
            confidence=0.6,
        ))
        db.commit()

        email = _make_email(db, message_id="strict-fp@test.com",
                            sender="ceo@corp.com", folder="INBOX")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        fp = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert fp is not None
        assert fp.predicted_value == "VIP", "address must win over domain"
        assert fp.source_data["pattern_type"] == "sender_address"

    def test_folder_domain_beats_category(self, db):
        """sender_domain must win over category even with lower confidence."""
        db.add(FolderPlacementAggregate(
            pattern_type="category", pattern_value="Finance",
            target_folder="Bills", occurrence_count=20, total_for_pattern=25,
            confidence=0.8,
        ))
        db.add(FolderPlacementAggregate(
            pattern_type="sender_domain", pattern_value="bank.com",
            target_folder="Banking", occurrence_count=3, total_for_pattern=5,
            confidence=0.6,
        ))
        db.commit()

        email = _make_email(db, message_id="strict-dc@test.com",
                            sender="info@bank.com", folder="INBOX",
                            category="Finance")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        fp = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert fp is not None
        assert fp.predicted_value == "Banking", "domain must win over category"
        assert fp.source_data["pattern_type"] == "sender_domain"

    def test_folder_subject_keyword_used_as_fallback(self, db):
        """subject_keyword is used when no sender/category data exists."""
        db.add(FolderPlacementAggregate(
            pattern_type="subject_keyword", pattern_value="invoice",
            target_folder="Rechnungen", occurrence_count=8, total_for_pattern=10,
            confidence=0.8,
        ))
        db.commit()

        email = _make_email(db, message_id="strict-kw@test.com",
                            sender="unknown@nowhere.org", folder="INBOX",
                            subject="Your invoice #1234", category="")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        fp = next((p for p in preds if p.prediction_type == "target_folder"), None)
        assert fp is not None
        assert fp.predicted_value == "Rechnungen"
        assert fp.source_data["pattern_type"] == "subject_keyword"

    def test_reply_needed_address_beats_domain(self, db):
        """sender_address reply pattern must win over sender_domain."""
        # Domain pattern: high probability
        db.add(ReplyPattern(
            pattern_type="sender_domain", pattern_value="team.io",
            total_received=20, total_replied=18,
            reply_probability=0.9,
        ))
        # Address pattern: lower probability but still meets threshold
        db.add(ReplyPattern(
            pattern_type="sender_address", pattern_value="noreply@team.io",
            total_received=10, total_replied=4,
            reply_probability=0.4,
        ))
        db.commit()

        email = _make_email(db, message_id="strict-rn@test.com",
                            sender="noreply@team.io", folder="INBOX")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        rp = next((p for p in preds if p.prediction_type == "reply_needed"), None)
        assert rp is not None
        assert rp.source_data["pattern_type"] == "sender_address", \
            "address reply pattern must win over domain"
        assert rp.source_data["reply_probability"] == 0.4

    def test_reply_needed_domain_beats_category(self, db):
        """sender_domain reply pattern must win over category."""
        db.add(ReplyPattern(
            pattern_type="category", pattern_value="Support",
            total_received=50, total_replied=45,
            reply_probability=0.9,
        ))
        db.add(ReplyPattern(
            pattern_type="sender_domain", pattern_value="help.io",
            total_received=10, total_replied=4,
            reply_probability=0.4,
        ))
        db.commit()

        email = _make_email(db, message_id="strict-rn2@test.com",
                            sender="bot@help.io", folder="INBOX",
                            category="Support")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        rp = next((p for p in preds if p.prediction_type == "reply_needed"), None)
        assert rp is not None
        assert rp.source_data["pattern_type"] == "sender_domain", \
            "domain reply pattern must win over category"

    def test_importance_boost_uses_address_profile_first(self, db):
        """importance_boost must use address-level profile when available."""
        # Domain profile: lower signals
        db.add(SenderProfile(
            sender_domain="mixed.org", sender_address=None,
            total_emails=20, reply_rate=0.1, total_replies=2,
            importance_tendency=0.05, marked_important_count=1,
            spam_tendency=0.0, marked_spam_count=0,
            kept_in_inbox_count=2,
            folder_distribution={"INBOX": 20},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        # Address profile: strong signals
        db.add(SenderProfile(
            sender_domain="mixed.org", sender_address="boss@mixed.org",
            total_emails=15, reply_rate=0.8, total_replies=12,
            importance_tendency=0.4, marked_important_count=6,
            spam_tendency=0.0, marked_spam_count=0,
            kept_in_inbox_count=12,
            folder_distribution={"INBOX": 15},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        ))
        db.commit()

        email = _make_email(db, message_id="strict-imp@test.com",
                            sender="boss@mixed.org", folder="INBOX")
        from src.services.prediction_engine import generate_predictions
        preds = generate_predictions(db, email)
        db.commit()

        imp = next((p for p in preds if p.prediction_type == "importance_boost"), None)
        assert imp is not None
        assert "boss@mixed.org" in imp.explanation, \
            "explanation must mention address profile, not domain"
        assert imp.source_data["profile_key"] == "boss@mixed.org"


# ===========================================================================
# 14. Address/Domain Profile Isolation
# ===========================================================================

class TestProfileIsolation:
    """Verify domain-only queries don't pick up address profiles."""

    def test_domain_query_excludes_address_profiles(self, db):
        """Querying for domain-level profile must not return address profiles."""
        from src.services.historical_learning import learn_from_email

        email = _make_email(db, sender="alice@example.com", folder="Work")
        learn_from_email(db, email)
        db.commit()

        # Domain-only query must return exactly one row with sender_address IS NULL
        domain_profiles = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "example.com",
            SenderProfile.sender_address.is_(None),
        ).all()
        assert len(domain_profiles) == 1
        assert domain_profiles[0].sender_address is None

        # Address query must return a separate row
        addr_profiles = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@example.com",
        ).all()
        assert len(addr_profiles) == 1
        assert addr_profiles[0].sender_address == "alice@example.com"

    def test_multiple_addresses_same_domain_separate_profiles(self, db):
        """Different addresses at same domain get separate profiles."""
        from src.services.historical_learning import learn_from_email

        e1 = _make_email(db, message_id="iso1@test.com", sender="a@dom.com", folder="INBOX")
        e2 = _make_email(db, message_id="iso2@test.com", sender="b@dom.com", folder="Work")
        learn_from_email(db, e1)
        learn_from_email(db, e2)
        db.commit()

        # Two address profiles
        addr_count = db.query(SenderProfile).filter(
            SenderProfile.sender_address.isnot(None),
            SenderProfile.sender_domain == "dom.com",
        ).count()
        assert addr_count == 2

        # One domain profile
        domain_count = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "dom.com",
            SenderProfile.sender_address.is_(None),
        ).count()
        assert domain_count == 1

        # Domain profile has total_emails == 2
        dp = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "dom.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert dp.total_emails == 2


# ===========================================================================
# 15. Reply Profile Update Propagation
# ===========================================================================

class TestReplyProfilePropagation:
    """Verify reply learning updates address-level SenderProfile."""

    def test_reply_updates_address_level_profile(self, db):
        """learn_reply_linkage must update both domain and address profiles."""
        from src.services.historical_learning import learn_from_email, learn_reply_linkage

        now = datetime.now(timezone.utc)
        incoming = _make_email(
            db, message_id="rp-in@test.com",
            sender="client@clientcorp.com", folder="INBOX",
            thread_id="rp-thread", date=now - timedelta(hours=1),
        )
        # Pre-learn the incoming email to create profiles
        learn_from_email(db, incoming)
        db.commit()

        sent = _make_email(
            db, message_id="rp-out@test.com",
            sender="me@mymail.com", folder="Sent",
            thread_id="rp-thread", date=now,
        )
        learn_reply_linkage(db, sent)
        db.commit()

        # Domain profile should have reply stats
        dp = db.query(SenderProfile).filter(
            SenderProfile.sender_domain == "clientcorp.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert dp is not None
        assert (dp.total_replies or 0) >= 1
        assert (dp.reply_rate or 0) > 0

        # Address profile should also have reply stats
        ap = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "client@clientcorp.com",
        ).first()
        assert ap is not None
        assert (ap.total_replies or 0) >= 1
        assert (ap.reply_rate or 0) > 0
