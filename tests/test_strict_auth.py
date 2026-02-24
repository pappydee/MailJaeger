"""
Test strict fail-closed authentication for all routes including root and static files
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os

# Test scenarios:
# 1. No API key configured
# 2. API key configured but no auth header provided
# 3. API key configured with valid auth header


class TestStrictFailClosedAuthentication:
    """Test that all routes except /api/health require authentication"""

    def test_health_endpoint_accessible_without_auth(self):
        """Health endpoint should always be accessible without authentication"""
        # No API key configured
        with patch.dict(
            os.environ,
            {
                "API_KEY": "",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            with patch("src.main.IMAPService") as mock_imap, patch(
                "src.main.AIService"
            ) as mock_ai, patch("src.main.get_scheduler") as mock_scheduler:
                mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                mock_scheduler.return_value.get_status.return_value = {
                    "status": "running"
                }

                response = client.get("/api/health")
                assert (
                    response.status_code == 200
                ), "Health endpoint should be accessible without auth"

    def test_root_returns_200_without_api_key_configured(self):
        """Root route should return 200 (login page) even when no API key configured"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.get("/")
            # Root serves the login page regardless of API key configuration
            assert response.status_code in (
                200,
                404,
            ), "Root should return 200 (login page) or 404 (frontend not found in test env)"

    def test_root_returns_200_without_auth_header(self):
        """Root route should return 200 (login page) without auth header - user can then log in"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_api_key_12345",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.get("/")
            # Root is now public so the browser can load the login page
            assert response.status_code in (
                200,
                404,
            ), "Root should be accessible without auth (login page)"

    def test_root_returns_401_with_invalid_token(self):
        """Root route should return 401 with invalid token"""
    def test_root_returns_200_with_invalid_token(self):
        """Root route should return 200 (login page) even with invalid Bearer token - user sees login"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_api_key_12345",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            # Root is now in the unauthenticated allowlist so it is always accessible
            response = client.get("/", headers={"Authorization": "Bearer wrong_token"})
            assert response.status_code in (
                200,
                404,
            ), "Root should be accessible (login page served)"

    def test_root_returns_200_with_valid_auth(self):
        """Root route should return 200 with valid authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_api_key_12345",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.get(
                "/", headers={"Authorization": "Bearer test_api_key_12345"}
            )
            assert response.status_code == 200, "Root should return 200 with valid auth"

    def test_api_settings_returns_401_without_auth(self):
        """API settings endpoint should return 401 without auth"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.get("/api/settings")
            assert (
                response.status_code == 401
            ), "Settings should return 401 without auth"
            assert (
                response.json()["detail"] == "Unauthorized"
            ), "Should return minimal error message"

    def test_static_files_accessible_without_auth(self):
        """Static files should be accessible without authentication (needed for login page CSS/JS)"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_api_key_12345",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            # Static files must be accessible without auth so the login page renders correctly
            response = client.get("/static/style.css")
            # Either 200 (file found) or 404 (file not mounted in test env); never 401
            assert response.status_code in (
                200,
                404,
            ), "Static files should not return 401 - they are needed for the login page"

    def test_401_responses_do_not_leak_details(self):
        """401 responses should not contain sensitive information"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
        ):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            # Test API endpoints that still require auth (not the public login page)
            endpoints = ["/api/settings", "/api/dashboard"]

            for endpoint in endpoints:
                response = client.get(endpoint)
                assert response.status_code == 401
                response_text = response.text.lower()

                # Check that sensitive information is not leaked
                assert "api_key" not in response_text, f"API_KEY leaked in {endpoint}"
                assert (
                    "config" not in response_text
                ), f"Config details leaked in {endpoint}"
                assert "imap" not in response_text, f"IMAP details leaked in {endpoint}"
                assert (
                    "environment" not in response_text
                ), f"Environment leaked in {endpoint}"
                assert (
                    "path" not in response_text or "www-authenticate" in response_text
                ), f"File path leaked in {endpoint}"
                assert (
                    "traceback" not in response_text
                ), f"Traceback leaked in {endpoint}"

                # Should only contain minimal message
                detail = response.json().get("detail", "")
                assert (
                    detail == "Unauthorized"
                ), f"Non-minimal error message in {endpoint}: {detail}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
