"""FastAPI application factory.

Run with uvicorn's factory mode (exact development command in README.md):
    uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.api.v1.routes import router as v1_router
from app.auth.csrf import CSRFMiddleware
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Outbound request logs include full URLs (query strings); keep them off by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    show_docs = settings.app_env != "production"
    app = FastAPI(
        title="Resume Tailoring API",
        version=__version__,
        docs_url="/api/docs" if show_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if show_docs else None,
    )
    app.state.settings = settings
    register_exception_handlers(app)
    app.include_router(v1_router, prefix="/api/v1")

    # Starlette runs the last-added middleware first (outermost).
    # Multipart framing adds a little overhead on top of the file itself.
    upload_limit = settings.max_upload_bytes + 64 * 1024
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        overrides={"/api/v1/masters": upload_limit},
    )
    app.add_middleware(CSRFMiddleware, settings=settings)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["X-Request-ID"],
            # No cookies yet; credentialed CORS is decided with auth in milestone 3.
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware, hsts=settings.app_env == "production")
    return app
