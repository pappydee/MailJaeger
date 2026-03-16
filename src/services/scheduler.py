"""
Scheduler service for automated email processing
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from src.config import get_settings
from src.database.connection import get_db_session
from src.services.email_processor import EmailProcessor
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


@dataclass
class RunStatus:
    """Shared mutable state describing the current or most-recent processing run.

    Valid ``status`` values (all lowercase):
      idle | running | cancelling | cancelled | success | failed
    """

    run_id: Optional[int] = None
    status: str = "idle"  # idle | running | cancelling | cancelled | success | failed
    current_step: Optional[str] = None
    progress_percent: int = 0
    processed: int = 0
    total: int = 0
    spam: int = 0
    action_required: int = 0
    failed: int = 0
    started_at: Optional[str] = None
    last_update: Optional[str] = None
    message: str = ""
    # Set to True to ask the running job to stop at the next safe checkpoint.
    cancel_requested: bool = False

    def reset(self) -> None:
        self.run_id = None
        self.status = "idle"
        self.current_step = None
        self.progress_percent = 0
        self.processed = 0
        self.total = 0
        self.spam = 0
        self.action_required = 0
        self.failed = 0
        self.started_at = None
        self.last_update = None
        self.message = ""
        self.cancel_requested = False

    def request_cancel(self) -> bool:
        """Signal a running job to stop.  Returns True if the signal was set."""
        if self.status not in ("running", "cancelling"):
            return False
        self.cancel_requested = True
        self.status = "cancelling"
        self.last_update = datetime.utcnow().isoformat()
        return True

    def update(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.last_update = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "current_step": self.current_step,
            "progress_percent": self.progress_percent,
            "processed": self.processed,
            "total": self.total,
            "spam": self.spam,
            "action_required": self.action_required,
            "failed": self.failed,
            "started_at": self.started_at,
            "last_update": self.last_update,
            "message": self.message,
            "cancel_requested": self.cancel_requested,
        }


# Module-level singleton shared across the app process
_run_status: RunStatus = RunStatus()


def get_run_status() -> RunStatus:
    """Return the singleton RunStatus."""
    return _run_status


class SchedulerService:
    """Service for scheduling email processing"""

    def __init__(self):
        self.settings = get_settings()
        self.scheduler = BackgroundScheduler(timezone=self.settings.schedule_timezone)
        self.is_running = False
        self.lock = threading.Lock()  # thread-safe lock
        self._locked = False  # track whether a run is active

        # Add event listeners
        self.scheduler.add_listener(self._job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._job_error, EVENT_JOB_ERROR)

    def start(self):
        """Start the scheduler"""
        if self.is_running:
            logger.warning("Scheduler is already running")
            return

        # Parse schedule time
        hour, minute = self._parse_schedule_time()

        # Add daily job
        trigger = CronTrigger(
            hour=hour, minute=minute, timezone=self.settings.schedule_timezone
        )

        self.scheduler.add_job(
            self._run_processing,
            trigger=trigger,
            id="daily_email_processing",
            name="Daily Email Processing",
            replace_existing=True,
        )

        self.scheduler.start()
        self.is_running = True

        logger.info(
            f"Scheduler started - daily run at {hour:02d}:{minute:02d} "
            f"{self.settings.schedule_timezone}"
        )

    def stop(self):
        """Stop the scheduler"""
        if not self.is_running:
            return

        self.scheduler.shutdown(wait=False)
        self.is_running = False
        logger.info("Scheduler stopped")

    def trigger_manual_run_async(self) -> tuple[bool, Optional[int]]:
        """
        Trigger a manual processing run in a background thread.

        Returns (started: bool, run_id_or_None).
        Returns immediately — the run executes in a daemon thread.
        """
        with self.lock:
            if self._locked:
                logger.warning("Processing run already in progress")
                return False, _run_status.run_id

        t = threading.Thread(
            target=self._run_processing,
            args=("MANUAL",),
            daemon=True,
            name="mailjaeger-manual-run",
        )
        t.start()
        return True, None  # run_id assigned once thread creates DB record

    # Keep the old synchronous helper for backward compat with scheduled jobs
    def trigger_manual_run(self) -> bool:
        """Trigger manual processing run (blocks until complete)."""
        with self.lock:
            if self._locked:
                logger.warning("Processing run already in progress")
                return False

        try:
            self._run_processing(trigger_type="MANUAL")
            return True
        except Exception as e:
            settings = get_settings()
            sanitized_error = sanitize_error(e, debug=settings.debug)
            logger.error(f"Manual run failed: {sanitized_error}")
            return False

    def _run_processing(self, trigger_type: str = "SCHEDULED"):
        """Execute processing run with lock and progress updates."""
        with self.lock:
            if self._locked:
                logger.warning("Processing run already in progress, skipping")
                return
            self._locked = True

        _run_status.reset()
        _run_status.update(
            status="running",
            current_step="Starting…",
            started_at=datetime.utcnow().isoformat(),
            message=f"Processing started ({trigger_type})",
        )

        logger.info(f"Starting {trigger_type} processing run")

        try:
            with get_db_session() as db:
                processor = EmailProcessor(db, status=_run_status)
                run = processor.process_emails(trigger_type=trigger_type)
                logger.info(f"Processing run completed: {run.status}")

                status_map = {
                    "SUCCESS": "success",
                    "FAILURE": "failed",
                    "PARTIAL": "success",
                    "CANCELLED": "cancelled",
                }
                final_status = status_map.get(run.status, "idle")
                # Show 100 % for any completed run; for CANCELLED keep the
                # partial progress so the user can see how far the run got.
                final_pct = (
                    _run_status.progress_percent
                    if run.status == "CANCELLED"
                    else 100
                )
                _run_status.update(
                    run_id=run.id,
                    status=final_status,
                    current_step=None,
                    progress_percent=final_pct,
                    message=f"Completed: {run.status}",
                )
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(
                f"Processing run failed: {sanitized_error}",
                exc_info=self.settings.debug,
            )
            _run_status.update(
                status="failed",
                current_step=None,
                message="Run failed with an unexpected error",
            )
        finally:
            with self.lock:
                self._locked = False

    def _parse_schedule_time(self) -> tuple:
        """Parse schedule time string"""
        try:
            hour, minute = self.settings.schedule_time.split(":")
            return int(hour), int(minute)
        except (ValueError, AttributeError) as e:
            logger.warning(
                f"Invalid schedule time: {self.settings.schedule_time}, using default 08:00"
            )
            return 8, 0

    def _job_executed(self, event):
        """Callback for successful job execution"""
        logger.info(f"Scheduled job executed successfully: {event.job_id}")

    def _job_error(self, event):
        """Callback for job execution error"""
        logger.error(f"Scheduled job error: {event.job_id} - {event.exception}")

    def get_next_run_time(self) -> Optional[datetime]:
        """Get next scheduled run time"""
        if not self.is_running:
            return None

        job = self.scheduler.get_job("daily_email_processing")
        if job:
            return job.next_run_time
        return None

    def get_status(self) -> dict:
        """Get scheduler status"""
        next_run = self.get_next_run_time()

        return {
            "is_running": self.is_running,
            "is_locked": self._locked,
            "next_run_time": next_run.isoformat() if next_run else None,
            "timezone": self.settings.schedule_timezone,
            "schedule_time": self.settings.schedule_time,
        }


# Global scheduler instance
_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """Get or create scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler
