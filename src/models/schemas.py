"""
Pydantic models for API requests and responses
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime
from enum import Enum


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Category(str, Enum):
    KLINIK = "Klinik"
    FORSCHUNG = "Forschung"
    PRIVAT = "Privat"
    VERWALTUNG = "Verwaltung"
    UNKLAR = "Unklar"


class ProcessingStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"


class TaskResponse(BaseModel):
    id: int
    description: str
    due_date: Optional[datetime] = None
    context: Optional[str] = None
    confidence: Optional[float] = None
    is_completed: bool
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EmailResponse(BaseModel):
    id: int
    message_id: str
    subject: Optional[str] = None
    sender: Optional[str] = None
    recipients: Optional[str] = None
    date: Optional[datetime] = None
    summary: Optional[str] = None
    category: Optional[str] = None
    spam_probability: Optional[float] = None
    action_required: bool
    priority: Optional[str] = None
    suggested_folder: Optional[str] = None
    reasoning: Optional[str] = None
    is_spam: bool
    is_archived: bool
    is_flagged: bool
    is_resolved: bool
    tasks: List[TaskResponse] = []
    created_at: datetime
    processed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EmailDetailResponse(EmailResponse):
    body_plain: Optional[str] = None
    body_html: Optional[str] = None
    actions_taken: Optional[dict] = None


class ProcessingRunResponse(BaseModel):
    id: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    emails_processed: int
    emails_spam: int
    emails_archived: int
    emails_action_required: int
    emails_failed: int
    error_message: Optional[str] = None
    trigger_type: Optional[str] = None

    class Config:
        from_attributes = True


class DashboardResponse(BaseModel):
    last_run: Optional[ProcessingRunResponse] = None
    next_scheduled_run: Optional[str] = None
    total_emails: int
    action_required_count: int
    unresolved_count: int
    health_status: dict


class EmailListRequest(BaseModel):
    action_required: Optional[bool] = None
    priority: Optional[Priority] = None
    category: Optional[Category] = None
    is_spam: Optional[bool] = None
    is_resolved: Optional[bool] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    sort_by: str = Field(default="date", pattern="^(date|priority|subject)$")
    sort_order: str = Field(default="desc", pattern="^(asc|desc)$")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class SearchRequest(BaseModel):
    query: str
    category: Optional[Category] = None
    priority: Optional[Priority] = None
    action_required: Optional[bool] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    semantic: bool = False
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class MarkResolvedRequest(BaseModel):
    email_id: int
    resolved: bool = True


class TriggerRunRequest(BaseModel):
    trigger_type: str = "MANUAL"


class SettingsUpdate(BaseModel):
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    spam_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    ai_endpoint: Optional[str] = None
    ai_model: Optional[str] = None
    schedule_time: Optional[str] = None
    learning_confidence_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    store_email_body: Optional[bool] = None
    store_attachments: Optional[bool] = None


class PendingActionStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class PendingActionResponse(BaseModel):
    id: int
    email_id: int
    action_type: str
    target_folder: Optional[str] = None
    status: str
    created_at: datetime
    approved_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class PendingActionWithEmailResponse(PendingActionResponse):
    email: EmailResponse


class ApproveActionRequest(BaseModel):
    approve: bool = True


class ApplyActionsRequest(BaseModel):
    dry_run: bool = False
    action_ids: Optional[List[int]] = None
    max_count: Optional[int] = None
    apply_token: Optional[str] = None


class PreviewActionsRequest(BaseModel):
    action_ids: Optional[List[int]] = None
    max_count: Optional[int] = None


class PreviewActionsResponse(BaseModel):
    success: bool
    apply_token: str
    token_expires_at: datetime
    action_count: int
    summary: dict
    actions: List[dict]


class ApplyActionsResponse(BaseModel):
    success: bool
    applied: int
    failed: int
    actions: List[dict]
