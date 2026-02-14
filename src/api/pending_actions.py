"""
API endpoints for pending actions approval workflow
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List

from src.database.connection import get_db
from src.models.schemas import (
    PendingActionResponse,
    PendingActionsListRequest,
    PendingActionsSummary,
    ApproveActionRequest,
    RejectActionRequest,
    ApplyActionRequest,
    BatchApplyRequest,
    ActionStatus,
    ActionType
)
from src.services.pending_actions_service import PendingActionsService
from src.middleware.auth import require_authentication
from src.middleware.rate_limiting import limiter
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/pending-actions", tags=["pending-actions"])


@router.get("", response_model=List[PendingActionResponse], dependencies=[Depends(require_authentication)])
@limiter.limit("60/minute")
async def list_pending_actions(
    request: Request,
    status: ActionStatus = Query(default=None),
    action_type: ActionType = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """List pending actions with filters and pagination"""
    try:
        service = PendingActionsService(db)
        result = service.list_pending_actions(
            status=status.value if status else None,
            action_type=action_type.value if action_type else None,
            page=page,
            page_size=page_size
        )
        
        return [PendingActionResponse.from_orm(action) for action in result['results']]
    
    except Exception as e:
        logger.error(f"List pending actions error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list pending actions")


@router.get("/summary", response_model=PendingActionsSummary, dependencies=[Depends(require_authentication)])
@limiter.limit("60/minute")
async def get_pending_actions_summary(
    request: Request,
    db: Session = Depends(get_db)
):
    """Get summary statistics of pending actions"""
    try:
        service = PendingActionsService(db)
        summary = service.get_summary()
        return PendingActionsSummary(**summary)
    
    except Exception as e:
        logger.error(f"Get pending actions summary error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get summary")


@router.post("/{action_id}/approve", dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")
async def approve_action(
    request: Request,
    action_id: int,
    approve_request: ApproveActionRequest,
    db: Session = Depends(get_db)
):
    """Approve a pending action"""
    try:
        service = PendingActionsService(db)
        success = service.approve_action(action_id, approved_by="admin")
        
        if success:
            return {"success": True, "action_id": action_id, "status": "APPROVED"}
        else:
            raise HTTPException(status_code=404, detail="Action not found or not pending")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Approve action error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to approve action")


@router.post("/{action_id}/reject", dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")
async def reject_action(
    request: Request,
    action_id: int,
    reject_request: RejectActionRequest,
    db: Session = Depends(get_db)
):
    """Reject a pending action"""
    try:
        service = PendingActionsService(db)
        success = service.reject_action(action_id, rejected_by="admin")
        
        if success:
            return {"success": True, "action_id": action_id, "status": "REJECTED"}
        else:
            raise HTTPException(status_code=404, detail="Action not found or not pending")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reject action error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reject action")


@router.post("/{action_id}/apply", dependencies=[Depends(require_authentication)])
@limiter.limit("10/minute")
async def apply_action(
    request: Request,
    action_id: int,
    apply_request: ApplyActionRequest,
    db: Session = Depends(get_db)
):
    """Apply a single approved action"""
    try:
        service = PendingActionsService(db)
        result = service.apply_action(action_id)
        
        if result["success"]:
            return {"success": True, "action_id": action_id, "status": "APPLIED"}
        else:
            # Check for concurrent apply lock
            if result.get("error_code") == "CONCURRENT_APPLY":
                raise HTTPException(status_code=409, detail=result["error"])
            
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to apply action"))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Apply action error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to apply action")


@router.post("/apply", dependencies=[Depends(require_authentication)])
@limiter.limit("5/minute")
async def batch_apply_actions(
    request: Request,
    batch_request: BatchApplyRequest,
    db: Session = Depends(get_db)
):
    """Apply multiple approved actions in batch"""
    try:
        service = PendingActionsService(db)
        result = service.apply_batch(max_count=batch_request.max_count)
        
        if not result["success"]:
            # Check for concurrent apply lock
            if result.get("error_code") == "CONCURRENT_APPLY":
                raise HTTPException(status_code=409, detail=result["error"])
            
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to apply batch"))
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Batch apply error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to apply batch")
