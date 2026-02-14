"""
Test global authentication middleware enforcement
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import os


class TestGlobalAuthMiddleware:
    """Test that global middleware enforces authentication for all routes except /api/health"""

    def test_health_endpoint_accessible_without_auth(self):
        """Health endpoint should be accessible without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

    def test_root_returns_401_without_auth(self):
        """Root route should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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
            assert response.status_code == 401, "Root should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"
            assert (
                "WWW-Authenticate" in response.headers
            ), "Should include WWW-Authenticate header"
            assert (
                response.headers["WWW-Authenticate"] == "Bearer"
            ), "Should be Bearer auth"

    def test_root_returns_200_with_valid_auth(self):
        """Root route should return 200 with valid authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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
                "/", headers={"Authorization": "Bearer test_key_12345"}
            )
            assert response.status_code == 200, "Root should return 200 with valid auth"

    def test_docs_returns_401_without_auth(self):
        """API docs endpoint should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            response = client.get("/api/docs")
            assert (
                response.status_code == 401
            ), "/api/docs should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_redoc_returns_401_without_auth(self):
        """ReDoc endpoint should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            response = client.get("/api/redoc")
            assert (
                response.status_code == 401
            ), "/api/redoc should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_openapi_returns_401_without_auth(self):
        """OpenAPI schema endpoint should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            response = client.get("/openapi.json")
            assert (
                response.status_code == 401
            ), "/openapi.json should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_static_files_return_401_without_auth(self):
        """Static files should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            # Try to access a static file
            response = client.get("/static/app.js")
            assert (
                response.status_code == 401
            ), "Static files should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_api_endpoint_returns_401_without_auth(self):
        """Regular API endpoints should return 401 without authentication"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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
            ), "/api/settings should return 401 without auth"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_invalid_token_returns_401(self):
        """Invalid Bearer token should return 401"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            response = client.get("/", headers={"Authorization": "Bearer wrong_key"})
            assert response.status_code == 401, "Invalid token should return 401"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_malformed_auth_header_returns_401(self):
        """Malformed auth header should return 401"""
        with patch.dict(
            os.environ,
            {
                "API_KEY": "test_key_12345",
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

            response = client.get("/", headers={"Authorization": "Bearer"})
            assert response.status_code == 401, "Malformed header should return 401"
            assert response.json() == {
                "detail": "Unauthorized"
            }, "Should return minimal error"

    def test_no_api_key_configured_returns_401(self):
        """When no API key is configured, all routes except health should return 401"""
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

            # Root should return 401
            response = client.get("/")
            assert (
                response.status_code == 401
            ), "Root should return 401 when no API key configured"

            # Docs should return 401
            response = client.get("/api/docs")
            assert (
                response.status_code == 401
            ), "Docs should return 401 when no API key configured"

            # Health should still return 200
            with patch("src.main.IMAPService") as mock_imap, patch(
                "src.main.AIService"
            ) as mock_ai, patch("src.main.get_scheduler") as mock_scheduler:
                mock_imap.return_value.check_health.return_value = {"status": "healthy"}
                mock_ai.return_value.check_health.return_value = {"status": "healthy"}
                mock_scheduler.return_value.get_status.return_value = {
                    "status": "running"
                }

                response = client.get("/api/health")
                assert response.status_code == 200, "Health should still be accessible"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
