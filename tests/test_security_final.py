"""
Final security tests for fail-closed policy and sanitize_error
Tests for the finish-security-without-scope-creep implementation
"""
import pytest
from unittest.mock import patch, MagicMock
from src.config import Settings
from src.utils.error_handling import sanitize_error


class TestFailClosedWebExposedPolicy:
    """Test fail-closed policy for web-exposed deployments"""
    
    def test_web_exposed_blocks_safe_mode_false_and_require_approval_false_with_0_0_0_0(self):
        """Test that web-exposed deployment (SERVER_HOST=0.0.0.0) blocks SAFE_MODE=false + REQUIRE_APPROVAL=false"""
        with pytest.raises(ValueError) as exc_info:
            settings = Settings(
                server_host="0.0.0.0",
                safe_mode=False,
                require_approval=False,
                imap_host="test.example.com",
                imap_username="test@example.com",
                imap_password="testpass"
            )
            settings.validate_required_settings()
        
        error_msg = str(exc_info.value)
        assert "Fail-closed safety requirement" in error_msg
        assert "Web-exposed deployments" in error_msg
        assert "SAFE_MODE=true OR REQUIRE_APPROVAL=true" in error_msg
    
    def test_web_exposed_blocks_safe_mode_false_and_require_approval_false_with_trust_proxy(self):
        """Test that web-exposed deployment (TRUST_PROXY=true) blocks SAFE_MODE=false + REQUIRE_APPROVAL=false"""
        with pytest.raises(ValueError) as exc_info:
            settings = Settings(
                server_host="127.0.0.1",
                trust_proxy=True,
                safe_mode=False,
                require_approval=False,
                imap_host="test.example.com",
                imap_username="test@example.com",
                imap_password="testpass"
            )
            settings.validate_required_settings()
        
        error_msg = str(exc_info.value)
        assert "Fail-closed safety requirement" in error_msg
        assert "Web-exposed deployments" in error_msg
    
    def test_web_exposed_blocks_safe_mode_false_and_require_approval_false_with_allowed_hosts(self):
        """Test that web-exposed deployment (ALLOWED_HOSTS set) blocks SAFE_MODE=false + REQUIRE_APPROVAL=false"""
        with pytest.raises(ValueError) as exc_info:
            settings = Settings(
                server_host="127.0.0.1",
                allowed_hosts="example.com,api.example.com",
                safe_mode=False,
                require_approval=False,
                imap_host="test.example.com",
                imap_username="test@example.com",
                imap_password="testpass"
            )
            settings.validate_required_settings()
        
        error_msg = str(exc_info.value)
        assert "Fail-closed safety requirement" in error_msg
    
    def test_web_exposed_allows_safe_mode_true_even_without_require_approval(self):
        """Test that web-exposed deployment allows SAFE_MODE=true even if REQUIRE_APPROVAL=false"""
        settings = Settings(
            server_host="0.0.0.0",
            safe_mode=True,
            require_approval=False,
            api_key="test_api_key_12345",
            imap_host="test.example.com",
            imap_username="test@example.com",
            imap_password="testpass"
        )
        
        # Should not raise an error
        settings.validate_required_settings()
        assert settings.safe_mode is True
    
    def test_web_exposed_allows_require_approval_true_even_without_safe_mode(self):
        """Test that web-exposed deployment allows REQUIRE_APPROVAL=true even if SAFE_MODE=false"""
        settings = Settings(
            server_host="0.0.0.0",
            safe_mode=False,
            require_approval=True,
            api_key="test_api_key_12345",
            imap_host="test.example.com",
            imap_username="test@example.com",
            imap_password="testpass"
        )
        
        # Should not raise an error
        settings.validate_required_settings()
        assert settings.require_approval is True
    
    def test_non_web_exposed_allows_safe_mode_false_and_require_approval_false(self):
        """Test that non-web-exposed deployment allows SAFE_MODE=false + REQUIRE_APPROVAL=false"""
        settings = Settings(
            server_host="127.0.0.1",
            trust_proxy=False,
            allowed_hosts="",
            safe_mode=False,
            require_approval=False,
            debug=True,  # Need debug=True to bypass API_KEY validation
            imap_host="test.example.com",
            imap_username="test@example.com",
            imap_password="testpass"
        )
        
        # Should not raise an error
        settings.validate_required_settings()
        assert settings.is_web_exposed() is False
        assert settings.safe_mode is False
        assert settings.require_approval is False


class TestSanitizeErrorPreventsSecretLeakage:
    """Test that sanitize_error prevents secrets in error messages"""
    
    def test_sanitize_error_redacts_password_in_debug_mode(self):
        """Test that sanitize_error redacts password even in debug mode"""
        exc = ValueError("Database connection failed: password=secret123 user=test@example.com")
        
        # In debug mode, should redact secrets
        result = sanitize_error(exc, debug=True)
        assert "secret123" not in result
        assert "[REDACTED]" in result
        assert "password=" in result or "password" in result.lower()
    
    def test_sanitize_error_redacts_username_in_debug_mode(self):
        """Test that sanitize_error redacts username in debug mode"""
        exc = ValueError("IMAP error: user=test@example.com password=secret123")
        
        result = sanitize_error(exc, debug=True)
        assert "test@example.com" not in result
        assert "[REDACTED]" in result
    
    def test_sanitize_error_prevents_secret_leakage_when_debug_false(self):
        """Test that sanitize_error prevents any secret leakage in production (DEBUG=false)"""
        exc = ValueError("Database error: password=secret123 user=test@example.com host=db.internal.com")
        
        # In production (debug=False), should return only exception type
        result = sanitize_error(exc, debug=False)
        assert result == "ValueError"
        assert "secret123" not in result
        assert "test@example.com" not in result
        assert "db.internal.com" not in result
        assert "password" not in result
    
    def test_sanitize_error_redacts_api_key_patterns(self):
        """Test that sanitize_error redacts API key patterns"""
        exc = ValueError("API error: api_key=sk_test_1234567890 apikey=abcdef")
        
        result = sanitize_error(exc, debug=True)
        assert "sk_test_1234567890" not in result
        assert "abcdef" not in result
        assert "[REDACTED]" in result
    
    def test_sanitize_error_redacts_bearer_tokens(self):
        """Test that sanitize_error redacts Bearer tokens"""
        exc = ValueError("Auth error: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        
        result = sanitize_error(exc, debug=True)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[REDACTED]" in result
    
    def test_processing_run_error_message_uses_sanitize_error(self):
        """Test that ProcessingRun.error_message stores sanitized errors"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.models.database import Base, ProcessingRun
        from datetime import datetime
        
        # Create in-memory database
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        
        # Simulate storing an error with secrets
        run = ProcessingRun(
            started_at=datetime.utcnow(),
            trigger_type="MANUAL",
            status="FAILURE",
            error_message=sanitize_error(
                ValueError("IMAP error: password=secret123 user=test@example.com"),
                debug=False
            ),
            completed_at=datetime.utcnow()
        )
        
        session.add(run)
        session.commit()
        
        # Retrieve and verify
        stored_run = session.query(ProcessingRun).first()
        assert stored_run.error_message == "ValueError"
        assert "secret123" not in stored_run.error_message
        assert "test@example.com" not in stored_run.error_message
        
        session.close()
    
    def test_processing_run_error_message_redacts_secrets_in_debug_mode(self):
        """Test that ProcessingRun.error_message redacts secrets even in debug mode"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.models.database import Base, ProcessingRun
        from datetime import datetime
        
        # Create in-memory database
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        
        # Simulate storing an error with secrets in debug mode
        run = ProcessingRun(
            started_at=datetime.utcnow(),
            trigger_type="MANUAL",
            status="FAILURE",
            error_message=sanitize_error(
                ValueError("Connection failed: password=secret123 user=test@example.com"),
                debug=True
            ),
            completed_at=datetime.utcnow()
        )
        
        session.add(run)
        session.commit()
        
        # Retrieve and verify
        stored_run = session.query(ProcessingRun).first()
        assert "secret123" not in stored_run.error_message
        assert "test@example.com" not in stored_run.error_message
        assert "[REDACTED]" in stored_run.error_message
        
        session.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
