"""
Tests for allowed_hosts middleware enforcement

These tests verify that the allowed_hosts middleware correctly enforces
host restrictions at runtime with proper support for proxies.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import os
import sys


def get_fresh_app(env_vars):
    """Get a fresh app instance with given environment variables"""
    # Reload modules to ensure clean state
    for module in list(sys.modules.keys()):
        if module.startswith('src.'):
            del sys.modules[module]
    
    with patch.dict(os.environ, env_vars, clear=True):
        from src.config import reload_settings
        reload_settings()
        from src.main import app
        return app


class TestAllowedHostsMiddleware:
    """Test allowed_hosts middleware with global auth"""

    def test_empty_allowed_hosts_allows_all(self):
        """When allowed_hosts is empty, all hosts should be allowed"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "",  # Empty = no restriction
        })
        
        client = TestClient(app)
        
        # Should succeed with any host
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "arbitrary.example.com",
            },
        )
        assert response.status_code == 200

    def test_allowed_host_succeeds(self):
        """When host is in allowed_hosts, request should succeed"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "example.com,api.example.com",
        })
        
        client = TestClient(app)
        
        # Should succeed with allowed host
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "example.com",
            },
        )
        assert response.status_code == 200

    def test_disallowed_host_returns_400(self):
        """When host is NOT in allowed_hosts, should return 400"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "example.com,api.example.com",
        })
        
        client = TestClient(app)
        
        # Should fail with disallowed host
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "evil.example.com",
            },
        )
        assert response.status_code == 400
        assert "Invalid host header" in response.json()["detail"]

    def test_allowed_host_with_port_succeeds(self):
        """Host with port should be stripped and matched"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "example.com",
        })
        
        client = TestClient(app)
        
        # Should succeed even with port in Host header
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "example.com:443",
            },
        )
        assert response.status_code == 200

    def test_trust_proxy_with_x_forwarded_host_allowed(self):
        """When trust_proxy=true and X-Forwarded-Host is allowed, should succeed"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "public.example.com",
            "TRUST_PROXY": "true",
        })
        
        client = TestClient(app)
        
        # Should use X-Forwarded-Host when trust_proxy is true
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "internal.local",  # Internal host (not allowed)
                "X-Forwarded-Host": "public.example.com",  # Public host (allowed)
            },
        )
        assert response.status_code == 200

    def test_trust_proxy_with_x_forwarded_host_disallowed(self):
        """When trust_proxy=true and X-Forwarded-Host is NOT allowed, should return 400"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "example.com",
            "TRUST_PROXY": "true",
        })
        
        client = TestClient(app)
        
        # Should reject when X-Forwarded-Host is not in allowed list
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "internal.local",
                "X-Forwarded-Host": "evil.example.com",  # Not allowed
            },
        )
        assert response.status_code == 400
        assert "Invalid host header" in response.json()["detail"]

    def test_trust_proxy_false_ignores_x_forwarded_host(self):
        """When trust_proxy=false, X-Forwarded-Host should be ignored"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "example.com",
            "TRUST_PROXY": "false",
        })
        
        client = TestClient(app)
        
        # Should use Host header, not X-Forwarded-Host
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "example.com",  # Allowed
                "X-Forwarded-Host": "evil.example.com",  # Should be ignored
            },
        )
        assert response.status_code == 200

    def test_case_insensitive_matching(self):
        """Host matching should be case-insensitive"""
        app = get_fresh_app({
            "API_KEY": "test_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
            "ALLOWED_HOSTS": "Example.COM",  # Mixed case in config
        })
        
        client = TestClient(app)
        
        # Should match case-insensitively
        response = client.get(
            "/",
            headers={
                "Authorization": "Bearer test_api_key_12345",
                "Host": "example.com",  # Different case
            },
        )
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
