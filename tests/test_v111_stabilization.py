"""
Tests for v1.1.1 stabilization patch:
1. /api/processing/trigger - optional body (no 422 without body, with body, OpenAPI)
2. ClassificationOverride DB model
3. POST /api/emails/{id}/override endpoint
4. EmailProcessor override-before-AI logic
5. Backward-compatible body schemas for existing endpoints
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import os

ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
    "LEARNING_ENABLED": "true",
}

AUTH = {"Authorization": "Bearer test_key_abc123"}


def _reset_rate_limiter():
    """Reset the in-memory rate limiter so tests don't interfere with each other."""
    try:
        from src.middleware.rate_limiting import limiter
        limiter.reset()
    except Exception:
        pass  # Best-effort; don't fail tests if reset isn't available


# 1. Trigger endpoint - optional body

class TestTriggerOptionalBody:
    def setup_method(self):
        """Reset rate limiter before each trigger test to avoid cross-test pollution."""
        _reset_rate_limiter()
    def test_trigger_without_body_returns_200(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_scheduler") as mock_sched:
                mock_sched.return_value.trigger_manual_run_async.return_value = (True, None)
                resp = client.post("/api/processing/trigger", headers=AUTH)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["success"] is True

    def test_trigger_with_json_body_returns_200(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_scheduler") as mock_sched:
                mock_sched.return_value.trigger_manual_run_async.return_value = (True, None)
                resp = client.post(
                    "/api/processing/trigger",
                    json={"trigger_type": "MANUAL"},
                    headers=AUTH,
                )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["success"] is True

    def test_trigger_already_running_returns_success_false(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_scheduler") as mock_sched:
                mock_sched.return_value.trigger_manual_run_async.return_value = (False, 42)
                resp = client.post("/api/processing/trigger", headers=AUTH)
        assert resp.status_code == 200
        d = resp.json()
        assert d["success"] is False
        assert d["run_id"] == 42

    def test_trigger_openapi_body_is_optional(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/openapi.json", headers=AUTH)
        assert resp.status_code == 200
        spec = resp.json()
        trigger = spec["paths"]["/api/processing/trigger"]["post"]
        body = trigger.get("requestBody", {})
        assert body.get("required", False) is False, "requestBody must not be required"


# 2. ClassificationOverride DB model

class TestClassificationOverrideModel:
    def test_model_importable(self):
        from src.models.database import ClassificationOverride
        assert ClassificationOverride.__tablename__ == "classification_overrides"

    def test_model_has_expected_columns(self):
        from src.models.database import ClassificationOverride
        cols = {c.name for c in ClassificationOverride.__table__.columns}
        expected = {
            "id", "sender_pattern", "subject_pattern",
            "category", "priority", "spam", "action_required",
            "suggested_folder", "created_at", "created_from_email_id",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_processed_email_has_override_columns(self):
        from src.models.database import ProcessedEmail
        cols = {c.name for c in ProcessedEmail.__table__.columns}
        assert "overridden" in cols
        assert "override_rule_id" in cols
        assert "original_classification" in cols


# 3. Override endpoint

class TestOverrideEndpoint:
    def test_override_404_for_nonexistent_email(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_db") as mock_db_dep:
                mock_session = MagicMock()
                mock_session.query.return_value.filter.return_value.first.return_value = None
                mock_db_dep.return_value = iter([mock_session])
                resp = client.post(
                    "/api/emails/9999/override",
                    json={"category": "Klinik"},
                    headers=AUTH,
                )
        assert resp.status_code == 404

    def test_override_requires_auth(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/emails/1/override", json={"category": "Klinik"})
        assert resp.status_code == 401

    def test_override_endpoint_exists_in_openapi(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/openapi.json", headers=AUTH)
        assert resp.status_code == 200
        spec = resp.json()
        assert "/api/emails/{email_id}/override" in spec["paths"]

    def test_override_schema_accepts_all_fields_no_422(self):
        """Schema accepts all classification override fields without 422."""
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            client = TestClient(app, raise_server_exceptions=False)
            with patch("src.main.get_db") as mock_db_dep:
                mock_session = MagicMock()
                mock_session.query.return_value.filter.return_value.first.return_value = None
                mock_db_dep.return_value = iter([mock_session])
                resp = client.post(
                    "/api/emails/1/override",
                    json={
                        "category": "Klinik",
                        "priority": "HIGH",
                        "spam": False,
                        "action_required": True,
                        "suggested_folder": "Inbox/Clinical",
                    },
                    headers=AUTH,
                )
        assert resp.status_code != 422, f"Got unexpected 422: {resp.text}"


# 4. EmailProcessor override-before-AI

class TestEmailProcessorOverrideLogic:
    def _make_processor(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
        from src.services.email_processor import EmailProcessor
        db = MagicMock()
        return EmailProcessor(db_session=db)

    def test_find_override_matches_sender_domain(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=1, sender_pattern="@example.com", subject_pattern=None,
            category="Klinik", priority="HIGH",
            spam=None, action_required=None, suggested_folder=None,
        )
        proc.db.query.return_value.all.return_value = [rule]
        result = proc._find_matching_override({"sender": "alice@example.com", "subject": "Hi"})
        assert result is rule

    def test_find_override_no_match_different_domain(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=1, sender_pattern="@blocked.com", subject_pattern=None,
            category=None, priority=None, spam=True, action_required=None, suggested_folder=None,
        )
        proc.db.query.return_value.all.return_value = [rule]
        result = proc._find_matching_override({"sender": "alice@legit.com", "subject": "Hi"})
        assert result is None

    def test_find_override_matches_subject_pattern(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=2, sender_pattern=None, subject_pattern="URGENT",
            category="Klinik", priority="HIGH",
            spam=None, action_required=True, suggested_folder=None,
        )
        proc.db.query.return_value.all.return_value = [rule]
        result = proc._find_matching_override({"sender": "boss@work.com", "subject": "URGENT: respond"})
        assert result is rule

    def test_find_override_no_match_when_no_rules(self):
        proc = self._make_processor()
        proc.db.query.return_value.all.return_value = []
        result = proc._find_matching_override({"sender": "x@y.com", "subject": "hello"})
        assert result is None

    def test_build_analysis_applies_rule_values(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=1, sender_pattern="@example.com", subject_pattern=None,
            category="Forschung", priority="MEDIUM",
            spam=False, action_required=True, suggested_folder="Research",
        )
        with patch.object(proc.ai_service, "fallback_classification") as mock_fb:
            mock_fb.return_value = {
                "summary": "x", "category": "Unklar", "spam_probability": 0.2,
                "action_required": False, "priority": "LOW",
                "tasks": [], "suggested_folder": "Archive", "reasoning": "fallback",
            }
            analysis = proc._build_analysis_from_override(rule, {"subject": "test"})
        assert analysis["category"] == "Forschung"
        assert analysis["priority"] == "MEDIUM"
        assert analysis["spam_probability"] == 0.05
        assert analysis["action_required"] is True
        assert analysis["suggested_folder"] == "Research"
        assert analysis["reasoning"] == "Applied override rule"

    def test_build_analysis_spam_true_sets_095(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=3, sender_pattern="@spam.com", subject_pattern=None,
            category=None, priority=None, spam=True, action_required=None, suggested_folder=None,
        )
        with patch.object(proc.ai_service, "fallback_classification") as mock_fb:
            mock_fb.return_value = {
                "summary": "", "category": "Unklar", "spam_probability": 0.1,
                "action_required": False, "priority": "LOW",
                "tasks": [], "suggested_folder": "Archive", "reasoning": "",
            }
            analysis = proc._build_analysis_from_override(rule, {})
        assert analysis["spam_probability"] == 0.95

    def test_build_analysis_none_fields_use_fallback(self):
        proc = self._make_processor()
        from src.models.database import ClassificationOverride
        rule = ClassificationOverride(
            id=4, sender_pattern="@x.com", subject_pattern=None,
            category=None, priority=None, spam=None, action_required=None, suggested_folder=None,
        )
        with patch.object(proc.ai_service, "fallback_classification") as mock_fb:
            mock_fb.return_value = {
                "summary": "text", "category": "Privat", "spam_probability": 0.3,
                "action_required": True, "priority": "HIGH",
                "tasks": [], "suggested_folder": "Personal", "reasoning": "fb",
            }
            analysis = proc._build_analysis_from_override(rule, {})
        assert analysis["category"] == "Privat"
        assert analysis["priority"] == "HIGH"
        assert analysis["spam_probability"] == 0.3
        assert analysis["action_required"] is True
        assert analysis["suggested_folder"] == "Personal"
        assert analysis["reasoning"] == "Applied override rule"


# 5. Version and changelog

class TestVersion111:
    def test_version_is_111(self):
        from src import __version__
        assert __version__ == "1.1.1"

    def test_changelog_has_111_entry(self):
        from src import CHANGELOG
        versions = [e["version"] for e in CHANGELOG]
        assert "1.1.1" in versions

    def test_changelog_still_has_110_and_100(self):
        from src import CHANGELOG
        versions = [e["version"] for e in CHANGELOG]
        assert "1.1.0" in versions
        assert "1.0.0" in versions


# 6. Backward-compatible body schemas
#    Problem statement requirement: verify that existing endpoints still accept
#    flat (non-embedded) JSON bodies after the auth middleware credentials fix.

class TestEndpointBodySchemas:
    """Verify existing endpoints accept flat JSON body (not embedded in a wrapper key).

    Root cause of 422 errors: require_authentication previously declared
    ``credentials: Optional[HTTPAuthorizationCredentials] = None`` which caused
    FastAPI to expect the endpoint body to be embedded as
    {"<field_name>": {...}} instead of a flat object.  After removing that
    parameter the endpoints must accept the exact flat structure the frontend sends.
    """

    def setup_method(self):
        _reset_rate_limiter()

    def _make_client(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            return TestClient(app, raise_server_exceptions=False), AUTH

    def test_emails_list_accepts_flat_body(self):
        """POST /api/emails/list must accept flat JSON (no wrapping key)."""
        client, auth = self._make_client()
        with patch("src.main.get_db") as mock_db:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_session.query.return_value.filter.return_value.count.return_value = 0
            mock_session.query.return_value.count.return_value = 0
            mock_db.return_value = iter([mock_session])
            resp = client.post(
                "/api/emails/list",
                json={"page": 1, "page_size": 10},
                headers=auth,
            )
        assert resp.status_code != 422, f"Got 422 (embedded body bug): {resp.text}"

    def test_emails_search_accepts_flat_body(self):
        """POST /api/emails/search must accept flat JSON (no wrapping key)."""
        client, auth = self._make_client()
        with patch("src.main.get_db") as mock_db:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_session.query.return_value.filter.return_value.count.return_value = 0
            mock_db.return_value = iter([mock_session])
            resp = client.post(
                "/api/emails/search",
                json={"query": "test", "page": 1},
                headers=auth,
            )
        assert resp.status_code != 422, f"Got 422 (embedded body bug): {resp.text}"

    def test_settings_accepts_flat_body(self):
        """POST /api/settings must accept flat JSON (no wrapping key)."""
        client, auth = self._make_client()
        resp = client.post(
            "/api/settings",
            json={"spam_threshold": 0.8},
            headers=auth,
        )
        assert resp.status_code != 422, f"Got 422 (embedded body bug): {resp.text}"

    def test_pending_actions_preview_accepts_flat_body(self):
        """POST /api/pending-actions/preview must accept flat JSON (no wrapping key)."""
        client, auth = self._make_client()
        with patch("src.main.get_db") as mock_db:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.all.return_value = []
            mock_session.query.return_value.filter.return_value.limit.return_value.all.return_value = []
            mock_db.return_value = iter([mock_session])
            resp = client.post(
                "/api/pending-actions/preview",
                json={},
                headers=auth,
            )
        assert resp.status_code != 422, f"Got 422 (embedded body bug): {resp.text}"

    def test_openapi_does_not_embed_emails_list_body(self):
        """OpenAPI schema for POST /api/emails/list must use a $ref, not an inline
        wrapper object.  If the auth credentials bug returned, FastAPI would generate
        a schema with a ``{"email_request": {...}}`` wrapper key."""
        client, auth = self._make_client()
        resp = client.get("/openapi.json", headers=auth)
        assert resp.status_code == 200
        spec = resp.json()
        post_schema = spec["paths"]["/api/emails/list"]["post"]
        # requestBody content schema must reference EmailListRequest directly
        # (not wrap it in a property key)
        body_schema = (
            post_schema.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        # A wrapped schema would look like {"properties": {"email_request": {...}}}
        props = body_schema.get("properties", {})
        assert "email_request" not in props, (
            "Body is incorrectly wrapped in 'email_request' key — "
            "auth credentials parameter bug is back"
        )

    def test_require_authentication_has_no_credentials_parameter(self):
        """require_authentication must not declare HTTPAuthorizationCredentials
        as a function parameter (this was the root cause of 422 errors)."""
        import inspect
        from src.middleware.auth import require_authentication
        sig = inspect.signature(require_authentication)
        for param_name, param in sig.parameters.items():
            annotation = param.annotation
            if annotation != inspect.Parameter.empty:
                ann_str = str(annotation)
                assert "HTTPAuthorizationCredentials" not in ann_str, (
                    f"Parameter '{param_name}' uses HTTPAuthorizationCredentials — "
                    "this causes FastAPI to embed request bodies in all protected endpoints"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
