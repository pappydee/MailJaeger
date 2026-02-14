"""
Authentication middleware for MailJaeger
"""
import secrets
from fastapi import Request, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

security = HTTPBearer(auto_error=False)


class AuthenticationError(HTTPException):
    """Authentication error exception"""
    def __init__(self, detail: str = "Unauthorized"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials]) -> bool:
    """
    Verify API key from Bearer token
    
    Args:
        credentials: HTTP Authorization credentials
        
    Returns:
        True if authenticated, False otherwise
    """
    settings = get_settings()
    api_keys = settings.get_api_keys()
    
    # Fail-closed: If no API keys configured, deny access
    if not api_keys:
        return False
    
    # Require credentials if API keys are configured
    if not credentials:
        return False
    
    # Verify token matches any configured API key using constant-time comparison
    # to prevent timing attacks
    token = credentials.credentials
    return any(secrets.compare_digest(token, key) for key in api_keys)


async def require_authentication(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None
) -> None:
    """
    Dependency that requires authentication
    
    Usage:
        @app.get("/protected", dependencies=[Depends(require_authentication)])
        async def protected_route():
            ...
    """
    settings = get_settings()
    api_keys = settings.get_api_keys()
    
    # Define explicit allowlist of unauthenticated routes
    UNAUTHENTICATED_ROUTES = {
        "/api/health",
    }
    
    # Allow unauthenticated access only to explicitly allowed routes
    if request.url.path in UNAUTHENTICATED_ROUTES:
        return
    
    # Fail-closed: If no API keys configured, deny all access except allowlist
    if not api_keys:
        logger.error(f"No API keys configured - denying access to {request.url.path}")
        raise AuthenticationError("Unauthorized")
    
    # Get credentials from header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning(f"Unauthenticated request to {request.url.path} from {request.client.host if request.client else 'unknown'}")
        raise AuthenticationError("Unauthorized")
    
    # Extract token
    try:
        token = auth_header.split(" ", 1)[1]
    except IndexError:
        raise AuthenticationError("Unauthorized")
    
    # Verify token against all valid API keys using constant-time comparison
    if not any(secrets.compare_digest(token, key) for key in api_keys):
        logger.warning(f"Failed authentication attempt for {request.url.path} from {request.client.host if request.client else 'unknown'}")
        raise AuthenticationError("Unauthorized")
    
    logger.debug(f"Authenticated request to {request.url.path}")
