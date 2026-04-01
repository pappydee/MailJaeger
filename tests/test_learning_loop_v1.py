"""
Tests for the learning loop feature:
  - Manual classification API endpoint
  - Sender learning info endpoint
  - Learning loop service (record_manual_classification, sender profiles)
  - Rule-based pre-classifier
  - Action queue lifecycle (default filtering, state transitions)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, ProcessedEmail, SenderProfile, DecisionEvent, ActionQueue


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db_session(tmp_path):
    """Create a fresh in-memory DB session for each test."""
    db_file = tmp_path / "test_learning_loop.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def sample_email(db_session):
    """Create a sample email for testing."""
    email = ProcessedEmail(
        message_id="test-001@example.com",
        uid="101",
        subject="Test Email Subject",
        sender="alice@example.com",
        category="Unklar",
        priority="MEDIUM",
        is_spam=False,
        is_processed=True,
        is_resolved=False,
        analysis_state="classified",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(email)
    db_session.commit()
    return email


@pytest.fixture
def newsletter_email(db_session):
    """Create a newsletter-like email."""
    email = ProcessedEmail(
        message_id="newsletter-001@example.com",
        uid="201",
        subject="Weekly Newsletter - Issue #42",
        sender="newsletter@company.com",
        category="Unklar",
        priority="LOW",
        is_spam=False,
        is_processed=True,
        is_resolved=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(email)
    db_session.commit()
    return email


@pytest.fixture
def spam_email(db_session):
    """Create a spam-like email."""
    email = ProcessedEmail(
        message_id="spam-001@example.com",
        uid="301",
        subject="You have won a million dollars!",
        sender="scam@suspicious.org",
        category="Unklar",
        priority="LOW",
        is_spam=False,
        is_processed=True,
        is_resolved=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(email)
    db_session.commit()
    return email


# ── Manual Classification Tests ─────────────────────────────────────────


class TestManualClassification:
    """Test the record_manual_classification function."""

    def test_classify_email_updates_category(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        result = record_manual_classification(db_session, sample_email, "work")
        db_session.commit()

        assert result["category"] == "work"
        assert result["email_id"] == sample_email.id
        assert sample_email.category == "work"
        assert sample_email.overridden is True

    def test_classify_email_with_target_folder(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        result = record_manual_classification(
            db_session, sample_email, "work", target_folder="Work/Projects"
        )
        db_session.commit()

        assert result["target_folder"] == "Work/Projects"
        assert sample_email.suggested_folder == "Work/Projects"

    def test_classify_creates_decision_event(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        record_manual_classification(db_session, sample_email, "private")
        db_session.commit()

        events = db_session.query(DecisionEvent).filter(
            DecisionEvent.email_id == sample_email.id,
            DecisionEvent.event_type == "manual_classify",
        ).all()
        assert len(events) == 1
        event = events[0]
        assert event.source == "user_manual"
        assert event.chosen_category == "private"
        assert event.sender == "alice@example.com"
        assert event.user_confirmed is True
        assert event.confidence == 1.0

    def test_classify_updates_sender_profile(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        result = record_manual_classification(
            db_session, sample_email, "work", target_folder="Work"
        )
        db_session.commit()

        assert result["sender_profile_updated"] is True

        # Check address-level profile
        addr_profile = db_session.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@example.com"
        ).first()
        assert addr_profile is not None
        assert addr_profile.preferred_category == "work"
        assert addr_profile.preferred_folder == "Work"
        assert addr_profile.user_classification_count == 1

        # Check domain-level profile
        domain_profile = db_session.query(SenderProfile).filter(
            SenderProfile.sender_domain == "example.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        assert domain_profile is not None
        assert domain_profile.preferred_category == "work"

    def test_classify_spam_sets_spam_flags(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        record_manual_classification(db_session, sample_email, "spam")
        db_session.commit()

        assert sample_email.is_spam is True
        assert sample_email.spam_probability == 0.95

    def test_classify_unmarks_spam(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        # First mark as spam
        sample_email.category = "spam"
        sample_email.is_spam = True
        db_session.commit()

        # Then reclassify as work
        record_manual_classification(db_session, sample_email, "work")
        db_session.commit()

        assert sample_email.is_spam is False
        assert sample_email.spam_probability == 0.05

    def test_classify_invalid_category_raises(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        with pytest.raises(ValueError, match="Invalid category"):
            record_manual_classification(db_session, sample_email, "invalid_cat")

    def test_multiple_classifications_increment_count(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        record_manual_classification(db_session, sample_email, "work")
        db_session.commit()

        # Create a second email from same sender
        email2 = ProcessedEmail(
            message_id="test-002@example.com",
            uid="102",
            sender="alice@example.com",
            subject="Second email",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email2)
        db_session.commit()

        record_manual_classification(db_session, email2, "work")
        db_session.commit()

        addr_profile = db_session.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@example.com"
        ).first()
        assert addr_profile.user_classification_count == 2

    def test_classify_preserves_original_classification(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification

        original_cat = sample_email.category
        record_manual_classification(db_session, sample_email, "todo")
        db_session.commit()

        assert sample_email.original_classification is not None
        assert sample_email.original_classification["category"] == original_cat


# ── Sender Learning Info Tests ──────────────────────────────────────────


class TestSenderLearningInfo:
    def test_no_profile_returns_empty(self, db_session):
        from src.services.learning_loop import get_sender_learning_info

        info = get_sender_learning_info(db_session, "unknown@nowhere.com")
        assert info["preferred_category"] is None
        assert info["user_classification_count"] == 0

    def test_returns_profile_after_classification(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification, get_sender_learning_info

        record_manual_classification(db_session, sample_email, "work", target_folder="Work")
        db_session.commit()

        info = get_sender_learning_info(db_session, "alice@example.com")
        assert info["preferred_category"] == "work"
        assert info["preferred_folder"] == "Work"
        assert info["user_classification_count"] == 1

    def test_address_takes_precedence_over_domain(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification, get_sender_learning_info

        record_manual_classification(db_session, sample_email, "work")
        db_session.commit()

        # Create a domain-level profile with different category
        domain_profile = db_session.query(SenderProfile).filter(
            SenderProfile.sender_domain == "example.com",
            SenderProfile.sender_address.is_(None),
        ).first()
        domain_profile.preferred_category = "private"
        db_session.commit()

        # Address-level should still win
        info = get_sender_learning_info(db_session, "alice@example.com")
        assert info["preferred_category"] == "work"


# ── Rule-based Pre-classifier Tests ─────────────────────────────────────


class TestRuleBasedClassifier:
    def test_known_sender_classification(self, db_session, sample_email):
        from src.services.learning_loop import record_manual_classification, rule_based_classify

        # First, classify an email to build the sender profile
        record_manual_classification(db_session, sample_email, "work", target_folder="Work")
        db_session.commit()

        # Now create a new email from the same sender
        email2 = ProcessedEmail(
            message_id="test-003@example.com",
            uid="103",
            sender="alice@example.com",
            subject="Another email",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email2)
        db_session.commit()

        result = rule_based_classify(db_session, email2)
        assert result is not None
        assert result["category"] == "work"
        assert result["suggested_folder"] == "Work"
        assert result["explanation"] == "Learned from previous user classification"
        assert result["source"] == "sender_profile"

    def test_newsletter_detection_by_sender(self, db_session, newsletter_email):
        from src.services.learning_loop import rule_based_classify

        result = rule_based_classify(db_session, newsletter_email)
        assert result is not None
        assert result["category"] == "newsletter"
        assert result["explanation"] == "Newsletter pattern detected"

    def test_newsletter_detection_by_subject(self, db_session):
        from src.services.learning_loop import rule_based_classify

        email = ProcessedEmail(
            message_id="nl-002@example.com",
            uid="202",
            sender="someone@regular.org",
            subject="Please unsubscribe me from this list",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email)
        db_session.commit()

        result = rule_based_classify(db_session, email)
        assert result is not None
        assert result["category"] == "newsletter"

    def test_spam_detection_by_subject(self, db_session, spam_email):
        from src.services.learning_loop import rule_based_classify

        result = rule_based_classify(db_session, spam_email)
        assert result is not None
        assert result["category"] == "spam"
        assert result["explanation"] == "Spam pattern detected"

    def test_no_match_returns_none(self, db_session):
        from src.services.learning_loop import rule_based_classify

        email = ProcessedEmail(
            message_id="normal-001@example.com",
            uid="401",
            sender="bob@company.com",
            subject="Meeting tomorrow at 10am",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email)
        db_session.commit()

        result = rule_based_classify(db_session, email)
        assert result is None

    def test_sender_profile_takes_precedence_over_newsletter(self, db_session):
        from src.services.learning_loop import record_manual_classification, rule_based_classify

        # Classify a noreply sender as "work" explicitly
        email1 = ProcessedEmail(
            message_id="noreply-001@example.com",
            uid="501",
            sender="noreply@company.com",
            subject="Deployment successful",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email1)
        db_session.commit()

        record_manual_classification(db_session, email1, "work")
        db_session.commit()

        # New email from same sender should use sender profile, not newsletter heuristic
        email2 = ProcessedEmail(
            message_id="noreply-002@example.com",
            uid="502",
            sender="noreply@company.com",
            subject="Another deployment",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email2)
        db_session.commit()

        result = rule_based_classify(db_session, email2)
        assert result is not None
        assert result["category"] == "work"
        assert result["source"] == "sender_profile"


# ── Action Queue Lifecycle Tests ─────────────────────────────────────────


class TestActionQueueLifecycle:
    def _create_action(self, db_session, status="proposed", email_id=None):
        """Helper to create an action queue entry."""
        if email_id is None:
            email = ProcessedEmail(
                message_id=f"aq-{status}-{id(status)}@example.com",
                uid=str(id(status)),
                sender="test@example.com",
                subject="Test",
                is_processed=True,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(email)
            db_session.commit()
            email_id = email.id

        action = ActionQueue(
            email_id=email_id,
            action_type="move",
            payload={"target_folder": "Archive"},
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(action)
        db_session.commit()
        return action

    def test_action_queue_explanation_field(self, db_session):
        """Test that the explanation field works on ActionQueue."""
        email = ProcessedEmail(
            message_id="explain-001@example.com",
            uid="601",
            sender="test@example.com",
            subject="Test",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email)
        db_session.commit()

        action = ActionQueue(
            email_id=email.id,
            action_type="move",
            payload={"target_folder": "Archive"},
            status="proposed",
            explanation="Known sender rule",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(action)
        db_session.commit()
        db_session.refresh(action)

        assert action.explanation == "Known sender rule"

    def test_new_action_queue_states_stored(self, db_session):
        """Test that waiting_for_user and expired states can be stored."""
        email = ProcessedEmail(
            message_id="state-001@example.com",
            uid="701",
            sender="test@example.com",
            subject="Test",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(email)
        db_session.commit()

        for state in ["proposed", "waiting_for_user", "approved", "rejected", "executed", "expired"]:
            action = ActionQueue(
                email_id=email.id,
                action_type="move",
                payload={},
                status=state,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(action)

        db_session.commit()

        all_actions = db_session.query(ActionQueue).all()
        states = {a.status for a in all_actions}
        assert "waiting_for_user" in states
        assert "expired" in states


# ── Decision Event Schema Tests ──────────────────────────────────────────


class TestDecisionEventSchema:
    def test_decision_event_has_learning_fields(self, db_session, sample_email):
        """Test that DecisionEvent has the new sender/subject/category/folder fields."""
        event = DecisionEvent(
            email_id=sample_email.id,
            event_type="manual_classify",
            source="user_manual",
            sender="alice@example.com",
            subject_snippet="Test Subject",
            chosen_category="work",
            chosen_folder="Work/Projects",
            user_confirmed=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(event)
        db_session.commit()
        db_session.refresh(event)

        assert event.sender == "alice@example.com"
        assert event.subject_snippet == "Test Subject"
        assert event.chosen_category == "work"
        assert event.chosen_folder == "Work/Projects"


# ── SenderProfile Learning Fields Tests ──────────────────────────────────


class TestSenderProfileLearningFields:
    def test_sender_profile_has_preference_fields(self, db_session):
        """Test that SenderProfile has preferred_category/folder/count."""
        profile = SenderProfile(
            sender_address="test@example.com",
            sender_domain="example.com",
            preferred_category="work",
            preferred_folder="Work",
            user_classification_count=3,
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        assert profile.preferred_category == "work"
        assert profile.preferred_folder == "Work"
        assert profile.user_classification_count == 3


# ── API Endpoint Tests (using TestClient) ────────────────────────────────


class TestClassifyEndpoint:
    """Test the /api/emails/{id}/classify endpoint via TestClient."""

    @pytest.fixture(autouse=True)
    def setup_client(self, db_session):
        """Set up TestClient with overridden DB dependency."""
        from fastapi.testclient import TestClient
        from src.main import app
        from src.database.connection import get_db

        self._original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = lambda: db_session
        self.client = TestClient(app)
        self.db = db_session
        self.headers = {"Authorization": "Bearer test_key_abc123"}
        yield
        self.client.close()
        app.dependency_overrides = self._original_overrides

    def test_classify_success(self):
        email = ProcessedEmail(
            message_id="api-001@example.com",
            uid="901",
            sender="bob@corp.com",
            subject="API Test",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(email)
        self.db.commit()

        resp = self.client.post(
            f"/api/emails/{email.id}/classify",
            json={"category": "work", "target_folder": "Work"},
            headers=self.headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["category"] == "work"
        assert body["target_folder"] == "Work"
        assert body["sender_profile_updated"] is True

    def test_classify_invalid_category(self):
        email = ProcessedEmail(
            message_id="api-002@example.com",
            uid="902",
            sender="bob@corp.com",
            subject="API Test",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(email)
        self.db.commit()

        resp = self.client.post(
            f"/api/emails/{email.id}/classify",
            json={"category": "invalid_cat"},
            headers=self.headers,
        )
        assert resp.status_code == 422  # Pydantic validation error

    def test_classify_nonexistent_email(self):
        resp = self.client.post(
            "/api/emails/99999/classify",
            json={"category": "work"},
            headers=self.headers,
        )
        assert resp.status_code == 404

    def test_classify_without_auth(self):
        resp = self.client.post(
            "/api/emails/1/classify",
            json={"category": "work"},
        )
        assert resp.status_code == 401

    def test_classify_category_only(self):
        email = ProcessedEmail(
            message_id="api-003@example.com",
            uid="903",
            sender="cat-only@test.org",
            subject="Category only",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(email)
        self.db.commit()

        resp = self.client.post(
            f"/api/emails/{email.id}/classify",
            json={"category": "newsletter"},
            headers=self.headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["category"] == "newsletter"
        assert body["target_folder"] is None


class TestActionQueueDefaultFilter:
    """Test that the /api/actions endpoint filters correctly by default."""

    @pytest.fixture(autouse=True)
    def setup_client(self, db_session):
        from fastapi.testclient import TestClient
        from src.main import app
        from src.database.connection import get_db

        self._original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = lambda: db_session
        self.client = TestClient(app)
        self.db = db_session
        self.headers = {"Authorization": "Bearer test_key_abc123"}
        yield
        self.client.close()
        app.dependency_overrides = self._original_overrides

    def _make_action(self, status, email_id=None):
        if not email_id:
            email = ProcessedEmail(
                message_id=f"aq-{status}-{id(status)}@example.com",
                uid=str(id(status)),
                sender="test@example.com",
                subject="Test",
                is_processed=True,
                created_at=datetime.now(timezone.utc),
            )
            self.db.add(email)
            self.db.commit()
            email_id = email.id

        action = ActionQueue(
            email_id=email_id,
            action_type="move",
            payload={"target_folder": "Archive"},
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(action)
        self.db.commit()
        return action

    def test_default_hides_rejected_and_executed(self):
        self._make_action("proposed")
        self._make_action("approved")
        self._make_action("rejected")
        self._make_action("executed")

        resp = self.client.get("/api/actions", headers=self.headers)
        assert resp.status_code == 200
        actions = resp.json()
        statuses = {a["status"] for a in actions}
        assert "proposed" in statuses
        assert "approved" in statuses
        assert "rejected" not in statuses
        assert "executed" not in statuses

    def test_status_all_shows_everything(self):
        self._make_action("proposed")
        self._make_action("rejected")
        self._make_action("executed")

        resp = self.client.get("/api/actions?status=all", headers=self.headers)
        assert resp.status_code == 200
        actions = resp.json()
        statuses = {a["status"] for a in actions}
        assert "proposed" in statuses
        assert "rejected" in statuses
        assert "executed" in statuses

    def test_status_filter_specific(self):
        self._make_action("proposed")
        self._make_action("approved")
        self._make_action("rejected")

        resp = self.client.get("/api/actions?status=rejected", headers=self.headers)
        assert resp.status_code == 200
        actions = resp.json()
        assert all(a["status"] == "rejected" for a in actions)
        assert len(actions) == 1


class TestSenderLearningEndpoint:
    """Test the /api/sender-learning/{sender} endpoint."""

    @pytest.fixture(autouse=True)
    def setup_client(self, db_session):
        from fastapi.testclient import TestClient
        from src.main import app
        from src.database.connection import get_db

        self._original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = lambda: db_session
        self.client = TestClient(app)
        self.db = db_session
        self.headers = {"Authorization": "Bearer test_key_abc123"}
        yield
        self.client.close()
        app.dependency_overrides = self._original_overrides

    def test_no_profile(self):
        resp = self.client.get(
            "/api/sender-learning/unknown@nowhere.com",
            headers=self.headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["preferred_category"] is None

    def test_after_classification(self):
        email = ProcessedEmail(
            message_id="sl-001@example.com",
            uid="801",
            sender="alice@example.com",
            subject="Test",
            category="Unklar",
            is_processed=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(email)
        self.db.commit()

        # Classify the email
        classify_resp = self.client.post(
            f"/api/emails/{email.id}/classify",
            json={"category": "work", "target_folder": "Work"},
            headers=self.headers,
        )
        assert classify_resp.status_code == 200

        # Now check sender learning
        resp = self.client.get(
            "/api/sender-learning/alice@example.com",
            headers=self.headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["preferred_category"] == "work"
        assert body["preferred_folder"] == "Work"
        assert body["user_classification_count"] == 1


# ── Schema Validation Tests ──────────────────────────────────────────────


class TestManualClassifyRequestSchema:
    def test_valid_categories(self):
        from src.models.schemas import ManualClassifyRequest

        for cat in ["work", "private", "newsletter", "todo", "spam"]:
            req = ManualClassifyRequest(category=cat)
            assert req.category == cat

    def test_case_insensitive(self):
        from src.models.schemas import ManualClassifyRequest

        req = ManualClassifyRequest(category="Work")
        assert req.category == "work"

    def test_invalid_category(self):
        from src.models.schemas import ManualClassifyRequest

        with pytest.raises(Exception):
            ManualClassifyRequest(category="invalid")

    def test_optional_target_folder(self):
        from src.models.schemas import ManualClassifyRequest

        req = ManualClassifyRequest(category="work")
        assert req.target_folder is None

        req2 = ManualClassifyRequest(category="work", target_folder="Work/Projects")
        assert req2.target_folder == "Work/Projects"


class TestActionQueueStatusEnum:
    def test_new_states_in_enum(self):
        from src.models.schemas import ActionQueueStatus

        assert ActionQueueStatus.waiting_for_user == "waiting_for_user"
        assert ActionQueueStatus.expired == "expired"
        assert ActionQueueStatus.proposed == "proposed"
        assert ActionQueueStatus.rejected == "rejected"
