"""
Tests for mailbox safety hardening features
"""
import pytest
from unittest.mock import patch, MagicMock
import os
from datetime import datetime, timedelta
from fastapi.testclient import TestClient


class TestFailClosedStartup:
    """Test fail-closed startup mode matrix"""
    
    def test_startup_fails_web_exposed_without_safety_controls(self):
        """Test that startup fails when web-exposed with both SAFE_MODE=false and REQUIRE_APPROVAL=false"""
        # Test with SERVER_HOST=0.0.0.0
        with patch.dict(os.environ, {
            "SERVER_HOST": "0.0.0.0",
            "SAFE_MODE": "false",
            "REQUIRE_APPROVAL": "false",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "Fail-closed safety requirement" in error_msg
            assert "SAFE_MODE=true OR REQUIRE_APPROVAL=true" in error_msg
    
    def test_startup_fails_trust_proxy_without_safety_controls(self):
        """Test that startup fails when TRUST_PROXY=true without safety controls"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "true",
            "SAFE_MODE": "false",
            "REQUIRE_APPROVAL": "false",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "Fail-closed safety requirement" in error_msg
    
    def test_startup_fails_allowed_hosts_without_safety_controls(self):
        """Test that startup fails when ALLOWED_HOSTS set without safety controls"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "ALLOWED_HOSTS": "example.com",
            "SAFE_MODE": "false",
            "REQUIRE_APPROVAL": "false",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            error_msg = str(exc_info.value)
            assert "Fail-closed safety requirement" in error_msg
    
    def test_startup_succeeds_web_exposed_with_safe_mode(self):
        """Test that startup succeeds when web-exposed with SAFE_MODE=true"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "0.0.0.0",
            "SAFE_MODE": "true",
            "REQUIRE_APPROVAL": "false",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            settings = reload_settings()
            settings.validate_required_settings()  # Should not raise
            assert settings.is_web_exposed()
            assert settings.safe_mode is True
    
    def test_startup_succeeds_web_exposed_with_require_approval(self):
        """Test that startup succeeds when web-exposed with REQUIRE_APPROVAL=true"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "0.0.0.0",
            "SAFE_MODE": "false",
            "REQUIRE_APPROVAL": "true",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            settings = reload_settings()
            settings.validate_required_settings()  # Should not raise
            assert settings.is_web_exposed()
            assert settings.require_approval is True
    
    def test_startup_succeeds_localhost_without_safety_controls(self):
        """Test that startup succeeds for localhost without safety controls"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "SAFE_MODE": "false",
            "REQUIRE_APPROVAL": "false",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            
            settings = reload_settings()
            settings.validate_required_settings()  # Should not raise
            assert not settings.is_web_exposed()


class TestApplySafetyLimits:
    """Test apply safety limits"""
    
    def test_apply_requires_token(self):
        """Test that apply endpoint requires apply_token"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "SAFE_MODE": "false",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            reload_settings()
            
            from src.main import app
            client = TestClient(app)
            
            # Attempt apply without token
            response = client.post(
                "/api/pending-actions/apply",
                headers={"Authorization": "Bearer testkey"},
                json={}
            )
            
            assert response.status_code == 409
            data = response.json()
            assert "Apply token required" in data["message"]
            assert data["applied"] == 0
    
    def test_apply_with_invalid_token_returns_409(self):
        """Test that apply with invalid token returns 409"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "SAFE_MODE": "false",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            reload_settings()
            
            from src.main import app
            client = TestClient(app)
            
            # Attempt apply with invalid token
            response = client.post(
                "/api/pending-actions/apply",
                headers={"Authorization": "Bearer testkey"},
                json={"apply_token": "invalid_token_xyz"}
            )
            
            assert response.status_code == 409
            data = response.json()
            assert "Invalid or already used apply token" in data["message"]
            assert data["applied"] == 0


class TestDestructiveOperationsBlocked:
    """Test that destructive operations are blocked by default"""
    
    def test_delete_blocked_when_allow_destructive_false(self):
        """Test that DELETE action is blocked when ALLOW_DESTRUCTIVE_IMAP=false"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "SAFE_MODE": "false",
            "ALLOW_DESTRUCTIVE_IMAP": "false",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.allow_destructive_imap is False
    
    def test_delete_allowed_when_allow_destructive_true(self):
        """Test that DELETE action is allowed when ALLOW_DESTRUCTIVE_IMAP=true"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "SAFE_MODE": "false",
            "ALLOW_DESTRUCTIVE_IMAP": "true",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.allow_destructive_imap is True


class TestSafetyFolderAllowlist:
    """Test safety folder allowlist enforcement"""
    
    def test_get_safe_folders_returns_configured_folders(self):
        """Test that get_safe_folders returns all configured safe folders"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
            "SPAM_FOLDER": "Spam",
            "QUARANTINE_FOLDER": "Quarantine",
            "ARCHIVE_FOLDER": "Archive",
            "SAFETY_REVIEW_FOLDER": "MailJaeger/Review",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            safe_folders = settings.get_safe_folders()
            
            assert "Spam" in safe_folders
            assert "Quarantine" in safe_folders
            assert "Archive" in safe_folders
            assert "MailJaeger/Review" in safe_folders
    
    def test_safe_folders_removes_duplicates(self):
        """Test that get_safe_folders removes duplicates"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
            "SPAM_FOLDER": "Spam",
            "QUARANTINE_FOLDER": "Spam",  # Duplicate
            "ARCHIVE_FOLDER": "Archive",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            safe_folders = settings.get_safe_folders()
            
            # Should have unique folders only
            assert safe_folders.count("Spam") == 1


class TestWebExposedDetection:
    """Test web-exposed detection logic"""
    
    def test_is_web_exposed_with_0_0_0_0(self):
        """Test that SERVER_HOST=0.0.0.0 is detected as web-exposed"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "0.0.0.0",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.is_web_exposed() is True
    
    def test_is_web_exposed_with_trust_proxy(self):
        """Test that TRUST_PROXY=true is detected as web-exposed"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "true",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.is_web_exposed() is True
    
    def test_is_web_exposed_with_allowed_hosts(self):
        """Test that ALLOWED_HOSTS set is detected as web-exposed"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "example.com",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.is_web_exposed() is True
    
    def test_is_not_web_exposed_localhost(self):
        """Test that localhost-only configuration is not web-exposed"""
        with patch.dict(os.environ, {
            "SERVER_HOST": "127.0.0.1",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.is_web_exposed() is False


class TestMaxApplyPerRequest:
    """Test MAX_APPLY_PER_REQUEST configuration"""
    
    def test_max_apply_per_request_default(self):
        """Test that MAX_APPLY_PER_REQUEST defaults to 20"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.max_apply_per_request == 20
    
    def test_max_apply_per_request_custom(self):
        """Test that MAX_APPLY_PER_REQUEST can be customized"""
        with patch.dict(os.environ, {
            "API_KEY": "testkey",
            "MAX_APPLY_PER_REQUEST": "50",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "testpass",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            from src.config import reload_settings
            settings = reload_settings()
            
            assert settings.max_apply_per_request == 50
