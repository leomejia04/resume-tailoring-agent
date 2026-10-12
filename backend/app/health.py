"""Readiness: can this process serve real requests right now? (ADR 0012)

Liveness (``/health``) only says the process answers. Readiness also checks
that the database is reachable and its schema is at the newest migration in
this image, so a host keeps traffic on the old release until migrations ran.
Results are cached for a few seconds so a flood of probes costs at most one
query per interval. The response names which check failed, never why: error
details stay in the server log.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text

from app.config import Settings
from app.db.session import system_session

logger = logging.getLogger("app.health")
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
CACHE_SECONDS = 5.0

CheckState = Literal["ok", "failed", "behind"]


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[Literal["database", "migrations"], CheckState]


@lru_cache(maxsize=1)
def _revisions() -> tuple[str | None, frozenset[str]]:
    """(newest revision in this image, every revision this image knows)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_MIGRATIONS_DIR))
    return script.get_current_head(), frozenset(r.revision for r in script.walk_revisions())


def migration_state(current: str | None) -> CheckState:
    head, known = _revisions()
    if current == head:
        return "ok"
    if current is None or current in known:
        return "behind"
    # A revision this image has never seen comes from a newer release whose
    # migrations ran first (migrate, then start the new release). Migrations stay
    # compatible with the previous release, so the old instances keep serving.
    return "ok"


def _probe(settings: Settings) -> ReadyResponse:
    checks: dict[str, CheckState] = {"database": "failed", "migrations": "failed"}
    try:
        with system_session(settings) as session:
            current = session.execute(text("SELECT version_num FROM app.alembic_version")).scalar()
        checks["database"] = "ok"
        checks["migrations"] = migration_state(current)
    except Exception as exc:
        logger.warning("readiness check failed error=%s", type(exc).__name__)
    ready = all(state == "ok" for state in checks.values())
    return ReadyResponse(status="ok" if ready else "unavailable", checks=checks)


_lock = threading.Lock()
_cached: tuple[float, ReadyResponse] | None = None


def readiness(settings: Settings) -> ReadyResponse:
    global _cached
    with _lock:
        now = time.monotonic()
        if _cached is None or now - _cached[0] >= CACHE_SECONDS:
            _cached = (now, _probe(settings))
        return _cached[1]


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None
