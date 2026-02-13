"""
Scheduler service for automated email processing
"""
import logging
from datetime import datetime, time
from typing import Optional
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from src.config import get_settings
from src.database.connection import get_db_session
from src.services.email_processor import EmailProcessor
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SchedulerService:
    """Service for scheduling email processing"""
    
    def __init__(self):
        self.settings = get_settings()
        self.scheduler = BackgroundScheduler(timezone=self.settings.schedule_timezone)
        self.is_running = False
        self.lock = False
        
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
            hour=hour,
            minute=minute,
            timezone=self.settings.schedule_timezone
        )
        
        self.scheduler.add_job(
            self._run_processing,
            trigger=trigger,
            id='daily_email_processing',
            name='Daily Email Processing',
            replace_existing=True
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
    
    def trigger_manual_run(self) -> bool:
        """Trigger manual processing run"""
        if self.lock:
            logger.warning("Processing run already in progress")
            return False
        
        try:
            self._run_processing(trigger_type="MANUAL")
            return True
        except Exception as e:
            logger.error(f"Manual run failed: {e}")
            return False
    
    def _run_processing(self, trigger_type: str = "SCHEDULED"):
        """Execute processing run with lock"""
        if self.lock:
            logger.warning("Processing run already in progress, skipping")
            return
        
        self.lock = True
        logger.info(f"Starting {trigger_type} processing run")
        
        try:
            with get_db_session() as db:
                processor = EmailProcessor(db)
                run = processor.process_emails(trigger_type=trigger_type)
                logger.info(f"Processing run completed: {run.status}")
        except Exception as e:
            logger.error(f"Processing run failed: {e}", exc_info=True)
        finally:
            self.lock = False
    
    def _parse_schedule_time(self) -> tuple:
        """Parse schedule time string"""
        try:
            hour, minute = self.settings.schedule_time.split(':')
            return int(hour), int(minute)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Invalid schedule time: {self.settings.schedule_time}, using default 08:00")
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
        
        job = self.scheduler.get_job('daily_email_processing')
        if job:
            return job.next_run_time
        return None
    
    def get_status(self) -> dict:
        """Get scheduler status"""
        next_run = self.get_next_run_time()
        
        return {
            "is_running": self.is_running,
            "is_locked": self.lock,
            "next_run_time": next_run.isoformat() if next_run else None,
            "timezone": self.settings.schedule_timezone,
            "schedule_time": self.settings.schedule_time
        }


# Global scheduler instance
_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """Get or create scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler
