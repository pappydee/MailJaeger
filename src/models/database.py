"""
Database models for MailJaeger
"""
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, 
    DateTime, JSON, ForeignKey, Index
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
    
    # Header information
    subject = Column(String(500))
    sender = Column(String(200), index=True)
    recipients = Column(Text)
    date = Column(DateTime, index=True)
    
    # Content
    body_plain = Column(Text)
    body_html = Column(Text)
    
    # AI Analysis Results
    summary = Column(Text)
    category = Column(String(50), index=True)  # Klinik, Forschung, Privat, Verwaltung, Unklar
    spam_probability = Column(Float)
    action_required = Column(Boolean, index=True)
    priority = Column(String(20), index=True)  # LOW, MEDIUM, HIGH
    suggested_folder = Column(String(200))
    reasoning = Column(Text)
    
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
    tasks = relationship("EmailTask", back_populates="email", cascade="all, delete-orphan")
    learning_signals = relationship("LearningSignal", back_populates="email", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('idx_action_priority', 'action_required', 'priority'),
        Index('idx_category_date', 'category', 'date'),
        Index('idx_spam_processed', 'is_spam', 'is_processed'),
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


class PendingAction(Base):
    """Pending IMAP action requiring approval"""
    __tablename__ = "pending_actions"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    approved_at = Column(DateTime)
    applied_at = Column(DateTime)
    
    # Action details
    email_id = Column(Integer, ForeignKey("processed_emails.id"), nullable=False, index=True)
    action_type = Column(String(50), nullable=False, index=True)  # MARK_READ, MOVE_FOLDER, FLAG, DELETE
    target_folder = Column(String(200))  # Only for MOVE_FOLDER
    reason = Column(String(200))  # spam, action_required, archive_policy, ai_suggestion, etc.
    proposed_by = Column(String(50), nullable=False)  # system or user
    
    # Status tracking
    status = Column(String(20), nullable=False, default="PENDING", index=True)  # PENDING, APPROVED, REJECTED, APPLIED, FAILED
    approved_by = Column(String(200))  # Safe placeholder like token hash
    
    # Error tracking
    error_code = Column(String(50))
    error_message = Column(Text)  # Sanitized error message
    
    # Indexes
    __table_args__ = (
        Index('idx_status_created', 'status', 'created_at'),
        Index('idx_email_status', 'email_id', 'status'),
    )


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
