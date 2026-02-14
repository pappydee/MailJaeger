"""
Comprehensive tests for approval safety guarantees with global authentication
Tests ensure all safety controls work correctly with proper authentication
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, Mock
from datetime import datetime, timedelta
import os


@pytest.fixture
def auth_headers():
    """Standard authentication headers for tests"""
    return {"Authorization": "Bearer test_api_key_123"}


@pytest.fixture
def test_env():
    """Standard test environment with authentication"""
    return {
        "API_KEY": "test_api_key_123",
        "IMAP_HOST": "imap.test.com",
        "IMAP_USERNAME": "test@test.com",
        "IMAP_PASSWORD": "test_password",
        "AI_ENDPOINT": "http://localhost:11434",
        "SAFE_MODE": "false",
        "ALLOW_DESTRUCTIVE_IMAP": "false",
        "SAFE_FOLDERS": "Archive,Spam,Quarantine",
    }


class TestSafeModeWithAuth:
    """Test SAFE_MODE blocks operations even with valid authentication"""

    def test_safe_mode_blocks_batch_apply_with_auth(self, test_env, auth_headers):
        """SAFE_MODE should block batch apply even with valid auth"""
        test_env["SAFE_MODE"] = "true"

        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

            assert response.status_code == 409
            assert "SAFE_MODE enabled" in response.json()["message"]

    def test_safe_mode_blocks_single_apply_with_auth(self, test_env, auth_headers):
        """SAFE_MODE should block single action apply even with valid auth"""
        test_env["SAFE_MODE"] = "true"

        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "test_token", "dry_run": False},
                headers=auth_headers,
            )

            assert response.status_code == 409
            assert "SAFE_MODE enabled" in response.json()["message"]

    def test_safe_mode_allows_preview_with_auth(self, test_env, auth_headers):
        """SAFE_MODE should still allow preview operations"""
        test_env["SAFE_MODE"] = "true"

        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            with patch("src.main.get_db") as mock_db:
                # Mock database to return no actions
                mock_db.return_value.query.return_value.filter.return_value.all.return_value = (
                    []
                )

                response = client.post(
                    "/api/pending-actions/preview",
                    json={"action_ids": [1]},
                    headers=auth_headers,
                )

                # Preview should work (returns 200 with no actions)
                assert response.status_code == 200


class TestAuthenticationRequired:
    """Test that authentication is required for all safety-critical endpoints"""

    def test_apply_requires_auth(self, test_env):
        """Batch apply should require authentication"""
        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.post(
                "/api/pending-actions/apply",
                json={"apply_token": "test", "dry_run": False},
            )

            assert response.status_code == 401
            assert response.json() == {"detail": "Unauthorized"}

    def test_single_apply_requires_auth(self, test_env):
        """Single action apply should require authentication"""
        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.post(
                "/api/pending-actions/1/apply",
                json={"apply_token": "test", "dry_run": False},
            )

            assert response.status_code == 401
            assert response.json() == {"detail": "Unauthorized"}

    def test_preview_requires_auth(self, test_env):
        """Preview endpoint should require authentication"""
        with patch.dict(os.environ, test_env, clear=True):
            from src.config import reload_settings

            reload_settings()
            from src.main import app

            client = TestClient(app)

            response = client.post(
                "/api/pending-actions/preview",
                json={"action_ids": [1]},
            )

            assert response.status_code == 401
            assert response.json() == {"detail": "Unauthorized"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
