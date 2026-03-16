"""
Shared in-memory session store for MailJaeger.

This module is imported by both src/main.py (which creates/destroys sessions)
and src/middleware/auth.py (which validates sessions on protected routes).
Keeping it here avoids a circular import between main and auth.

Sessions are intentionally in-memory: they are lost on restart, which is
acceptable for a local single-user tool.
"""

from datetime import datetime
from typing import Dict

# Cookie name sent to the browser after a successful POST /api/auth/login
SESSION_COOKIE = "mailjaeger_session"

# How long a session stays valid (hours)
SESSION_EXPIRY_HOURS: int = 24

# token -> expiry datetime
# Both main.py and auth.py reference *this same dict*.
_sessions: Dict[str, datetime] = {}
