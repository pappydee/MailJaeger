"""
Error handling utilities for MailJaeger
"""

import re
from typing import Optional

# Maximum characters in a sanitized error message.  IMAP exceptions frequently
# embed the raw server response (which includes email headers, body text, MIME
# boundaries, and HTML — all printable ASCII).  Allowing the full ``str(e)``
# through, even with credential redaction, leaks email content into logs and
# API responses.  A hard cap prevents that.
_MAX_SANITIZED_LEN = 200


def sanitize_error(e: Exception, debug: bool = False) -> str:
    """
    Sanitize error messages to prevent credential and content leakage.

    In all modes the output is capped to ``_MAX_SANITIZED_LEN`` characters.
    In production (debug=False) only the exception type name is returned.
    In debug mode the first line of the message is appended (still capped)
    with secrets AND IMAP payload content stripped.

    IMAP exceptions can embed raw server responses containing full email
    headers, body text, BODYSTRUCTURE, and MIME content — all printable
    ASCII.  The hard length cap combined with payload stripping ensures
    that even in debug mode no email content leaks into logs.
    """
    error_type = type(e).__name__ or "UnknownError"

    if not debug:
        # In production, return only the exception type, no details
        return error_type

    # In debug mode, include the first line of the message (stripped of secrets
    # and IMAP payload fragments)
    first_line = str(e).split("\n")[0].split("\r")[0]
    first_line = _redact_secrets(first_line)
    first_line = _strip_imap_payload(first_line)
    summary = f"{error_type}: {first_line}"
    if len(summary) > _MAX_SANITIZED_LEN:
        summary = summary[:_MAX_SANITIZED_LEN] + " [truncated]"
    return summary


def _strip_imap_payload(text: str) -> str:
    """Remove IMAP/email payload fragments from error text.

    IMAP exceptions frequently embed raw server responses that contain
    email headers (From:, To:, Subject:, etc.), body content, BODYSTRUCTURE
    dumps, and MIME boundary fragments.  All of this is printable ASCII and
    would pass through a naive length cap.  This function aggressively
    strips any content that follows known IMAP payload markers.
    """
    if not text:
        return text
    for marker in (
        "BODY[", "BODY.PEEK[", "BODYSTRUCTURE", "FLAGS (", "ENVELOPE",
        "INTERNALDATE", "RFC822", "\\Seen", "\\Recent", "\\Flagged",
        "Content-Type:", "From:", "To:", "Subject:", "Date:", "MIME-",
        "Message-ID:", "Received:", "Return-Path:", "boundary=",
        "Content-Transfer-Encoding:", "Content-Disposition:", "X-Mailer:",
    ):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    # Remove byte-literal fragments like b'...' or b"..."
    text = re.sub(r"b['\"].*?['\"]", "[data]", text)
    # Remove long mixed-case hex/base64 sequences (must contain both upper+lower or digits)
    text = re.sub(r"(?=[A-Za-z0-9+/=]{40,})(?=.*[A-Z])(?=.*[a-z0-9+/=])[A-Za-z0-9+/=]{40,}", "[data]", text)
    return text.strip()


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
        r"(password|passwd)\s*[=:]\s*[^\s,;)}\]]+",
        r"\1=[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )

    # Pattern: username=... or user=...
    redacted = re.sub(
        r"(username|user)\s*[=:]\s*[^\s,;)}\]]+",
        r"\1=[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )

    # Pattern: Bearer <token> (with or without Authorization:)
    redacted = re.sub(
        r"Bearer\s+[^\s,;)}\]]+", "Bearer [REDACTED]", redacted, flags=re.IGNORECASE
    )

    # Pattern: api_key=... or apikey=...
    redacted = re.sub(
        r"(api[_-]?key)\s*[=:]\s*[^\s,;)}\]]+",
        r"\1=[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )

    # Pattern: token=...
    redacted = re.sub(
        r"(token)\s*[=:]\s*[^\s,;)}\]]+",
        r"\1=[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )

    return redacted
