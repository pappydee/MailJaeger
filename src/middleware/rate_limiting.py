"""
Rate limiting for MailJaeger API
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from starlette.responses import JSONResponse

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


def get_client_identifier(request: Request) -> str:
    """
    Get client identifier for rate limiting
    
    Uses X-Forwarded-For if TRUST_PROXY is enabled, otherwise uses direct IP
    """
    settings = get_settings()
    
    if settings.trust_proxy:
        # Check X-Forwarded-For header
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Use first IP in the chain (original client)
            return forwarded_for.split(",")[0].strip()
    
    # Fall back to direct connection IP
    return get_remote_address(request)


# Create limiter instance
limiter = Limiter(
    key_func=get_client_identifier,
    default_limits=["200/minute"],  # Default global limit
    storage_uri="memory://"  # In-memory storage (consider Redis for multi-instance deployments)
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Custom handler for rate limit exceeded"""
    logger.warning(f"Rate limit exceeded for {get_client_identifier(request)} on {request.url.path}")
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please try again later.",
            "retry_after": exc.detail.split("Retry in ")[1] if "Retry in" in exc.detail else "60 seconds"
        },
        headers={"Retry-After": "60"}
    )
