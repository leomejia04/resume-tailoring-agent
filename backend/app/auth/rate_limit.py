"""Shared fixed-window rate limits stored in PostgreSQL.

Counters live in the database (not process memory), so limits hold across
multiple backend instances and restarts. The increment is one atomic
INSERT ... ON CONFLICT ... RETURNING statement, so concurrent requests cannot
both slip under the limit. Keys are SHA-256 hashes; raw emails and IP
addresses are never stored.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import Settings
from app.db.session import system_session


@dataclass(frozen=True)
class Limit:
    name: str
    max_requests: int
    window_seconds: int


SIGNUP_IP = Limit("signup-ip", 5, 3600)
LOGIN_IP = Limit("login-ip", 50, 900)
LOGIN_EMAIL = Limit("login-email", 10, 900)
REFRESH_IP = Limit("refresh-ip", 120, 900)
RESET_IP = Limit("reset-ip", 10, 3600)
RESET_EMAIL = Limit("reset-email", 3, 3600)
UPLOAD_USER = Limit("upload-user", 20, 3600)
# Account deletion re-checks the password: a password-guessing surface.
ACCOUNT_DELETE_USER = Limit("account-delete-user", 5, 3600)
# Paid model calls (also bounded by the daily spending limits).
AI_RUN_USER = Limit("ai-run-user", 20, 3600)
AI_DRAFT_USER = Limit("ai-draft-user", 30, 3600)
# PDF rendering is CPU heavy (LibreOffice in a container).
RENDER_USER = Limit("render-user", 40, 3600)
# Re-running ATS checks downloads and parses files (no model calls).
ATS_CHECK_USER = Limit("ats-check-user", 30, 3600)


class RateLimitedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


_INCREMENT = text(
    """
    INSERT INTO app.rate_limit_counters (bucket_key, window_start, count)
    VALUES (:key, :window_start, 1)
    ON CONFLICT (bucket_key, window_start)
    DO UPDATE SET count = app.rate_limit_counters.count + 1
    RETURNING count
    """
)
_PRUNE = text("DELETE FROM app.rate_limit_counters WHERE window_start < now() - interval '1 day'")


def bucket_key(limit: Limit, identifier: str) -> str:
    return hashlib.sha256(f"{limit.name}:{identifier.strip().lower()}".encode()).hexdigest()


def hit(settings: Settings, limit: Limit, identifier: str) -> None:
    """Count one request; raise RateLimitedError once the window's budget is spent."""
    now = time.time()
    start = math.floor(now / limit.window_seconds) * limit.window_seconds
    with system_session(settings) as session:
        count = session.execute(
            _INCREMENT,
            {
                "key": bucket_key(limit, identifier),
                "window_start": datetime.fromtimestamp(start, UTC),
            },
        ).scalar_one()
        if secrets.randbelow(100) == 0:
            session.execute(_PRUNE)
    if count > limit.max_requests:
        raise RateLimitedError(retry_after=max(1, int(start + limit.window_seconds - now)))
