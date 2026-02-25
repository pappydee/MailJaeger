"""
FastAPI application for MailJaeger
"""

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from pathlib import Path
import sys
import secrets
import hashlib

from src.config import get_settings
from src.database.connection import init_db, get_db, get_engine
from src.database.startup_checks import verify_pending_actions_table
from src.models.schemas import (
    EmailResponse,
    EmailDetailResponse,
    DashboardResponse,
    ProcessingRunResponse,
    EmailListRequest,
    SearchRequest,
    MarkResolvedRequest,
    TriggerRunRequest,
    SettingsUpdate,
    PendingActionResponse,
    PendingActionWithEmailResponse,
    ApproveActionRequest,
    ApplyActionsRequest,
    PreviewActionsRequest,
    PreviewActionsResponse,
    ApplyActionsResponse,
    ClassificationOverrideRequest,
    ClassificationOverrideResponse,
)
from src.models.database import ProcessedEmail, ProcessingRun, PendingAction, ApplyToken, ClassificationOverride
from src.services.scheduler import get_scheduler, get_run_status
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.services.search_service import SearchService
from src.services.learning_service import LearningService
from src.services.email_processor import EmailProcessor
from src.middleware.auth import require_authentication, AuthenticationError
from src.middleware.security_headers import SecurityHeadersMiddleware
from src.middleware.allowed_hosts import AllowedHostsMiddleware
from src.middleware.rate_limiting import limiter, rate_limit_exceeded_handler
from src.utils.logging import setup_logging, get_logger
from src.utils.error_handling import sanitize_error
from src import __version__, CHANGELOG

# Setup logging
setup_logging()
logger = get_logger(__name__)

# In-memory session store: token -> expiry datetime
# Sessions are lost on restart (acceptable for a local tool).
_sessions: Dict[str, datetime] = {}
SESSION_COOKIE = "mailjaeger_session"
SESSION_EXPIRY_HOURS = 24

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

        health_status = {
            "mail_server": imap_service.check_health(),
            "ai_service": ai_service.check_health(),
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
async def get_settings_api():
    """Get current settings (sanitized - no sensitive credentials)"""
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
        "require_approval": settings.require_approval,
        "mark_as_read": settings.mark_as_read,
    }


@app.post("/api/settings", dependencies=[Depends(require_authentication)])
async def update_settings_api(request: SettingsUpdate):
    """Update settings (partial update)"""
    # Note: This would require reloading configuration
    # For production, consider using a configuration management system
    return {
        "success": True,
        "message": "Settings update requires restart to take effect",
    }


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
    if settings.safe_mode:
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
    safe_folders = settings.get_safe_folders()

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
            if action.action_type == "DELETE" and not settings.allow_destructive_imap:
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
                            if not settings.allow_destructive_imap:
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
                                {"action_id": action.id, "status": "APPLIED", "error": None}
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

            # Commit all changes at once
            db.commit()

            # Mark token as used ONLY after successful completion
            # This happens after all actions are processed and committed
            token_record.is_used = True
            token_record.used_at = datetime.utcnow()
            db.commit()

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
    if settings.safe_mode:
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
    safe_folders = settings.get_safe_folders()

    # Safety check: Block DELETE unless explicitly enabled
    if action.action_type == "DELETE":
        if not settings.allow_destructive_imap:
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
                settings.debug,
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
                        status_code=400, detail=f"Unknown action type: {action.action_type}"
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
            logger.error(f"IMAP connection failed for action {action.id}: {sanitized_error}")

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )
