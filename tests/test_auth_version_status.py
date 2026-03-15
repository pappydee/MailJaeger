"""
Smoke tests for new endpoints:
- POST /api/auth/login  (browser-based API-key login → session cookie)
- POST /api/auth/logout
- GET  /api/auth/verify
- GET  /api/version     (version + changelog, no auth required)
- GET  /api/status      (current run status, requires auth)
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os


ENV = {
    "API_KEY": "test_secret_key_12345",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}


def make_client():
    with patch.dict(os.environ, ENV):
        from src.config import reload_settings
        reload_settings()
        from src.main import app
        return TestClient(app, raise_server_exceptions=False)


class TestAuthLogin:
    """POST /api/auth/login"""

    def test_login_with_valid_key_sets_cookie(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app, _sessions

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/api/auth/login",
                json={"api_key": "test_secret_key_12345"},
            )
            assert response.status_code == 200
            assert response.json().get("success") is True
            # Session cookie must be present
            assert "mailjaeger_session" in response.cookies

    def test_login_with_invalid_key_returns_401(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/api/auth/login",
                json={"api_key": "wrong_key"},
            )
            assert response.status_code == 401

    def test_login_without_body_returns_400(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/api/auth/login",
                json={},
            )
            assert response.status_code == 400

    def test_login_endpoint_is_public(self):
        """Login endpoint must be accessible without any existing auth."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            # No cookies, no Authorization header
            response = client.post(
                "/api/auth/login",
                json={"api_key": "test_secret_key_12345"},
            )
            assert response.status_code == 200


class TestAuthVerify:
    """GET /api/auth/verify"""

    def test_verify_without_auth_returns_401(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/auth/verify")
            assert response.status_code == 401

    def test_verify_with_bearer_returns_200(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(
                "/api/auth/verify",
                headers={"Authorization": "Bearer test_secret_key_12345"},
            )
            assert response.status_code == 200
            assert response.json().get("authenticated") is True

    def test_verify_with_session_cookie_returns_200(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            # Login first to get cookie
            login_resp = client.post(
                "/api/auth/login",
                json={"api_key": "test_secret_key_12345"},
            )
            assert login_resp.status_code == 200
            # Verify with cookie (TestClient sends cookies automatically)
            verify_resp = client.get("/api/auth/verify")
            assert verify_resp.status_code == 200
            assert verify_resp.json().get("authenticated") is True


class TestAuthLogout:
    """POST /api/auth/logout"""

    def test_logout_clears_session(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            # Login
            client.post("/api/auth/login", json={"api_key": "test_secret_key_12345"})
            # Should be authenticated
            assert client.get("/api/auth/verify").status_code == 200
            # Logout
            logout_resp = client.post("/api/auth/logout")
            assert logout_resp.status_code == 200
            # Should no longer be authenticated via cookie
            assert client.get("/api/auth/verify").status_code == 401


class TestVersionEndpoint:
    """GET /api/version — must be accessible without auth"""

    def test_version_endpoint_is_public(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/version")
            assert response.status_code == 200

    def test_version_returns_version_and_changelog(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/version")
            data = response.json()
            assert "version" in data
            assert "changelog" in data
            assert isinstance(data["changelog"], list)
            assert len(data["changelog"]) >= 1
            # Each entry must have version + changes
            for entry in data["changelog"]:
                assert "version" in entry
                assert "changes" in entry

    def test_version_is_1_1_1(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/version")
            assert response.json()["version"] == "1.1.1"


class TestStatusEndpoint:
    """GET /api/status — requires auth"""

    def test_status_requires_auth(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/status")
            assert response.status_code == 401

    def test_status_with_bearer_returns_200(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(
                "/api/status",
                headers={"Authorization": "Bearer test_secret_key_12345"},
            )
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "progress_percent" in data

    def test_status_fields_present(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(
                "/api/status",
                headers={"Authorization": "Bearer test_secret_key_12345"},
            )
            assert response.status_code == 200
            data = response.json()
            required_fields = {
                "status",
                "progress_percent",
                "run_id",
                "current_step",
                "processed",
                "total",
                "spam",
                "action_required",
                "failed",
                "started_at",
                "last_update",
                "message",
            }
            assert required_fields.issubset(data.keys()), (
                f"Missing fields: {required_fields - data.keys()}"
            )

    def test_status_with_session_cookie_returns_200(self):
        """Session-cookie login must allow access to protected routes (regression: cookie auth loop)."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            # Step 1: log in with API key to obtain a session cookie
            login = client.post(
                "/api/auth/login",
                json={"api_key": "test_secret_key_12345"},
            )
            assert login.status_code == 200, "Login must succeed before testing cookie auth"
            assert "mailjaeger_session" in login.cookies

            # Step 2: two different protected endpoints must be reachable with
            # only the session cookie and no Authorization header, confirming
            # this is not route-specific.
            status_resp = client.get("/api/status")
            assert status_resp.status_code == 200, (
                "Session-cookie auth must be accepted by Depends(require_authentication). "
                "If this fails, the browser will enter an infinite re-auth loop."
            )
            assert "status" in status_resp.json()

            # /api/auth/verify also uses the cookie — confirm consistent behaviour
            verify_resp = client.get("/api/auth/verify")
            assert verify_resp.status_code == 200
            assert verify_resp.json().get("authenticated") is True

    def test_cookie_auth_invalidated_after_logout(self):
        """Protected routes must return 401 after the session is logged out."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            login = client.post("/api/auth/login", json={"api_key": "test_secret_key_12345"})
            assert login.status_code == 200, "Login must succeed before testing logout"
            assert client.get("/api/status").status_code == 200
            client.post("/api/auth/logout")
            assert client.get("/api/status").status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
