"""
Database startup checks for production safety

These checks ensure critical tables exist before the application serves requests,
preventing silent failures where features queue actions but the DB schema is missing.
"""

import logging
import re
from sqlalchemy import inspect, text
from src.utils.error_handling import sanitize_error

logger = logging.getLogger(__name__)

_ACTION_QUEUE_REQUIRED_COLUMNS = {
    "thread_id": "VARCHAR(200)",
    "payload": "JSON",
    "status": "VARCHAR(30) DEFAULT 'proposed_action'",
    "explanation": "VARCHAR(500)",
    "created_at": "DATETIME",
    "updated_at": "DATETIME",
    "queued_at": "DATETIME",
    "approved_at": "DATETIME",
    "executed_at": "DATETIME",
    "error_message": "TEXT",
}

_ACTION_QUEUE_REQUIRED_INDEXES = {
    "idx_action_queue_status": "status",
    "idx_action_queue_email": "email_id",
    "idx_action_queue_thread": "thread_id",
}

_PROCESSED_EMAILS_REQUIRED_COLUMNS = {
    "thread_state": "VARCHAR(30) DEFAULT 'informational'",
    "thread_priority": "VARCHAR(20) DEFAULT 'normal'",
    "thread_importance_score": "FLOAT DEFAULT 0.0",
}

_SENDER_PROFILES_REQUIRED_COLUMNS = {
    "spam_probability": "FLOAT DEFAULT 0.0",
    "interaction_count": "INTEGER DEFAULT 0",
}

_DECISION_EVENTS_REQUIRED_COLUMNS = {
    "action_type": "VARCHAR(50)",
    "target_folder": "VARCHAR(200)",
}

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_sql_identifier(identifier: str) -> str:
    if not _SQL_IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return identifier


def ensure_action_queue_schema_compatibility(engine, debug: bool = False):
    """
    Repair legacy SQLite action_queue table schemas in place.

    Adds missing columns and indexes expected by the current ActionQueue model,
    preserving existing rows.
    """
    if engine.dialect.name != "sqlite":
        return {"columns_added": [], "indexes_added": []}

    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        if "action_queue" not in table_names:
            return {"columns_added": [], "indexes_added": []}

        existing_columns = {
            column["name"] for column in inspector.get_columns("action_queue")
        }
        existing_indexes = {
            index["name"] for index in inspector.get_indexes("action_queue")
        }

        columns_added = []
        indexes_added = []

        with engine.begin() as connection:
            for column_name, column_type in _ACTION_QUEUE_REQUIRED_COLUMNS.items():
                if column_name in existing_columns:
                    continue
                safe_column_name = _safe_sql_identifier(column_name)
                connection.execute(
                    text(
                        "ALTER TABLE action_queue ADD COLUMN "
                        f"{safe_column_name} {column_type}"
                    )
                )
                columns_added.append(column_name)
                logger.warning(
                    "SQLite schema repair: added missing action_queue column '%s'",
                    column_name,
                )

            for index_name, index_column in _ACTION_QUEUE_REQUIRED_INDEXES.items():
                if index_name in existing_indexes:
                    continue
                if (
                    index_column not in existing_columns
                    and index_column not in columns_added
                ):
                    continue
                safe_index_name = _safe_sql_identifier(index_name)
                safe_index_column = _safe_sql_identifier(index_column)
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        f"{safe_index_name} ON action_queue ({safe_index_column})"
                    )
                )
                indexes_added.append(index_name)
                logger.warning(
                    "SQLite schema repair: added missing action_queue index '%s'",
                    index_name,
                )

        if columns_added or indexes_added:
            logger.info(
                "SQLite action_queue schema repair applied: columns=%s indexes=%s",
                columns_added,
                indexes_added,
            )
        else:
            logger.debug("SQLite action_queue schema already compatible")

        return {"columns_added": columns_added, "indexes_added": indexes_added}

    except Exception as e:
        sanitized = sanitize_error(e, debug=debug)
        error_msg = f"Failed to repair SQLite action_queue schema: {sanitized}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


def ensure_processed_emails_thread_state_schema(engine, debug: bool = False):
    """Repair legacy SQLite processed_emails schema for thread_state support."""
    if engine.dialect.name != "sqlite":
        return {"columns_added": []}
    try:
        inspector = inspect(engine)
        if "processed_emails" not in inspector.get_table_names():
            return {"columns_added": []}
        existing_columns = {
            column["name"] for column in inspector.get_columns("processed_emails")
        }
        columns_added = []
        with engine.begin() as connection:
            for column_name, column_type in _PROCESSED_EMAILS_REQUIRED_COLUMNS.items():
                if column_name in existing_columns:
                    continue
                safe_column_name = _safe_sql_identifier(column_name)
                connection.execute(
                    text(
                        "ALTER TABLE processed_emails ADD COLUMN "
                        f"{safe_column_name} {column_type}"
                    )
                )
                columns_added.append(column_name)
                logger.warning(
                    "SQLite schema repair: added missing processed_emails column '%s'",
                    column_name,
                )
        return {"columns_added": columns_added}
    except Exception as e:
        sanitized = sanitize_error(e, debug=debug)
        error_msg = f"Failed to repair SQLite processed_emails schema: {sanitized}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


def _ensure_sqlite_table_columns(
    engine,
    *,
    table_name: str,
    required_columns: dict,
    debug: bool = False,
):
    """Repair a legacy SQLite table by adding missing required columns."""
    if engine.dialect.name != "sqlite":
        return {"columns_added": []}
    try:
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            return {"columns_added": []}
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        columns_added = []
        with engine.begin() as connection:
            for column_name, column_type in required_columns.items():
                if column_name in existing_columns:
                    continue
                safe_table_name = _safe_sql_identifier(table_name)
                safe_column_name = _safe_sql_identifier(column_name)
                connection.execute(
                    text(
                        f"ALTER TABLE {safe_table_name} ADD COLUMN "
                        f"{safe_column_name} {column_type}"
                    )
                )
                columns_added.append(column_name)
                logger.warning(
                    "SQLite schema repair: added missing %s column '%s'",
                    table_name,
                    column_name,
                )
        if columns_added:
            logger.info(
                "SQLite %s schema repair applied: columns=%s",
                table_name,
                columns_added,
            )
        else:
            logger.debug("SQLite %s schema already compatible", table_name)
        return {"columns_added": columns_added}
    except Exception as e:
        sanitized = sanitize_error(e, debug=debug)
        error_msg = f"Failed to repair SQLite {table_name} schema: {sanitized}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


def ensure_historical_learning_schema_compatibility(engine, debug: bool = False):
    """Repair historical-learning SQLite schema for legacy databases."""
    if engine.dialect.name != "sqlite":
        return {
            "sender_profiles_columns_added": [],
            "decision_events_columns_added": [],
            "tables_created": [],
        }

    sender_result = _ensure_sqlite_table_columns(
        engine,
        table_name="sender_profiles",
        required_columns=_SENDER_PROFILES_REQUIRED_COLUMNS,
        debug=debug,
    )
    decision_result = _ensure_sqlite_table_columns(
        engine,
        table_name="decision_events",
        required_columns=_DECISION_EVENTS_REQUIRED_COLUMNS,
        debug=debug,
    )

    from src.models.database import LearningRun, LearningProgress

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    tables_created = []
    with engine.begin() as connection:
        if "learning_runs" not in existing_tables:
            LearningRun.__table__.create(bind=connection, checkfirst=True)
            tables_created.append("learning_runs")
            logger.warning("SQLite schema repair: created missing table 'learning_runs'")
        if "learning_progress" not in existing_tables:
            LearningProgress.__table__.create(bind=connection, checkfirst=True)
            tables_created.append("learning_progress")
            logger.warning("SQLite schema repair: created missing table 'learning_progress'")

    if sender_result["columns_added"] or decision_result["columns_added"] or tables_created:
        logger.info(
            "SQLite historical learning schema repair applied: "
            "sender_profiles_columns=%s decision_events_columns=%s tables_created=%s",
            sender_result["columns_added"],
            decision_result["columns_added"],
            tables_created,
        )
    else:
        logger.debug("SQLite historical learning schema already compatible")

    return {
        "sender_profiles_columns_added": sender_result["columns_added"],
        "decision_events_columns_added": decision_result["columns_added"],
        "tables_created": tables_created,
    }


def verify_pending_actions_table(engine, debug: bool = False):
    """
    Verify that the pending_actions table exists in the database.

    This is a fail-closed check to prevent silent failures when REQUIRE_APPROVAL
    is enabled but the database schema is incomplete.

    Args:
        engine: SQLAlchemy engine
        debug: Whether to include detailed error info

    Raises:
        RuntimeError: If the pending_actions table is missing or unreachable
    """
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()

        if "pending_actions" not in table_names:
            error_msg = (
                "Database schema incomplete: 'pending_actions' table not found. "
                "This table is required when REQUIRE_APPROVAL=true. "
                "Please run database initialization to create all tables."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        logger.info("Startup check passed: pending_actions table exists")
        return True

    except RuntimeError:
        # Re-raise our own RuntimeError as-is
        raise
    except Exception as e:
        # Sanitize any other exceptions to prevent credential leakage
        sanitized = sanitize_error(e, debug=debug)
        error_msg = f"Failed to verify database schema: {sanitized}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
