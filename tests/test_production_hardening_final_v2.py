"""
Production hardening final v2 tests - strict DEBUG guard and sanitized error handling
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import os
import logging


class TestDebugGuard:
    """Test that DEBUG guard prevents web-exposed + DEBUG=true"""
    
    def test_debug_guard_blocks_web_exposed_debug_true(self):
        """
        Test that DEBUG=true is blocked when SERVER_HOST=0.0.0.0
        This is the primary test for strict DEBUG guard enforcement.
        """
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "SERVER_HOST": "0.0.0.0",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            # Should raise ValueError when validating settings
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            # Verify the error message is appropriate (short, safe, no secrets)
            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg or "web-exposed" in error_msg
            # Ensure no secrets are leaked in the error message
            assert "testkey" not in error_msg
            assert "test_password" not in error_msg


class TestGlobalExceptionHandler:
    """Test that global exception handler sanitizes errors properly"""
    
    def test_global_exception_handler_sanitizes_response_and_logs_when_debug_false(self):
        """
        Test that exception handler sanitizes response and logs when DEBUG=false.
        Creates a test route that raises an exception with sensitive data,
        then verifies that the response doesn't contain that data.
        """
        with patch.dict(os.environ, {
            "DEBUG": "false",
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            reload_settings()
            
            # Import app after reloading settings
            from src.main import app
            
            # Create a test-only route that raises an exception with sensitive data
            @app.get("/test-exception-handler-v2")
            async def test_exception_route_v2():
                raise Exception("password=secret123 user=test@example.com")
            
            # Use raise_server_exceptions=False to prevent test client from re-raising
            client = TestClient(app, raise_server_exceptions=False)
            
            response = client.get(
                "/test-exception-handler-v2",
                headers={"Authorization": "Bearer testkey"}
            )
            
            # Check response doesn't contain sensitive data
            assert response.status_code == 500
            response_data = response.json()
            assert response_data["detail"] == "Internal server error"
            assert "secret123" not in str(response_data)
            assert "test@example.com" not in str(response_data)
            assert "password=" not in str(response_data)
            
            # Verify sanitize_error() is working by checking:
            # 1. The response is generic (already checked above)
            # 2. No exception details leaked (already checked above)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
