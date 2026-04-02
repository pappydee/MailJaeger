"""
Tests for v1.1.0 features:
- Non-blocking trigger: returns immediately, run_id in response
- RunStatus / progress tracking
- AI service: JSON parsing improvements, HTML stripping, content cap
- Config: new Ollama options have correct defaults
"""

import pytest
from unittest.mock import patch, MagicMock
import os

ENV = {
    "API_KEY": "test_key_abc123",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
    "AI_ENDPOINT": "http://localhost:11434",
}


# ─── Config defaults ───────────────────────────────────────────────────────────

class TestConfigDefaults:
    def test_ai_timeout_default_30(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            s = reload_settings()
            assert s.ai_timeout == 30

    def test_ollama_options_present(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            s = reload_settings()
            assert s.ai_num_ctx == 2048
            assert s.ai_num_predict == 600
            assert s.ai_temperature == 0.2
            assert s.ai_top_p == 0.9
            assert s.ai_keep_alive == "30m"


# ─── AI service: JSON extraction ──────────────────────────────────────────────

class TestAIJsonExtraction:
    def _make_service(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService
            return AIService()

    def test_bare_json(self):
        svc = self._make_service()
        raw = '{"summary":"ok","category":"Privat","spam_probability":0.1,"action_required":false,"priority":"LOW","tasks":[],"suggested_folder":"Archive","reasoning":"r"}'
        result = svc._extract_json_string(raw)
        import json; d = json.loads(result)
        assert d["category"] == "Privat"

    def test_code_fence_json(self):
        svc = self._make_service()
        raw = '```json\n{"summary":"ok","category":"Klinik","spam_probability":0.0,"action_required":true,"priority":"HIGH","tasks":[],"suggested_folder":"Archive","reasoning":"r"}\n```'
        result = svc._extract_json_string(raw)
        import json; d = json.loads(result)
        assert d["category"] == "Klinik"

    def test_json_with_leading_text(self):
        svc = self._make_service()
        raw = 'Here is my analysis:\n{"summary":"ok","category":"Privat","spam_probability":0.0,"action_required":false,"priority":"LOW","tasks":[],"suggested_folder":"Archive","reasoning":"r"}'
        result = svc._extract_json_string(raw)
        import json; d = json.loads(result)
        assert d["summary"] == "ok"

    def test_no_json_raises(self):
        svc = self._make_service()
        with pytest.raises(ValueError, match="No JSON object found"):
            svc._extract_json_string("just plain text without braces")

    def test_parse_full_response_code_fence(self):
        svc = self._make_service()
        raw = '```json\n{"summary":"Test","category":"Privat","spam_probability":0.05,"action_required":false,"priority":"LOW","tasks":[],"suggested_folder":"Archive","reasoning":"ok"}\n```'
        analysis = svc._parse_ai_response(raw)
        assert analysis["category"] == "Privat"
        assert analysis["priority"] == "LOW"

    def test_invalid_category_clamped(self):
        svc = self._make_service()
        raw = '{"summary":"x","category":"INVALID_CAT","spam_probability":0.5,"action_required":false,"priority":"LOW","tasks":[],"suggested_folder":"Archive","reasoning":"r"}'
        analysis = svc._parse_ai_response(raw)
        assert analysis["category"] == "Unklar"

    def test_spam_probability_clamped(self):
        svc = self._make_service()
        raw = '{"summary":"x","category":"Privat","spam_probability":99.9,"action_required":false,"priority":"LOW","tasks":[],"suggested_folder":"Archive","reasoning":"r"}'
        analysis = svc._parse_ai_response(raw)
        assert analysis["spam_probability"] == 1.0

    def test_invalid_priority_clamped(self):
        svc = self._make_service()
        raw = '{"summary":"x","category":"Privat","spam_probability":0.1,"action_required":false,"priority":"CRITICAL","tasks":[],"suggested_folder":"Archive","reasoning":"r"}'
        analysis = svc._parse_ai_response(raw)
        assert analysis["priority"] == "LOW"


# ─── AI service: content preparation ─────────────────────────────────────────

class TestAIContentPrep:
    def _make_service(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.services.ai_service import AIService
            return AIService()

    def test_html_stripped(self):
        svc = self._make_service()
        email = {"subject": "Test", "sender": "a@b.com", "body_html": "<html><body><b>Hello</b></body></html>", "body_plain": ""}
        content = svc._prepare_content(email)
        assert "<html>" not in content
        assert "Hello" in content

    def test_content_capped_at_1500(self):
        svc = self._make_service()
        long_body = "x" * 5000
        email = {"subject": "S", "sender": "a@b.com", "body_plain": long_body}
        content = svc._prepare_content(email)
        # The body part should be capped to 1500 chars (plus the header text)
        assert len(content) < 1700  # 1500 body + small header


# ─── RunStatus ────────────────────────────────────────────────────────────────

class TestRunStatus:
    def test_default_idle(self):
        from src.services.scheduler import RunStatus
        s = RunStatus()
        assert s.status == "idle"
        assert s.progress_percent == 0

    def test_update_sets_last_update(self):
        from src.services.scheduler import RunStatus
        s = RunStatus()
        s.update(status="running", progress_percent=42)
        assert s.status == "running"
        assert s.progress_percent == 42
        assert s.last_update is not None

    def test_reset(self):
        from src.services.scheduler import RunStatus
        s = RunStatus()
        s.update(status="running", run_id=99)
        s.reset()
        assert s.status == "idle"
        assert s.run_id is None

    def test_to_dict_has_all_fields(self):
        from src.services.scheduler import RunStatus
        s = RunStatus()
        d = s.to_dict()
        required = {"run_id","status","current_step","progress_percent","processed","total","spam","action_required","failed","started_at","last_update","message"}
        # cancel_requested was added in v1.1.2 — use subset so the test remains
        # valid if further fields are added
        assert required.issubset(set(d.keys()))


# ─── Trigger endpoint: non-blocking ────────────────────────────────────────────

class TestTriggerEndpoint:
    def test_trigger_returns_immediately_with_success_true(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)

            with patch("src.main.get_scheduler") as mock_sched:
                mock_sched.return_value.trigger_manual_run_async.return_value = (True, None)

                resp = client.post(
                    "/api/processing/trigger",
                    headers={"Authorization": "Bearer test_key_abc123"},
                )
                assert resp.status_code == 200
                d = resp.json()
                assert d["success"] is True
                assert "run_id" in d
                assert "message" in d

    def test_trigger_returns_success_false_when_locked(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            reload_settings()
            from src.main import app
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)

            with patch("src.main.get_scheduler") as mock_sched:
                mock_sched.return_value.trigger_manual_run_async.return_value = (False, 7)

                resp = client.post(
                    "/api/processing/trigger",
                    headers={"Authorization": "Bearer test_key_abc123"},
                )
                assert resp.status_code == 200
                d = resp.json()
                assert d["success"] is False
                assert d["run_id"] == 7


# ─── Version bump ─────────────────────────────────────────────────────────────

class TestVersion110:
    def test_version_matches_central_constant(self):
        from src import __version__
        from src.version import VERSION
        assert __version__ == VERSION

    def test_changelog_has_current_version_entry(self):
        from src import CHANGELOG
        from src.version import VERSION
        versions = [e["version"] for e in CHANGELOG]
        assert VERSION in versions

    def test_changelog_oldest_is_100(self):
        from src import CHANGELOG
        assert CHANGELOG[-1]["version"] == "1.0.0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
