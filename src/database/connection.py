"""
Database setup and session management
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Generator
import logging

from src.config import get_settings
from src.models.database import Base

logger = logging.getLogger(__name__)

# Global engine and session factory
_engine = None
_SessionLocal = None


def init_db():
    """Initialize database connection and create tables"""
    global _engine, _SessionLocal
    
    settings = get_settings()
    
    # Create engine
    _engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
        pool_pre_ping=True
    )
    
    # Create session factory
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    
    # Create tables
    Base.metadata.create_all(bind=_engine)
    
    logger.info("Database initialized successfully")


def get_engine():
    """Get database engine"""
    if _engine is None:
        init_db()
    return _engine


def get_session_factory():
    """Get session factory"""
    if _SessionLocal is None:
        init_db()
    return _SessionLocal


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Get database session context manager"""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """Dependency for FastAPI"""
    with get_db_session() as session:
        yield session
