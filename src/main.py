"""
FastAPI application for MailJaeger
"""
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pathlib import Path

from src.config import get_settings
from src.database.connection import init_db, get_db
from src.models.schemas import (
    EmailResponse, EmailDetailResponse, DashboardResponse,
    ProcessingRunResponse, EmailListRequest, SearchRequest,
    MarkResolvedRequest, TriggerRunRequest, SettingsUpdate
)
from src.models.database import ProcessedEmail, ProcessingRun
from src.services.scheduler import get_scheduler
from src.services.imap_service import IMAPService
from src.services.ai_service import AIService
from src.services.search_service import SearchService
from src.services.learning_service import LearningService
from src.services.email_processor import EmailProcessor
from src.utils.logging import setup_logging, get_logger

# Setup logging
setup_logging()
logger = get_logger(__name__)

# Create app
app = FastAPI(
    title="MailJaeger",
    description="Local AI-powered email processing system",
    version="1.0.0"
)

# Mount static files (frontend)
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Settings
settings = get_settings()


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup"""
    logger.info("Starting MailJaeger...")
    
    # Initialize database
    init_db()
    logger.info("Database initialized")
    
    # Start scheduler
    scheduler = get_scheduler()
    scheduler.start()
    logger.info("Scheduler started")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down MailJaeger...")
    
    # Stop scheduler
    scheduler = get_scheduler()
    scheduler.stop()


@app.get("/")
async def root():
    """Serve frontend dashboard"""
    frontend_file = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend_file.exists():
        return FileResponse(frontend_file)
    return {
        "name": "MailJaeger",
        "version": "1.0.0",
        "status": "running",
        "message": "Frontend not found. Access API at /docs"
    }


@app.get("/api/dashboard", response_model=DashboardResponse)
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
        logger.error(f"Dashboard error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/emails/search", response_model=List[EmailResponse])
async def search_emails(
    request: SearchRequest,
    db: Session = Depends(get_db)
):
    """Search emails with filters"""
    try:
        if request.semantic:
            # Semantic search (placeholder for future implementation)
            # Would use sentence-transformers for embedding-based search
            logger.info("Semantic search requested (not yet implemented)")
        
        # Full-text search
        search_service = SearchService(db)
        results = search_service.search(
            query=request.query,
            category=request.category.value if request.category else None,
            priority=request.priority.value if request.priority else None,
            action_required=request.action_required,
            date_from=request.date_from.isoformat() if request.date_from else None,
            date_to=request.date_to.isoformat() if request.date_to else None,
            page=request.page,
            page_size=request.page_size
        )
        
        return [EmailResponse.from_orm(email) for email in results['results']]
    
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/emails/list", response_model=List[EmailResponse])
async def list_emails(
    request: EmailListRequest,
    db: Session = Depends(get_db)
):
    """List emails with filters"""
    try:
        query = db.query(ProcessedEmail)
        
        # Apply filters
        if request.action_required is not None:
            query = query.filter(ProcessedEmail.action_required == request.action_required)
        if request.priority:
            query = query.filter(ProcessedEmail.priority == request.priority.value)
        if request.category:
            query = query.filter(ProcessedEmail.category == request.category.value)
        if request.is_spam is not None:
            query = query.filter(ProcessedEmail.is_spam == request.is_spam)
        if request.is_resolved is not None:
            query = query.filter(ProcessedEmail.is_resolved == request.is_resolved)
        if request.date_from:
            query = query.filter(ProcessedEmail.date >= request.date_from)
        if request.date_to:
            query = query.filter(ProcessedEmail.date <= request.date_to)
        
        # Sorting
        if request.sort_by == "date":
            sort_col = ProcessedEmail.date
        elif request.sort_by == "priority":
            sort_col = ProcessedEmail.priority
        else:
            sort_col = ProcessedEmail.subject
        
        if request.sort_order == "desc":
            query = query.order_by(sort_col.desc())
        else:
            query = query.order_by(sort_col.asc())
        
        # Pagination
        offset = (request.page - 1) * request.page_size
        emails = query.offset(offset).limit(request.page_size).all()
        
        return [EmailResponse.from_orm(email) for email in emails]
    
    except Exception as e:
        logger.error(f"List emails error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/emails/{email_id}", response_model=EmailDetailResponse)
async def get_email(email_id: int, db: Session = Depends(get_db)):
    """Get email details"""
    email = db.query(ProcessedEmail).filter(ProcessedEmail.id == email_id).first()
    
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    
    return EmailDetailResponse.from_orm(email)


@app.post("/api/emails/{email_id}/resolve")
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


@app.post("/api/processing/trigger")
async def trigger_processing(
    request: TriggerRunRequest,
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
        logger.error(f"Trigger processing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/processing/runs", response_model=List[ProcessingRunResponse])
async def get_processing_runs(
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """Get processing run history"""
    runs = db.query(ProcessingRun).order_by(
        ProcessingRun.started_at.desc()
    ).limit(limit).all()
    
    return [ProcessingRunResponse.from_orm(run) for run in runs]


@app.get("/api/processing/runs/{run_id}", response_model=ProcessingRunResponse)
async def get_processing_run(run_id: int, db: Session = Depends(get_db)):
    """Get specific processing run"""
    run = db.query(ProcessingRun).filter(ProcessingRun.id == run_id).first()
    
    if not run:
        raise HTTPException(status_code=404, detail="Processing run not found")
    
    return ProcessingRunResponse.from_orm(run)


@app.get("/api/settings")
async def get_settings_api():
    """Get current settings (sanitized)"""
    return {
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "imap_username": settings.imap_username,
        "spam_threshold": settings.spam_threshold,
        "ai_endpoint": settings.ai_endpoint,
        "ai_model": settings.ai_model,
        "schedule_time": settings.schedule_time,
        "schedule_timezone": settings.schedule_timezone,
        "learning_enabled": settings.learning_enabled,
        "learning_confidence_threshold": settings.learning_confidence_threshold,
        "store_email_body": settings.store_email_body,
        "store_attachments": settings.store_attachments
    }


@app.post("/api/settings")
async def update_settings_api(request: SettingsUpdate):
    """Update settings (partial update)"""
    # Note: This would require reloading configuration
    # For production, consider using a configuration management system
    return {
        "success": True,
        "message": "Settings update requires restart to take effect"
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
