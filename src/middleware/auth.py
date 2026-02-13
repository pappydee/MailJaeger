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
    def __init__(self, detail: str = "Authentication required"):
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
    
    # If no API key is configured, allow access (with warning logged at startup)
    if not settings.api_key:
        return True
    
    # Require credentials if API key is configured
    if not credentials:
        return False
    
    # Verify token matches configured API key using constant-time comparison
    # to prevent timing attacks
    return secrets.compare_digest(credentials.credentials, settings.api_key)


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
    
    # Skip auth check if no API key configured (already warned at startup)
    if not settings.api_key:
        return
    
    # Get credentials from header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning(f"Unauthenticated request to {request.url.path}")
        raise AuthenticationError("Missing or invalid authentication token")
    
    # Extract token
    try:
        token = auth_header.split(" ", 1)[1]
    except IndexError:
        raise AuthenticationError("Malformed authentication token")
    
    # Verify token
    if not secrets.compare_digest(token, settings.api_key):
        logger.warning(f"Failed authentication attempt for {request.url.path}")
        raise AuthenticationError("Invalid authentication token")
    
    logger.debug(f"Authenticated request to {request.url.path}")
