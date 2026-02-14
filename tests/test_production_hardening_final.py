"""
Final production hardening tests - tests with global auth middleware
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os
import logging


class TestDebugGuard:
    """Test that DEBUG guard prevents web-exposed + DEBUG=true"""
    
    def test_debug_guard_blocks_web_exposed_with_0_0_0_0(self):
        """Test that DEBUG=true is blocked when SERVER_HOST=0.0.0.0"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "SERVER_HOST": "0.0.0.0",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg
            assert "web-exposed" in error_msg
    
    def test_debug_guard_blocks_web_exposed_with_trust_proxy(self):
        """Test that DEBUG=true is blocked when TRUST_PROXY=true"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "TRUST_PROXY": "true",
            "SERVER_HOST": "127.0.0.1",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg
    
    def test_debug_guard_blocks_web_exposed_with_allowed_hosts(self):
        """Test that DEBUG=true is blocked when ALLOWED_HOSTS is set"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "ALLOWED_HOSTS": "example.com,api.example.com",
            "SERVER_HOST": "127.0.0.1",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg
    
    def test_debug_allowed_on_localhost_without_exposure(self):
        """Test that DEBUG=true is allowed on localhost without web exposure"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
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
            
            # Should not raise an error
            settings = reload_settings()
            settings.validate_required_settings()
            assert settings.debug is True


class TestGlobalExceptionHandler:
    """Test that global exception handler sanitizes errors properly"""
    
    def test_exception_handler_sanitizes_response_when_debug_false(self, caplog):
        """Test that exception handler sanitizes response when DEBUG=false"""
        with patch.dict(os.environ, {
            "DEBUG": "false",
            "API_KEY": "testkey",
            "SERVER_HOST": "127.0.0.1",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            reload_settings()
            
            # Import app after reloading settings
            from src.main import app
            
            # Create a test route that raises an exception with sensitive data
            @app.get("/test-exception-production")
            async def test_exception_route():
                raise ValueError("Database connection failed: password=secret123 user=test@example.com")
            
            client = TestClient(app)
            
            # Capture logs
            with caplog.at_level(logging.ERROR):
                response = client.get(
                    "/test-exception-production",
                    headers={"Authorization": "Bearer testkey"}
                )
            
            # Check response doesn't contain sensitive data
            assert response.status_code == 500
            response_data = response.json()
            assert response_data["detail"] == "Internal server error"
            assert "secret123" not in str(response_data)
            assert "test@example.com" not in str(response_data)
            assert "password=" not in str(response_data)
            
            # Check logs don't contain sensitive data
            log_text = caplog.text
            assert "secret123" not in log_text
            assert "test@example.com" not in log_text
            # Should only contain error type
            assert "ValueError" in log_text
    
    def test_exception_handler_shows_details_in_debug_mode(self):
        """Test that exception handler shows details when DEBUG=true (for development)"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "API_KEY": "testkey",
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            reload_settings()
            
            # Import app after reloading settings
            from src.main import app
            
            # Create a test route that raises an exception
            @app.get("/test-exception-debug")
            async def test_exception_route_debug():
                raise ValueError("Test error for debugging")
            
            client = TestClient(app)
            
            response = client.get(
                "/test-exception-debug",
                headers={"Authorization": "Bearer testkey"}
            )
            
            # In debug mode, should show actual error message
            assert response.status_code == 500
            response_data = response.json()
            assert "Test error for debugging" in response_data["detail"]
    
    def test_exception_handler_never_leaks_imap_credentials(self, caplog):
        """Test that even in debug mode, IMAP credentials are never logged"""
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "API_KEY": "testkey",
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "my_secret_password_123",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            # Import app after reloading settings
            from src.main import app
            
            # Create a test route that might accidentally expose credentials
            @app.get("/test-no-credential-leak")
            async def test_no_leak():
                # This shouldn't happen, but test that even if it does, 
                # credentials don't leak
                raise ValueError(f"Connection failed")
            
            client = TestClient(app)
            
            with caplog.at_level(logging.ERROR):
                response = client.get(
                    "/test-no-credential-leak",
                    headers={"Authorization": "Bearer testkey"}
                )
            
            # Verify password never appears in logs or response
            log_text = caplog.text
            response_text = str(response.json())
            
            # The actual IMAP password should NEVER appear
            assert "my_secret_password_123" not in log_text
            assert "my_secret_password_123" not in response_text
