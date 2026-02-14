"""
Learning service for adaptive folder suggestions
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.config import get_settings
from src.models.database import ProcessedEmail, LearningSignal, FolderPattern, AuditLog
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


class LearningService:
    """Service for learning from user behavior"""
    
    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
    
    def detect_folder_movements(self) -> int:
        """
        Detect manual folder movements by comparing IMAP state
        with database records (for future implementation with IMAP folder monitoring)
        
        For now, this is a placeholder for the learning signal recording
        """
        # This would require periodic IMAP folder checks
        # Implementation depends on IMAP capability to track moved emails
        logger.debug("Folder movement detection placeholder")
        return 0
    
    def record_learning_signal(
        self,
        email_id: int,
        signal_type: str,
        original_value: str,
        new_value: str,
        context: Optional[Dict] = None
    ):
        """Record a learning signal"""
        try:
            signal = LearningSignal(
                email_id=email_id,
                signal_type=signal_type,
                original_value=original_value,
                new_value=new_value,
                context=context or {},
                detected_at=datetime.utcnow()
            )
            
            self.db.add(signal)
            self.db.commit()
            
            logger.info(
                f"Recorded learning signal: {signal_type} "
                f"{original_value} -> {new_value}"
            )
            
            # Update patterns
            if signal_type == "FOLDER_MOVE":
                self._update_folder_pattern(email_id, new_value)
        
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to record learning signal: {sanitized_error}")
            self.db.rollback()
    
    def _update_folder_pattern(self, email_id: int, target_folder: str):
        """Update folder pattern based on learning signal"""
        try:
            # Get email
            email = self.db.query(ProcessedEmail).filter(
                ProcessedEmail.id == email_id
            ).first()
            
            if not email:
                return
            
            # Extract pattern features
            sender_pattern = self._extract_sender_pattern(email.sender)
            
            # Find or create pattern
            pattern = self.db.query(FolderPattern).filter(
                FolderPattern.sender_pattern == sender_pattern,
                FolderPattern.category == email.category,
                FolderPattern.target_folder == target_folder
            ).first()
            
            if pattern:
                # Update existing pattern
                pattern.occurrence_count += 1
                pattern.success_count += 1
                pattern.last_seen = datetime.utcnow()
                
                # Update confidence
                pattern.confidence = pattern.success_count / pattern.occurrence_count
            else:
                # Create new pattern
                pattern = FolderPattern(
                    sender_pattern=sender_pattern,
                    category=email.category,
                    target_folder=target_folder,
                    occurrence_count=1,
                    success_count=1,
                    confidence=1.0,
                    first_seen=datetime.utcnow(),
                    last_seen=datetime.utcnow()
                )
                self.db.add(pattern)
            
            self.db.commit()
            logger.debug(f"Updated folder pattern: {sender_pattern} -> {target_folder}")
        
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to update folder pattern: {sanitized_error}")
            self.db.rollback()
    
    def _extract_sender_pattern(self, sender: str) -> str:
        """Extract sender pattern from email address"""
        if not sender:
            return "unknown"
        
        # Extract domain
        if '@' in sender:
            parts = sender.split('@')
            if len(parts) > 1:
                domain = parts[-1].split('>')[0].strip()
                return f"@{domain}"
        
        return sender.lower()
    
    def get_suggested_folder(self, email: ProcessedEmail) -> Optional[str]:
        """Get suggested folder based on learned patterns"""
        if not self.settings.learning_enabled:
            return None
        
        try:
            sender_pattern = self._extract_sender_pattern(email.sender)
            
            # Find matching patterns
            patterns = self.db.query(FolderPattern).filter(
                FolderPattern.sender_pattern == sender_pattern,
                FolderPattern.category == email.category,
                FolderPattern.confidence >= self.settings.learning_confidence_threshold
            ).order_by(FolderPattern.confidence.desc()).all()
            
            if patterns:
                # Return highest confidence pattern
                pattern = patterns[0]
                logger.info(
                    f"Suggested folder for {sender_pattern}: "
                    f"{pattern.target_folder} (confidence: {pattern.confidence:.2f})"
                )
                return pattern.target_folder
            
            return None
        
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to get suggested folder: {sanitized_error}")
            return None
    
    def apply_learned_routing(self, email: ProcessedEmail) -> bool:
        """
        Apply learned folder routing if confidence threshold is met
        
        Returns True if routing was applied
        """
        suggested_folder = self.get_suggested_folder(email)
        
        if suggested_folder:
            # Update email suggestion
            email.suggested_folder = suggested_folder
            
            # Could automatically move here if confidence is very high
            # For now, just suggest
            return True
        
        return False
    
    def get_pattern_statistics(self) -> Dict[str, Any]:
        """Get statistics about learned patterns"""
        try:
            total_patterns = self.db.query(FolderPattern).count()
            
            high_confidence = self.db.query(FolderPattern).filter(
                FolderPattern.confidence >= self.settings.learning_confidence_threshold
            ).count()
            
            total_signals = self.db.query(LearningSignal).count()
            
            recent_signals = self.db.query(LearningSignal).filter(
                LearningSignal.detected_at >= datetime.utcnow() - timedelta(days=30)
            ).count()
            
            return {
                "total_patterns": total_patterns,
                "high_confidence_patterns": high_confidence,
                "total_learning_signals": total_signals,
                "recent_signals_30d": recent_signals
            }
        
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to get pattern statistics: {sanitized_error}")
            return {}
