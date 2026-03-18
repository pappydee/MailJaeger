"""
Database models for MailJaeger
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Float,
    Boolean,
    DateTime,
    JSON,
    ForeignKey,
    Index,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()


class ProcessedEmail(Base):
    """Processed email record"""

    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True, index=True)

    # Email identifiers
    message_id = Column(String(500), unique=True, index=True, nullable=False)
    uid = Column(String(100), index=True)
    imap_uid = Column(String(100), index=True)  # Explicit IMAP UID for ingestion pipeline

    # Thread reconstruction (Priority 3)
    thread_id = Column(String(200), index=True)  # Derived from Message-ID/In-Reply-To/References

    # Header information
    subject = Column(String(500))
    sender = Column(String(200), index=True)
    recipients = Column(Text)
    date = Column(DateTime, index=True)
    received_at = Column(DateTime, index=True)  # Explicit receive time
    folder = Column(String(200), index=True)  # Source IMAP folder

    # Content
    body_plain = Column(Text)
    body_html = Column(Text)
    snippet = Column(String(500))  # First ~200 chars of plain text for quick display

    # Body deduplication (Priority 4)
    body_hash = Column(String(64), index=True)  # SHA256 of normalized body for dedup

    # Flags from IMAP
    flags = Column(JSON)  # List of IMAP flags e.g. ["\\Seen", "\\Flagged"]

    # AI Analysis Results
    summary = Column(Text)
    category = Column(
        String(50), index=True
    )  # Klinik, Forschung, Privat, Verwaltung, Unklar
    spam_probability = Column(Float)
    action_required = Column(Boolean, index=True)
    priority = Column(String(20), index=True)  # LOW, MEDIUM, HIGH
    suggested_folder = Column(String(200))
    reasoning = Column(Text)

    # Multi-stage analysis pipeline state (Priority 9)
    analysis_state = Column(
        String(50), default="pending", index=True
    )  # pending, pre_classified, classified, deep_analyzed, skipped
    analysis_version = Column(String(20))  # Version of the analysis model/pipeline
    importance_score = Column(Float)  # Computed importance 0.0-1.0

    # Classification override tracking
    overridden = Column(Boolean, default=False, index=True)
    override_rule_id = Column(Integer, nullable=True)  # FK to ClassificationOverride.id
    original_classification = Column(JSON, nullable=True)  # snapshot before override

    # Processing metadata
    is_spam = Column(Boolean, default=False, index=True)
    is_processed = Column(Boolean, default=False, index=True)
    is_archived = Column(Boolean, default=False)
    is_flagged = Column(Boolean, default=False)
    is_resolved = Column(Boolean, default=False, index=True)

    # Actions taken
    actions_taken = Column(JSON)

    # Integrity
    integrity_hash = Column(String(64))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    processed_at = Column(DateTime, index=True)
    resolved_at = Column(DateTime)

    # Relationships
    tasks = relationship(
        "EmailTask", back_populates="email", cascade="all, delete-orphan"
    )
    learning_signals = relationship(
        "LearningSignal", back_populates="email", cascade="all, delete-orphan"
    )
    pending_actions = relationship(
        "PendingAction", back_populates="email", cascade="all, delete-orphan"
    )
    decision_events = relationship(
        "DecisionEvent", back_populates="email", cascade="all, delete-orphan"
    )
    action_queue_items = relationship(
        "ActionQueue", back_populates="email", cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index("idx_action_priority", "action_required", "priority"),
        Index("idx_category_date", "category", "date"),
        Index("idx_spam_processed", "is_spam", "is_processed"),
        Index("idx_thread_date", "thread_id", "date"),
        Index("idx_body_hash", "body_hash"),
        Index("idx_analysis_state", "analysis_state"),
    )


class EmailTask(Base):
    """Extracted task from email"""

    __tablename__ = "email_tasks"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False)

    # Task details
    description = Column(Text, nullable=False)
    due_date = Column(DateTime)
    context = Column(Text)
    confidence = Column(Float)

    # Status
    is_completed = Column(Boolean, default=False, index=True)
    completed_at = Column(DateTime)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    email = relationship("ProcessedEmail", back_populates="tasks")


class ProcessingRun(Base):
    """Processing run record"""

    __tablename__ = "processing_runs"

    id = Column(Integer, primary_key=True, index=True)

    # Run metadata
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)
    status = Column(String(20), index=True)  # SUCCESS, FAILURE, PARTIAL

    # Statistics
    emails_processed = Column(Integer, default=0)
    emails_spam = Column(Integer, default=0)
    emails_archived = Column(Integer, default=0)
    emails_action_required = Column(Integer, default=0)
    emails_failed = Column(Integer, default=0)

    # Error information
    error_message = Column(Text)
    error_details = Column(JSON)

    # Trigger
    trigger_type = Column(String(20))  # SCHEDULED, MANUAL


class LearningSignal(Base):
    """Learning signal from user behavior"""

    __tablename__ = "learning_signals"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False)

    # Signal type
    signal_type = Column(String(50), index=True)  # FOLDER_MOVE, CATEGORY_CHANGE, etc.

    # Original and new values
    original_value = Column(String(200))
    new_value = Column(String(200))

    # Context
    context = Column(JSON)

    # Timestamps
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    email = relationship("ProcessedEmail", back_populates="learning_signals")


class FolderPattern(Base):
    """Learned folder patterns"""

    __tablename__ = "folder_patterns"

    id = Column(Integer, primary_key=True, index=True)

    # Pattern
    sender_pattern = Column(String(200), index=True)
    subject_pattern = Column(String(200))
    category = Column(String(50))

    # Target folder
    target_folder = Column(String(200))

    # Statistics
    occurrence_count = Column(Integer, default=1)
    success_count = Column(Integer, default=0)
    confidence = Column(Float, default=0.0)

    # Timestamps
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow, index=True)
    last_applied = Column(DateTime)


class AuditLog(Base):
    """Audit log for all actions"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)

    # Event details
    event_type = Column(String(50), index=True)
    email_message_id = Column(String(500), index=True)
    description = Column(Text)

    # Data
    data = Column(JSON)

    # Timestamp
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PendingAction(Base):
    """Pending IMAP actions awaiting approval"""

    __tablename__ = "pending_actions"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False)

    # Action details
    action_type = Column(
        String(50), nullable=False, index=True
    )  # MOVE_FOLDER, MARK_READ, ADD_FLAG, DELETE
    target_folder = Column(String(200))  # For MOVE_FOLDER actions

    # Status
    status = Column(
        String(20), default="PENDING", index=True
    )  # PENDING, APPROVED, REJECTED, APPLIED, FAILED

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    approved_at = Column(DateTime)
    applied_at = Column(DateTime)

    # Error tracking
    error_message = Column(Text)

    # Relationships
    email = relationship("ProcessedEmail", back_populates="pending_actions")

    # Indexes
    __table_args__ = (Index("idx_status_created", "status", "created_at"),)


class ApplyToken(Base):
    """Short-lived tokens for two-step action application"""

    __tablename__ = "apply_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)

    # Token metadata
    action_ids = Column(
        JSON, nullable=False
    )  # List of action IDs this token is valid for
    action_count = Column(Integer, nullable=False)  # Number of actions

    # Summary for verification
    summary = Column(JSON)  # Summary of actions by type and folder

    # Expiry
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime)

    # Status
    is_used = Column(Boolean, default=False, index=True)

    # Indexes
    __table_args__ = (
        Index("idx_token_expires", "token", "expires_at"),
        Index("idx_used_expires", "is_used", "expires_at"),
    )


class ClassificationOverride(Base):
    """Application-level classification override rules (no LLM fine-tuning)."""

    __tablename__ = "classification_overrides"

    id = Column(Integer, primary_key=True, index=True)

    # Match criteria — at least one must be set
    sender_pattern = Column(String(200), nullable=True, index=True)  # domain or full addr
    subject_pattern = Column(String(500), nullable=True)              # keyword substring

    # Override values (any subset may be set)
    category = Column(String(50), nullable=True)
    priority = Column(String(20), nullable=True)
    spam = Column(Boolean, nullable=True)
    action_required = Column(Boolean, nullable=True)
    suggested_folder = Column(String(200), nullable=True)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_from_email_id = Column(Integer, nullable=True)  # source email (informational)


class DecisionEvent(Base):
    """Decision events table — captures all system and user decisions (Priority 5)"""

    __tablename__ = "decision_events"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False)
    thread_id = Column(String(200), index=True)

    # Event classification
    event_type = Column(
        String(50), nullable=False, index=True
    )  # move_to_folder, mark_spam, archive, approve_suggestion, reject_suggestion
    source = Column(
        String(50), index=True
    )  # system, user, rule, llm, pipeline_stage1, pipeline_stage2

    # Before/after values
    old_value = Column(String(200))
    new_value = Column(String(200))

    # Confidence and versioning
    confidence = Column(Float)
    model_version = Column(String(50))
    rule_id = Column(Integer, nullable=True)  # FK to ClassificationOverride if rule-based

    # User confirmation state
    user_confirmed = Column(Boolean, default=False, index=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    email = relationship("ProcessedEmail", back_populates="decision_events")

    __table_args__ = (
        Index("idx_decision_email_type", "email_id", "event_type"),
        Index("idx_decision_thread", "thread_id", "created_at"),
    )


class ActionQueue(Base):
    """Structured action queue for propose/approve/execute flow."""

    __tablename__ = "action_queue"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False)
    thread_id = Column(String(200), index=True, nullable=True)

    # Action details
    action_type = Column(String(50), nullable=False, index=True)
    payload = Column(JSON)

    # Compatibility note:
    # Legacy state-machine docs/tests reference:
    # proposed_action, queued_action, approved_action, executed_action,
    # rejected_action, failed_action.
    # The API foundation introduced in this branch uses:
    # proposed, approved, executed, failed.
    status = Column(String(30), default="proposed_action", index=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    queued_at = Column(DateTime)
    approved_at = Column(DateTime)
    executed_at = Column(DateTime)

    # Error tracking
    error_message = Column(Text)

    # Relationships
    email = relationship("ProcessedEmail", back_populates="action_queue_items")

    __table_args__ = (
        Index("idx_action_queue_status", "status"),
        Index("idx_action_queue_email", "email_id"),
        Index("idx_action_queue_thread", "thread_id"),
    )


class DailyReport(Base):
    """Cached daily report snapshots and async generation status."""

    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, index=True)
    generated_at = Column(DateTime, default=datetime.utcnow, index=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    report_json = Column(JSON)
    report_text = Column(Text)
    generation_status = Column(String(20), default="pending", nullable=False, index=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_daily_reports_generated_at", "generated_at"),
        Index("idx_daily_reports_status_generated", "generation_status", "generated_at"),
    )


class AnalysisProgress(Base):
    """Analysis progress tracking for pausable large-scale processing (Priority 7)"""

    __tablename__ = "analysis_progress"

    id = Column(Integer, primary_key=True, index=True)

    # Stage identification
    stage = Column(
        String(100), nullable=False, index=True
    )  # ingestion, pre_classification, classification, deep_analysis
    run_id = Column(String(100), index=True)  # Links to ProcessingRun

    # Progress tracking
    last_email_id = Column(Integer)  # Last processed email ID for resumption
    last_message_id = Column(String(500))  # Last processed Message-ID
    processed_count = Column(Integer, default=0)
    total_count = Column(Integer)  # Total emails to process (may be estimated)
    failed_count = Column(Integer, default=0)

    # State
    status = Column(
        String(20), default="running", index=True
    )  # running, paused, completed, failed
    paused_reason = Column(String(200))  # Why processing was paused

    # Resource tracking
    llm_calls_used = Column(Integer, default=0)
    elapsed_seconds = Column(Float, default=0.0)

    # Timestamps
    started_at = Column(DateTime, default=datetime.utcnow)
    paused_at = Column(DateTime)
    resumed_at = Column(DateTime)
    completed_at = Column(DateTime)
    timestamp = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    __table_args__ = (
        Index("idx_progress_stage_status", "stage", "status"),
        Index("idx_progress_run", "run_id"),
    )
