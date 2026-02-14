"""
Security headers middleware for MailJaeger
"""
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers to all responses"""
    
    async def dispatch(self, request: Request, call_next):
        """Add security headers to response"""
        response = await call_next(request)
        
        settings = get_settings()
        
        # X-Content-Type-Options: Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        
        # X-Frame-Options: Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        
        # Referrer-Policy: Control referrer information (more restrictive)
        response.headers["Referrer-Policy"] = "no-referrer"
        
        # Permissions-Policy: Restrict browser features (more restrictive)
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), "
            "payment=(), usb=(), magnetometer=(), gyroscope=(), "
            "accelerometer=(), midi=(), sync-xhr=(), "
            "fullscreen=(), display-capture=()"
        )
        
        # Content-Security-Policy: Prevent XSS and data injection
        # Relaxed for self-hosted app with inline styles/scripts
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",  # Allow inline scripts for dashboard
            "style-src 'self' 'unsafe-inline'",   # Allow inline styles for dashboard
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
            "upgrade-insecure-requests"
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_directives)
        
        # HSTS: Force HTTPS (only if behind HTTPS proxy or direct HTTPS)
        # Check if request came through HTTPS proxy or is direct HTTPS
        is_https = False
        
        # Check direct HTTPS
        if request.url.scheme == "https":
            is_https = True
        
        # Check trusted proxy forwarded headers
        if settings.trust_proxy:
            forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
            if forwarded_proto.lower() == "https":
                # Optionally validate proxy IP
                trusted_ips = settings.get_trusted_proxy_ips()
                if not trusted_ips:
                    # Trust all proxies when no specific IPs configured
                    is_https = True
                else:
                    # Validate proxy IP
                    client_host = request.client.host if request.client else None
                    if client_host in trusted_ips:
                        is_https = True
        
        if is_https:
            # 1 year max-age, include subdomains, preload
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        
        return response
