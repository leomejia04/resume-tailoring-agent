"""CSRF protection for cookie-authenticated, state-changing requests.

Every POST/PUT/PATCH/DELETE under /api must:
1. come from a trusted origin (Origin header, or Referer when Origin is absent), and
2. carry an X-CSRF-Token header equal to the CSRF cookie.

Another site can make a browser send our cookies, but it can neither read our
CSRF cookie nor set custom headers on a cross-site request without CORS
permission, so forged requests fail one or both checks.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import error_body
from app.auth.cookies import cookie_names
from app.config import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HEADER = "x-csrf-token"


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.trusted = settings.effective_trusted_origins
        self.cookie_name = cookie_names(settings).csrf

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in SAFE_METHODS
            or not scope["path"].startswith("/api/")
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = scope.get("state", {}).get("request_id", "unknown")
        origin = headers.get("origin")
        if origin is None and headers.get("referer"):
            origin = _origin_of(headers["referer"])
        if origin is None or origin not in self.trusted:
            await self._reject(send, request_id, "This request came from an untrusted page.")
            return

        cookie_token = cookie_parser(headers.get("cookie", "")).get(self.cookie_name, "")
        header_token = headers.get(HEADER, "")
        if (
            len(cookie_token) < 32
            or not header_token
            or not hmac.compare_digest(cookie_token.encode(), header_token.encode())
        ):
            await self._reject(
                send,
                request_id,
                "Your session security token is missing or stale. Reload the page and try again.",
            )
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, request_id: str, message: str) -> None:
        body = error_body("csrf_failed", message, request_id)
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
