"""
Allowed hosts middleware for MailJaeger

Enforces allowed_hosts restriction at runtime to prevent host header attacks.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
import logging

logger = logging.getLogger(__name__)


class AllowedHostsMiddleware:
    """
    Middleware to enforce allowed hosts restriction.
    
    When settings.allowed_hosts is configured, this middleware validates
    that incoming requests have an allowed Host header (or X-Forwarded-Host
    if trust_proxy is enabled).
    """

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.allowed_hosts = self._parse_allowed_hosts()

    def _parse_allowed_hosts(self):
        """
        Parse allowed_hosts from settings.
        
        Returns:
            set: Set of allowed hostnames, or None if no restriction
        """
        if not self.settings.allowed_hosts:
            return None

        hosts = [
            h.strip().lower()
            for h in self.settings.allowed_hosts.split(",")
            if h.strip()
        ]
        return set(hosts) if hosts else None

    def _get_effective_host(self, request: Request) -> str:
        """
        Get the effective host from the request.
        
        If trust_proxy is enabled, checks X-Forwarded-Host first.
        Otherwise uses the Host header.
        
        Args:
            request: The incoming request
            
        Returns:
            str: The effective hostname (without port)
        """
        if self.settings.trust_proxy:
            # When behind a trusted proxy, prefer X-Forwarded-Host
            forwarded_host = request.headers.get("X-Forwarded-Host")
            if forwarded_host:
                # X-Forwarded-Host can contain multiple values; take the first
                host = forwarded_host.split(",")[0].strip()
            else:
                host = request.headers.get("Host", "")
        else:
            host = request.headers.get("Host", "")

        # Strip port if present (e.g., "example.com:443" -> "example.com")
        if ":" in host:
            host = host.split(":")[0]

        return host.lower()

    async def __call__(self, scope, receive, send):
        """
        ASGI middleware implementation.
        
        Validates the host header against allowed_hosts if configured.
        Returns 400 if host is not allowed.
        """
        # Only process HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # If no allowed_hosts configured, allow all
        if self.allowed_hosts is None:
            await self.app(scope, receive, send)
            return

        # Get the request to access headers
        request = Request(scope, receive)
        effective_host = self._get_effective_host(request)

        # Check if host is allowed
        if effective_host not in self.allowed_hosts:
            logger.warning(
                f"Request rejected: host '{effective_host}' not in allowed_hosts. "
                f"Path: {request.url.path}"
            )

            # Return 400 Bad Request with minimal error
            response = JSONResponse(
                status_code=400,
                content={"detail": "Invalid host header"},
            )
            await response(scope, receive, send)
            return

        # Host is allowed, proceed with request
        await self.app(scope, receive, send)
