"""Serve the built frontend from the API's own origin (ADR 0012).

One origin means the session cookies (SameSite=Strict, ``__Host-``) and the
CSRF Origin check work exactly as with the Vite proxy in development, with no
CORS. Everything under /api goes to the API untouched, so its 404/405 errors
keep their JSON shape.

The build output never changes while the process runs, so the file list is
read once at startup and requests are looked up in that dictionary: a URL can
only ever name a file that was in the build, never a path the client made up.
"""

from __future__ import annotations

from pathlib import Path

from starlette.responses import FileResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Vite puts a content hash in every asset name, so a cached copy is never stale.
_ASSET_CACHE = "public, max-age=31536000, immutable"
# Other top-level files (favicon etc.) keep their name across builds.
_FILE_CACHE = "public, max-age=3600"
# The page must be revalidated so a new deploy's asset names are picked up.
_PAGE_CACHE = "no-cache"


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class FrontendMiddleware:
    def __init__(self, app: ASGIApp, *, dist: Path) -> None:
        self.app = app
        dist = dist.resolve()
        self.index = dist / "index.html"
        if not self.index.is_file():
            raise RuntimeError(f"FRONTEND_DIST has no index.html: {dist}")
        self.files = {
            "/" + path.relative_to(dist).as_posix(): path
            for path in dist.rglob("*")
            if path.is_file() and path != self.index
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in {"GET", "HEAD"}
            or is_api_path(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        file = self.files.get(path)
        if file is not None:
            cache = _ASSET_CACHE if path.startswith("/assets/") else _FILE_CACHE
            response = FileResponse(file, headers={"Cache-Control": cache})
        elif path.startswith("/assets/"):
            # A missing script must not come back as HTML with a 200.
            response = PlainTextResponse("Not found", status_code=404)
        else:
            # Client-side routes (/runs/..., /reset-password) all load the app.
            response = FileResponse(self.index, headers={"Cache-Control": _PAGE_CACHE})
        await response(scope, receive, send)
