"""
Database startup checks for production safety

These checks ensure critical tables exist before the application serves requests,
preventing silent failures where features queue actions but the DB schema is missing.
"""

import logging
from sqlalchemy import inspect
from src.utils.error_handling import sanitize_error

logger = logging.getLogger(__name__)


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
