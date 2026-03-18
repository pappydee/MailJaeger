"""
FastAPI application for MailJaeger
"""

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
    Body,
    BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import sys
import secrets
import hashlib
import json
import time

from src.config import get_settings
from src.database.connection import init_db, get_db, get_engine, get_db_session
from src.database.startup_checks import verify_pending_actions_table
from src.models.schemas import (
    EmailResponse,
    EmailDetailResponse,
    DashboardResponse,
    DailyReportResponse,
    DailyReportEndpointResponse,
    DailyReportThreadGroup,
    ReportTotals,
    ReportEmailItem,
    ReportSuggestedAction,
    ProcessingRunResponse,
    EmailListRequest,
    SearchRequest,
    MarkResolvedRequest,
    TriggerRunRequest,
    SettingsUpdate,
    PendingActionResponse,
    PendingActionWithEmailResponse,
    ApproveActionRequest,
    QueueSuggestedActionRequest,
    ReportDecisionEventRequest,
    ApplyActionsRequest,
    PreviewActionsRequest,
    PreviewActionsResponse,
    ApplyActionsResponse,
    ActionQueueResponse,
    ClassificationOverrideRequest,
    ClassificationOverrideResponse,
)
from src.models.database import (
    ProcessedEmail,
    ProcessingRun,
    PendingAction,
    ApplyToken,
    ClassificationOverride,
    ActionQueue,
    DecisionEvent,
    DailyReport,
    AppSetting,
)
from src.services.scheduler import get_scheduler, get_run_status
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.services.search_service import SearchService
from src.services.learning_service import LearningService
from src.services.email_processor import EmailProcessor
from src.services.action_executor import ActionExecutor
from src.services.thread_context import (
    get_thread_summary,
    normalize_thread_state,
    update_thread_state_for_thread,
)
from src.services.thread_aggregator import (
    build_thread_context,
    query_open_action_count,
    thread_sort_key,
)
from src.services.thread_summary_service import ThreadSummaryService
from src.middleware.auth import require_authentication, AuthenticationError
from src.middleware.session_store import _sessions, SESSION_COOKIE, SESSION_EXPIRY_HOURS
from src.middleware.security_headers import SecurityHeadersMiddleware
from src.middleware.allowed_hosts import AllowedHostsMiddleware
from src.middleware.rate_limiting import limiter, rate_limit_exceeded_handler
from src.utils.logging import setup_logging, get_logger
from src.utils.error_handling import sanitize_error
from src import __version__, CHANGELOG

# Setup logging
setup_logging()
logger = get_logger(__name__)

DAILY_REPORT_ACTION_TYPES = {
    "move",
    "archive",
    "mark_spam",
    "delete",
    "mark_read",
    "mark_resolved",
    "reply_draft",
}
DECISION_EVENT_TYPES = {
    "approve_suggestion",
    "reject_suggestion",
    "execute_suggestion",
    "queue_suggestion",
    "preview_reply_draft",
    "open_related_email_from_report",
}
DECISION_EVENT_SOURCES = {"daily_report", "report_suggestion", "queue_ui", "user"}
APP_SETTING_SAFE_MODE = "safe_mode"
APP_SETTING_ARCHIVE_FOLDER = "archive_folder"
APP_SETTING_IMAP_FOLDERS_CACHE = "imap_folders_cache"
DAILY_REPORT_SCHEMA_VERSION = 2

# In-memory session store: imported from session_store so that
# require_authentication() in auth.py can validate cookies without a
# circular import.  _sessions is the same dict object in both modules.
# (SESSION_COOKIE and SESSION_EXPIRY_HOURS are also imported above.)

# Settings with validation
try:
    settings = get_settings()
    settings.validate_required_settings()
except ValueError as e:
    # Use sanitize_error to prevent credential leakage in logs
    sanitized = sanitize_error(e, debug=False)
    logger.error("Configuration validation failed: %s", sanitized)
    # Redact stderr output even when showing user-facing error
    stderr_msg = sanitize_error(
        e, debug=settings.debug if hasattr(settings, "debug") else False
    )
    print(f"\n❌ Configuration Error:\n{stderr_msg}\n", file=sys.stderr)
    print("Please check your .env file and environment variables.", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    # Use sanitize_error to prevent credential leakage in logs
    sanitized = sanitize_error(e, debug=False)
    logger.error("Failed to load configuration: %s", sanitized)
    # Redact stderr output
    stderr_msg = sanitize_error(e, debug=False)
    print(f"\n❌ Configuration Error: {stderr_msg}\n", file=sys.stderr)
    sys.exit(1)

# Create app
app = FastAPI(
    title="MailJaeger",
    description="Local AI-powered email processing system (Secure)",
    version=__version__,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)


def _service_is_unhealthy(health: dict) -> bool:
    """Return True when a service health-check dict indicates a non-healthy state."""
    return isinstance(health, dict) and health.get("status") not in ("healthy", "ok")


def _daily_report_available(db: Session) -> bool:
    """Return True when at least one email was processed in the last 24 hours."""
    try:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        count = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.processed_at >= cutoff)
            .count()
        )
        return count > 0
    except Exception:
        return False


def _is_report_suggested_action(action: ActionQueue) -> bool:
    payload = action.payload or {}
    return (
        isinstance(payload, dict) and payload.get("source") == "daily_report_suggestion"
    )


def _record_decision_event(
    db: Session,
    *,
    email_id: int,
    thread_id: Optional[str],
    event_type: str,
    source: str = "user",
    old_value: Optional[str],
    new_value: Optional[str],
    user_confirmed: bool,
) -> None:
    normalized_source = source if source in DECISION_EVENT_SOURCES else "user"
    db.add(
        DecisionEvent(
            email_id=email_id,
            thread_id=thread_id,
            event_type=event_type,
            source=normalized_source,
            old_value=old_value,
            new_value=new_value,
            user_confirmed=user_confirmed,
        )
    )


def _build_reply_draft_payload(subject: Optional[str]) -> Dict[str, str]:
    safe_subject = subject or "(kein Betreff)"
    return {
        "draft_summary": f"Antwortentwurf für {safe_subject}",
        "draft_text": (
            f"Hallo,\n\nvielen Dank für Ihre Nachricht "
            f"zu „{subject or 'dem Thema'}“.\n"
            "Ich melde mich mit einer detaillierten Rückmeldung.\n\n"
            "Viele Grüße"
        ),
    }


def _set_app_setting(db: Session, *, key: str, value) -> None:
    setting = db.query(AppSetting).filter(AppSetting.key == key).first()
    if setting:
        setting.value = value
        setting.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=key, value=value))
    db.flush()


def _set_noncritical_cache_setting(
    db: Session,
    *,
    key: str,
    value,
    max_attempts: int = 2,
    retry_backoff_seconds: float = 0.05,
) -> bool:
    """Best-effort persistence for non-critical cache values."""
    for attempt in range(1, max_attempts + 1):
        try:
            _set_app_setting(db, key=key, value=value)
            return True
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            db.rollback()
            if attempt < max_attempts:
                time.sleep(retry_backoff_seconds)
                continue
            logger.warning(
                "Non-critical cache write skipped for '%s' due to SQLite lock: %s",
                key,
                sanitize_error(exc, debug=settings.debug),
            )
            return False


def _get_app_setting(db: Session, *, key: str):
    setting = db.query(AppSetting).filter(AppSetting.key == key).first()
    return setting.value if setting else None


def _apply_persisted_safe_mode(db: Session) -> bool:
    persisted_safe_mode = _get_app_setting(db, key=APP_SETTING_SAFE_MODE)
    if persisted_safe_mode is None:
        _set_app_setting(
            db, key=APP_SETTING_SAFE_MODE, value=bool(get_settings().safe_mode)
        )
        return bool(get_settings().safe_mode)

    get_settings().safe_mode = bool(persisted_safe_mode)
    return bool(get_settings().safe_mode)


def _apply_persisted_archive_folder(db: Session) -> str:
    persisted_archive_folder = _get_app_setting(db, key=APP_SETTING_ARCHIVE_FOLDER)
    if isinstance(persisted_archive_folder, str) and persisted_archive_folder.strip():
        get_settings().archive_folder = persisted_archive_folder.strip()
        return get_settings().archive_folder
    return get_settings().archive_folder


def _normalize_folder_name(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _folder_pref_keys_for_email(email: Optional[ProcessedEmail], action_type: str) -> List[str]:
    keys = []
    if email and email.category:
        keys.append(f"folder_pref::{action_type}::category::{email.category.lower()}")
    if email and email.sender and "@" in email.sender:
        domain = email.sender.rsplit("@", 1)[-1].strip().lower()
        if domain:
            keys.append(f"folder_pref::{action_type}::domain::{domain}")
    keys.append(f"folder_pref::{action_type}")
    return keys


def _learn_folder_preference(
    db: Session,
    *,
    action_type: str,
    target_folder: Optional[str],
    email: Optional[ProcessedEmail] = None,
) -> None:
    folder = (target_folder or "").strip()
    if not folder:
        return
    normalized_action = (action_type or "").strip().lower()
    if normalized_action not in {"move", "archive"}:
        return
    for key in _folder_pref_keys_for_email(email, normalized_action):
        _set_app_setting(db, key=key, value=folder)
    if normalized_action in {"move", "archive"}:
        _set_app_setting(db, key=APP_SETTING_ARCHIVE_FOLDER, value=folder)
        get_settings().archive_folder = folder


def _discover_live_imap_folders() -> List[Dict[str, Any]]:
    try:
        with IMAPService() as imap:
            folders = imap.list_folders()
            return folders if isinstance(folders, list) else []
    except Exception as exc:
        logger.warning(
            "Could not fetch IMAP folder list: %s",
            sanitize_error(exc, debug=settings.debug),
        )
        return []


def _choose_archive_folder_from_discovered(folders: List[Dict[str, Any]]) -> Optional[str]:
    if not folders:
        return None
    names = [str(folder.get("name", "")) for folder in folders if folder.get("name")]
    normalized_map = {name: _normalize_folder_name(name) for name in names}

    # Strong preference: explicit archive semantics in multiple languages.
    for needle in ("archive", "archiv"):
        for name, normalized in normalized_map.items():
            if needle in normalized:
                return name

    # Secondary: "All mail"/"Alles …" style folders often used as archive sink.
    for needle in ("all mail", "alles"):
        for name, normalized in normalized_map.items():
            if needle in normalized:
                return name

    # Fallback: first non-INBOX user folder.
    for name, normalized in normalized_map.items():
        if normalized and normalized != "inbox":
            return name
    return None


def _resolve_archive_folder(
    db: Session,
    *,
    email: Optional[ProcessedEmail] = None,
    allow_live_discovery: bool = False,
) -> Optional[str]:
    configured = _get_app_setting(db, key=APP_SETTING_ARCHIVE_FOLDER)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()

    for key in _folder_pref_keys_for_email(email, "archive"):
        candidate = _get_app_setting(db, key=key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for key in _folder_pref_keys_for_email(email, "move"):
        candidate = _get_app_setting(db, key=key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    cached = _get_app_setting(db, key=APP_SETTING_IMAP_FOLDERS_CACHE)
    if isinstance(cached, dict) and isinstance(cached.get("folders"), list):
        candidate = _choose_archive_folder_from_discovered(cached.get("folders", []))
        if candidate:
            return candidate

    if allow_live_discovery:
        live_folders = _discover_live_imap_folders()
        if live_folders:
            _ = _set_noncritical_cache_setting(
                db,
                key=APP_SETTING_IMAP_FOLDERS_CACHE,
                value={
                    "folders": live_folders,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            candidate = _choose_archive_folder_from_discovered(live_folders)
            if candidate:
                return candidate
    return None


def _item_has_thread_intelligence(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    thread_state = item.get("thread_state")
    thread_priority = item.get("thread_priority")
    thread_importance_score = item.get("thread_importance_score")
    return bool(thread_state) and bool(thread_priority) and (
        isinstance(thread_importance_score, (int, float))
    )


def _is_daily_report_payload_compatible(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("report_version") != DAILY_REPORT_SCHEMA_VERSION:
        return False

    threads = payload.get("threads")
    if not isinstance(threads, list):
        return False
    for thread in threads:
        if not isinstance(thread, dict):
            return False
        if not all(
            key in thread
            for key in (
                "thread_id",
                "thread_state",
                "priority",
                "importance_score",
                "emails",
            )
        ):
            return False
        if not isinstance(thread.get("emails"), list):
            return False

    for section in ("important_items", "action_items", "unresolved_items", "spam_items"):
        items = payload.get(section)
        if not isinstance(items, list):
            return False
        for item in items:
            if not _item_has_thread_intelligence(item):
                return False
    return True


def _normalize_action_status(status_value: Optional[str]) -> str:
    aliases = {
        "proposed_action": "proposed",
        "approved_action": "approved",
        "executed_action": "executed",
        "failed_action": "failed",
        "rejected_action": "rejected",
    }
    return aliases.get((status_value or "").lower(), (status_value or "").lower())


def _validate_daily_report_action_payload(
    action_type: str, payload: Dict, *, email: ProcessedEmail
) -> Dict:
    """Validate and normalize queue payloads from report suggestions."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    normalized_payload = dict(payload)

    if action_type in ("move", "archive", "mark_spam"):
        target_folder = normalized_payload.get("target_folder")
        if not isinstance(target_folder, str) or not target_folder.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{action_type} requires payload.target_folder",
            )
        normalized_payload["target_folder"] = target_folder.strip()

    if action_type == "mark_resolved":
        reason = normalized_payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise HTTPException(
                status_code=400,
                detail="mark_resolved reason must be a non-empty string",
            )

    if action_type == "reply_draft":
        summary = normalized_payload.get("draft_summary")
        text = normalized_payload.get("draft_text")
        if summary is not None and (
            not isinstance(summary, str) or not summary.strip()
        ):
            raise HTTPException(
                status_code=400,
                detail="reply_draft payload.draft_summary must be a non-empty string",
            )
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise HTTPException(
                status_code=400,
                detail="reply_draft payload.draft_text must be a non-empty string",
            )

    if action_type in ("delete", "mark_read", "mark_resolved", "reply_draft"):
        normalized_payload.pop("target_folder", None)

    if not email.id:
        raise HTTPException(status_code=404, detail="Email not found")

    return normalized_payload


def _payload_fingerprint(payload: Dict) -> str:
    """Create stable payload fingerprint for duplicate detection."""
    return hashlib.sha256(
        json.dumps(
            payload or {}, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def _safe_thread_state_from_context(
    db: Session, *, thread_id: Optional[str], email: Optional[ProcessedEmail]
) -> str:
    if email and normalize_thread_state(email.thread_state) != "informational":
        return normalize_thread_state(email.thread_state)
    if thread_id:
        try:
            return update_thread_state_for_thread(
                db,
                thread_id=thread_id,
                user_address=getattr(settings, "imap_username", None),
            )
        except Exception as exc:
            logger.warning(
                "Could not infer thread_state for thread=%s: %s",
                thread_id,
                sanitize_error(exc, debug=settings.debug),
            )
    if email:
        return normalize_thread_state(email.thread_state)
    return "informational"


def _serialize_action_queue(
    db: Session,
    action: ActionQueue,
    *,
    email: Optional[ProcessedEmail] = None,
    thread_context: Optional[dict] = None,
) -> Dict:
    payload_out = action.payload if isinstance(action.payload, dict) else {}
    thread_id = action.thread_id or (email.thread_id if email else None)
    thread_state = (
        thread_context.get("thread_state")
        if isinstance(thread_context, dict)
        else _safe_thread_state_from_context(db, thread_id=thread_id, email=email)
    )
    thread_priority = (
        thread_context.get("thread_priority")
        if isinstance(thread_context, dict)
        else (email.thread_priority if email else None)
    )
    thread_importance_score = (
        thread_context.get("thread_importance_score")
        if isinstance(thread_context, dict)
        else (email.thread_importance_score if email else None)
    )
    thread_last_activity_at = (
        thread_context.get("thread_last_activity_at")
        if isinstance(thread_context, dict)
        else (email.date if email else None)
    )
    return {
        "id": action.id,
        "email_id": action.email_id,
        "thread_id": thread_id,
        "thread_state": thread_state,
        "thread_priority": thread_priority,
        "thread_importance_score": thread_importance_score,
        "thread_last_activity_at": thread_last_activity_at,
        "thread_summary": get_thread_summary(db, thread_id=thread_id),
        "action_type": action.action_type,
        "payload": action.payload,
        "status": _normalize_action_status(action.status),
        "created_at": action.created_at,
        "updated_at": action.updated_at,
        "executed_at": action.executed_at,
        "error_message": action.error_message,
        "source": payload_out.get("source"),
    }


# Global authentication middleware (fail-closed)
# This enforces authentication for ALL routes except explicit allowlist
@app.middleware("http")
async def global_auth_middleware(request: Request, call_next):
    """
    Global authentication middleware that enforces auth for all routes
    except those in the explicit allowlist. This is fail-closed by default.

    Accepts either:
    - Authorization: Bearer <API_KEY>  (CLI/curl compatibility)
    - Session cookie set by POST /api/auth/login  (browser usage)
    """
    # Explicit allowlist of unauthenticated routes.
    # "/" is allowed so the browser can load the login page.
    # "/api/auth/*" is allowed so login/logout work without credentials.
    UNAUTHENTICATED_ROUTES = {"/api/health", "/", "/api/version"}
    UNAUTHENTICATED_PREFIXES = ("/api/auth/", "/static/")

    path = request.url.path

    # Allow unauthenticated access to explicitly allowed routes and prefixes
    if path in UNAUTHENTICATED_ROUTES or any(
        path.startswith(p) for p in UNAUTHENTICATED_PREFIXES
    ):
        return await call_next(request)

    # Check authentication for all other routes
    settings = get_settings()
    api_keys = settings.get_api_keys()

    # Fail-closed: If no API keys configured, deny all access except allowlist
    if not api_keys:
        logger.error(f"No API keys configured - denying access to {path}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- Option 1: Bearer token (CLI / curl) ---
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ", 1)[1]
            if any(secrets.compare_digest(token, key) for key in api_keys):
                logger.debug(f"Bearer-authenticated request to {path}")
                return await call_next(request)
        except IndexError:
            pass
        logger.warning(
            f"Failed Bearer auth for {path} from {request.client.host if request.client else 'unknown'}"
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- Option 2: Session cookie (browser) ---
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        expiry = _sessions.get(session_token)
        if expiry and expiry > datetime.utcnow():
            logger.debug(f"Cookie-authenticated request to {path}")
            return await call_next(request)
        # Expired or invalid session
        if session_token in _sessions:
            del _sessions[session_token]

    logger.warning(
        f"Unauthenticated request to {path} from {request.client.host if request.client else 'unknown'}"
    )
    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )


# Request size limit (10MB default for API requests)
# This prevents large payload attacks
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse


class RequestSizeLimiterMiddleware(BaseHTTPMiddleware):
    """Middleware to limit request body size"""

    def __init__(self, app, max_size: int = 10 * 1024 * 1024):  # 10MB default
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: StarletteRequest, call_next):
        """Check request size before processing"""
        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length:
            if int(content_length) > self.max_size:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"Request body too large. Maximum size: {self.max_size} bytes"
                    },
                )
        return await call_next(request)


app.add_middleware(RequestSizeLimiterMiddleware, max_size=10 * 1024 * 1024)

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add allowed hosts middleware (after security headers, before CORS)
app.add_middleware(AllowedHostsMiddleware, settings=settings)

# Add rate limiting state
app.state.limiter = limiter

# Mount static files (frontend) - will be protected by global auth middleware
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# CORS - Restrictive configuration
cors_origins = (
    settings.cors_origins
    if isinstance(settings.cors_origins, list)
    else ["http://localhost:8000", "http://127.0.0.1:8000"]
)
logger.info(f"CORS enabled for origins: {cors_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,  # Don't use credentials with CORS (using Bearer tokens instead)
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=600,  # Cache preflight requests for 10 minutes
)


# Rate limit exceeded handler
from slowapi.errors import RateLimitExceeded

app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


# Exception handlers for better error responses
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors with sanitized responses"""
    logger.warning(f"Validation error on {request.url.path}: {exc.errors()}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid request data", "errors": exc.errors()},
    )


@app.exception_handler(AuthenticationError)
async def auth_exception_handler(request: Request, exc: AuthenticationError):
    """Handle authentication errors"""
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions with sanitized error messages"""
    # Use sanitized error in logs to prevent credential leakage
    sanitized_error = sanitize_error(exc, settings.debug)

    # In debug mode, include full trace; in production, use safe logging
    if settings.debug:
        logger.error(
            "Unhandled exception on %s: %s",
            request.url.path,
            sanitized_error,
            exc_info=True,
        )
    else:
        logger.error("Unhandled exception on %s: %s", request.url.path, sanitized_error)

    # Don't leak internal details in production
    detail = sanitized_error if settings.debug else "Internal server error"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": detail}
    )


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup"""
    logger.info("=" * 60)
    logger.info("Starting MailJaeger...")
    logger.info(f"Version: {__version__}")
    logger.info(f"Server: {settings.server_host}:{settings.server_port}")
    logger.info(f"Debug mode: {settings.debug}")
    logger.info(f"Safe mode: {settings.safe_mode}")

    # Check API keys
    api_keys = settings.get_api_keys()
    if api_keys:
        logger.info(f"API authentication: ENABLED ({len(api_keys)} key(s) configured)")
    else:
        logger.warning("API authentication: DISABLED - No API keys configured!")

    logger.info(f"CORS origins: {cors_origins}")
    logger.info(f"Trust proxy: {settings.trust_proxy}")
    logger.info("=" * 60)

    # Create data directories
    from pathlib import Path
    from urllib.parse import urlparse

    # Extract database directory from URL
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    db_dir = db_path.parent if db_path.name else db_path

    for directory in [
        db_dir,
        settings.search_index_dir,
        settings.attachment_dir,
        settings.log_file.parent if settings.log_file else None,
    ]:
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")

    # Initialize database
    init_db()
    logger.info("Database initialized")

    with get_db_session() as db:
        effective_safe_mode = _apply_persisted_safe_mode(db)
        effective_archive_folder = _apply_persisted_archive_folder(db)
    logger.info(f"Safe mode (effective): {effective_safe_mode}")
    logger.info(f"Archive folder (effective): {effective_archive_folder}")

    # Verify critical tables exist (fail-closed startup check)
    try:
        engine = get_engine()
        verify_pending_actions_table(engine, debug=settings.debug)
    except RuntimeError as e:
        # Fail closed: exit if critical table is missing
        sanitized = sanitize_error(e, debug=False)
        logger.error("Startup check failed: %s", sanitized)
        print(f"\n❌ Startup Error: {sanitized}\n", file=sys.stderr)
        sys.exit(1)

    # Start scheduler
    scheduler = get_scheduler()
    scheduler.start()
    logger.info("Scheduler started")
    logger.info("MailJaeger startup complete")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down MailJaeger...")

    # Stop scheduler
    scheduler = get_scheduler()
    scheduler.stop()
    logger.info("MailJaeger shutdown complete")


@app.get("/")
async def root(request: Request):
    """Serve frontend dashboard - authentication enforced by global middleware"""
    # Serve frontend
    frontend_file = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend_file.exists():
        return FileResponse(frontend_file)

    return {
        "name": "MailJaeger",
        "version": __version__,
        "status": "running",
        "message": "Frontend not found. Access API at /api/docs",
    }


# ─── Auth endpoints (no authentication required) ──────────────────────────────


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    """
    Exchange API key for a session cookie.
    Accepts JSON body: {"api_key": "..."}
    On success sets an HttpOnly session cookie and returns {"success": true}.
    Keeps Bearer token support intact for CLI usage.
    """
    try:
        body = await request.json()
        provided_key = body.get("api_key", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not provided_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    settings = get_settings()
    api_keys = settings.get_api_keys()

    if not api_keys:
        raise HTTPException(status_code=503, detail="No API keys configured on server")

    if not any(secrets.compare_digest(provided_key, key) for key in api_keys):
        logger.warning(
            f"Failed login from {request.client.host if request.client else 'unknown'}"
        )
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Create session token
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS)
    logger.info(
        f"Session created for {request.client.host if request.client else 'unknown'}"
    )

    # Set HttpOnly cookie.
    # SameSite=Lax: works for normal LAN navigation and is safe against CSRF.
    # Secure=False: required for plain HTTP on a local network; a HTTPS reverse
    # proxy can add Secure=True via its own cookie rewriting.
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_EXPIRY_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=False,  # Allow HTTP for LAN deployments
    )
    return {"success": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    """Invalidate current session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if token and token in _sessions:
        del _sessions[token]
    response.delete_cookie(key=SESSION_COOKIE, samesite="lax")
    return {"success": True}


@app.get("/api/auth/verify")
async def auth_verify(request: Request):
    """
    Check whether the current request is authenticated.
    Returns 200 if authenticated (Bearer or session cookie), 401 otherwise.
    This is called by the frontend to decide whether to show the login screen.
    """
    settings = get_settings()
    api_keys = settings.get_api_keys()

    if not api_keys:
        return JSONResponse(status_code=401, content={"authenticated": False})

    # Check Bearer
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        if any(secrets.compare_digest(token, key) for key in api_keys):
            return {"authenticated": True}

    # Check session cookie
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        expiry = _sessions.get(session_token)
        if expiry and expiry > datetime.utcnow():
            return {"authenticated": True}

    return JSONResponse(status_code=401, content={"authenticated": False})


# ─── Version endpoint ──────────────────────────────────────────────────────────


@app.get("/api/version")
async def get_version():
    """Return current version and changelog."""
    return {"version": __version__, "changelog": CHANGELOG}


# ─── Status endpoint ───────────────────────────────────────────────────────────


@app.get("/api/status", dependencies=[Depends(require_authentication)])
async def get_status():
    """
    Return real-time system status for the UI progress bar.
    Schema: run_id, status (idle/running/success/failed), current_step,
            progress_percent (0-100), processed, total, spam,
            action_required, failed, started_at, last_update, message.
    """
    return get_run_status().to_dict()


@app.get(
    "/api/dashboard",
    response_model=DashboardResponse,
    dependencies=[Depends(require_authentication)],
)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get dashboard overview"""
    try:
        # Get last run
        last_run = (
            db.query(ProcessingRun).order_by(ProcessingRun.started_at.desc()).first()
        )

        # Get scheduler info
        scheduler = get_scheduler()
        next_run = scheduler.get_next_run_time()

        # Get statistics
        total_emails = db.query(ProcessedEmail).count()
        action_required_count = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.action_required == True, ProcessedEmail.is_spam == False
            )
            .count()
        )
        unresolved_count = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.action_required == True,
                ProcessedEmail.is_resolved == False,
                ProcessedEmail.is_spam == False,
            )
            .count()
        )

        # Health checks
        imap_service = IMAPService()
        ai_service = AIService()

        imap_health = imap_service.check_health()
        ai_health = ai_service.check_health()

        # Derive an overall system status:
        #   OK       — all critical services healthy
        #   DEGRADED — at least one service is unhealthy but the app is running
        #   ERROR    — critical failure (reserved for future use)
        degraded = _service_is_unhealthy(imap_health) or _service_is_unhealthy(
            ai_health
        )
        overall_status = "DEGRADED" if degraded else "OK"

        health_status = {
            "overall_status": overall_status,
            "mail_server": imap_health,
            "ai_service": ai_health,
            "database": {"status": "healthy", "message": "Database operational"},
            "scheduler": scheduler.get_status(),
        }

        return DashboardResponse(
            last_run=ProcessingRunResponse.from_orm(last_run) if last_run else None,
            next_scheduled_run=next_run.isoformat() if next_run else None,
            total_emails=total_emails,
            action_required_count=action_required_count,
            unresolved_count=unresolved_count,
            health_status=health_status,
            run_status=get_run_status().to_dict(),
            daily_report_available=_daily_report_available(db),
            safe_mode=settings.safe_mode,
        )

    except Exception as e:
        sanitized_error = sanitize_error(e, settings.debug)
        if settings.debug:
            logger.error(f"Dashboard error: {sanitized_error}", exc_info=True)
        else:
            logger.error(f"Dashboard error: {sanitized_error}")
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to load dashboard" if not settings.debug else sanitized_error
            ),
        )


@app.post(
    "/api/emails/search",
    response_model=List[EmailResponse],
    dependencies=[Depends(require_authentication)],
)
@limiter.limit("30/minute")  # Rate limit expensive search operations
async def search_emails(
    request: Request, search_request: SearchRequest, db: Session = Depends(get_db)
):
    """Search emails with filters"""
    try:
        if search_request.semantic:
            # Semantic search (placeholder for future implementation)
            # Would use sentence-transformers for embedding-based search
            logger.info("Semantic search requested (not yet implemented)")

        # Full-text search
        search_service = SearchService(db)
        results = search_service.search(
            query=search_request.query,
            category=search_request.category.value if search_request.category else None,
            priority=search_request.priority.value if search_request.priority else None,
            action_required=search_request.action_required,
            date_from=(
                search_request.date_from.isoformat()
                if search_request.date_from
                else None
            ),
            date_to=(
                search_request.date_to.isoformat() if search_request.date_to else None
            ),
            page=search_request.page,
            page_size=search_request.page_size,
        )

        return [EmailResponse.from_orm(email) for email in results["results"]]

    except Exception as e:
        sanitized_error = sanitize_error(e, settings.debug)
        if settings.debug:
            logger.error(f"Search error: {sanitized_error}", exc_info=True)
        else:
            logger.error(f"Search error: {sanitized_error}")
        raise HTTPException(
            status_code=500,
            detail="Search failed" if not settings.debug else sanitized_error,
        )


@app.post(
    "/api/emails/list",
    response_model=List[EmailResponse],
    dependencies=[Depends(require_authentication)],
)
@limiter.limit("60/minute")  # Rate limit list operations
async def list_emails(
    request: Request, email_request: EmailListRequest, db: Session = Depends(get_db)
):
    """List emails with filters"""
    try:
        query = db.query(ProcessedEmail)

        # Apply filters
        if email_request.action_required is not None:
            query = query.filter(
                ProcessedEmail.action_required == email_request.action_required
            )
        if email_request.priority:
            query = query.filter(
                ProcessedEmail.priority == email_request.priority.value
            )
        if email_request.category:
            query = query.filter(
                ProcessedEmail.category == email_request.category.value
            )
        if email_request.is_spam is not None:
            query = query.filter(ProcessedEmail.is_spam == email_request.is_spam)
        if email_request.is_resolved is not None:
            query = query.filter(
                ProcessedEmail.is_resolved == email_request.is_resolved
            )
        if email_request.date_from:
            query = query.filter(ProcessedEmail.date >= email_request.date_from)
        if email_request.date_to:
            query = query.filter(ProcessedEmail.date <= email_request.date_to)

        # Sorting
        if email_request.sort_by == "date":
            sort_col = ProcessedEmail.date
        elif email_request.sort_by == "priority":
            sort_col = ProcessedEmail.priority
        else:
            sort_col = ProcessedEmail.subject

        if email_request.sort_order == "desc":
            query = query.order_by(sort_col.desc())
        else:
            query = query.order_by(sort_col.asc())

        # Pagination
        offset = (email_request.page - 1) * email_request.page_size
        emails = query.offset(offset).limit(email_request.page_size).all()

        return [EmailResponse.from_orm(email) for email in emails]

    except Exception as e:
        sanitized_error = sanitize_error(e, settings.debug)
        if settings.debug:
            logger.error(f"List emails error: {sanitized_error}", exc_info=True)
        else:
            logger.error(f"List emails error: {sanitized_error}")
        raise HTTPException(
            status_code=500,
            detail="Failed to list emails" if not settings.debug else sanitized_error,
        )


@app.get(
    "/api/emails/{email_id}",
    response_model=EmailDetailResponse,
    dependencies=[Depends(require_authentication)],
)
async def get_email(email_id: int, db: Session = Depends(get_db)):
    """Get email details"""
    email = db.query(ProcessedEmail).filter(ProcessedEmail.id == email_id).first()

    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    return EmailDetailResponse.from_orm(email)


@app.post(
    "/api/emails/{email_id}/resolve", dependencies=[Depends(require_authentication)]
)
async def mark_email_resolved(
    email_id: int, request: MarkResolvedRequest, db: Session = Depends(get_db)
):
    """Mark email as resolved/unresolved"""
    email = db.query(ProcessedEmail).filter(ProcessedEmail.id == email_id).first()

    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    email.is_resolved = request.resolved
    if request.resolved:
        email.resolved_at = datetime.utcnow()
    else:
        email.resolved_at = None

    db.commit()

    return {"success": True, "email_id": email_id, "resolved": request.resolved}


@app.post(
    "/api/emails/{email_id}/override",
    response_model=ClassificationOverrideResponse,
    dependencies=[Depends(require_authentication)],
)
async def override_email_classification(
    email_id: int,
    override: ClassificationOverrideRequest,
    db: Session = Depends(get_db),
):
    """
    Override the AI classification of an email.

    If LEARNING_ENABLED=true, a ClassificationOverride rule is created from the
    sender domain so future emails from that domain are classified automatically.
    """
    email = db.query(ProcessedEmail).filter(ProcessedEmail.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    # Snapshot current classification before overriding
    if not email.overridden:
        email.original_classification = {
            "category": email.category,
            "priority": email.priority,
            "is_spam": email.is_spam,
            "action_required": email.action_required,
            "suggested_folder": email.suggested_folder,
            "reasoning": email.reasoning,
            "spam_probability": email.spam_probability,
        }

    # Apply the requested overrides
    if override.category is not None:
        email.category = override.category
    if override.priority is not None:
        email.priority = override.priority
    if override.spam is not None:
        email.is_spam = override.spam
        email.spam_probability = 0.95 if override.spam else 0.05
    if override.action_required is not None:
        email.action_required = override.action_required
    if override.suggested_folder is not None:
        email.suggested_folder = override.suggested_folder

    email.overridden = True
    email.reasoning = "Manually overridden by user"

    rule_id: Optional[int] = None
    rule_created = False

    # Persist a learning rule when learning is enabled
    if settings.learning_enabled:
        sender = email.sender or ""
        # Derive domain pattern (use full address for exact senders)
        if "@" in sender:
            domain = sender.split("@", 1)[1].strip(">").lower()
            sender_pattern = f"@{domain}"
        else:
            sender_pattern = sender.lower() if sender else None

        if sender_pattern:
            rule = ClassificationOverride(
                sender_pattern=sender_pattern,
                category=override.category,
                priority=override.priority,
                spam=override.spam,
                action_required=override.action_required,
                suggested_folder=override.suggested_folder,
                created_from_email_id=email_id,
            )
            db.add(rule)
            db.flush()  # get the id
            email.override_rule_id = rule.id
            rule_id = rule.id
            rule_created = True

    db.commit()

    return ClassificationOverrideResponse(
        success=True,
        email_id=email_id,
        overridden=True,
        rule_id=rule_id,
        rule_created=rule_created,
        classification={
            "category": email.category,
            "priority": email.priority,
            "is_spam": email.is_spam,
            "action_required": email.action_required,
            "suggested_folder": email.suggested_folder,
            "spam_probability": email.spam_probability,
        },
    )


@app.post("/api/processing/trigger", dependencies=[Depends(require_authentication)])
@limiter.limit("5/minute")  # Strict rate limit on manual processing trigger
async def trigger_processing(
    request: Request,
    trigger_request: Optional[TriggerRunRequest] = Body(default=None),
):
    """
    Manually trigger email processing.

    Body is optional. When omitted trigger_type defaults to "MANUAL".
    Returns immediately with run_id.  Processing runs in a background thread.
    If a run is already active returns success=false with the active run_id.
    """
    try:
        scheduler = get_scheduler()
        started, run_id = scheduler.trigger_manual_run_async()

        if started:
            return {"success": True, "message": "Processing started", "run_id": run_id}
        else:
            return {
                "success": False,
                "message": "Processing already in progress",
                "run_id": run_id,
            }

    except Exception as e:
        sanitized_error = sanitize_error(e, settings.debug)
        if settings.debug:
            logger.error(f"Trigger processing error: {sanitized_error}", exc_info=True)
        else:
            logger.error(f"Trigger processing error: {sanitized_error}")
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to trigger processing"
                if not settings.debug
                else sanitized_error
            ),
        )


@app.post("/api/processing/cancel", dependencies=[Depends(require_authentication)])
@limiter.limit("10/minute")
async def cancel_processing(request: Request):
    """
    Request cancellation of the currently running processing job.

    If a run is active its status transitions to "cancelling"; the processing
    loop checks this flag before each email and exits cleanly, persisting
    partial progress.  The final run status in the DB is set to CANCELLED.

    Returns:
      success=True  when the cancel signal was accepted (run was active)
      success=False when there is no active run to cancel
    """
    run_status = get_run_status()
    accepted = run_status.request_cancel()
    return {
        "success": accepted,
        "message": "Cancellation requested" if accepted else "No active run to cancel",
        "run_id": run_status.run_id,
        "status": run_status.status,
    }


@app.get(
    "/api/processing/runs",
    response_model=List[ProcessingRunResponse],
    dependencies=[Depends(require_authentication)],
)
async def get_processing_runs(
    limit: int = Query(default=10, ge=1, le=100), db: Session = Depends(get_db)
):
    """Get processing run history"""
    runs = (
        db.query(ProcessingRun)
        .order_by(ProcessingRun.started_at.desc())
        .limit(limit)
        .all()
    )

    return [ProcessingRunResponse.from_orm(run) for run in runs]


@app.get(
    "/api/processing/runs/{run_id}",
    response_model=ProcessingRunResponse,
    dependencies=[Depends(require_authentication)],
)
async def get_processing_run(run_id: int, db: Session = Depends(get_db)):
    """Get specific processing run"""
    run = db.query(ProcessingRun).filter(ProcessingRun.id == run_id).first()

    if not run:
        raise HTTPException(status_code=404, detail="Processing run not found")

    return ProcessingRunResponse.from_orm(run)


@app.get("/api/settings", dependencies=[Depends(require_authentication)])
async def get_settings_api(db: Session = Depends(get_db)):
    """Get current settings (sanitized - no sensitive credentials)"""
    _apply_persisted_safe_mode(db)
    _apply_persisted_archive_folder(db)
    return {
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "spam_threshold": settings.spam_threshold,
        "ai_endpoint": settings.ai_endpoint,
        "ai_model": settings.ai_model,
        "schedule_time": settings.schedule_time,
        "schedule_timezone": settings.schedule_timezone,
        "learning_enabled": settings.learning_enabled,
        "learning_confidence_threshold": settings.learning_confidence_threshold,
        "store_email_body": settings.store_email_body,
        "store_attachments": settings.store_attachments,
        "safe_mode": settings.safe_mode,
        "archive_folder": settings.archive_folder,
        "require_approval": settings.require_approval,
        "mark_as_read": settings.mark_as_read,
    }


@app.post("/api/settings", dependencies=[Depends(require_authentication)])
async def update_settings_api(request: SettingsUpdate, db: Session = Depends(get_db)):
    """Update settings (partial update)"""
    updated_fields = []
    if request.safe_mode is not None:
        settings.safe_mode = bool(request.safe_mode)
        _set_app_setting(db, key=APP_SETTING_SAFE_MODE, value=settings.safe_mode)
        updated_fields.append("safe_mode")
    if request.archive_folder is not None:
        selected_folder = (request.archive_folder or "").strip()
        if not selected_folder:
            raise HTTPException(status_code=400, detail="archive_folder must be non-empty")
        settings.archive_folder = selected_folder
        _set_app_setting(
            db, key=APP_SETTING_ARCHIVE_FOLDER, value=settings.archive_folder
        )
        _learn_folder_preference(
            db,
            action_type="archive",
            target_folder=settings.archive_folder,
            email=None,
        )
        updated_fields.append("archive_folder")

    return {
        "success": True,
        "message": (
            "Settings updated"
            if updated_fields
            else "No runtime-updatable settings provided"
        ),
        "updated_fields": updated_fields,
        "safe_mode": settings.safe_mode,
        "archive_folder": settings.archive_folder,
    }


@app.get(
    "/api/folders",
    dependencies=[Depends(require_authentication)],
)
async def list_imap_folders(db: Session = Depends(get_db)):
    """Return live IMAP folders with exact and normalized names."""
    folders = _discover_live_imap_folders()
    if not folders:
        raise HTTPException(
            status_code=503,
            detail="Could not retrieve IMAP folders from mail server",
        )
    cache_saved = _set_noncritical_cache_setting(
        db,
        key=APP_SETTING_IMAP_FOLDERS_CACHE,
        value={"folders": folders, "fetched_at": datetime.now(timezone.utc).isoformat()},
    )
    return {
        "folders": folders,
        "current_archive_folder": _resolve_archive_folder(
            db, email=None, allow_live_discovery=False
        )
        or settings.archive_folder,
        "cache_saved": cache_saved,
    }


# Action Queue API endpoints
@app.get(
    "/api/actions",
    response_model=List[ActionQueueResponse],
    dependencies=[Depends(require_authentication)],
)
async def list_actions(
    status: Optional[str] = Query(
        None,
        description="Optional filter: proposed, approved, executed, failed, rejected",
    ),
    db: Session = Depends(get_db),
):
    """List action_queue entries with optional status filter."""
    query = db.query(ActionQueue)
    if status:
        normalized = status.lower()
        aliases = {
            "proposed": ["proposed", "proposed_action"],
            "approved": ["approved", "approved_action"],
            "executed": ["executed", "executed_action"],
            "failed": ["failed", "failed_action", "rejected_action"],
            "rejected": ["rejected", "rejected_action"],
        }
        query = query.filter(
            ActionQueue.status.in_(aliases.get(normalized, [normalized]))
        )
    actions = query.order_by(ActionQueue.created_at.desc()).all()

    action_rows = []
    emails_by_id: Dict[int, ProcessedEmail] = {}
    thread_ids = set()
    for action in actions:
        email = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.id == action.email_id)
            .first()
        )
        action_rows.append((action, email))
        if email:
            emails_by_id[email.id] = email
        thread_id = action.thread_id or (email.thread_id if email else None)
        if thread_id:
            thread_ids.add(thread_id)

    contexts_by_thread: Dict[str, dict] = {}
    for thread_id in thread_ids:
        thread_emails = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.thread_id == thread_id)
            .order_by(
                ProcessedEmail.date.desc(),
                ProcessedEmail.processed_at.desc(),
                ProcessedEmail.created_at.desc(),
                ProcessedEmail.id.desc(),
            )
            .all()
        )
        if not thread_emails:
            continue
        context = build_thread_context(
            thread_id=thread_id,
            emails=thread_emails,
            user_address=getattr(settings, "imap_username", None),
            open_actions_count=query_open_action_count(db, thread_id=thread_id),
        )
        for email in thread_emails:
            email.thread_state = context.thread_state
            email.thread_priority = context.thread_priority
            email.thread_importance_score = context.thread_importance_score
        contexts_by_thread[thread_id] = {
            "thread_state": context.thread_state,
            "thread_priority": context.thread_priority,
            "thread_importance_score": context.thread_importance_score,
            "thread_last_activity_at": context.thread_last_activity_at,
            "sort_key": thread_sort_key(context),
        }
    db.flush()

    action_rows.sort(
        key=lambda row: (
            contexts_by_thread.get(
                row[0].thread_id or (row[1].thread_id if row[1] else None), {}
            ).get("sort_key", (2, 0.0, 0.0))[0],
            -contexts_by_thread.get(
                row[0].thread_id or (row[1].thread_id if row[1] else None), {}
            ).get("sort_key", (2, 0.0, 0.0))[1],
            -contexts_by_thread.get(
                row[0].thread_id or (row[1].thread_id if row[1] else None), {}
            ).get("sort_key", (2, 0.0, 0.0))[2],
            -(
                row[0].created_at.timestamp()
                if row[0].created_at
                else 0.0
            ),
        )
    )

    normalized_actions = []
    for action, email in action_rows:
        thread_id = action.thread_id or (email.thread_id if email else None)
        normalized_actions.append(
            _serialize_action_queue(
                db,
                action,
                email=email,
                thread_context=contexts_by_thread.get(thread_id),
            )
        )
    return normalized_actions


@app.post(
    "/api/reports/daily/suggested-actions",
    response_model=ActionQueueResponse,
    dependencies=[Depends(require_authentication)],
)
async def queue_daily_report_suggested_action(
    request: QueueSuggestedActionRequest, db: Session = Depends(get_db)
):
    """
    Queue one daily-report suggested action.

    This endpoint never executes actions directly; it only creates a proposal
    in the existing action queue/approval flow.
    """
    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == request.email_id).first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    normalized_type = (request.action_type or "").strip().lower()
    if not normalized_type:
        raise HTTPException(status_code=400, detail="action_type is required")
    if normalized_type not in DAILY_REPORT_ACTION_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported suggested action type")

    payload = dict(request.payload or {})
    queue_action_type = normalized_type

    if normalized_type == "archive":
        queue_action_type = "move"
        payload["target_folder"] = payload.get("target_folder") or _resolve_archive_folder(
            db, email=email, allow_live_discovery=True
        )
        payload["target_folder"] = payload.get("target_folder") or settings.archive_folder
    elif normalized_type == "mark_spam":
        queue_action_type = "move"
        payload["target_folder"] = (
            payload.get("target_folder") or settings.quarantine_folder
        )
    elif normalized_type == "move":
        queue_action_type = "move"
        payload["target_folder"] = payload.get("target_folder") or _resolve_archive_folder(
            db, email=email, allow_live_discovery=True
        )
        payload["target_folder"] = payload.get("target_folder") or settings.archive_folder
    elif normalized_type in ("mark_read", "delete", "mark_resolved", "reply_draft"):
        queue_action_type = normalized_type

    payload = _validate_daily_report_action_payload(
        normalized_type, payload, email=email
    )

    if queue_action_type == "reply_draft":
        reply_payload = _build_reply_draft_payload(email.subject)
        payload.setdefault("draft_summary", reply_payload["draft_summary"])
        payload.setdefault("draft_text", reply_payload["draft_text"])
        payload["draft_state"] = payload.get("draft_state") or "proposed_manual_send"

    if queue_action_type == "move":
        _learn_folder_preference(
            db,
            action_type=normalized_type,
            target_folder=payload.get("target_folder"),
            email=email,
        )

    payload_source = (request.source or "daily_report").strip().lower()
    payload["source"] = "daily_report_suggestion"
    payload["source_context"] = (
        payload_source if payload_source in DECISION_EVENT_SOURCES else "daily_report"
    )
    payload["safe_mode"] = bool(request.safe_mode)
    if request.description:
        payload["description"] = request.description

    duplicate_payload_fingerprint = _payload_fingerprint(payload)
    duplicate = (
        db.query(ActionQueue)
        .filter(
            ActionQueue.email_id == email.id,
            ActionQueue.thread_id == (request.thread_id or email.thread_id),
            ActionQueue.action_type == queue_action_type,
            ActionQueue.status.in_(
                [
                    "proposed",
                    "proposed_action",
                    "approved",
                    "approved_action",
                    "executed",
                    "executed_action",
                ]
            ),
        )
        .order_by(ActionQueue.created_at.desc())
        .first()
    )
    if duplicate:
        duplicate_payload = (
            duplicate.payload if isinstance(duplicate.payload, dict) else {}
        )
        if _payload_fingerprint(duplicate_payload) == duplicate_payload_fingerprint:
            logger.info(
                "Skipped duplicate report suggestion for email=%s thread=%s action=%s existing_action_id=%s",
                email.id,
                request.thread_id or email.thread_id,
                queue_action_type,
                duplicate.id,
            )
            raise HTTPException(
                status_code=409,
                detail=f"Action already queued as #{duplicate.id} ({_normalize_action_status(duplicate.status)})",
            )

    action = ActionQueue(
        email_id=email.id,
        thread_id=request.thread_id or email.thread_id,
        action_type=queue_action_type,
        payload=payload,
        status="proposed_action",
    )
    db.add(action)
    db.flush()
    _record_decision_event(
        db,
        email_id=action.email_id,
        thread_id=action.thread_id,
        event_type="queue_suggestion",
        source=payload.get("source_context", "daily_report"),
        old_value=None,
        new_value=action.action_type,
        user_confirmed=True,
    )
    if action.action_type == "move":
        email_for_learning = (
            db.query(ProcessedEmail).filter(ProcessedEmail.id == action.email_id).first()
        )
        move_payload = action.payload if isinstance(action.payload, dict) else {}
        _learn_folder_preference(
            db,
            action_type="move",
            target_folder=move_payload.get("target_folder"),
            email=email_for_learning,
        )
    logger.info(
        "Action %s queued from daily report: email=%s thread=%s type=%s",
        action.id,
        action.email_id,
        action.thread_id,
        action.action_type,
    )
    db.commit()
    db.refresh(action)
    return _serialize_action_queue(db, action, email=email)


@app.post(
    "/api/reports/daily/events",
    dependencies=[Depends(require_authentication)],
)
async def record_report_decision_event(
    request: ReportDecisionEventRequest, db: Session = Depends(get_db)
):
    """Record report UI interaction events for future learning hooks."""
    event_type = (request.event_type or "").strip().lower()
    if event_type not in DECISION_EVENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported decision event type")

    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == request.email_id).first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    if request.action_queue_id:
        action = (
            db.query(ActionQueue)
            .filter(ActionQueue.id == request.action_queue_id)
            .first()
        )
        if not action:
            raise HTTPException(status_code=404, detail="Action queue item not found")

    _record_decision_event(
        db,
        email_id=request.email_id,
        thread_id=request.thread_id or email.thread_id,
        event_type=event_type,
        source=request.source or "report_suggestion",
        old_value=None,
        new_value=str(request.action_queue_id) if request.action_queue_id else None,
        user_confirmed=event_type not in {"reject_suggestion"},
    )
    db.commit()
    return {"success": True}


@app.post(
    "/api/actions/{action_id}/approve",
    response_model=ActionQueueResponse,
    dependencies=[Depends(require_authentication)],
)
async def approve_action(
    action_id: int,
    source: Optional[str] = Query(None, description="UI source for decision event"),
    db: Session = Depends(get_db),
):
    """Approve a proposed action."""
    action = db.query(ActionQueue).filter(ActionQueue.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status not in ("proposed", "proposed_action"):
        raise HTTPException(
            status_code=400, detail=f"Cannot approve action with status {action.status}"
        )
    previous_status = action.status
    transition_time = datetime.now(timezone.utc)
    action.status = "approved"
    action.approved_at = transition_time
    action.updated_at = transition_time
    # Intentional broad capture: every user queue decision is now used
    # as thread-level learning signal (not only daily-report suggestions).
    _record_decision_event(
        db,
        email_id=action.email_id,
        thread_id=action.thread_id,
        event_type="approve_suggestion",
        source=source or "queue_ui",
        old_value=previous_status,
        new_value=action.action_type,
        user_confirmed=True,
    )
    logger.info(
        "Action %s transition %s -> %s",
        action.id,
        previous_status,
        action.status,
    )
    db.commit()
    db.refresh(action)
    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == action.email_id).first()
    )
    return _serialize_action_queue(db, action, email=email)


@app.post(
    "/api/actions/{action_id}/reject",
    response_model=ActionQueueResponse,
    dependencies=[Depends(require_authentication)],
)
async def reject_action(
    action_id: int,
    source: Optional[str] = Query(None, description="UI source for decision event"),
    db: Session = Depends(get_db),
):
    """Reject an action by marking it failed."""
    action = db.query(ActionQueue).filter(ActionQueue.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status in ("executed", "executed_action"):
        raise HTTPException(status_code=400, detail="Cannot reject an executed action")
    previous_status = action.status
    transition_time = datetime.now(timezone.utc)
    action.status = "rejected"
    action.error_message = "Rejected by user"
    action.updated_at = transition_time
    _record_decision_event(
        db,
        email_id=action.email_id,
        thread_id=action.thread_id,
        event_type="reject_suggestion",
        source=source or "queue_ui",
        old_value=previous_status,
        new_value=action.action_type,
        user_confirmed=False,
    )
    logger.info(
        "Action %s transition %s -> %s",
        action.id,
        previous_status,
        action.status,
    )
    db.commit()
    db.refresh(action)
    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == action.email_id).first()
    )
    return _serialize_action_queue(db, action, email=email)


@app.post(
    "/api/actions/{action_id}/execute",
    response_model=ActionQueueResponse,
    dependencies=[Depends(require_authentication)],
)
async def execute_action(
    action_id: int,
    source: Optional[str] = Query(None, description="UI source for decision event"),
    db: Session = Depends(get_db),
):
    """Execute an approved action via explicit API call only."""
    action = db.query(ActionQueue).filter(ActionQueue.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    if action.status in ("executed", "executed_action"):
        email = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.id == action.email_id)
            .first()
        )
        return _serialize_action_queue(db, action, email=email)

    if action.status not in ("approved", "approved_action"):
        raise HTTPException(
            status_code=400,
            detail=f"Only approved actions can be executed (got {action.status})",
        )

    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == action.email_id).first()
    )

    try:
        with IMAPService() as imap:
            executor = ActionExecutor(imap)
            success = executor.execute(action, email)
            if not success and not action.error_message:
                action.error_message = "Execution failed"
            action.updated_at = datetime.utcnow()
            thread_id = action.thread_id or (email.thread_id if email else None)
            if thread_id:
                update_thread_state_for_thread(
                    db,
                    thread_id=thread_id,
                    user_address=getattr(settings, "imap_username", None),
                )
            db.add(action)
            if email:
                db.add(email)
            # Intentional broad capture for thread-level learning hooks.
            if action.status in (
                "executed",
                "executed_action",
            ):
                _record_decision_event(
                    db,
                    email_id=action.email_id,
                    thread_id=action.thread_id,
                    event_type="execute_suggestion",
                    source=source or "queue_ui",
                    old_value="approved",
                    new_value=action.action_type,
                    user_confirmed=True,
                )
                if action.action_type == "move":
                    move_payload = action.payload if isinstance(action.payload, dict) else {}
                    _learn_folder_preference(
                        db,
                        action_type="move",
                        target_folder=move_payload.get("target_folder"),
                        email=email,
                    )
            logger.info(
                "Action %s execution result action_type=%s status=%s",
                action.id,
                action.action_type,
                action.status,
            )
            db.commit()
            db.refresh(action)
            return _serialize_action_queue(db, action, email=email)
    except RuntimeError as exc:
        sanitized_error = sanitize_error(exc, debug=get_settings().debug)
        raise HTTPException(status_code=503, detail=sanitized_error)


# Pending Actions API endpoints
@app.get(
    "/api/pending-actions",
    response_model=List[PendingActionWithEmailResponse],
    dependencies=[Depends(require_authentication)],
)
async def list_pending_actions(
    status: Optional[str] = Query(
        None,
        description="Filter by status (PENDING, APPROVED, REJECTED, APPLIED, FAILED)",
    ),
    db: Session = Depends(get_db),
):
    """List all pending actions with optional status filter"""
    query = db.query(PendingAction)

    if status:
        query = query.filter(PendingAction.status == status.upper())

    actions = query.order_by(PendingAction.created_at.desc()).all()

    return actions


# NOTE: Preview route MUST be defined BEFORE {action_id} route to avoid routing collision
# FastAPI matches routes in order, so /preview would match /{action_id} if defined after
@app.post(
    "/api/pending-actions/preview",
    response_model=PreviewActionsResponse,
    dependencies=[Depends(require_authentication)],
)
async def preview_pending_actions(
    request: PreviewActionsRequest = PreviewActionsRequest(),
    db: Session = Depends(get_db),
):
    """
    Preview pending actions and generate apply token for two-step safety.

    Generates a short-lived token that must be used in the apply endpoint.
    This prevents accidental "apply all" and ensures user reviews before applying.
    """
    # Get actions based on request
    query = db.query(PendingAction).filter(PendingAction.status == "APPROVED")

    if request.action_ids:
        # Specific actions requested
        query = query.filter(PendingAction.id.in_(request.action_ids))
        actions = query.all()
    else:
        # All approved actions, but respect max_count limit
        max_count = (
            request.max_count
            if request.max_count is not None
            else settings.max_apply_per_request
        )
        actions = query.limit(max_count).all()

    if not actions:
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "apply_token": "",
                "token_expires_at": None,
                "action_count": 0,
                "summary": {},
                "actions": [],
            },
        )

    # Build action preview and summary
    preview = []
    summary = {"by_type": {}, "by_folder": {}}
    action_ids = []

    for action in actions:
        email = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.id == action.email_id)
            .first()
        )

        action_ids.append(action.id)
        preview.append(
            {
                "action_id": action.id,
                "email_id": action.email_id,
                "email_subject": email.subject if email else "Unknown",
                "email_sender": email.sender if email else "Unknown",
                "action_type": action.action_type,
                "target_folder": action.target_folder,
                "created_at": (
                    action.created_at.isoformat() if action.created_at else None
                ),
                "approved_at": (
                    action.approved_at.isoformat() if action.approved_at else None
                ),
            }
        )

        # Update summary
        action_type = action.action_type
        summary["by_type"][action_type] = summary["by_type"].get(action_type, 0) + 1

        if action.target_folder:
            summary["by_folder"][action.target_folder] = (
                summary["by_folder"].get(action.target_folder, 0) + 1
            )

    # Generate apply token
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=5)  # 5 minute expiry

    # Clean up expired tokens
    db.query(ApplyToken).filter(ApplyToken.expires_at < datetime.utcnow()).delete()

    # Create token record
    apply_token = ApplyToken(
        token=token,
        action_ids=action_ids,
        action_count=len(action_ids),
        summary=summary,
        expires_at=expires_at,
    )
    db.add(apply_token)
    db.commit()

    logger.info(
        f"Generated apply token for {len(action_ids)} actions (expires in 5 minutes)"
    )

    return PreviewActionsResponse(
        success=True,
        apply_token=token,
        token_expires_at=expires_at,
        action_count=len(action_ids),
        summary=summary,
        actions=preview,
    )


@app.get(
    "/api/pending-actions/{action_id}",
    response_model=PendingActionWithEmailResponse,
    dependencies=[Depends(require_authentication)],
)
async def get_pending_action(action_id: int, db: Session = Depends(get_db)):
    """Get a single pending action by ID"""
    action = db.query(PendingAction).filter(PendingAction.id == action_id).first()

    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")

    return PendingActionWithEmailResponse.from_orm(action)


@app.post(
    "/api/pending-actions/{action_id}/approve",
    dependencies=[Depends(require_authentication)],
)
async def approve_pending_action(
    action_id: int, request: ApproveActionRequest, db: Session = Depends(get_db)
):
    """Approve or reject a pending action"""
    action = db.query(PendingAction).filter(PendingAction.id == action_id).first()

    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")

    if action.status != "PENDING":
        raise HTTPException(
            status_code=400, detail=f"Cannot approve action with status {action.status}"
        )

    if request.approve:
        action.status = "APPROVED"
        action.approved_at = datetime.utcnow()
        logger.info(f"Pending action {action_id} approved")
    else:
        action.status = "REJECTED"
        action.approved_at = datetime.utcnow()  # Set timestamp for rejection too
        logger.info(f"Pending action {action_id} rejected")

    db.commit()

    return {"success": True, "action_id": action_id, "status": action.status}


@app.post(
    "/api/pending-actions/apply",
    response_model=ApplyActionsResponse,
    dependencies=[Depends(require_authentication)],
)
async def apply_all_approved_actions(
    request: ApplyActionsRequest = ApplyActionsRequest(), db: Session = Depends(get_db)
):
    """
    Apply approved pending actions to IMAP mailboxes with strict safety controls.

    Safety requirements:
    - SAFE_MODE always wins (returns 409 if enabled)
    - Requires apply_token from preview endpoint (two-step safety)
    - Requires explicit action_ids OR max_count (prevents accidental "apply all")
    - Blocks DELETE operations unless ALLOW_DESTRUCTIVE_IMAP=true
    - Validates target folders against allowlist
    """
    # Check SAFE_MODE first - it always wins
    if get_settings().safe_mode:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "SAFE_MODE enabled; no actions applied",
                "applied": 0,
                "failed": 0,
                "actions": [],
            },
        )

    # Require apply_token (two-step safety)
    if not request.apply_token:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Apply token required. Use /api/pending-actions/preview to generate token.",
                "applied": 0,
                "failed": 0,
                "actions": [],
            },
        )

    # Validate and consume apply_token
    token_record = (
        db.query(ApplyToken)
        .filter(ApplyToken.token == request.apply_token, ApplyToken.is_used == False)
        .first()
    )

    if not token_record:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Invalid or already used apply token",
                "applied": 0,
                "failed": 0,
                "actions": [],
            },
        )

    if token_record.expires_at < datetime.utcnow():
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Apply token has expired. Generate a new token with /api/pending-actions/preview",
                "applied": 0,
                "failed": 0,
                "actions": [],
            },
        )

    # Get actions based on token (enforces preview-apply matching)
    actions = (
        db.query(PendingAction)
        .filter(
            PendingAction.id.in_(token_record.action_ids),
            PendingAction.status == "APPROVED",
        )
        .all()
    )

    if not actions:
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "No approved actions to apply",
                "applied": 0,
                "failed": 0,
                "actions": [],
            },
        )

    # Get safe folders
    _cur_settings = get_settings()
    safe_folders = _cur_settings.get_safe_folders()

    if request.dry_run:
        # Preview mode - just return what would be done
        # DO NOT mark token as used for dry run
        preview = []
        for action in actions:
            email = (
                db.query(ProcessedEmail)
                .filter(ProcessedEmail.id == action.email_id)
                .first()
            )

            # Check safety validations
            warnings = []
            if (
                action.action_type == "DELETE"
                and not _cur_settings.allow_destructive_imap
            ):
                warnings.append("DELETE blocked (ALLOW_DESTRUCTIVE_IMAP=false)")
            if action.target_folder and action.target_folder not in safe_folders:
                warnings.append(
                    f"Target folder '{action.target_folder}' not in safe folder allowlist"
                )

            preview.append(
                {
                    "action_id": action.id,
                    "email_id": action.email_id,
                    "email_subject": email.subject if email else "Unknown",
                    "action_type": action.action_type,
                    "target_folder": action.target_folder,
                    "warnings": warnings,
                }
            )

        return ApplyActionsResponse(success=True, applied=0, failed=0, actions=preview)

    # Apply actions - use context manager for IMAP connection
    applied = 0
    failed = 0
    results = []

    try:
        try:
            with IMAPService() as imap:
                # Process each action
                for action in actions:
                    try:
                        email = (
                            db.query(ProcessedEmail)
                            .filter(ProcessedEmail.id == action.email_id)
                            .first()
                        )

                        if not email or not email.uid:
                            action.status = "FAILED"
                            action.error_message = "Email or UID not found"
                            failed += 1
                            results.append(
                                {
                                    "action_id": action.id,
                                    "status": "FAILED",
                                    "error": action.error_message,
                                }
                            )
                            continue

                        # Safety check: Block DELETE unless explicitly enabled
                        if action.action_type == "DELETE":
                            if not _cur_settings.allow_destructive_imap:
                                action.status = "REJECTED"
                                action.error_message = (
                                    "DELETE blocked: ALLOW_DESTRUCTIVE_IMAP is false"
                                )
                                failed += 1
                                logger.warning(
                                    f"Blocked DELETE action {action.id}: destructive operations disabled"
                                )
                                results.append(
                                    {
                                        "action_id": action.id,
                                        "status": "REJECTED",
                                        "error": action.error_message,
                                    }
                                )
                                continue

                        # Safety check: Validate target folder against allowlist
                        if action.action_type == "MOVE_FOLDER":
                            if action.target_folder not in safe_folders:
                                action.status = "FAILED"
                                action.error_message = f"Target folder not in safe folder allowlist. Allowed: {', '.join(safe_folders)}"
                                failed += 1
                                logger.error(
                                    f"Failed action {action.id}: target folder '{action.target_folder}' not in allowlist"
                                )
                                results.append(
                                    {
                                        "action_id": action.id,
                                        "status": "FAILED",
                                        "error": "Target folder not in safe folder allowlist",
                                    }
                                )
                                continue

                        uid = int(email.uid)
                        success = False

                        # Execute the IMAP action
                        if action.action_type == "MOVE_FOLDER":
                            success = imap.move_to_folder(uid, action.target_folder)
                            if success:
                                email.is_archived = True
                        elif action.action_type == "MARK_READ":
                            success = imap.mark_as_read(uid)
                        elif action.action_type == "ADD_FLAG":
                            success = imap.add_flag(uid)
                            if success:
                                email.is_flagged = True
                        elif action.action_type == "DELETE":
                            # DELETE is already checked above; should not reach here unless enabled
                            success = (
                                imap.delete_message(uid)
                                if hasattr(imap, "delete_message")
                                else False
                            )
                        else:
                            action.status = "FAILED"
                            action.error_message = (
                                f"Unknown action type: {action.action_type}"
                            )
                            failed += 1
                            results.append(
                                {
                                    "action_id": action.id,
                                    "status": "FAILED",
                                    "error": action.error_message,
                                }
                            )
                            continue

                        if success:
                            action.status = "APPLIED"
                            action.applied_at = datetime.utcnow()
                            applied += 1
                            logger.info(
                                f"Applied action {action.id}: {action.action_type} for email {email.message_id}"
                            )
                            results.append(
                                {
                                    "action_id": action.id,
                                    "status": "APPLIED",
                                    "error": None,
                                }
                            )
                        else:
                            action.status = "FAILED"
                            action.error_message = "IMAP operation failed"
                            failed += 1
                            logger.error(
                                f"Failed to apply action {action.id}: {action.action_type}"
                            )
                            results.append(
                                {
                                    "action_id": action.id,
                                    "status": "FAILED",
                                    "error": action.error_message,
                                }
                            )

                    except Exception as e:
                        action.status = "FAILED"
                        action.error_message = sanitize_error(e, settings.debug)
                        failed += 1
                        sanitized_error = sanitize_error(e, settings.debug)
                        logger.error(
                            f"Error applying action {action.id}: {sanitized_error}"
                        )
                        results.append(
                            {
                                "action_id": action.id,
                                "status": "FAILED",
                                "error": sanitized_error,
                            }
                        )

        except RuntimeError as e:
            # IMAP connection failed - DO NOT mark token as used
            # Return 503 without mutating database or consuming token
            sanitized_error = sanitize_error(e, debug=settings.debug)
            logger.error(f"IMAP connection failed for batch apply: {sanitized_error}")

            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "message": (
                        "IMAP connection failed"
                        if settings.debug
                        else "Service temporarily unavailable"
                    ),
                    "applied": 0,
                    "failed": 0,
                    "actions": [],
                },
            )

    except Exception as e:
        sanitized_error = sanitize_error(e, settings.debug)
        logger.error(f"Error in apply_all_approved_actions: {sanitized_error}")
        # DO NOT mark token as used on exception
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to apply actions"
                if not settings.debug
                else f"Failed to apply actions: {sanitized_error}"
            ),
        )

    # Commit all changes at once
    db.commit()

    # Mark token as used ONLY after successful completion
    # This happens after all actions are processed and committed
    token_record.is_used = True
    token_record.used_at = datetime.utcnow()
    db.commit()

    return ApplyActionsResponse(
        success=True, applied=applied, failed=failed, actions=results
    )


@app.post(
    "/api/pending-actions/{action_id}/apply",
    dependencies=[Depends(require_authentication)],
)
async def apply_single_action(
    action_id: int,
    request: ApplyActionsRequest = Body(default_factory=lambda: ApplyActionsRequest()),
    db: Session = Depends(get_db),
):
    """
    Apply a single approved pending action to IMAP mailbox with strict safety controls.

    Safety requirements:
    - SAFE_MODE always wins (returns 409 if enabled)
    - Requires apply_token from preview endpoint (two-step safety)
    - Blocks DELETE operations unless ALLOW_DESTRUCTIVE_IMAP=true
    - Validates target folders against allowlist
    """
    # Check SAFE_MODE first - it always wins
    if get_settings().safe_mode:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "SAFE_MODE enabled; no actions applied",
            },
        )

    # Require apply_token (two-step safety) - must be provided and valid
    if not request.apply_token:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Apply token required. Use /api/pending-actions/preview to generate token.",
            },
        )

    # Validate and consume apply_token
    token_record = (
        db.query(ApplyToken)
        .filter(ApplyToken.token == request.apply_token, ApplyToken.is_used == False)
        .first()
    )

    if not token_record:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Invalid or already used apply token",
            },
        )

    if token_record.expires_at < datetime.utcnow():
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Apply token has expired. Generate a new token with /api/pending-actions/preview",
            },
        )

    # Verify action_id is in the token's action_ids (token must be bound to this specific action)
    if action_id not in token_record.action_ids:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "message": "Apply token is not valid for this action",
            },
        )

    action = db.query(PendingAction).filter(PendingAction.id == action_id).first()

    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")

    if action.status != "APPROVED":
        raise HTTPException(
            status_code=400, detail=f"Cannot apply action with status {action.status}"
        )

    email = (
        db.query(ProcessedEmail).filter(ProcessedEmail.id == action.email_id).first()
    )

    if not email or not email.uid:
        raise HTTPException(status_code=404, detail="Email or UID not found")

    # Get safe folders for validation
    _cur_settings = get_settings()
    safe_folders = _cur_settings.get_safe_folders()

    # Safety check: Block DELETE unless explicitly enabled
    if action.action_type == "DELETE":
        if not _cur_settings.allow_destructive_imap:
            # Do NOT connect to IMAP - refuse immediately
            action.status = "REJECTED"
            action.error_message = "DELETE blocked: ALLOW_DESTRUCTIVE_IMAP is false"
            db.commit()
            logger.warning(
                f"Blocked DELETE action {action.id}: destructive operations disabled"
            )
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "message": "DELETE operations are not allowed",
                    "action_id": action.id,
                    "status": "REJECTED",
                },
            )

    # Safety check: Validate target folder against allowlist (BEFORE IMAP connection)
    if action.action_type == "MOVE_FOLDER":
        if action.target_folder not in safe_folders:
            # Do NOT connect to IMAP - refuse immediately
            action.status = "FAILED"
            action.error_message = sanitize_error(
                ValueError(
                    f"Target folder not in safe folder allowlist. Allowed: {', '.join(safe_folders)}"
                ),
                _cur_settings.debug,
            )
            db.commit()
            logger.error(
                f"Failed action {action.id}: target folder '{action.target_folder}' not in allowlist"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "message": "Target folder not in safe folder allowlist",
                    "action_id": action.id,
                    "status": "FAILED",
                },
            )

    if request.dry_run:
        # Preview mode - DO NOT mark token as used
        return {
            "success": True,
            "dry_run": True,
            "action_id": action.id,
            "email_id": action.email_id,
            "email_subject": email.subject,
            "action_type": action.action_type,
            "target_folder": action.target_folder,
            "message": "Dry run - action not applied",
        }

    # Apply the action - use context manager for IMAP connection
    try:
        try:
            with IMAPService() as imap:
                uid = int(email.uid)
                success = False

                # Execute the IMAP action
                if action.action_type == "MOVE_FOLDER":
                    success = imap.move_to_folder(uid, action.target_folder)
                    if success:
                        email.is_archived = True
                elif action.action_type == "MARK_READ":
                    success = imap.mark_as_read(uid)
                elif action.action_type == "ADD_FLAG":
                    success = imap.add_flag(uid)
                    if success:
                        email.is_flagged = True
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unknown action type: {action.action_type}",
                    )

                if success:
                    action.status = "APPLIED"
                    action.applied_at = datetime.utcnow()
                    db.commit()

                    # Mark token as used ONLY after successful application
                    token_record.is_used = True
                    token_record.used_at = datetime.utcnow()
                    db.commit()

                    logger.info(
                        f"Applied action {action.id}: {action.action_type} for email {email.message_id}"
                    )

                    return {
                        "success": True,
                        "action_id": action.id,
                        "status": "APPLIED",
                        "message": f"Action {action.action_type} applied successfully",
                    }
                else:
                    action.status = "FAILED"
                    action.error_message = "IMAP operation failed"
                    db.commit()
                    # DO NOT mark token as used on failure
                    logger.error(
                        f"Failed to apply action {action.id}: {action.action_type}"
                    )

                    raise HTTPException(
                        status_code=500, detail="Failed to apply IMAP action"
                    )

        except RuntimeError as e:
            # IMAP connection failed - DO NOT mark token as used or change action status
            sanitized_error = sanitize_error(e, debug=settings.debug)
            logger.error(
                f"IMAP connection failed for action {action.id}: {sanitized_error}"
            )

            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "message": (
                        "IMAP connection failed"
                        if settings.debug
                        else "Service temporarily unavailable"
                    ),
                    "action_id": action.id,
                    "status": "APPROVED",  # Status remains APPROVED, not FAILED
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        action.status = "FAILED"
        action.error_message = sanitize_error(e, settings.debug)
        db.commit()
        # DO NOT mark token as used on exception
        sanitized_error = sanitize_error(e, settings.debug)
        logger.error(f"Error applying action {action.id}: {sanitized_error}")
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to apply action"
                if not settings.debug
                else f"Failed to apply action: {sanitized_error}"
            ),
        )


@app.get("/api/health")
async def health_check():
    """Health check endpoint (unauthenticated for monitoring)"""
    imap_service = IMAPService()
    ai_service = AIService()

    return {
        "status": "healthy",
        "checks": {
            "mail_server": imap_service.check_health(),
            "ai_service": ai_service.check_health(),
            "database": {"status": "healthy"},
            "scheduler": get_scheduler().get_status(),
        },
    }


def _build_daily_report_response(
    db: Session, *, period_start: datetime, period_end: datetime
) -> DailyReportResponse:
    recent_emails = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= period_start)
        .order_by(ProcessedEmail.processed_at.desc())
        .limit(100)  # cap to keep the prompt manageable
        .all()
    )

    total_processed = len(recent_emails)
    action_required_count = sum(1 for e in recent_emails if e.action_required)
    spam_count = sum(1 for e in recent_emails if e.is_spam)
    unresolved_count = sum(
        1 for e in recent_emails if e.action_required and not e.is_resolved
    )
    thread_ids = {e.thread_id for e in recent_emails if e.thread_id}
    thread_recent_emails_map: Dict[str, List[ProcessedEmail]] = defaultdict(list)
    for email in recent_emails:
        if email.thread_id:
            thread_recent_emails_map[email.thread_id].append(email)

    thread_contexts: Dict[str, dict] = {}
    summary_service = ThreadSummaryService()
    for thread_id in thread_ids:
        thread_emails = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.thread_id == thread_id)
            .order_by(
                ProcessedEmail.date.desc(),
                ProcessedEmail.processed_at.desc(),
                ProcessedEmail.created_at.desc(),
                ProcessedEmail.id.desc(),
            )
            .all()
        )
        if not thread_emails:
            continue
        context = build_thread_context(
            thread_id=thread_id,
            emails=thread_emails,
            user_address=getattr(settings, "imap_username", None),
            open_actions_count=query_open_action_count(db, thread_id=thread_id),
        )
        for email in thread_emails:
            email.thread_state = context.thread_state
            email.thread_priority = context.thread_priority
            email.thread_importance_score = context.thread_importance_score
        thread_summary = summary_service.get_or_generate_summary(
            db,
            thread_id=thread_id,
            emails=thread_emails,
            thread_state=context.thread_state,
            allow_generate=True,
        )
        thread_contexts[thread_id] = {
            "thread_state": context.thread_state,
            "thread_priority": context.thread_priority,
            "thread_importance_score": context.thread_importance_score,
            "thread_last_activity_at": context.thread_last_activity_at,
            "summary": (thread_summary or {}).get("summary"),
            "key_topic": (thread_summary or {}).get("key_topic"),
            "status": (thread_summary or {}).get("status"),
        }
    db.flush()

    def _to_item(e: ProcessedEmail) -> ReportEmailItem:
        thread_context = thread_contexts.get(e.thread_id) if e.thread_id else None
        item_thread_state = normalize_thread_state(
            (thread_context or {}).get("thread_state", e.thread_state)
        )
        item_thread_priority = (thread_context or {}).get(
            "thread_priority", e.thread_priority or "normal"
        )
        item_thread_importance_score = float(
            (thread_context or {}).get(
                "thread_importance_score",
                e.thread_importance_score if e.thread_importance_score is not None else 0.0,
            )
            or 0.0
        )
        return ReportEmailItem(
            email_id=e.id,
            thread_id=e.thread_id,
            subject=e.subject,
            sender=e.sender,
            summary=e.summary,
            priority=e.priority,
            category=e.category,
            thread_state=item_thread_state,
            thread_priority=item_thread_priority,
            thread_importance_score=item_thread_importance_score,
        )

    important_items = [
        _to_item(e)
        for e in recent_emails
        if (e.priority == "HIGH" or e.is_flagged) and not e.is_spam
    ][:10]
    action_items = [
        _to_item(e)
        for e in recent_emails
        if e.action_required and not e.is_resolved and not e.is_spam
    ][:10]
    unresolved_items = [
        _to_item(e) for e in recent_emails if e.action_required and not e.is_resolved
    ][:10]
    spam_items = [_to_item(e) for e in recent_emails if e.is_spam][:10]

    safe_mode_active = settings.safe_mode
    suggested_actions: List[ReportSuggestedAction] = []
    recent_email_ids = [e.id for e in recent_emails if e.id]
    latest_action_by_key: Dict[tuple, ActionQueue] = {}

    if recent_email_ids:
        existing_actions = (
            db.query(ActionQueue)
            .filter(ActionQueue.email_id.in_(recent_email_ids))
            .order_by(ActionQueue.created_at.desc())
            .all()
        )
        for existing_action in existing_actions:
            key = (
                existing_action.email_id,
                existing_action.thread_id or "",
                existing_action.action_type,
            )
            latest_action_by_key.setdefault(key, existing_action)

    def _suggestion_queue_type_and_key(
        action_type: str, email_id: int, thread_id: Optional[str]
    ) -> tuple:
        queue_type = (
            "move" if action_type in ("archive", "mark_spam", "move") else action_type
        )
        return (email_id, thread_id or "", queue_type)

    for e in recent_emails:
        if not e.id:
            continue
        if e.is_spam and not e.is_archived:
            key = _suggestion_queue_type_and_key("mark_spam", e.id, e.thread_id)
            existing = latest_action_by_key.get(key)
            suggested_actions.append(
                ReportSuggestedAction(
                    email_id=e.id,
                    thread_id=e.thread_id,
                    action_type="mark_spam",
                    payload={"target_folder": settings.quarantine_folder},
                    target_folder=settings.quarantine_folder,
                    description=f"Spam verschieben: {e.subject or '(kein Betreff)'}",
                    safe_mode=safe_mode_active,
                    queue_status=(
                        _normalize_action_status(existing.status) if existing else None
                    ),
                    queue_action_id=existing.id if existing else None,
                    queue_error=existing.error_message if existing else None,
                )
            )
        elif e.action_required and not e.is_resolved:
            resolved_key = _suggestion_queue_type_and_key(
                "mark_resolved", e.id, e.thread_id
            )
            resolved_existing = latest_action_by_key.get(resolved_key)
            suggested_actions.append(
                ReportSuggestedAction(
                    email_id=e.id,
                    thread_id=e.thread_id,
                    action_type="mark_resolved",
                    payload={"reason": "daily_report_unresolved"},
                    description=f"Als erledigt markieren: {e.subject or '(kein Betreff)'}",
                    safe_mode=safe_mode_active,
                    queue_status=(
                        _normalize_action_status(resolved_existing.status)
                        if resolved_existing
                        else None
                    ),
                    queue_action_id=resolved_existing.id if resolved_existing else None,
                    queue_error=(
                        resolved_existing.error_message if resolved_existing else None
                    ),
                )
            )
            reply_key = _suggestion_queue_type_and_key("reply_draft", e.id, e.thread_id)
            reply_existing = latest_action_by_key.get(reply_key)
            suggested_actions.append(
                ReportSuggestedAction(
                    email_id=e.id,
                    thread_id=e.thread_id,
                    action_type="reply_draft",
                    payload=_build_reply_draft_payload(e.subject),
                    description=f"Antwort-Entwurf erstellen: {e.subject or '(kein Betreff)'}",
                    safe_mode=safe_mode_active,
                    queue_status=(
                        _normalize_action_status(reply_existing.status)
                        if reply_existing
                        else None
                    ),
                    queue_action_id=reply_existing.id if reply_existing else None,
                    queue_error=(
                        reply_existing.error_message if reply_existing else None
                    ),
                )
            )
        elif not e.is_archived and not e.is_spam:
            archive_key = _suggestion_queue_type_and_key("archive", e.id, e.thread_id)
            archive_existing = latest_action_by_key.get(archive_key)
            archive_target = _resolve_archive_folder(
                db, email=e, allow_live_discovery=False
            )
            if archive_target:
                suggested_actions.append(
                    ReportSuggestedAction(
                        email_id=e.id,
                        thread_id=e.thread_id,
                        action_type="archive",
                        payload={"target_folder": archive_target},
                        target_folder=archive_target,
                        description=f"Archivieren: {e.subject or '(kein Betreff)'}",
                        safe_mode=safe_mode_active,
                        queue_status=(
                            _normalize_action_status(archive_existing.status)
                            if archive_existing
                            else None
                        ),
                        queue_action_id=archive_existing.id if archive_existing else None,
                        queue_error=(
                            archive_existing.error_message if archive_existing else None
                        ),
                    )
                )
        if len(suggested_actions) >= 20:
            break

    thread_suggestion_counts: Dict[str, int] = {}
    for suggestion in suggested_actions:
        if suggestion.thread_id:
            thread_suggestion_counts[suggestion.thread_id] = (
                thread_suggestion_counts.get(suggestion.thread_id, 0) + 1
            )
    for suggestion in suggested_actions:
        if suggestion.thread_id:
            suggestion.thread_suggestion_count = thread_suggestion_counts.get(
                suggestion.thread_id, 0
            )
    suggested_actions.sort(
        key=lambda suggestion: (
            0
            if suggestion.thread_id
            and thread_contexts.get(suggestion.thread_id, {}).get("thread_state")
            == "waiting_for_me"
            else 1,
            float(
                thread_contexts.get(suggestion.thread_id, {}).get(
                    "thread_importance_score", 0.0
                )
            )
            if suggestion.thread_id
            else 0.0,
        ),
        reverse=True,
    )

    thread_groups: List[DailyReportThreadGroup] = []
    for thread_id, thread_emails in thread_recent_emails_map.items():
        context = thread_contexts.get(thread_id, {})
        items = [_to_item(email) for email in thread_emails]
        items.sort(
            key=lambda item: (
                item.thread_importance_score or 0.0,
                item.email_id or 0,
            ),
            reverse=True,
        )
        thread_groups.append(
            DailyReportThreadGroup(
                thread_id=thread_id,
                thread_state=normalize_thread_state(
                    context.get("thread_state", "informational")
                ),
                priority=context.get("thread_priority", "normal"),
                importance_score=float(context.get("thread_importance_score") or 0.0),
                thread_last_activity_at=(
                    context.get("thread_last_activity_at").isoformat()
                    if context.get("thread_last_activity_at")
                    else None
                ),
                summary=context.get("summary"),
                key_topic=context.get("key_topic"),
                status=context.get("status"),
                emails=items,
            )
        )
    thread_groups.sort(key=lambda group: group.importance_score, reverse=True)

    email_summaries = []
    for e in recent_emails[:30]:  # include top 30 in prompt to stay concise
        parts = [f"- {e.sender or '?'}: {e.subject or '(kein Betreff)'}"]
        if e.action_required:
            parts.append("[Aktion erforderlich]")
        if e.is_spam:
            parts.append("[Spam]")
        if e.summary:
            parts.append(f"({e.summary[:120]})")
        email_summaries.append(" ".join(parts))

    email_list_str = "\n".join(email_summaries) if email_summaries else "(keine)"

    prompt = f"""Erstelle einen täglichen E-Mail-Bericht auf Deutsch basierend auf den folgenden verarbeiteten E-Mails der letzten 24 Stunden.

Statistiken:
- Verarbeitete E-Mails: {total_processed}
- Aktion erforderlich: {action_required_count}
- Spam erkannt: {spam_count}
- Ungelöst: {unresolved_count}

E-Mails:
{email_list_str}

Erstelle einen strukturierten Bericht mit den folgenden Abschnitten:
1. Zusammenfassung (2-3 Sätze)
2. Wichtige E-Mails (maximal 5)
3. Aktionen erforderlich (mit konkreten Vorschlägen)
4. Spam-Filter-Ergebnis
5. Empfohlene nächste Schritte

Halte den Bericht präzise und handlungsorientiert."""

    ai_service = AIService()
    raw_response = ai_service.generate_report(prompt)
    if raw_response and raw_response.strip():
        report_text = raw_response.strip()
    else:
        lines = [
            f"Täglicher E-Mail-Bericht ({datetime.utcnow().strftime('%d.%m.%Y')})",
            "",
            "Zusammenfassung",
            f"• {total_processed} E-Mails verarbeitet",
            f"• {action_required_count} erfordern eine Aktion",
            f"• {spam_count} Spam erkannt",
            f"• {unresolved_count} ungelöst",
            "",
            "(KI-Zusammenfassung nicht verfügbar — Ollama nicht erreichbar)",
        ]
        if action_items:
            lines += ["", "Offene Aktionen:"]
            for item in action_items[:5]:
                lines.append(
                    f"  • [{item.priority or 'LOW'}] {item.sender or '?'}: {item.subject or '(kein Betreff)'}"
                )
        report_text = "\n".join(lines)

    return DailyReportResponse(
        report_version=DAILY_REPORT_SCHEMA_VERSION,
        generated_at=datetime.utcnow().isoformat(),
        period_hours=24,
        totals=ReportTotals(
            total_processed=total_processed,
            action_required=action_required_count,
            unresolved=unresolved_count,
            spam_detected=spam_count,
        ),
        total_processed=total_processed,
        action_required=action_required_count,
        spam_detected=spam_count,
        unresolved=unresolved_count,
        important_items=important_items,
        action_items=action_items,
        unresolved_items=unresolved_items,
        spam_items=spam_items,
        threads=thread_groups,
        suggested_actions=suggested_actions,
        report_text=report_text,
    )


def _generate_daily_report_in_background(report_id: int) -> None:
    try:
        with get_db_session() as background_db:
            report_row = background_db.get(DailyReport, report_id)
            if not report_row:
                return
            report_row.generation_status = "running"
            report_row.error_message = None
            period_start = report_row.period_start
            period_end = report_row.period_end
            report_payload = _build_daily_report_response(
                background_db, period_start=period_start, period_end=period_end
            )
            report_row.report_json = report_payload.model_dump()
            report_row.report_text = report_payload.report_text
            report_row.generated_at = datetime.utcnow()
            report_row.generation_status = "ready"
            report_row.error_message = None
    except Exception as e:
        sanitized_error = sanitize_error(e, debug=settings.debug)
        logger.error("Daily report background generation failed: %s", sanitized_error)
        try:
            with get_db_session() as background_db:
                report_row = background_db.get(DailyReport, report_id)
                if report_row:
                    report_row.generation_status = "failed"
                    report_row.error_message = sanitized_error
        except Exception:
            logger.error(
                "Failed to persist daily report generation error", exc_info=True
            )


@app.get(
    "/api/reports/daily",
    response_model=DailyReportEndpointResponse,
    dependencies=[Depends(require_authentication)],
)
@limiter.limit("10/minute")
async def get_daily_report(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Return cached daily report state quickly and generate reports asynchronously.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    latest_report = (
        db.query(DailyReport)
        .filter(DailyReport.generated_at >= cutoff)
        .order_by(DailyReport.generated_at.desc())
        .first()
    )

    if latest_report and latest_report.generated_at:
        status_value = (latest_report.generation_status or "failed").lower()
        if status_value == "ready":
            cached_report = (
                latest_report.report_json
                if isinstance(latest_report.report_json, dict)
                else None
            )
            if not cached_report or not _is_daily_report_payload_compatible(cached_report):
                latest_report.generation_status = "pending"
                latest_report.error_message = "stale_or_incompatible_cached_report"
                latest_report.report_json = None
                latest_report.generated_at = now
                db.add(latest_report)
                db.commit()
                db.refresh(latest_report)
                if isinstance(latest_report.id, int):
                    background_tasks.add_task(
                        _generate_daily_report_in_background, latest_report.id
                    )
                return DailyReportEndpointResponse(
                    status="pending",
                    generated_at=latest_report.generated_at.isoformat(),
                )
            return DailyReportEndpointResponse(
                status="ready",
                report=cached_report,
                generated_at=latest_report.generated_at.isoformat(),
            )
        if status_value in {"pending", "running"}:
            return DailyReportEndpointResponse(
                status=status_value,
                generated_at=latest_report.generated_at.isoformat(),
            )

    queued_report = DailyReport(
        generated_at=now,
        period_start=cutoff,
        period_end=now,
        generation_status="pending",
        error_message=None,
    )
    db.add(queued_report)
    db.commit()
    db.refresh(queued_report)
    # Defensive guard: mocked DB sessions in tests may not populate PKs.
    if isinstance(queued_report.id, int):
        background_tasks.add_task(
            _generate_daily_report_in_background, queued_report.id
        )
    else:
        logger.warning(
            "Daily report queued without integer id; background generation not scheduled"
        )

    return DailyReportEndpointResponse(
        status="pending",
        generated_at=queued_report.generated_at.isoformat(),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )
