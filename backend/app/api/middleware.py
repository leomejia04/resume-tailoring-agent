"""Pure ASGI middleware: request IDs, security headers, safe 500s, body limits.

Pure ASGI (rather than BaseHTTPMiddleware) lets us count request body bytes as
they stream in and replace a crash with a safe response before anything leaks.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_body
from app.web import is_api_path

logger = logging.getLogger("app.request")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}
# The API serves JSON only; nothing it returns should run scripts or be framed.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
# The built frontend (app/web.py): scripts, styles, images and API calls from
# this origin only; no inline scripts, plugins, framing, or foreign form posts.
# React's style={{...}} sets styles through the DOM, which style-src allows.
_PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Interactive docs (development only) load Swagger UI from its CDN.
_DOCS_PATHS = ("/api/docs", "/api/openapi.json")


async def _send_error(send: Send, status: int, body: bytes, extra: dict[str, str]) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += [(k.lower().encode(), v.encode()) for k, v in extra.items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    def _headers_for(self, path: str, request_id: str) -> dict[str, str]:
        headers = {"X-Request-ID": request_id, **_SECURITY_HEADERS}
        if not is_api_path(path):
            headers["Content-Security-Policy"] = _PAGE_CSP
        elif not path.startswith(_DOCS_PATHS):
            headers["Content-Security-Policy"] = _API_CSP
        if self.hsts:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        extra = self._headers_for(scope["path"], request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = MutableHeaders(scope=message)
                for key, value in extra.items():
                    if key not in headers:
                        headers[key] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            # Full details go to server logs only, tagged with the request ID.
            logger.exception("unhandled error request_id=%s", request_id)
            status = 500
            if not response_started:
                body = error_body("internal_error", "Something went wrong on our side.", request_id)
                await _send_error(send, 500, body, extra)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # Method, path, status, and timing only: never bodies or query strings.
            logger.info(
                "%s %s %s %.1fms request_id=%s",
                scope["method"],
                scope["path"],
                status,
                elapsed_ms,
                request_id,
            )


class _BodyTooLarge(HTTPException):
    """An HTTPException so FastAPI re-raises it (other errors become a generic 400)."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(413, f"The request is larger than the {max_bytes // 1024} KB limit.")


class BodySizeLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, max_bytes: int, overrides: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self.default_max = max_bytes
        # Exact path -> larger limit (e.g. the upload endpoint).
        self.overrides = overrides or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self.overrides.get(scope["path"].rstrip("/"), self.default_max)
        request_id = scope.get("state", {}).get("request_id", "unknown")
        too_large = error_body("payload_too_large", _BodyTooLarge(max_bytes).detail, request_id)

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not declared.isdigit():
                body = error_body("bad_request", "Invalid Content-Length header.", request_id)
                await _send_error(send, 400, body, {})
                return
            if int(declared) > max_bytes:
                await _send_error(send, 413, too_large, {})
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _BodyTooLarge(max_bytes)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_error(send, 413, too_large, {})
