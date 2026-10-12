"""Version 1 API routes.

Everything except /health, /health/ready and the /auth routes requires a signed-in user.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app import __version__
from app.api.v1 import (
    auth_routes,
    evidence_routes,
    masters_routes,
    profile_routes,
    review_routes,
    tailoring_routes,
    tracker_routes,
)
from app.auth.dependencies import current_user, get_app_settings
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.health import ReadyResponse, readiness
from app.schemas.errors import ErrorResponse
from app.schemas.jobs import (
    DetectedSection,
    JobInputPreview,
    JobInputRequest,
    PreviewWarning,
    SectionKind,
)
from app.services.job_text import find_headings, word_count

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    403: {"model": ErrorResponse, "description": "CSRF check failed"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Invalid input"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
}
SHORT_DESCRIPTION_WORDS = 80


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Liveness check used by the frontend to show whether the API is reachable."""
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Not ready to serve requests"}},
    tags=["system"],
)
def ready(response: Response, settings: Settings = Depends(get_app_settings)) -> ReadyResponse:
    """Readiness check for the host: database reachable and schema migrated."""
    result = readiness(settings)
    if result.status != "ok":
        response.status_code = 503
    return result


@router.post("/jobs/preview", response_model=JobInputPreview, responses=_ERRORS, tags=["jobs"])
def preview_job_input(
    job: JobInputRequest, user: VerifiedUser = Depends(current_user)
) -> JobInputPreview:
    """Validate a pasted job posting and report what was recognized.

    Nothing is saved and no AI provider is called.
    """
    headings = find_headings(job.description)
    words = word_count(job.description)
    warnings = []
    if words < SHORT_DESCRIPTION_WORDS:
        warnings.append(
            PreviewWarning(
                code="short_description",
                message="This looks shorter than a typical posting. "
                "Paste the full description, including requirements.",
            )
        )
    if not any(h.kind in {"required", "preferred"} for h in headings):
        warnings.append(
            PreviewWarning(
                code="no_requirements_heading",
                message="No requirements or qualifications heading was found. "
                "Check that the requirements were included.",
            )
        )
    return JobInputPreview(
        title=job.title,
        company=job.company,
        source_url=job.source_url,
        character_count=len(job.description),
        word_count=words,
        sections=[
            DetectedSection(kind=SectionKind(h.kind), heading=h.text, line=h.line) for h in headings
        ],
        warnings=warnings,
    )


router.include_router(auth_routes.router)
router.include_router(profile_routes.router)
router.include_router(masters_routes.router)
router.include_router(evidence_routes.router)
router.include_router(tailoring_routes.router)
router.include_router(review_routes.router)
router.include_router(tracker_routes.router)
