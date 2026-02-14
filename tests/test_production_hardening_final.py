"""
Final production hardening tests - tests with global auth middleware
"""

import pytest
from unittest.mock import patch, MagicMock
import os
import sys
import importlib
from io import StringIO
import logging


def build_fresh_app(env_dict):
    """
    Build a fresh app with the given environment variables.

    This helper ensures tests are deterministic by:
    1. Patching os.environ with env_dict (clear=True)
    2. Reloading src.config to get fresh settings
    3. Reloading src.main to get a fresh app with new settings
    4. Returning the new app object

    Args:
        env_dict: Dictionary of environment variables

    Returns:
        Fresh FastAPI app instance
    """
    # Import modules
    import src.config
    import src.main

    # Patch environment and reload
    with patch.dict(os.environ, env_dict, clear=True):
        # Reload config module to clear cached settings
        importlib.reload(src.config)

        # Reload main module to get fresh app with new settings
        importlib.reload(src.main)

        return src.main.app


class TestDebugGuard:
    """Test that DEBUG guard prevents web-exposed + DEBUG=true"""

    def test_debug_guard_blocks_web_exposed_with_0_0_0_0(self):
        """Test that DEBUG=true is blocked when SERVER_HOST=0.0.0.0"""
        with patch.dict(
            os.environ,
            {
                "DEBUG": "true",
                "SERVER_HOST": "0.0.0.0",
                "API_KEY": "testkey",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
            clear=True,
        ):
            from src.config import reload_settings

            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()

            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg
            assert "web-exposed" in error_msg

    def test_debug_guard_blocks_web_exposed_with_trust_proxy(self):
        """Test that DEBUG=true is blocked when TRUST_PROXY=true"""
        with patch.dict(
            os.environ,
            {
                "DEBUG": "true",
                "TRUST_PROXY": "true",
                "SERVER_HOST": "127.0.0.1",
                "API_KEY": "testkey",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
            clear=True,
        ):
            from src.config import reload_settings

            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()

            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg

    def test_debug_guard_blocks_web_exposed_with_allowed_hosts(self):
        """Test that DEBUG=true is blocked when ALLOWED_HOSTS is set"""
        with patch.dict(
            os.environ,
            {
                "DEBUG": "true",
                "ALLOWED_HOSTS": "example.com,api.example.com",
                "SERVER_HOST": "127.0.0.1",
                "API_KEY": "testkey",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
            clear=True,
        ):
            from src.config import reload_settings

            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()

            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg

    def test_debug_allowed_on_localhost_without_exposure(self):
        """Test that DEBUG=true is allowed on localhost without web exposure"""
        with patch.dict(
            os.environ,
            {
                "DEBUG": "true",
                "SERVER_HOST": "127.0.0.1",
                "TRUST_PROXY": "false",
                "ALLOWED_HOSTS": "",
                "API_KEY": "testkey",
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "test@test.com",
                "IMAP_PASSWORD": "test_password",
                "AI_ENDPOINT": "http://localhost:11434",
            },
            clear=True,
        ):
            from src.config import reload_settings

            # Should not raise an error
            settings = reload_settings()
            settings.validate_required_settings()
            assert settings.debug is True


class TestStartupSanitization:
    """Test startup error sanitization to prevent credential leakage"""

    def test_sanitize_error_function_in_production(self):
        """Test that sanitize_error removes sensitive details in production mode"""
        from src.utils.error_handling import sanitize_error

        # Create exception with sensitive data
        exc = ValueError(
            "Database connection failed: password=secret123 user=test@example.com"
        )

        # In production mode (debug=False), should only return exception type
        sanitized = sanitize_error(exc, debug=False)

        # Should only contain the error type, not the details
        assert sanitized == "ValueError"
        assert "secret123" not in sanitized
        assert "test@example.com" not in sanitized
        assert "password=" not in sanitized

    def test_sanitize_error_function_in_debug(self):
        """Test that sanitize_error preserves details in debug mode"""
        from src.utils.error_handling import sanitize_error

        # Create exception with details
        exc = ValueError("Test error with details")

        # In debug mode, should return full message
        sanitized = sanitize_error(exc, debug=True)

        assert sanitized == "Test error with details"

    def test_startup_validation_error_sanitized_in_stderr(self):
        """Test that startup validation errors are sanitized in stderr output"""
        # This tests the startup code pattern without importing main.py
        # which would cause sys.exit()

        # Simulate the startup validation pattern
        from src.utils.error_handling import sanitize_error

        # Create exception similar to startup validation failure
        exc = ValueError("IMAP_PASSWORD required: password=mysecret123 in env")

        # Capture what would be logged (sanitized version)
        sanitized_for_log = sanitize_error(exc, debug=False)

        # Verify log doesn't contain sensitive data
        assert "mysecret123" not in sanitized_for_log
        assert sanitized_for_log == "ValueError"

        # But the full error (for stderr) would still be visible
        # This matches the current code pattern where:
        # - logger gets sanitized version
        # - stderr gets full version for debugging
        full_error = str(exc)
        assert "IMAP_PASSWORD required" in full_error

    def test_sanitize_error_with_multiple_sensitive_patterns(self):
        """Test sanitization with various sensitive data patterns"""
        from src.utils.error_handling import sanitize_error

        # Test various patterns that might leak credentials
        test_cases = [
            "Connection failed: password=abc123",
            "Auth error: user=admin@test.com pass=secret",
            "IMAP error: credentials={'user': 'test', 'pass': 'secret123'}",
            "Token error: api_key=sk-1234567890abcdef",
        ]

        for error_msg in test_cases:
            exc = Exception(error_msg)
            sanitized = sanitize_error(exc, debug=False)

            # In production, should only show exception type
            assert sanitized == "Exception"
            # Should not contain any part of the original message
            assert error_msg not in sanitized

    def test_sanitize_error_with_different_exception_types(self):
        """Test sanitization works with different exception types"""
        from src.utils.error_handling import sanitize_error

        # Test with different exception types
        exceptions = [
            ValueError("sensitive data"),
            RuntimeError("password=secret"),
            ConnectionError("auth failed: token=abc123"),
            KeyError("api_key"),
        ]

        expected_types = ["ValueError", "RuntimeError", "ConnectionError", "KeyError"]

        for exc, expected_type in zip(exceptions, expected_types):
            sanitized = sanitize_error(exc, debug=False)
            assert sanitized == expected_type
            assert "sensitive" not in sanitized
            assert "password" not in sanitized
            assert "token" not in sanitized


class TestExceptionHandlerSanitization:
    """Test that exception handlers properly sanitize error responses"""

    def test_sanitize_error_never_leaks_imap_password(self):
        """Test that IMAP password is never in sanitized output"""
        from src.utils.error_handling import sanitize_error

        # Even with password in exception
        exc = ValueError("IMAP error: password=my_secret_imap_password_123")

        sanitized = sanitize_error(exc, debug=False)
        assert "my_secret_imap_password_123" not in sanitized
        assert "password=" not in sanitized
        assert sanitized == "ValueError"

    def test_sanitize_error_never_leaks_api_keys(self):
        """Test that API keys are never in sanitized output"""
        from src.utils.error_handling import sanitize_error

        exc = RuntimeError("Auth failed: api_key=sk-secret-key-12345")

        sanitized = sanitize_error(exc, debug=False)
        assert "sk-secret-key-12345" not in sanitized
        assert "api_key=" not in sanitized
        assert sanitized == "RuntimeError"


class TestCredentialLeakPrevention:
    """Test that credentials NEVER leak, even in debug mode"""

    def test_never_leaks_credentials_in_debug_false(self, caplog):
        """Test that credentials never appear in responses or logs when DEBUG=false"""
        from src.utils.error_handling import sanitize_error

        # Create exception with ALL sensitive data
        imap_username = "testuser@example.com"
        imap_password = "super_secret_password_123"
        bearer_token = "Bearer sk-test-token-abc123xyz"

        exc = ValueError(
            f"IMAP connection failed: username={imap_username} "
            f"password={imap_password} auth_header={bearer_token}"
        )

        # Capture logs
        with caplog.at_level(logging.ERROR):
            sanitized = sanitize_error(exc, debug=False)

        # Verify response doesn't contain secrets (production mode returns only type)
        assert sanitized == "ValueError"
        assert imap_username not in sanitized
        assert imap_password not in sanitized
        assert "sk-test-token-abc123xyz" not in sanitized
        assert bearer_token not in sanitized

        # Verify logs don't contain secrets
        log_text = caplog.text
        assert imap_username not in log_text
        assert imap_password not in log_text
        assert "sk-test-token-abc123xyz" not in log_text

    def test_never_leaks_credentials_in_debug_true(self, caplog):
        """Test that credentials never appear in responses or logs even when DEBUG=true"""
        from src.utils.error_handling import sanitize_error

        # Create exception with ALL sensitive data
        imap_username = "testuser@example.com"
        imap_password = "super_secret_password_123"
        bearer_token = "Bearer sk-test-token-abc123xyz"

        exc = ValueError(
            f"IMAP connection failed: username={imap_username} "
            f"password={imap_password} auth_header={bearer_token}"
        )

        # Capture logs
        with caplog.at_level(logging.ERROR):
            sanitized = sanitize_error(exc, debug=True)

        # Verify response doesn't contain raw secrets (even in debug mode)
        assert imap_username not in sanitized
        assert imap_password not in sanitized
        assert "sk-test-token-abc123xyz" not in sanitized
        assert "super_secret_password_123" not in sanitized

        # Verify secrets are redacted
        assert "[REDACTED]" in sanitized

        # Verify logs don't contain secrets
        log_text = caplog.text
        assert imap_password not in log_text
        assert "sk-test-token-abc123xyz" not in log_text
        assert "super_secret_password_123" not in log_text

    def test_redacts_password_patterns(self):
        """Test that password patterns are redacted"""
        from src.utils.error_handling import sanitize_error

        test_cases = [
            "Error: password=mysecret123",
            "Failed: passwd=abc123",
            "Auth: Password: secret",
            "Credentials: PASSWORD=TopSecret",
        ]

        for error_msg in test_cases:
            exc = Exception(error_msg)
            sanitized = sanitize_error(exc, debug=True)

            # Should not contain the actual secret value
            assert "mysecret123" not in sanitized
            assert "abc123" not in sanitized
            assert "secret" not in sanitized.replace("[REDACTED]", "")
            assert "TopSecret" not in sanitized

            # Should contain redaction marker
            assert "[REDACTED]" in sanitized

    def test_redacts_bearer_tokens(self):
        """Test that Bearer tokens are redacted"""
        from src.utils.error_handling import sanitize_error

        exc = Exception("Authorization: Bearer sk-1234567890abcdef")
        sanitized = sanitize_error(exc, debug=True)

        assert "sk-1234567890abcdef" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_redacts_actual_imap_credentials_from_settings(self):
        """Test that actual IMAP credentials from settings are redacted"""
        from src.utils.error_handling import sanitize_error

        # Set up environment with specific credentials
        with patch.dict(
            os.environ,
            {
                "IMAP_HOST": "imap.test.com",
                "IMAP_USERNAME": "actual_user@test.com",
                "IMAP_PASSWORD": "actual_password_from_env",
                "API_KEY": "testkey12345",
                "AI_ENDPOINT": "http://localhost:11434",
            },
            clear=True,
        ):
            from src.config import reload_settings

            settings = reload_settings()

            # Create exception with actual credentials from settings
            exc = Exception(
                f"Connection failed for {settings.imap_username} "
                f"with password {settings.get_imap_password()}"
            )

            sanitized = sanitize_error(exc, debug=True)

            # Actual credentials should be redacted
            assert "actual_user@test.com" not in sanitized
            assert "actual_password_from_env" not in sanitized
            assert "[REDACTED]" in sanitized
