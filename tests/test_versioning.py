"""
Tests for the centralized versioning system.

Verifies:
  - src/version.py is the single source of truth
  - __version__ in src/__init__.py matches VERSION
  - /api/version endpoint returns the central VERSION
  - /api/health endpoint includes the version
  - CHANGELOG contains the current VERSION entry
"""

import os
import pytest
from unittest.mock import patch


# Standard test env (mirrors conftest.py canonical env)
ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "SAFE_MODE": "false",
    "ALLOW_DESTRUCTIVE_IMAP": "false",
    "REQUIRE_APPROVAL": "false",
}

AUTH = {"Authorization": "Bearer test_key_abc123"}


class TestVersionSingleSourceOfTruth:
    """src/version.py must be the authoritative version definition."""

    def test_version_module_exists(self):
        from src.version import VERSION
        assert isinstance(VERSION, str)
        assert len(VERSION.split(".")) == 3, "VERSION must be semver MAJOR.MINOR.PATCH"

    def test_init_version_matches(self):
        from src.version import VERSION
        from src import __version__
        assert __version__ == VERSION

    def test_version_is_1_1_0(self):
        from src.version import VERSION
        assert VERSION == "1.1.0"


class TestApiVersionEndpoint:
    """GET /api/version must return the central VERSION."""

    def test_api_version_matches_constant(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.version import VERSION
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/version")
            assert response.status_code == 200
            assert response.json()["version"] == VERSION

    def test_api_version_includes_changelog(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/version")
            data = response.json()
            assert "changelog" in data
            assert isinstance(data["changelog"], list)
            assert len(data["changelog"]) >= 1


class TestApiHealthIncludesVersion:
    """GET /api/health must include the version field."""

    def test_health_has_version_field(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/health")
            assert response.status_code == 200
            data = response.json()
            assert "version" in data

    def test_health_version_matches_constant(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from src.version import VERSION
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/health")
            assert response.json()["version"] == VERSION


class TestChangelogConsistency:
    """CHANGELOG must contain an entry for the current VERSION."""

    def test_changelog_has_current_version(self):
        from src.version import VERSION
        from src import CHANGELOG
        versions = [e["version"] for e in CHANGELOG]
        assert VERSION in versions, f"CHANGELOG missing entry for current VERSION={VERSION}"

    def test_changelog_entries_have_required_fields(self):
        from src import CHANGELOG
        for entry in CHANGELOG:
            assert "version" in entry
            assert "date" in entry
            assert "changes" in entry
            assert isinstance(entry["changes"], list)

    def test_changelog_oldest_is_100(self):
        from src import CHANGELOG
        assert CHANGELOG[-1]["version"] == "1.0.0"
