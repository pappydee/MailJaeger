"""
Authentication middleware for MailJaeger

Single source of truth for request authentication.  Both the ASGI middleware in
src/main.py and the FastAPI dependency (require_authentication) call the shared
helper validate_request_auth() so that Bearer-token auth and session-cookie auth
are evaluated identically in both layers.

Session store
-------------
The in-memory session store (_sessions) is intentionally kept here so that
validate_request_auth() can inspect it without importing from src.main (which
would create a circular dependency).  src.main imports SESSION_COOKIE,
SESSION_EXPIRY_HOURS, and _sessions from this module to create/delete sessions.
"""

import secrets
from datetime import datetime, timedelta
from typing import Dict

from fastapi import Request, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Session store – owned here so validate_request_auth() can read it without
# circular imports.  src.main uses these names directly via import.
# ---------------------------------------------------------------------------
SESSION_COOKIE: str = "mailjaeger_session"
SESSION_EXPIRY_HOURS: int = 24

# token -> expiry datetime  (lost on restart; acceptable for a local tool)
_sessions: Dict[str, datetime] = {}


class AuthenticationError(HTTPException):
    """Authentication error exception"""

    def __init__(self, detail: str = "Unauthorized"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_request_auth(request: Request) -> bool:
    """
    Shared authentication validator used by both the global ASGI middleware and
    the FastAPI Depends() dependency.

    Accepts either:
    - ``Authorization: Bearer <API_KEY>`` header  (CLI / curl)
    - A valid ``mailjaeger_session`` cookie created by POST /api/auth/login

    Returns True if the request is authenticated, False otherwise.
    Never raises; callers decide what to do with a False result.
    """
    settings = get_settings()
    api_keys = settings.get_api_keys()

    # Fail-closed: no API keys configured → deny everything
    if not api_keys:
        return False

    # --- Option 1: Bearer token (CLI / curl) ---
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ", 1)[1]
            if any(secrets.compare_digest(token, key) for key in api_keys):
                return True
        except IndexError:
            pass
        # A Bearer header was present but the token is invalid.  Reject
        # immediately rather than falling through to the cookie check so that
        # a forged/expired Bearer header cannot be bypassed by also sending a
        # valid cookie.
        return False

    # --- Option 2: Session cookie (browser) ---
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        expiry = _sessions.get(session_token)
        if expiry and expiry > datetime.utcnow():
            return True
        # Expired or unknown token – clean up lazily
        if session_token in _sessions:
            del _sessions[session_token]

    return False


def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials]) -> bool:
    """
    Verify API key from Bearer token credentials object.

    Kept for backwards-compatibility with any callers that use
    HTTPBearer directly.
    """
    settings = get_settings()
    api_keys = settings.get_api_keys()

    if not api_keys:
        return False
    if not credentials:
        return False

    token = credentials.credentials
    return any(secrets.compare_digest(token, key) for key in api_keys)


async def require_authentication(request: Request) -> None:
    """
    FastAPI dependency that enforces authentication for protected routes.

    Usage::

        @app.get("/protected", dependencies=[Depends(require_authentication)])
        async def protected_route(): ...

    Calls validate_request_auth() directly so that both Bearer-token requests
    (CLI/curl) and session-cookie requests (browser GUI) are accepted.  The
    same function is used by the global ASGI middleware in src/main.py,
    ensuring there is only one authentication code-path.
    """
    # Allow the health-check route through without credentials
    if request.url.path == "/api/health":
        return

    if not validate_request_auth(request):
        host = request.client.host if request.client else "unknown"
        logger.warning(
            "Unauthenticated request to %s from %s",
            request.url.path,
            host,
        )
        raise AuthenticationError("Unauthorized")

    logger.debug("Authenticated request to %s", request.url.path)
