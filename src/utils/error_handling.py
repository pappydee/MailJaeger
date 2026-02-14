"""
Error handling utilities for MailJaeger
"""
import re
from typing import Optional


def sanitize_error(e: Exception, debug: bool = False) -> str:
    """
    Sanitize error messages to prevent credential leakage.
    
    In production (debug=False), only returns the exception type name.
    In debug mode, returns the error message with secrets redacted.
    
    Secrets are ALWAYS redacted, even in debug mode:
    - IMAP passwords and usernames
    - API keys
    - Common credential patterns (password=, passwd=, Authorization: Bearer)
    
    Args:
        e: The exception to sanitize
        debug: Whether to return error details (default: False)
    
    Returns:
        Sanitized error message safe for storage/API responses
    """
    if debug:
        # In debug mode, return message but redact secrets
        error_msg = str(e)
        return _redact_secrets(error_msg)
    else:
        # In production, return only the exception type, no details
        error_type = type(e).__name__
        return error_type if error_type else "UnknownError"


def _redact_secrets(text: str) -> str:
    """
    Redact secrets from text.
    
    This function redacts:
    1. IMAP credentials from settings (if available)
    2. API keys from settings (if available)
    3. Common credential patterns (case-insensitive)
    
    Args:
        text: Text to redact
    
    Returns:
        Text with secrets replaced by [REDACTED]
    """
    if not text:
        return text
    
    redacted = text
    
    # Try to get settings and redact actual credentials
    try:
        from src.config import get_settings
        settings = get_settings()
        
        # Redact IMAP password
        try:
            imap_password = settings.get_imap_password()
            if imap_password:
                redacted = redacted.replace(imap_password, "[REDACTED]")
        except Exception:
            pass
        
        # Redact IMAP username
        if settings.imap_username:
            redacted = redacted.replace(settings.imap_username, "[REDACTED]")
        
        # Redact API keys
        try:
            api_keys = settings.get_api_keys()
            for key in api_keys:
                if key:
                    redacted = redacted.replace(key, "[REDACTED]")
        except Exception:
            pass
    except Exception:
        # If we can't load settings, continue with pattern-based redaction
        pass
    
    # Redact common credential patterns (case-insensitive)
    # Pattern: password=... or passwd=... (capture until whitespace or end)
    redacted = re.sub(
        r'(password|passwd)\s*[=:]\s*[^\s,;)}\]]+',
        r'\1=[REDACTED]',
        redacted,
        flags=re.IGNORECASE
    )
    
    # Pattern: username=... or user=...
    redacted = re.sub(
        r'(username|user)\s*[=:]\s*[^\s,;)}\]]+',
        r'\1=[REDACTED]',
        redacted,
        flags=re.IGNORECASE
    )
    
    # Pattern: Bearer <token> (with or without Authorization:)
    redacted = re.sub(
        r'Bearer\s+[^\s,;)}\]]+',
        'Bearer [REDACTED]',
        redacted,
        flags=re.IGNORECASE
    )
    
    # Pattern: api_key=... or apikey=...
    redacted = re.sub(
        r'(api[_-]?key)\s*[=:]\s*[^\s,;)}\]]+',
        r'\1=[REDACTED]',
        redacted,
        flags=re.IGNORECASE
    )
    
    # Pattern: token=...
    redacted = re.sub(
        r'(token)\s*[=:]\s*[^\s,;)}\]]+',
        r'\1=[REDACTED]',
        redacted,
        flags=re.IGNORECASE
    )
    
    return redacted
