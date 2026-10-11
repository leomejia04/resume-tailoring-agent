"""Session and CSRF cookies.

Session tokens live only in HttpOnly cookies (JavaScript cannot read them).
The CSRF token cookie is deliberately readable so the frontend can echo it in
the X-CSRF-Token header (double-submit pattern).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from starlette.responses import Response

from app.auth.gotrue import AuthSession
from app.config import Settings


@dataclass(frozen=True)
class CookieNames:
    access: str
    refresh: str
    csrf: str


def cookie_names(settings: Settings) -> CookieNames:
    # The __Host- prefix makes browsers require Secure, Path=/ and no Domain.
    prefix = "__Host-" if settings.secure_cookies else ""
    return CookieNames(f"{prefix}rt_access", f"{prefix}rt_refresh", f"{prefix}rt_csrf")


def _set(
    response: Response, settings: Settings, name: str, value: str, max_age: int, http_only: bool
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=settings.secure_cookies,
        httponly=http_only,
        samesite="strict",
    )


def set_session_cookies(response: Response, settings: Settings, session: AuthSession) -> None:
    names = cookie_names(settings)
    _set(response, settings, names.access, session.access_token, session.expires_in, True)
    _set(
        response,
        settings,
        names.refresh,
        session.refresh_token,
        settings.refresh_cookie_max_age,
        True,
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    names = cookie_names(settings)
    for name in (names.access, names.refresh):
        response.delete_cookie(
            name, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict"
        )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, settings: Settings, token: str) -> None:
    _set(
        response,
        settings,
        cookie_names(settings).csrf,
        token,
        settings.refresh_cookie_max_age,
        False,
    )
