"""SQLAlchemy engine + session factory for PostgreSQL."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from api.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """Lazily create and cache the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    """Lazily create and cache the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# User lookup helpers (raw SQL — matches the project's non-ORM pattern)
# ---------------------------------------------------------------------------
def get_user_by_username(session: Session, username: str) -> dict | None:
    """Fetch a full user row by username for authentication.
    Returns a plain dict (including ``hashed_password``) or ``None``.
    """
    row = session.execute(
        text("SELECT * FROM users WHERE username = :username"),
        {"username": username},
    ).mappings().first()
    return dict(row) if row else None

def get_user_by_email(session: Session, email: str) -> dict | None:
    """Fetch a full user row by email for authentication.
    Returns a plain dict (including ``hashed_password``) or ``None``.
    """
    row = session.execute(
        text("SELECT * FROM users WHERE email = :email"),
        {"email": email},
    ).mappings().first()
    return dict(row) if row else None
