"""
Logging configuration for MailJaeger with security filtering
"""
import logging
import sys
import re
from pathlib import Path
from typing import Optional
from datetime import datetime

from src.config import get_settings


class SensitiveDataFilter(logging.Filter):
    """Filter to remove sensitive data from logs"""
    
    # Patterns to redact
    SENSITIVE_PATTERNS = [
        # Password patterns
        (re.compile(r'(password["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(passwd["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(pwd["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        
        # Username patterns
        (re.compile(r'(username["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(user["\s:=]+)([^\s,}\]]+@[^\s,}\]]+)', re.IGNORECASE), r'\1[REDACTED]'),
        
        # API key and token patterns
        (re.compile(r'(api[_-]?key["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(bearer\s+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(authorization["\s:=\s]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(token["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(secret["\s:=]+)[^\s,}\]]+', re.IGNORECASE), r'\1[REDACTED]'),
        (re.compile(r'(key["\s:=]+)[A-Za-z0-9_\-]{20,}', re.IGNORECASE), r'\1[REDACTED]'),
        
        # Authorization headers in various formats
        (re.compile(r'Authorization:\s*Bearer\s+[^\s,}\]]+', re.IGNORECASE), r'Authorization: Bearer [REDACTED]'),
        (re.compile(r'Authorization:\s*[^\s,}\]]+', re.IGNORECASE), r'Authorization: [REDACTED]'),
        (re.compile(r'"Authorization":\s*"[^"]*"', re.IGNORECASE), r'"Authorization": "[REDACTED]"'),
        
        # Email patterns in credentials context
        (re.compile(r'(login|auth|credential)[^\n]*?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', re.IGNORECASE), r'\1...[REDACTED]'),
        
        # Email body patterns (to avoid logging full email content)
        # Using [\s\S] to match any character including newlines
        (re.compile(r'(body_plain["\s:=]+)[\s\S]{200,}', re.IGNORECASE), r'\1[EMAIL_BODY_REDACTED]'),
        (re.compile(r'(body_html["\s:=]+)[\s\S]{200,}', re.IGNORECASE), r'\1[EMAIL_BODY_REDACTED]'),
        (re.compile(r'(content["\s:=]+)[\s\S]{200,}', re.IGNORECASE), r'\1[CONTENT_REDACTED]'),
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to remove sensitive data"""
        if record.msg:
            record.msg = self._redact_message(str(record.msg))
        
        # Also filter args if present
        if record.args:
            record.args = self._redact_args(record.args)
        
        return True
    
    def _redact_message(self, msg: str) -> str:
        """Redact sensitive patterns from message"""
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            msg = pattern.sub(replacement, msg)
        return msg
    
    def _redact_args(self, args):
        """Redact sensitive patterns from log arguments"""
        if isinstance(args, tuple):
            filtered_args = []
            for arg in args:
                arg_str = str(arg)
                for pattern, replacement in self.SENSITIVE_PATTERNS:
                    arg_str = pattern.sub(replacement, arg_str)
                filtered_args.append(arg_str)
            return tuple(filtered_args)
        elif args:
            arg_str = str(args)
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                arg_str = pattern.sub(replacement, arg_str)
            return arg_str
        return args


def setup_logging(log_file: Optional[Path] = None, log_level: str = "INFO"):
    """Setup logging configuration with security filtering"""
    settings = get_settings()
    
    # Use settings if not provided
    if log_file is None:
        log_file = settings.log_file
    if log_level == "INFO":
        log_level = settings.log_level
    
    # Create log directory if needed
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Create formatter with structured logging
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Add sensitive data filter
    sensitive_filter = SensitiveDataFilter()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    root_logger.addHandler(console_handler)
    
    # File handler
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(getattr(logging, log_level.upper()))
        file_handler.setFormatter(formatter)
        file_handler.addFilter(sensitive_filter)
        root_logger.addHandler(file_handler)
    
    # Reduce noise from external libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get logger instance"""
    return logging.getLogger(name)
