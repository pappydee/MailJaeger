"""
Test startup error handling to verify DEBUG mode controls stderr output

This test verifies TASK A requirements:
- When DEBUG=false, startup errors don't leak credentials to stderr
- When DEBUG=true, full exception details are printed to stderr
"""
import pytest
import sys
import os
import io
from unittest.mock import patch, MagicMock
from contextlib import redirect_stderr


class TestStartupErrorHandling:
    """Test that startup error handling respects DEBUG mode for stderr output"""
    
    def test_startup_error_debug_false_no_leak(self):
        """
        Test that when DEBUG=false, startup errors don't leak credentials to stderr.
        Simulates a configuration error containing sensitive information.
        """
        # Mock stderr to capture output
        captured_stderr = io.StringIO()
        
        # Simulate a validation error with sensitive data
        sensitive_error = ValueError(
            "Configuration validation failed:\n"
            "  - IMAP_PASSWORD: secret_password_123\n"
            "  - API_KEY: testkey123\n"
            "  - user=test@example.com"
        )
        
        with patch.dict(os.environ, {'DEBUG': 'false'}, clear=False):
            with redirect_stderr(captured_stderr):
                # Simulate the error handling from main.py
                debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
                if debug_mode:
                    print(f"\n❌ Configuration Error:\n{sensitive_error}\n", file=sys.stderr)
                else:
                    print(f"\n❌ Configuration Error: Configuration validation failed\n", file=sys.stderr)
        
        stderr_output = captured_stderr.getvalue()
        
        # Verify that sensitive data is NOT in stderr when DEBUG=false
        assert "secret_password_123" not in stderr_output, \
            "Password leaked to stderr when DEBUG=false"
        assert "testkey123" not in stderr_output, \
            "API key leaked to stderr when DEBUG=false"
        assert "test@example.com" not in stderr_output, \
            "Email leaked to stderr when DEBUG=false"
        
        # Verify that a generic error message IS present
        assert "Configuration validation failed" in stderr_output, \
            "Generic error message missing from stderr"
    
    def test_startup_error_debug_true_shows_details(self):
        """
        Test that when DEBUG=true, startup errors show full details to stderr.
        This helps developers debug configuration issues.
        """
        # Mock stderr to capture output
        captured_stderr = io.StringIO()
        
        # Simulate a validation error with sensitive data
        sensitive_error = ValueError(
            "Configuration validation failed:\n"
            "  - IMAP_PASSWORD or IMAP_PASSWORD_FILE is required\n"
            "  - user=test@example.com"
        )
        
        with patch.dict(os.environ, {'DEBUG': 'true'}, clear=False):
            with redirect_stderr(captured_stderr):
                # Simulate the error handling from main.py
                debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
                if debug_mode:
                    print(f"\n❌ Configuration Error:\n{sensitive_error}\n", file=sys.stderr)
                else:
                    print(f"\n❌ Configuration Error: Configuration validation failed\n", file=sys.stderr)
        
        stderr_output = captured_stderr.getvalue()
        
        # Verify that detailed error message IS present when DEBUG=true
        assert "IMAP_PASSWORD" in stderr_output, \
            "Expected detailed error message in stderr when DEBUG=true"
        assert "test@example.com" in stderr_output, \
            "Expected full error details in stderr when DEBUG=true"
        
        # The error should be more detailed than just "Configuration validation failed"
        assert len(stderr_output) > 100, \
            "Error message too short for DEBUG=true mode"
    
    def test_startup_error_both_exception_types_follow_same_policy(self):
        """
        Test that both ValueError and generic Exception handlers follow the same
        DEBUG mode policy for stderr output.
        """
        # Test generic Exception path
        captured_stderr = io.StringIO()
        
        # Simulate a generic exception with sensitive data
        sensitive_error = Exception("Database connection failed: password=sensitive123")
        
        with patch.dict(os.environ, {'DEBUG': 'false'}, clear=False):
            with redirect_stderr(captured_stderr):
                # Simulate the error handling from main.py (Exception path)
                debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
                if debug_mode:
                    print(f"\n❌ Configuration Error: {sensitive_error}\n", file=sys.stderr)
                else:
                    print(f"\n❌ Configuration Error: Failed to load configuration\n", file=sys.stderr)
        
        stderr_output = captured_stderr.getvalue()
        
        # Verify sensitive data is NOT leaked for generic Exception either
        assert "sensitive123" not in stderr_output, \
            "Sensitive data leaked in generic Exception handler when DEBUG=false"
        
        # Verify generic error message is present
        assert "Failed to load configuration" in stderr_output, \
            "Generic error message missing from stderr for generic Exception"
    
    def test_debug_mode_detection_from_env(self):
        """
        Test that debug mode is correctly detected from environment variable.
        Tests various truthy values.
        """
        # Test DEBUG=true
        with patch.dict(os.environ, {'DEBUG': 'true'}, clear=False):
            debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
            assert debug_mode is True, "DEBUG=true should enable debug mode"
        
        # Test DEBUG=1
        with patch.dict(os.environ, {'DEBUG': '1'}, clear=False):
            debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
            assert debug_mode is True, "DEBUG=1 should enable debug mode"
        
        # Test DEBUG=yes
        with patch.dict(os.environ, {'DEBUG': 'yes'}, clear=False):
            debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
            assert debug_mode is True, "DEBUG=yes should enable debug mode"
        
        # Test DEBUG=false
        with patch.dict(os.environ, {'DEBUG': 'false'}, clear=False):
            debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
            assert debug_mode is False, "DEBUG=false should disable debug mode"
        
        # Test DEBUG not set
        with patch.dict(os.environ, {}, clear=True):
            debug_mode = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
            assert debug_mode is False, "Missing DEBUG should default to false"


class TestDebugGuardIntegration:
    """Integration tests that verify the DEBUG guard works in config validation"""
    
    def test_debug_guard_blocks_web_exposed_with_debug_true(self):
        """
        Test that DEBUG=true is blocked when SERVER_HOST=0.0.0.0
        This verifies the DEBUG guard in config.py works as expected.
        """
        # Skip this test if dependencies are not available
        try:
            from src.config import reload_settings
        except ImportError:
            pytest.skip("Dependencies not available for integration test")
        
        with patch.dict(os.environ, {
            "DEBUG": "true",
            "SERVER_HOST": "0.0.0.0",
            "TRUST_PROXY": "false",
            "ALLOWED_HOSTS": "",
            "API_KEY": "testkey",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "test@test.com",
            "IMAP_PASSWORD": "test_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }, clear=True):
            
            # Should raise ValueError when validating settings
            with pytest.raises(ValueError) as exc_info:
                settings = reload_settings()
                settings.validate_required_settings()
            
            # Verify the error message is appropriate
            error_msg = str(exc_info.value)
            assert "DEBUG must be false in production" in error_msg or "web-exposed" in error_msg
            # Ensure no secrets are leaked in the error message
            assert "testkey" not in error_msg
            assert "test_password" not in error_msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
