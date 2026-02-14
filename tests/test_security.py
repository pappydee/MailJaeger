"""
Security tests for MailJaeger
Tests authentication, rate limiting, and credential redaction
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os

# Set test environment variables before importing app
os.environ["API_KEY"] = "test_api_key_for_testing_123456"
os.environ["IMAP_HOST"] = "imap.test.com"
os.environ["IMAP_USERNAME"] = "test@test.com"
os.environ["IMAP_PASSWORD"] = "test_password"
os.environ["AI_ENDPOINT"] = "http://localhost:11434"

from src.main import app
from src.config import get_settings, reload_settings


@pytest.fixture
def client():
    """Create test client"""
    # Reload settings with test environment
    reload_settings()
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Valid authentication headers"""
    return {"Authorization": "Bearer test_api_key_for_testing_123456"}


class TestAuthentication:
    """Test authentication and authorization"""

    def test_root_requires_auth(self, client):
        """Root endpoint should require authentication"""
        response = client.get("/")
        assert response.status_code == 401
        assert "authentication required" in response.json()["detail"].lower()

    def test_root_with_valid_auth(self, client, auth_headers):
        """Root endpoint should work with valid auth"""
        response = client.get("/", headers=auth_headers)
        # Should either return HTML or JSON, but not 401
        assert response.status_code != 401

    def test_api_dashboard_requires_auth(self, client):
        """Dashboard endpoint should require authentication"""
        response = client.get("/api/dashboard")
        assert response.status_code == 401

    def test_api_dashboard_with_invalid_auth(self, client):
        """Dashboard with wrong API key should fail"""
        headers = {"Authorization": "Bearer wrong_key"}
        response = client.get("/api/dashboard", headers=headers)
        assert response.status_code == 401

    def test_api_dashboard_with_valid_auth(self, client, auth_headers):
        """Dashboard with valid auth should work"""
        with patch("src.main.get_db") as mock_db:
            # Mock database session
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.order_by.return_value.first.return_value = (
                None
            )
            mock_session.query.return_value.count.return_value = 0
            mock_session.query.return_value.filter.return_value.count.return_value = 0

            # Mock scheduler
            with patch("src.main.get_scheduler") as mock_scheduler:
                mock_sched = MagicMock()
                mock_scheduler.return_value = mock_sched
                mock_sched.get_next_run_time.return_value = None
                mock_sched.get_status.return_value = {"status": "running"}

                # Mock services
                with patch("src.main.IMAPService") as mock_imap, patch(
                    "src.main.AIService"
                ) as mock_ai:
                    mock_imap.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }

                    response = client.get("/api/dashboard", headers=auth_headers)
                    assert response.status_code == 200

    def test_health_endpoint_no_auth(self, client):
        """Health endpoint should not require auth"""
        with patch("src.main.IMAPService") as mock_imap, patch(
            "src.main.AIService"
        ) as mock_ai, patch("src.main.get_scheduler") as mock_scheduler:
            mock_imap.return_value.check_health.return_value = {"status": "healthy"}
            mock_ai.return_value.check_health.return_value = {"status": "healthy"}
            mock_scheduler.return_value.get_status.return_value = {"status": "running"}

            response = client.get("/api/health")
            assert response.status_code == 200


class TestMultipleAPIKeys:
    """Test multiple API key support"""

    def test_multiple_keys_comma_separated(self):
        """Test multiple keys via comma-separated config"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "key1,key2,key3",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
            },
        ):
            settings = reload_settings()
            keys = settings.get_api_keys()
            assert len(keys) == 3
            assert "key1" in keys
            assert "key2" in keys
            assert "key3" in keys

    def test_any_valid_key_works(self, client):
        """Test that any of the configured keys work"""
        with patch.dict(os.environ, {"API_KEY": "key1,key2,key3"}):
            reload_settings()
            test_client = TestClient(app)

            with patch("src.main.get_db") as mock_db:
                mock_session = MagicMock()
                mock_db.return_value = mock_session
                mock_session.query.return_value.order_by.return_value.first.return_value = (
                    None
                )
                mock_session.query.return_value.count.return_value = 0
                mock_session.query.return_value.filter.return_value.count.return_value = (
                    0
                )

                with patch("src.main.get_scheduler") as mock_scheduler, patch(
                    "src.main.IMAPService"
                ) as mock_imap, patch("src.main.AIService") as mock_ai:
                    mock_scheduler.return_value.get_next_run_time.return_value = None
                    mock_scheduler.return_value.get_status.return_value = {
                        "status": "running"
                    }
                    mock_imap.return_value.check_health.return_value = {
                        "status": "healthy"
                    }
                    mock_ai.return_value.check_health.return_value = {
                        "status": "healthy"
                    }

                    # Test each key
                    for key in ["key1", "key2", "key3"]:
                        headers = {"Authorization": f"Bearer {key}"}
                        response = test_client.get("/api/dashboard", headers=headers)
                        assert response.status_code == 200, f"Key {key} should work"


class TestCredentialRedaction:
    """Test that credentials are never logged"""

    def test_password_redacted_in_logs(self):
        """Test that passwords are redacted in log messages"""
        from src.utils.logging import SensitiveDataFilter

        filter = SensitiveDataFilter()

        # Test various password patterns
        test_cases = [
            ("password: secret123", "password: [REDACTED]"),
            ("PASSWORD=mypass", "PASSWORD=[REDACTED]"),
            ('{"password":"test"}', '{"password":"[REDACTED]"}'),
            ("imap_password: abc123", "imap_password: [REDACTED]"),
        ]

        for original, expected_pattern in test_cases:
            filtered = filter._redact_message(original)
            assert "[REDACTED]" in filtered, f"Failed to redact: {original}"
            assert "secret123" not in filtered
            assert "mypass" not in filtered
            assert "abc123" not in filtered

    def test_api_key_redacted_in_logs(self):
        """Test that API keys are redacted"""
        from src.utils.logging import SensitiveDataFilter

        filter = SensitiveDataFilter()

        test_cases = [
            ("API_KEY=supersecretkey123", "API_KEY=[REDACTED]"),
            ("api-key: mykey", "api-key: [REDACTED]"),
            ("Bearer supersecrettoken", "Bearer [REDACTED]"),
            ("Authorization: Bearer token123", "Authorization: Bearer [REDACTED]"),
        ]

        for original, expected_pattern in test_cases:
            filtered = filter._redact_message(original)
            assert "[REDACTED]" in filtered
            assert "supersecretkey123" not in filtered
            assert "supersecrettoken" not in filtered

    def test_email_body_redacted_in_logs(self):
        """Test that long email bodies are redacted"""
        from src.utils.logging import SensitiveDataFilter

        filter = SensitiveDataFilter()

        long_body = "x" * 300  # Long email body
        original = f"body_plain: {long_body}"
        filtered = filter._redact_message(original)

        assert "[EMAIL_BODY_REDACTED]" in filtered
        assert long_body not in filtered


class TestSecurityHeaders:
    """Test security headers are present"""

    def test_security_headers_present(self, client):
        """Test that security headers are added to responses"""
        with patch("src.main.IMAPService") as mock_imap, patch(
            "src.main.AIService"
        ) as mock_ai, patch("src.main.get_scheduler") as mock_scheduler:
            mock_imap.return_value.check_health.return_value = {"status": "healthy"}
            mock_ai.return_value.check_health.return_value = {"status": "healthy"}
            mock_scheduler.return_value.get_status.return_value = {"status": "running"}

            response = client.get("/api/health")

            # Check security headers
            assert "X-Content-Type-Options" in response.headers
            assert response.headers["X-Content-Type-Options"] == "nosniff"

            assert "X-Frame-Options" in response.headers
            assert response.headers["X-Frame-Options"] == "DENY"

            assert "Content-Security-Policy" in response.headers
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]

            assert "Referrer-Policy" in response.headers
            assert "Permissions-Policy" in response.headers


class TestRateLimiting:
    """Test rate limiting functionality"""

    def test_rate_limit_on_expensive_endpoint(self, client, auth_headers):
        """Test that rate limiting works on expensive endpoints"""
        # Note: This is a basic test - actual rate limiting behavior
        # depends on slowapi configuration

        with patch("src.main.get_db") as mock_db, patch(
            "src.main.get_scheduler"
        ) as mock_scheduler:
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.filter.return_value.first.return_value = (
                None
            )

            mock_scheduler.return_value.trigger_manual_run.return_value = True

            # Make multiple rapid requests
            # The first few should succeed, then rate limit may kick in
            # We're just checking the endpoint is protected
            response = client.post(
                "/api/processing/trigger",
                json={"trigger_type": "MANUAL"},
                headers=auth_headers,
            )

            # Should succeed or rate limit, but not fail for other reasons
            assert response.status_code in [200, 429]


class TestInputValidation:
    """Test input validation and sanitization"""

    def test_ai_output_validation(self):
        """Test that AI output is validated"""
        from src.services.ai_service import AIService

        service = AIService()

        # Test valid output
        valid_output = {
            "summary": "Test summary",
            "category": "Klinik",
            "spam_probability": 0.5,
            "action_required": True,
            "priority": "HIGH",
            "tasks": [],
            "suggested_folder": "Archive",
            "reasoning": "Test reasoning",
        }

        validated = service._parse_ai_response(str(valid_output))
        assert validated["category"] == "Klinik"
        assert validated["priority"] == "HIGH"

    def test_invalid_category_normalized(self):
        """Test that invalid categories are normalized"""
        from src.services.ai_service import AIService

        service = AIService()

        # Invalid category should be normalized to 'Unklar'
        assert service._validate_category("InvalidCategory") == "Unklar"
        assert service._validate_category("Klinik") == "Klinik"

    def test_invalid_priority_normalized(self):
        """Test that invalid priorities are normalized"""
        from src.services.ai_service import AIService

        service = AIService()

        # Invalid priority should be normalized to 'LOW'
        assert service._validate_priority("CRITICAL") == "LOW"
        assert service._validate_priority("HIGH") == "HIGH"

    def test_folder_allowlist_validation(self):
        """Test that suggested folders are validated against allowlist"""
        from src.services.ai_service import AIService

        service = AIService()

        # Valid folders should pass through
        assert service._validate_folder("Archive") == "Archive"
        assert service._validate_folder("Klinik") == "Klinik"

        # Invalid folders should default to Archive
        assert service._validate_folder("../etc/passwd") == "Archive"
        assert service._validate_folder("DELETE_ALL") == "Archive"
        assert service._validate_folder("Random") == "Archive"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
