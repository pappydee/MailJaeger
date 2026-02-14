"""
Test that sensitive credentials are never returned via API
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import os


class TestCredentialLeakage:
    """Test that credentials and sensitive info are never exposed"""
    
    def test_settings_endpoint_does_not_leak_credentials(self):
        """Test that /api/settings does not return credentials"""
        with patch.dict(os.environ, {
            "API_KEY": "super_secret_api_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "user@test.com",
            "IMAP_PASSWORD": "super_secret_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app)
            
            response = client.get(
                "/api/settings",
                headers={"Authorization": "Bearer super_secret_api_key_12345"}
            )
            
            assert response.status_code == 200
            data = response.json()
            
            # Check that sensitive fields are NOT in response
            sensitive_fields = [
                "api_key",
                "imap_username",
                "imap_password",
                "imap_password_file",
                "api_key_file"
            ]
            
            response_str = str(data).lower()
            
            # Check that field names don't appear
            for field in sensitive_fields:
                assert field not in data, f"Sensitive field '{field}' should not be in settings response"
            
            # Check that actual values don't appear
            assert "super_secret_api_key_12345" not in response_str
            assert "super_secret_password" not in response_str
            assert "user@test.com" not in response_str
            
            # Check that safe fields ARE present
            assert "imap_host" in data
            assert "imap_port" in data
            assert "safe_mode" in data
    
    def test_settings_endpoint_does_not_leak_authorization_header(self):
        """Test that Authorization header is never logged or returned"""
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app)
            
            response = client.get(
                "/api/settings",
                headers={"Authorization": "Bearer test_key_12345"}
            )
            
            # Check response doesn't contain authorization-related content
            response_json = response.json() if response.status_code == 200 else {}
            response_text = response.text.lower()
            
            # Verify the actual token value doesn't appear
            assert "test_key_12345" not in response_text
            
            # Verify sensitive headers aren't in response
            assert "authorization" not in response_json
    
    def test_error_responses_do_not_leak_credentials(self):
        """Test that error responses don't leak credentials"""
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app)
            
            # Try to access a non-existent endpoint
            response = client.get(
                "/api/nonexistent",
                headers={"Authorization": "Bearer test_key_12345"}
            )
            
            response_text = response.text.lower()
            
            # Check that credentials don't appear in error
            assert "test_password" not in response_text
            assert "test_key_12345" not in response_text
            assert "test@test.com" not in response_text
    
    def test_api_key_not_in_health_endpoint(self):
        """Test that health endpoint doesn't leak any credentials"""
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app)
            
            with patch('src.main.IMAPService') as mock_imap, \
                 patch('src.main.AIService') as mock_ai, \
                 patch('src.main.get_scheduler') as mock_scheduler:
                mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                mock_scheduler.return_value.get_status.return_value = {"status": "running"}
                
                response = client.get("/api/health")
                
                assert response.status_code == 200
                response_text = response.text.lower()
                
                # Check no credentials
                assert "test_password" not in response_text
                assert "test_key_12345" not in response_text
                assert "test@test.com" not in response_text
    
    def test_pending_actions_do_not_leak_credentials(self):
        """Test that pending actions endpoints don't leak credentials"""
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app)
            
            # Mock database
            with patch('src.api.pending_actions.get_db'):
                response = client.get(
                    "/api/pending-actions/summary",
                    headers={"Authorization": "Bearer test_key_12345"}
                )
                
                if response.status_code == 200:
                    response_text = response.text.lower()
                    
                    # Check no credentials
                    assert "test_password" not in response_text
                    assert "test@test.com" not in response_text
