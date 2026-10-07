"""Database engine and session factory.

Each HTTP request and each worker execution must use its own session;
sessions are not safe to share across concurrent execution contexts.
"""

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

# Fail fast instead of hanging when the database host is unreachable.
CONNECT_TIMEOUT_SECONDS = 5

engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def database_is_reachable(session: Session) -> bool:
    """Run a trivial query. Logs and returns False on any database error."""
    try:
        session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        logger.exception("Database connectivity check failed")
        return False
