"""
Pydantic models for API requests and responses
"""

from pydantic import BaseModel, Field, EmailStr, field_validator
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
    action_required: bool = False
    priority: Optional[str] = None
    suggested_folder: Optional[str] = None
    reasoning: Optional[str] = None
    is_spam: bool = False
    is_archived: bool = False
    is_flagged: bool = False
    is_resolved: bool = False
    tasks: List[TaskResponse] = []
    created_at: datetime
    processed_at: Optional[datetime] = None

    # Coerce NULL DB values (emails not yet analysed) to False so that
    # Pydantic v2 strict bool validation never raises a ValidationError.
    @field_validator(
        "action_required",
        "is_spam",
        "is_archived",
        "is_flagged",
        "is_resolved",
        mode="before",
    )
    @classmethod
    def coerce_none_to_false(cls, v: object) -> object:
        return False if v is None else v

    class Config:
        from_attributes = True


class EmailDetailResponse(EmailResponse):
    body_plain: Optional[str] = None
    body_html: Optional[str] = None
    actions_taken: Optional[dict] = None
    # Override tracking
    overridden: bool = False
    override_rule_id: Optional[int] = None
    original_classification: Optional[dict] = None


class ClassificationOverrideRequest(BaseModel):
    """Body for POST /api/emails/{id}/override"""

    category: Optional[str] = None
    priority: Optional[str] = None
    spam: Optional[bool] = None
    action_required: Optional[bool] = None
    suggested_folder: Optional[str] = None


class ClassificationOverrideResponse(BaseModel):
    """Response from POST /api/emails/{id}/override"""

    success: bool
    email_id: int
    overridden: bool
    rule_id: Optional[int] = None
    rule_created: bool = False
    classification: dict


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
    # Live in-progress run state (same source as /api/status).
    # When no run is active this mirrors the last completed run_status.
    run_status: Optional[dict] = None
    # True when a daily report has been generated and is ready to view.
    daily_report_available: bool = False
    # Indicates whether SAFE_MODE is active in backend config.
    safe_mode: bool = True


class ReportEmailItem(BaseModel):
    """A single email entry inside the structured daily report."""

    email_id: int
    thread_id: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    summary: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None


class ReportTotals(BaseModel):
    total_processed: int = 0
    action_required: int = 0
    unresolved: int = 0
    spam_detected: int = 0


class ReportSuggestedAction(BaseModel):
    """
    A suggested IMAP action tied to a specific email.

    In SAFE MODE these are *not* executed automatically.  The frontend
    renders them as clickable buttons that submit the action to the
    approval/action-queue flow.
    """

    email_id: int
    thread_id: Optional[str] = None
    action_type: str  # move | archive | mark_spam | delete | mark_read | mark_resolved | reply_draft
    payload: Optional[dict] = None
    target_folder: Optional[str] = (
        None  # Backward-compatible mirror for move/archive actions
    )
    safe_mode: bool = True
    description: str  # human-readable label shown in the UI
    queue_status: Optional[str] = None
    queue_action_id: Optional[int] = None
    queue_error: Optional[str] = None
    thread_suggestion_count: Optional[int] = None


class DailyReportResponse(BaseModel):
    generated_at: str
    period_hours: int = 24
    totals: ReportTotals = ReportTotals()
    # Flat counters (backward-compatible)
    total_processed: int = 0
    action_required: int = 0
    spam_detected: int = 0
    unresolved: int = 0
    # Structured item lists for frontend rendering
    important_items: List[ReportEmailItem] = []
    action_items: List[ReportEmailItem] = []
    unresolved_items: List[ReportEmailItem] = []
    spam_items: List[ReportEmailItem] = []
    # Clickable suggested actions (safe-mode-aware)
    suggested_actions: List[ReportSuggestedAction] = []
    report_text: str  # AI-generated or fallback plain-text summary


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


class QueueSuggestedActionRequest(BaseModel):
    email_id: int
    thread_id: Optional[str] = None
    action_type: str
    payload: Optional[dict] = None
    safe_mode: bool = True
    description: Optional[str] = None
    source: Optional[str] = "daily_report"


class ReportDecisionEventRequest(BaseModel):
    event_type: str
    email_id: int
    thread_id: Optional[str] = None
    action_queue_id: Optional[int] = None
    source: Optional[str] = "report_suggestion"


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


class ActionQueueStatus(str, Enum):
    proposed = "proposed"
    approved = "approved"
    executed = "executed"
    failed = "failed"
    rejected = "rejected"


class ActionQueueResponse(BaseModel):
    id: int
    email_id: int
    thread_id: Optional[str] = None
    action_type: str
    payload: Optional[dict] = None
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    source: Optional[str] = None

    class Config:
        from_attributes = True
