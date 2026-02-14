"""
Tests for database startup checks

Verifies that the application correctly detects missing critical tables
and fails closed with appropriate error messages.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from sqlalchemy import inspect


class TestPendingActionsTableCheck:
    """Test startup check for pending_actions table"""

    def test_table_exists_check_passes(self):
        """When pending_actions table exists, check should pass"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine and inspector
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            mock_inspector = Mock()
            mock_inspector.get_table_names.return_value = [
                "processed_emails",
                "pending_actions",  # Table exists
                "apply_tokens",
                "email_tasks"
            ]
            mock_inspect.return_value = mock_inspector
            
            # Should return True without raising
            result = verify_pending_actions_table(mock_engine, debug=False)
            assert result is True

    def test_table_missing_raises_error(self):
        """When pending_actions table is missing, check should raise RuntimeError"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine and inspector
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            mock_inspector = Mock()
            mock_inspector.get_table_names.return_value = [
                "processed_emails",
                # pending_actions is missing!
                "apply_tokens",
                "email_tasks"
            ]
            mock_inspect.return_value = mock_inspector
            
            # Should raise RuntimeError
            with pytest.raises(RuntimeError) as exc_info:
                verify_pending_actions_table(mock_engine, debug=False)
            
            # Error message should be clear
            assert "pending_actions" in str(exc_info.value)
            assert "table not found" in str(exc_info.value).lower()

    def test_debug_false_sanitizes_errors(self):
        """When DEBUG=false, raw exception text should not leak"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine that raises an exception with sensitive data
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            # Simulate an exception that might contain credentials
            sensitive_error = Exception("Connection failed: password=secret123 user=admin")
            mock_inspect.side_effect = sensitive_error
            
            # Should raise RuntimeError with sanitized message
            with pytest.raises(RuntimeError) as exc_info:
                verify_pending_actions_table(mock_engine, debug=False)
            
            error_msg = str(exc_info.value)
            
            # Should not contain sensitive data
            assert "secret123" not in error_msg
            assert "password=" not in error_msg
            
            # Should contain generic error info
            assert "Failed to verify database schema" in error_msg

    def test_debug_true_includes_more_details(self):
        """When DEBUG=true, error details should be more verbose"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine that raises an exception
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            test_error = Exception("Test connection error")
            mock_inspect.side_effect = test_error
            
            # Should raise RuntimeError
            with pytest.raises(RuntimeError) as exc_info:
                verify_pending_actions_table(mock_engine, debug=True)
            
            error_msg = str(exc_info.value)
            
            # Should contain error info
            assert "Failed to verify database schema" in error_msg

    def test_runtime_error_preserved(self):
        """RuntimeError from table check should be preserved as-is"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine and inspector with no tables
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            mock_inspector = Mock()
            mock_inspector.get_table_names.return_value = []  # No tables at all
            mock_inspect.return_value = mock_inspector
            
            # Should raise RuntimeError with our specific message
            with pytest.raises(RuntimeError) as exc_info:
                verify_pending_actions_table(mock_engine, debug=False)
            
            # Check it's our error message, not wrapped
            assert "pending_actions" in str(exc_info.value)
            assert "table not found" in str(exc_info.value).lower()
            # Should mention the requirement
            assert "REQUIRE_APPROVAL" in str(exc_info.value)

    def test_inspector_exception_wrapped(self):
        """Exceptions from inspector should be caught and wrapped"""
        from src.database.startup_checks import verify_pending_actions_table
        
        # Mock engine and inspector that raises
        mock_engine = Mock()
        
        with patch('src.database.startup_checks.inspect') as mock_inspect:
            mock_inspect.side_effect = ConnectionError("Database unreachable")
            
            # Should catch and wrap in RuntimeError
            with pytest.raises(RuntimeError) as exc_info:
                verify_pending_actions_table(mock_engine, debug=False)
            
            # Should wrap the error
            assert "Failed to verify database schema" in str(exc_info.value)


class TestStartupIntegration:
    """Test integration of startup check with main app"""

    def test_startup_check_in_main_file(self):
        """Verify that startup check is present in main.py startup_event"""
        # This is a documentation/verification test
        # Read the main.py file to verify the check is present
        
        import os
        main_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
        
        with open(main_path, "r") as f:
            source = f.read()
        
        # Verify init_db is called
        assert "init_db()" in source
        
        # Verify our check is imported
        assert "from src.database.startup_checks import verify_pending_actions_table" in source
        
        # Verify the check is called
        assert "verify_pending_actions_table" in source
        
        # Verify sys.exit(1) on failure
        assert "sys.exit(1)" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
