"""
FastAPI application for MailJaeger
"""
from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pathlib import Path
import sys
import secrets

from src.config import get_settings
from src.database.connection import init_db, get_db
from src.models.schemas import (
    EmailResponse, EmailDetailResponse, DashboardResponse,
    ProcessingRunResponse, EmailListRequest, SearchRequest,
    MarkResolvedRequest, TriggerRunRequest, SettingsUpdate,
    PendingActionResponse, PendingActionListRequest,
    ApproveRejectRequest, ApplyActionsRequest, PurgeRequest
)
from src.models.database import ProcessedEmail, ProcessingRun, PendingAction
from src.services.scheduler import get_scheduler
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.services.search_service import SearchService
from src.services.learning_service import LearningService
from src.services.email_processor import EmailProcessor
from src.services.purge_service import PurgeService
from src.middleware.auth import require_authentication, AuthenticationError
from src.middleware.security_headers import SecurityHeadersMiddleware
from src.middleware.rate_limiting import limiter, rate_limit_exceeded_handler
from src.utils.logging import setup_logging, get_logger

# Setup logging
setup_logging()
logger = get_logger(__name__)

# Settings with validation
try:
    settings = get_settings()
    settings.validate_required_settings()
except ValueError as e:
    logger.error(f"Configuration validation failed: {e}")
    print(f"\n❌ Configuration Error:\n{e}\n", file=sys.stderr)
    print("Please check your .env file and environment variables.", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    print(f"\n❌ Configuration Error: {e}\n", file=sys.stderr)
    sys.exit(1)

# Create app
app = FastAPI(
    title="MailJaeger",
    description="Local AI-powered email processing system (Secure)",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# Global authentication middleware (fail-closed)
# This enforces authentication for ALL routes except explicit allowlist
@app.middleware("http")
async def global_auth_middleware(request: Request, call_next):
    """
    Global authentication middleware that enforces Bearer token auth for all routes
    except those in the explicit allowlist. This is fail-closed by default.
    """
    # Explicit allowlist of unauthenticated routes
    UNAUTHENTICATED_ROUTES = {"/api/health"}
    
    # Allow unauthenticated access only to explicitly allowed routes
    if request.url.path in UNAUTHENTICATED_ROUTES:
        return await call_next(request)
    
    # Check authentication for all other routes
    settings = get_settings()
    api_keys = settings.get_api_keys()
    
    # Fail-closed: If no API keys configured, deny all access except allowlist
    if not api_keys:
        logger.error(f"No API keys configured - denying access to {request.url.path}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Get credentials from header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning(f"Unauthenticated request to {request.url.path} from {request.client.host if request.client else 'unknown'}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Extract and verify token
    try:
        token = auth_header.split(" ", 1)[1]
        if not any(secrets.compare_digest(token, key) for key in api_keys):
            logger.warning(f"Failed authentication attempt for {request.url.path} from {request.client.host if request.client else 'unknown'}")
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"}
            )
    except IndexError:
        logger.warning(f"Malformed auth header for {request.url.path}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Authentication successful, log and proceed
    logger.debug(f"Authenticated request to {request.url.path}")
    return await call_next(request)


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
                    content={"detail": f"Request body too large. Maximum size: {self.max_size} bytes"}
                )
        return await call_next(request)

app.add_middleware(RequestSizeLimiterMiddleware, max_size=10 * 1024 * 1024)

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add rate limiting state
app.state.limiter = limiter

# Mount static files (frontend) - will be protected by global auth middleware
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# CORS - Restrictive configuration
cors_origins = settings.cors_origins if isinstance(settings.cors_origins, list) else ["http://localhost:8000", "http://127.0.0.1:8000"]
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
        content={
            "detail": "Invalid request data",
            "errors": exc.errors()
        }
    )


@app.exception_handler(AuthenticationError)
async def auth_exception_handler(request: Request, exc: AuthenticationError):
    """Handle authentication errors"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions with sanitized error messages"""
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    
    # Don't leak internal details in production
    detail = str(exc) if settings.debug else "An internal error occurred"
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": detail}
    )


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup"""
    logger.info("=" * 60)
    logger.info("Starting MailJaeger...")
    logger.info(f"Version: 1.0.0")
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
        settings.log_file.parent if settings.log_file else None
    ]:
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")
    
    # Initialize database
    init_db()
    logger.info("Database initialized")
    
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
        "version": "1.0.0",
        "status": "running",
        "message": "Frontend not found. Access API at /api/docs"
    }


@app.get("/api/dashboard", response_model=DashboardResponse, dependencies=[Depends(require_authentication)])
async def get_dashboard(db: Session = Depends(get_db)):
    """Get dashboard overview"""
    try:
        # Get last run
        last_run = db.query(ProcessingRun).order_by(
            ProcessingRun.started_at.desc()
        ).first()
        
        # Get scheduler info
        scheduler = get_scheduler()
        next_run = scheduler.get_next_run_time()
        
        # Get statistics
        total_emails = db.query(ProcessedEmail).count()
        action_required_count = db.query(ProcessedEmail).filter(
            ProcessedEmail.action_required == True,
            ProcessedEmail.is_spam == False
        ).count()
        unresolved_count = db.query(ProcessedEmail).filter(
            ProcessedEmail.action_required == True,
            ProcessedEmail.is_resolved == False,
            ProcessedEmail.is_spam == False
        ).count()
        
        # Health checks
        imap_service = IMAPService()
        ai_service = AIService()
        
        health_status = {
            "mail_server": imap_service.check_health(),
            "ai_service": ai_service.check_health(),
            "database": {"status": "healthy", "message": "Database operational"},
            "scheduler": scheduler.get_status()
        }
        
        return DashboardResponse(
            last_run=ProcessingRunResponse.from_orm(last_run) if last_run else None,
            next_scheduled_run=next_run.isoformat() if next_run else None,
            total_emails=total_emails,
            action_required_count=action_required_count,
            unresolved_count=unresolved_count,
            health_status=health_status
        )
    
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, 
            detail="Failed to load dashboard" if not settings.debug else str(e)
        )


@app.post("/api/emails/search", response_model=List[EmailResponse], dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")  # Rate limit expensive search operations
async def search_emails(
    request: Request,
    search_request: SearchRequest,
    db: Session = Depends(get_db)
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
            date_from=search_request.date_from.isoformat() if search_request.date_from else None,
            date_to=search_request.date_to.isoformat() if search_request.date_to else None,
            page=search_request.page,
            page_size=search_request.page_size
        )
        
        return [EmailResponse.from_orm(email) for email in results['results']]
    
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, 
            detail="Search failed" if not settings.debug else str(e)
        )


@app.post("/api/emails/list", response_model=List[EmailResponse], dependencies=[Depends(require_authentication)])
@limiter.limit("60/minute")  # Rate limit list operations
async def list_emails(
    request: Request,
    email_request: EmailListRequest,
    db: Session = Depends(get_db)
):
    """List emails with filters"""
    try:
        query = db.query(ProcessedEmail)
        
        # Apply filters
        if email_request.action_required is not None:
            query = query.filter(ProcessedEmail.action_required == email_request.action_required)
        if email_request.priority:
            query = query.filter(ProcessedEmail.priority == email_request.priority.value)
        if email_request.category:
            query = query.filter(ProcessedEmail.category == email_request.category.value)
        if email_request.is_spam is not None:
            query = query.filter(ProcessedEmail.is_spam == email_request.is_spam)
        if email_request.is_resolved is not None:
            query = query.filter(ProcessedEmail.is_resolved == email_request.is_resolved)
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
        logger.error(f"List emails error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to list emails" if not settings.debug else str(e)
        )


@app.get("/api/emails/{email_id}", response_model=EmailDetailResponse, dependencies=[Depends(require_authentication)])
async def get_email(email_id: int, db: Session = Depends(get_db)):
    """Get email details"""
    email = db.query(ProcessedEmail).filter(ProcessedEmail.id == email_id).first()
    
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    
    return EmailDetailResponse.from_orm(email)


@app.post("/api/emails/{email_id}/resolve", dependencies=[Depends(require_authentication)])
async def mark_email_resolved(
    email_id: int,
    request: MarkResolvedRequest,
    db: Session = Depends(get_db)
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


@app.post("/api/processing/trigger", dependencies=[Depends(require_authentication)])
@limiter.limit("5/minute")  # Strict rate limit on manual processing trigger
async def trigger_processing(
    request: Request,
    trigger_request: TriggerRunRequest,
    db: Session = Depends(get_db)
):
    """Manually trigger email processing"""
    try:
        scheduler = get_scheduler()
        success = scheduler.trigger_manual_run()
        
        if success:
            return {"success": True, "message": "Processing triggered"}
        else:
            return {"success": False, "message": "Processing already in progress"}
    
    except Exception as e:
        logger.error(f"Trigger processing error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to trigger processing" if not settings.debug else str(e)
        )


@app.get("/api/processing/runs", response_model=List[ProcessingRunResponse], dependencies=[Depends(require_authentication)])
async def get_processing_runs(
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """Get processing run history"""
    runs = db.query(ProcessingRun).order_by(
        ProcessingRun.started_at.desc()
    ).limit(limit).all()
    
    return [ProcessingRunResponse.from_orm(run) for run in runs]


@app.get("/api/processing/runs/{run_id}", response_model=ProcessingRunResponse, dependencies=[Depends(require_authentication)])
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
        "mark_as_read": settings.mark_as_read
    }


@app.post("/api/settings", dependencies=[Depends(require_authentication)])
async def update_settings_api(request: SettingsUpdate):
    """Update settings (partial update)"""
    # Note: This would require reloading configuration
    # For production, consider using a configuration management system
    return {
        "success": True,
        "message": "Settings update requires restart to take effect"
    }




# ============================================================================
# Pending Actions Endpoints
# ============================================================================

@app.get("/api/pending-actions", response_model=List[PendingActionResponse], dependencies=[Depends(require_authentication)])
@limiter.limit("60/minute")
async def list_pending_actions(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by status"),
    email_id: Optional[int] = Query(None, description="Filter by email ID"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """List pending actions with optional filters"""
    try:
        query = db.query(PendingAction)
        
        if status:
            query = query.filter(PendingAction.status == status)
        if email_id:
            query = query.filter(PendingAction.email_id == email_id)
        if action_type:
            query = query.filter(PendingAction.action_type == action_type)
        
        # Order by created_at descending (newest first)
        query = query.order_by(PendingAction.created_at.desc())
        
        # Pagination
        offset = (page - 1) * page_size
        actions = query.offset(offset).limit(page_size).all()
        
        return actions
    except Exception as e:
        logger.error(f"Error listing pending actions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list pending actions")


@app.post("/api/pending-actions/approve", dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")
async def approve_actions(
    request: Request,
    approve_request: ApproveRejectRequest,
    db: Session = Depends(get_db)
):
    """Approve pending actions"""
    try:
        actions = db.query(PendingAction).filter(
            PendingAction.id.in_(approve_request.action_ids),
            PendingAction.status == 'PENDING'
        ).all()
        
        if not actions:
            raise HTTPException(status_code=404, detail="No pending actions found with given IDs")
        
        for action in actions:
            action.status = 'APPROVED'
            action.approved_by = approve_request.approved_by
            action.approved_at = datetime.utcnow()
            
            # Add audit log
            audit = AuditLog(
                event_type="ACTION_APPROVED",
                email_message_id=None,
                description=f"Action {action.id} approved by {approve_request.approved_by}",
                data={
                    "action_id": action.id,
                    "action_type": action.action_type,
                    "approved_by": approve_request.approved_by
                }
            )
            db.add(audit)
        
        db.commit()
        
        logger.info(
            f"Approved {len(actions)} actions by {approve_request.approved_by}"
        )
        
        return {
            "success": True,
            "approved_count": len(actions),
            "message": f"Approved {len(actions)} action(s)"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error approving actions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to approve actions")


@app.post("/api/pending-actions/reject", dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")
async def reject_actions(
    request: Request,
    reject_request: ApproveRejectRequest,
    db: Session = Depends(get_db)
):
    """Reject pending actions"""
    try:
        actions = db.query(PendingAction).filter(
            PendingAction.id.in_(reject_request.action_ids),
            PendingAction.status == 'PENDING'
        ).all()
        
        if not actions:
            raise HTTPException(status_code=404, detail="No pending actions found with given IDs")
        
        for action in actions:
            action.status = 'REJECTED'
            action.approved_by = reject_request.approved_by
            action.approved_at = datetime.utcnow()
            
            # Add audit log
            audit = AuditLog(
                event_type="ACTION_REJECTED",
                email_message_id=None,
                description=f"Action {action.id} rejected by {reject_request.approved_by}",
                data={
                    "action_id": action.id,
                    "action_type": action.action_type,
                    "approved_by": reject_request.approved_by
                }
            )
            db.add(audit)
        
        db.commit()
        
        logger.info(
            f"Rejected {len(actions)} actions by {reject_request.approved_by}"
        )
        
        return {
            "success": True,
            "rejected_count": len(actions),
            "message": f"Rejected {len(actions)} action(s)"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error rejecting actions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reject actions")


@app.post("/api/pending-actions/apply", dependencies=[Depends(require_authentication)])
@limiter.limit("10/minute")
async def apply_actions(
    request: Request,
    apply_request: ApplyActionsRequest,
    db: Session = Depends(get_db)
):
    """Apply approved actions to IMAP server"""
    try:
        settings = get_settings()
        
        # Build query for approved actions
        query = db.query(PendingAction).filter(
            PendingAction.status == 'APPROVED'
        )
        
        if apply_request.action_ids:
            query = query.filter(PendingAction.id.in_(apply_request.action_ids))
        
        query = query.order_by(PendingAction.created_at)
        
        if apply_request.max_count:
            query = query.limit(apply_request.max_count)
        
        actions = query.all()
        
        if not actions:
            return {
                "success": True,
                "applied_count": 0,
                "failed_count": 0,
                "message": "No approved actions to apply"
            }
        
        # Apply actions atomically (each action isolated)
        applied_count = 0
        failed_count = 0
        imap_service = None
        
        try:
            # Initialize IMAP connection once for all actions
            if not settings.safe_mode:
                imap_service = IMAPService()
        except Exception as e:
            logger.error(f"Failed to initialize IMAP service: {e}", exc_info=True)
            # Mark all actions as failed
            for action in actions:
                action.status = 'FAILED'
                action.error_message = "IMAP connection failed"
                action.applied_at = datetime.utcnow()
                failed_count += 1
            
            db.commit()
            return {
                "success": False,
                "applied_count": 0,
                "failed_count": failed_count,
                "message": "IMAP connection failed"
            }
        
        for action in actions:
            try:
                if settings.safe_mode:
                    # In safe mode, just mark as applied without actual IMAP
                    action.status = 'APPLIED'
                    action.applied_at = datetime.utcnow()
                    applied_count += 1
                    logger.info(
                        f"Action {action.id} marked as applied (SAFE_MODE)"
                    )
                    
                    # Add audit log
                    audit = AuditLog(
                        event_type="ACTION_APPLIED",
                        email_message_id=None,
                        description=f"Action {action.id} applied in safe mode",
                        data={
                            "action_id": action.id,
                            "action_type": action.action_type,
                            "safe_mode": True
                        }
                    )
                    db.add(audit)
                else:
                    # Execute actual IMAP operation
                    # Get the email to find UID
                    email = db.query(ProcessedEmail).filter(
                        ProcessedEmail.id == action.email_id
                    ).first()
                    
                    if not email or not email.uid:
                        raise ValueError("Email or UID not found")
                    
                    uid = email.uid
                    success = False
                    
                    # Execute action based on type
                    if action.action_type == "MOVE":
                        if not action.target_folder:
                            raise ValueError("Target folder required for MOVE")
                        success = imap_service.move_to_folder(
                            uid,
                            action.target_folder
                        )
                        if success:
                            email.is_archived = True
                    
                    elif action.action_type == "MARK_READ":
                        success = imap_service.mark_as_read(uid)
                    
                    elif action.action_type == "FLAG":
                        success = imap_service.add_flag(uid)
                        if success:
                            email.is_flagged = True
                    
                    elif action.action_type == "DELETE":
                        # DELETE would move to trash/delete folder
                        # For safety, treat as MOVE to spam
                        success = imap_service.move_to_folder(
                            uid,
                            settings.spam_folder
                        )
                        if success:
                            email.is_archived = True
                    
                    else:
                        raise ValueError(
                            f"Unknown action type: {action.action_type}"
                        )
                    
                    if success:
                        action.status = 'APPLIED'
                        action.applied_at = datetime.utcnow()
                        applied_count += 1
                        logger.info(
                            f"Action {action.id} applied successfully: "
                            f"{action.action_type}"
                        )
                        
                        # Add audit log
                        audit = AuditLog(
                            event_type="ACTION_APPLIED",
                            email_message_id=email.message_id,
                            description=(
                                f"Action {action.action_type} applied "
                                f"to email {email.message_id}"
                            ),
                            data={
                                "action_id": action.id,
                                "action_type": action.action_type,
                                "target_folder": action.target_folder,
                                "safe_mode": False
                            }
                        )
                        db.add(audit)
                    else:
                        raise RuntimeError("IMAP operation failed")
                        
            except Exception as e:
                # Sanitize error message - never expose credentials or internal details
                error_msg = "Operation failed"
                if settings.debug:
                    error_msg = str(e)
                else:
                    # In production, use generic error message
                    if "password" in str(e).lower() or "credential" in str(e).lower():
                        error_msg = "Authentication error"
                    elif "connection" in str(e).lower():
                        error_msg = "Connection error"
                    elif "not found" in str(e).lower():
                        error_msg = "Resource not found"
                    else:
                        error_msg = "Operation failed"
                
                action.status = 'FAILED'
                action.error_message = error_msg
                action.applied_at = datetime.utcnow()
                failed_count += 1
                logger.error(
                    f"Failed to apply action {action.id}: {type(e).__name__}",
                    exc_info=settings.debug
                )
        
        db.commit()
        
        return {
            "success": True,
            "applied_count": applied_count,
            "failed_count": failed_count,
            "message": f"Applied {applied_count} action(s), {failed_count} failed"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error applying actions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to apply actions")


# ============================================================================
# Purge Endpoint
# ============================================================================

@app.post("/api/purge", dependencies=[Depends(require_authentication)])
@limiter.limit("2/hour")  # Strict rate limit for admin operations
async def trigger_purge(
    request: Request,
    purge_request: PurgeRequest,
    db: Session = Depends(get_db)
):
    """Manually trigger data purge (admin only)"""
    try:
        purge_service = PurgeService(db)
        stats = purge_service.execute_purge(dry_run=purge_request.dry_run)
        
        return {
            "success": True,
            "dry_run": stats['dry_run'],
            "stats": {
                "emails_deleted": stats['emails_deleted'],
                "actions_deleted": stats['actions_deleted'],
                "audit_logs_deleted": stats['audit_logs_deleted']
            },
            "errors": stats['errors'],
            "message": "Purge completed successfully" if not stats['errors'] else "Purge completed with errors"
        }
    except Exception as e:
        logger.error(f"Error triggering purge: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to trigger purge")


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
            "scheduler": get_scheduler().get_status()
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower()
    )
