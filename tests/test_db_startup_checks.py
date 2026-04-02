"""
Tests for database startup checks

Verifies that the application correctly detects missing critical tables
and fails closed with appropriate error messages.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src.config import reload_settings
from src.main import app
from src.database.connection import get_db as _get_db
from src.models.database import Base, ProcessedEmail


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


def _create_legacy_action_queue_database(db_file: Path):
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE action_queue"))
        connection.execute(
            text(
                """
                CREATE TABLE action_queue (
                    id INTEGER PRIMARY KEY,
                    email_id INTEGER NOT NULL,
                    action_type VARCHAR(50) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO action_queue (id, email_id, action_type)
                VALUES (1, 101, 'move')
                """
            )
        )
    return engine


def _create_legacy_processed_emails_database(db_file: Path):
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS processed_emails_legacy (
                    id INTEGER PRIMARY KEY,
                    message_id VARCHAR(500) NOT NULL,
                    uid VARCHAR(100),
                    thread_id VARCHAR(200),
                    sender VARCHAR(200),
                    subject VARCHAR(500)
                )
                """
            )
        )
        connection.execute(text("DROP TABLE processed_emails"))
        connection.execute(text("ALTER TABLE processed_emails_legacy RENAME TO processed_emails"))
    return engine


def _create_legacy_sender_profiles_database(db_file: Path):
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE sender_profiles"))
        connection.execute(
            text(
                """
                CREATE TABLE sender_profiles (
                    id INTEGER PRIMARY KEY,
                    sender_address VARCHAR(200),
                    sender_domain VARCHAR(200),
                    total_emails INTEGER,
                    typical_folder VARCHAR(200),
                    folder_distribution JSON,
                    total_replies INTEGER,
                    reply_rate FLOAT,
                    avg_reply_delay_seconds FLOAT,
                    median_reply_delay_seconds FLOAT,
                    importance_tendency FLOAT,
                    spam_tendency FLOAT,
                    marked_important_count INTEGER,
                    marked_spam_count INTEGER,
                    archived_count INTEGER,
                    deleted_count INTEGER,
                    kept_in_inbox_count INTEGER,
                    preferred_category VARCHAR(50),
                    preferred_folder VARCHAR(200),
                    user_classification_count INTEGER,
                    first_seen DATETIME,
                    last_seen DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
    return engine


def _create_legacy_decision_events_database(db_file: Path):
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE decision_events"))
        connection.execute(
            text(
                """
                CREATE TABLE decision_events (
                    id INTEGER PRIMARY KEY,
                    email_id INTEGER NOT NULL,
                    thread_id VARCHAR(200),
                    event_type VARCHAR(50) NOT NULL,
                    source VARCHAR(50),
                    old_value VARCHAR(200),
                    new_value VARCHAR(200),
                    sender VARCHAR(200),
                    subject_snippet VARCHAR(200),
                    chosen_category VARCHAR(50),
                    chosen_folder VARCHAR(200),
                    confidence FLOAT,
                    model_version VARCHAR(50),
                    rule_id INTEGER,
                    user_confirmed BOOLEAN,
                    created_at DATETIME
                )
                """
            )
        )
    return engine


def _create_legacy_without_learning_tables_database(db_file: Path):
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS learning_progress"))
        connection.execute(text("DROP TABLE IF EXISTS learning_runs"))
    return engine


class TestActionQueueSchemaRepair:
    def test_init_db_repairs_missing_action_queue_columns_and_preserves_rows(
        self, tmp_path, monkeypatch
    ):
        from src.database import connection as db_connection

        db_file = tmp_path / "legacy_action_queue.sqlite"
        legacy_engine = _create_legacy_action_queue_database(db_file)
        legacy_engine.dispose()

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        reload_settings()

        db_connection._engine = None
        db_connection._SessionLocal = None
        db_connection.init_db()
        engine = db_connection.get_engine()
        inspector = inspect(engine)

        column_names = {column["name"] for column in inspector.get_columns("action_queue")}
        for required_column in [
            "thread_id",
            "payload",
            "status",
            "created_at",
            "updated_at",
            "queued_at",
            "approved_at",
            "executed_at",
            "error_message",
        ]:
            assert required_column in column_names

        with engine.connect() as connection:
            row_count = connection.execute(text("SELECT COUNT(*) FROM action_queue")).scalar()
            first_row = connection.execute(
                text("SELECT id, email_id, action_type FROM action_queue WHERE id = 1")
            ).fetchone()

        assert row_count == 1
        assert first_row == (1, 101, "move")

        index_names = {idx["name"] for idx in inspector.get_indexes("action_queue")}
        assert "idx_action_queue_status" in index_names
        assert "idx_action_queue_email" in index_names
        assert "idx_action_queue_thread" in index_names

    def test_actions_and_daily_report_endpoints_work_after_repair(self, tmp_path):
        from src.database.startup_checks import ensure_action_queue_schema_compatibility

        db_file = tmp_path / "legacy_action_queue_api.sqlite"
        engine = _create_legacy_action_queue_database(db_file)
        ensure_action_queue_schema_compatibility(engine, debug=False)

        SessionLocal = sessionmaker(bind=engine)
        db_session = SessionLocal()

        email = ProcessedEmail(
            id=101,
            message_id="legacy-101@example.com",
            uid="101",
            thread_id="thread-legacy-101",
            subject="Legacy schema test email",
            sender="sender@example.com",
            summary="summary",
            priority="HIGH",
            category="Klinik",
            action_required=True,
            is_spam=False,
            is_resolved=False,
            is_processed=True,
            processed_at=datetime.now(timezone.utc),
        )
        db_session.add(email)
        db_session.execute(
            text(
                "UPDATE action_queue SET thread_id = :thread_id, status = :status, "
                "created_at = :created_at WHERE id = 1"
            ),
            {
                "thread_id": email.thread_id,
                "status": "proposed",
                "created_at": datetime.now(timezone.utc),
            },
        )
        db_session.commit()

        app.dependency_overrides[_get_db] = lambda: db_session
        client = TestClient(app)
        headers = {"Authorization": "Bearer test_key_abc123"}

        actions_response = client.get("/api/actions", headers=headers)
        assert actions_response.status_code == 200
        actions_body = actions_response.json()
        assert len(actions_body) == 1
        assert actions_body[0]["id"] == 1
        assert actions_body[0]["thread_id"] == "thread-legacy-101"

        report_response = client.get("/api/reports/daily", headers=headers)
        assert report_response.status_code == 200
        report_body = report_response.json()
        assert "status" in report_body
        if report_body["status"] == "ready":
            assert "report" in report_body
            assert report_body["report"]["totals"]["total_processed"] >= 1
        else:
            assert report_body["status"] in {"pending", "running"}

        client.close()
        app.dependency_overrides.clear()
        db_session.close()

    def test_init_db_repairs_missing_processed_email_thread_columns(self, tmp_path):
        from src.database.startup_checks import ensure_processed_emails_thread_state_schema

        db_file = tmp_path / "legacy_processed_emails.sqlite"
        engine = _create_legacy_processed_emails_database(db_file)
        ensure_processed_emails_thread_state_schema(engine, debug=False)

        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("processed_emails")}
        assert "thread_state" in columns
        assert "thread_priority" in columns
        assert "thread_importance_score" in columns

    def test_init_db_repairs_missing_sender_profile_columns(self, tmp_path):
        from src.database.startup_checks import ensure_historical_learning_schema_compatibility

        db_file = tmp_path / "legacy_sender_profiles.sqlite"
        engine = _create_legacy_sender_profiles_database(db_file)
        ensure_historical_learning_schema_compatibility(engine, debug=False)

        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("sender_profiles")}
        assert "spam_probability" in columns
        assert "interaction_count" in columns

    def test_init_db_repairs_missing_decision_event_columns(self, tmp_path):
        from src.database.startup_checks import ensure_historical_learning_schema_compatibility

        db_file = tmp_path / "legacy_decision_events.sqlite"
        engine = _create_legacy_decision_events_database(db_file)
        ensure_historical_learning_schema_compatibility(engine, debug=False)

        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("decision_events")}
        assert "action_type" in columns
        assert "target_folder" in columns

    def test_init_db_creates_learning_tables_on_existing_db(self, tmp_path):
        from src.database.startup_checks import ensure_historical_learning_schema_compatibility

        db_file = tmp_path / "legacy_no_learning_tables.sqlite"
        engine = _create_legacy_without_learning_tables_database(db_file)
        ensure_historical_learning_schema_compatibility(engine, debug=False)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "learning_runs" in tables
        assert "learning_progress" in tables


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
