"""Database engine and per-request sessions.

The runtime role (app_backend) cannot bypass row-level security. Each user
request runs in one transaction that first records the verified user ID with
``set_config('app.user_id', ..., true)``; ``true`` makes the setting local to
the transaction, so it disappears at commit/rollback and can never leak to the
next request that reuses the pooled connection.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


class DatabaseNotConfiguredError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def _engine_for(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        connect_args={"connect_timeout": 5, "application_name": "resume-backend"},
    )


def get_engine(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise DatabaseNotConfiguredError("DATABASE_URL is not set")
    return _engine_for(settings.database_url.get_secret_value())


@contextmanager
def user_session(settings: Settings, user_id: uuid.UUID) -> Iterator[Session]:
    """A transaction in which row-level security sees ``user_id`` as the current user."""
    factory = sessionmaker(bind=get_engine(settings), expire_on_commit=False)
    with factory() as session, session.begin():
        session.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"), {"user_id": str(user_id)}
        )
        yield session


@contextmanager
def system_session(settings: Settings) -> Iterator[Session]:
    """A transaction with no user identity (only rate-limit counters are visible)."""
    factory = sessionmaker(bind=get_engine(settings), expire_on_commit=False)
    with factory() as session, session.begin():
        yield session
