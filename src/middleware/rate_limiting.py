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
    Get client identifier for rate limiting with strict proxy validation
    
    Uses X-Forwarded-For / X-Real-IP headers ONLY when:
    1. TRUST_PROXY is enabled AND
    2. Either:
       a. Direct client IP is in TRUSTED_PROXY_IPS, OR
       b. TRUSTED_PROXY_IPS is empty (trust all proxies - use with caution)
    
    Otherwise falls back to direct connection IP for security.
    """
    settings = get_settings()
    
    # Get direct client IP
    direct_ip = get_remote_address(request)
    
    if settings.trust_proxy:
        # Get list of trusted proxy IPs
        trusted_ips = settings.get_trusted_proxy_ips()
        
        # If trusted IPs list is empty, trust all proxies (as per documentation requirement)
        # If list exists, only trust if direct IP is in the list
        if not trusted_ips or direct_ip in trusted_ips:
            # Try X-Real-IP first (single IP, simpler)
            real_ip = request.headers.get("X-Real-IP")
            if real_ip:
                return real_ip.strip()
            
            # Fall back to X-Forwarded-For (can be a chain)
            forwarded_for = request.headers.get("X-Forwarded-For")
            if forwarded_for:
                # Use first IP in the chain (original client)
                return forwarded_for.split(",")[0].strip()
    
    # Fall back to direct connection IP
    return direct_ip


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
