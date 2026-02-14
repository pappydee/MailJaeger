"""
Unit tests for production safety features
"""

import pytest
from unittest.mock import patch, MagicMock
from src.config import Settings


def test_debug_guard_blocks_web_exposed_debug_true_with_0_0_0_0():
    """Test that DEBUG=true is blocked when SERVER_HOST=0.0.0.0"""
    with pytest.raises(ValueError) as exc_info:
        settings = Settings(
            debug=True,
            server_host="0.0.0.0",
            imap_host="test.example.com",
            imap_username="test@example.com",
            imap_password="testpass",
        )
        settings.validate_required_settings()

    error_msg = str(exc_info.value)
    assert "DEBUG must be false in production" in error_msg
    assert "web-exposed" in error_msg


def test_debug_guard_blocks_web_exposed_debug_true_with_trust_proxy():
    """Test that DEBUG=true is blocked when TRUST_PROXY=true"""
    with pytest.raises(ValueError) as exc_info:
        settings = Settings(
            debug=True,
            trust_proxy=True,
            server_host="127.0.0.1",  # Even with localhost
            imap_host="test.example.com",
            imap_username="test@example.com",
            imap_password="testpass",
        )
        settings.validate_required_settings()

    error_msg = str(exc_info.value)
    assert "DEBUG must be false in production" in error_msg


def test_debug_allowed_localhost_with_no_proxy():
    """Test that DEBUG=true is allowed on localhost without trust_proxy"""
    settings = Settings(
        debug=True,
        server_host="127.0.0.1",
        trust_proxy=False,
        imap_host="test.example.com",
        imap_username="test@example.com",
        imap_password="testpass",
    )

    # Should not raise an error
    settings.validate_required_settings()
    assert settings.debug is True


def test_debug_false_allowed_with_web_exposed():
    """Test that DEBUG=false is allowed even when web-exposed"""
    settings = Settings(
        debug=False,
        server_host="0.0.0.0",
        trust_proxy=True,
        imap_host="test.example.com",
        imap_username="test@example.com",
        imap_password="testpass",
    )

    # Should not raise an error
    settings.validate_required_settings()
    assert settings.debug is False


def test_global_exception_handler_sanitizes_response_when_debug_false():
    """Test that exception handler sanitizes response when DEBUG=false"""
    from fastapi.testclient import TestClient
    from src.main import app

    client = TestClient(app)

    # Create a test route that raises an exception with sensitive data
    @app.get("/test-exception-handler")
    async def test_exception_route():
        raise ValueError(
            "Database connection failed: password=secret123 user=admin@example.com"
        )

    # Test with DEBUG=false
    with patch("src.main.settings") as mock_settings:
        mock_settings.debug = False

        response = client.get("/test-exception-handler")

        assert response.status_code == 500
        response_data = response.json()

        # Response should contain generic error message, not sensitive data
        assert response_data["detail"] == "Internal server error"
        assert "secret123" not in str(response_data)
        assert "admin@example.com" not in str(response_data)
        assert "password=" not in str(response_data)


def test_global_exception_handler_shows_details_when_debug_true():
    """Test that exception handler shows details when DEBUG=true (for development)"""
    from fastapi.testclient import TestClient
    from src.main import app

    client = TestClient(app)

    # Create a test route that raises an exception
    @app.get("/test-exception-handler-debug")
    async def test_exception_route_debug():
        raise ValueError("Test error for debugging")

    # Test with DEBUG=true
    with patch("src.main.settings") as mock_settings:
        mock_settings.debug = True

        response = client.get("/test-exception-handler-debug")

        assert response.status_code == 500
        response_data = response.json()

        # In debug mode, should show actual error message
        assert "Test error for debugging" in response_data["detail"]


def test_global_exception_handler_sanitizes_logs_when_debug_false(caplog):
    """Test that exception handler sanitizes logs when DEBUG=false"""
    from fastapi.testclient import TestClient
    from src.main import app
    import logging

    client = TestClient(app)

    # Create a test route that raises an exception with sensitive data
    @app.get("/test-log-sanitization")
    async def test_log_route():
        raise ValueError("IMAP error: password='secret123' user='admin@example.com'")

    # Test with DEBUG=false
    with patch("src.main.settings") as mock_settings:
        mock_settings.debug = False

        with caplog.at_level(logging.ERROR):
            response = client.get("/test-log-sanitization")

            assert response.status_code == 500

            # Check that logs don't contain sensitive data
            log_text = caplog.text
            assert "secret123" not in log_text
            assert "admin@example.com" not in log_text

            # Should contain only error type
            assert "ValueError" in log_text or "Internal server error" in log_text


def test_sanitize_error_function_returns_only_type_when_debug_false():
    """Test that sanitize_error returns only exception type in production"""
    from src.utils.error_handling import sanitize_error

    exc = ValueError("password=secret123 host=db.internal.com")

    # In production (debug=False), should return only type
    result = sanitize_error(exc, debug=False)
    assert result == "ValueError"
    assert "secret123" not in result
    assert "db.internal.com" not in result


def test_sanitize_error_function_returns_full_message_when_debug_true():
    """Test that sanitize_error returns full message in debug mode"""
    from src.utils.error_handling import sanitize_error

    exc = ValueError("Test error with details")

    # In debug mode, should return full message
    result = sanitize_error(exc, debug=True)
    assert "Test error with details" in result
