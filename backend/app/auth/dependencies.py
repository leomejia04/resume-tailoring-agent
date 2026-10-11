"""Reusable FastAPI dependencies for authentication and per-user database access."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Depends, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.auth import rate_limit
from app.auth.cookies import cookie_names
from app.auth.tokens import TokenError, VerifiedUser, verifier_for
from app.config import Settings
from app.db.session import DatabaseNotConfiguredError, user_session


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def unavailable(what: str = "This feature") -> ApiError:
    return ApiError(
        503, "service_unavailable", f"{what} is temporarily unavailable. Please try again soon."
    )


def current_user(request: Request, settings: Settings = Depends(get_app_settings)) -> VerifiedUser:
    """The signed-in user, verified from the HttpOnly access-token cookie."""
    token = request.cookies.get(cookie_names(settings).access)
    if not token:
        raise ApiError(401, "unauthenticated", "Please sign in.")
    try:
        return verifier_for(settings.jwt_issuer).verify(token)
    except TokenError as exc:
        if exc.reason == "expired":
            raise ApiError(401, "session_expired", "Your session has expired.") from exc
        raise ApiError(401, "unauthenticated", "Please sign in.") from exc


@contextmanager
def user_db(settings: Settings, user: VerifiedUser) -> Iterator[Session]:
    """One transaction in which row-level security sees ``user`` as the current user."""
    try:
        with user_session(settings, user.id) as session:
            yield session
    except DatabaseNotConfiguredError as exc:
        raise unavailable("Storage") from exc
    except OperationalError as exc:
        raise unavailable("Storage") from exc


def client_ip(request: Request) -> str:
    # Behind a reverse proxy, run uvicorn with --proxy-headers and an explicit
    # --forwarded-allow-ips list so this reflects the real client address.
    return request.client.host if request.client else "unknown"


def enforce_limit(settings: Settings, limit: rate_limit.Limit, identifier: str) -> None:
    try:
        rate_limit.hit(settings, limit, identifier)
    except rate_limit.RateLimitedError as exc:
        raise ApiError(
            429,
            "rate_limited",
            "Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except (DatabaseNotConfiguredError, OperationalError) as exc:
        # Fail closed: without working limits, sensitive endpoints stay off.
        raise unavailable() from exc
