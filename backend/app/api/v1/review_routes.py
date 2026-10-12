"""Draft review: versions, edits, revisions, rendering, approval, downloads.

Direct edits, restores, and approval are quick and answer synchronously.
Section rewrites, chat revisions, PDF rendering, and ATS checks are durable
jobs: those routes answer 202 Accepted with the run; poll ``GET /runs/{id}``.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.api.errors import ApiError
from app.api.v1.auth_routes import ensure_profile
from app.api.v1.tailoring_routes import _load_run_out
from app.auth import rate_limit
from app.auth.dependencies import current_user, enforce_limit, get_app_settings, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import (
    Approval,
    ChatMessage,
    Draft,
    JobPosting,
    JobPostingVersion,
    Profile,
    TailoringRun,
)
from app.documents.pdf_inspect import render_page_png
from app.jobs import requests as job_requests
from app.render.format import FormatPreferences
from app.schemas.ats import ShownStatus
from app.schemas.errors import ErrorResponse
from app.schemas.tailoring import RunOut
from app.tailoring import review
from app.tailoring.models import DraftContent, ValidationReport

router = APIRouter(tags=["review"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 404, 409, 422, 429, 503)}


class VersionSummary(BaseModel):
    version: int
    created_by: str
    created_at: datetime
    passed: bool
    model: str | None
    ats_status: ShownStatus
    # The version's ATS match estimate; None unless its check is complete.
    ats_score: int | None


class VersionOut(VersionSummary):
    content: DraftContent
    validation: ValidationReport


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    operations: list[review.EditOperation] = Field(min_length=1, max_length=20)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class RegenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_ref: str = Field(max_length=10)
    expected_version: int = Field(ge=1)


class ImproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=2, max_length=500)
    expected_version: int = Field(ge=1)


class ChatMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    draft_version: int | None
    created_at: datetime


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_version: int = Field(ge=1)
    artifact_id: uuid.UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovalOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    job_title: str | None
    company: str | None
    # The posting link the user entered (a label; never fetched).
    source_url: str | None
    filename: str
    draft_version: int
    artifact_sha256: str
    approved_at: datetime


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    preferences: FormatPreferences


class PreferencesOut(BaseModel):
    preferences: FormatPreferences
    lock_version: int


# --- Versions and edits ---------------------------------------------------------


@router.get("/runs/{run_id}/draft/versions", response_model=list[VersionSummary], responses=_ERRORS)
def versions(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[VersionSummary]:
    return [
        VersionSummary(
            version=v.version,
            created_by=v.created_by,
            created_at=v.created_at,
            passed=bool(v.validation.get("passed")),
            model=v.model,
            ats_status=ats_status,
            ats_score=ats_score,
        )
        for v, ats_status, ats_score in review.list_versions(settings, user, run_id)
    ]


@router.get("/runs/{run_id}/draft/versions/{number}", response_model=VersionOut, responses=_ERRORS)
def version_detail(
    run_id: uuid.UUID,
    number: int,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> VersionOut:
    for v, ats_status, ats_score in review.list_versions(settings, user, run_id):
        if v.version == number:
            return VersionOut(
                version=v.version,
                created_by=v.created_by,
                created_at=v.created_at,
                passed=bool(v.validation.get("passed")),
                model=v.model,
                ats_status=ats_status,
                ats_score=ats_score,
                content=DraftContent.model_validate(v.content),
                validation=ValidationReport.model_validate(v.validation),
            )
    raise ApiError(404, "not_found", "That draft version was not found.")


@router.post("/runs/{run_id}/draft/edits", response_model=RunOut, responses=_ERRORS)
def edit(
    run_id: uuid.UUID,
    body: EditRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    review.apply_edits(settings, user, run_id, body.expected_version, body.operations)
    return _load_run_out(settings, user, run_id)


@router.post("/runs/{run_id}/draft/restore", response_model=RunOut, responses=_ERRORS)
def restore(
    run_id: uuid.UUID,
    body: RestoreRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    review.restore_version(settings, user, run_id, body.version, body.expected_version)
    return _load_run_out(settings, user, run_id)


_ACCEPTED = {
    "response_model": RunOut,
    "status_code": status.HTTP_202_ACCEPTED,
    "responses": _ERRORS,
}


@router.post("/runs/{run_id}/draft/regenerate", **_ACCEPTED)
def regenerate(
    run_id: uuid.UUID,
    body: RegenerateRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    """Queue a rewrite of one section; the new version appears when the job finishes."""
    enforce_limit(settings, rate_limit.AI_DRAFT_USER, str(user.id))
    job_requests.request_regenerate(settings, user, run_id, body.entry_ref, body.expected_version)
    return _load_run_out(settings, user, run_id)


@router.post("/runs/{run_id}/draft/chat", **_ACCEPTED)
def chat(
    run_id: uuid.UUID,
    body: ChatRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    """Save the instruction and queue the revision; the reply appears in the chat history."""
    enforce_limit(settings, rate_limit.AI_DRAFT_USER, str(user.id))
    job_requests.request_chat(settings, user, run_id, body.message, body.expected_version)
    return _load_run_out(settings, user, run_id)


@router.post("/runs/{run_id}/draft/improve", **_ACCEPTED)
def improve(
    run_id: uuid.UUID,
    body: ImproveRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    """Queue automatic improvement again (it also runs by itself after each draft).

    At most two attempts per run in total; each saves a version only if it
    raises the ATS match estimate using confirmed facts (ADR 0008).
    """
    enforce_limit(settings, rate_limit.AI_DRAFT_USER, str(user.id))
    job_requests.request_improve(settings, user, run_id, body.expected_version)
    return _load_run_out(settings, user, run_id)


@router.get("/runs/{run_id}/chat", response_model=list[ChatMessageOut], responses=_ERRORS)
def chat_history(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[ChatMessageOut]:
    with user_db(settings, user) as db:
        if (
            db.scalar(
                select(TailoringRun.id).where(
                    TailoringRun.id == run_id, TailoringRun.owner_id == user.id
                )
            )
            is None
        ):
            raise ApiError(404, "not_found", "That tailoring run was not found.")
        messages = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.run_id == run_id, ChatMessage.owner_id == user.id)
            .order_by(ChatMessage.created_at)
        ).all()
        return [
            ChatMessageOut(
                role=m.role,
                content=m.content,
                draft_version=m.draft_version,
                created_at=m.created_at,
            )  # type: ignore[arg-type]
            for m in messages
        ]


# --- Rendering, preview, approval, downloads ------------------------------------


@router.post("/runs/{run_id}/render", **_ACCEPTED)
def render(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    """Queue rendering of the current draft version to the PDF you will review."""
    enforce_limit(settings, rate_limit.RENDER_USER, str(user.id))
    job_requests.request_render(settings, user, run_id)
    return _load_run_out(settings, user, run_id)


@router.get(
    "/artifacts/{artifact_id}/pdf", responses={**_ERRORS, 200: {"content": {"application/pdf": {}}}}
)
def artifact_pdf(
    artifact_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    data, artifact = review.artifact_bytes(settings, user, artifact_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{artifact.filename}"',
            # The only response our own pages may embed (PDF preview frame).
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get(
    "/artifacts/{artifact_id}/preview.png",
    responses={**_ERRORS, 200: {"content": {"image/png": {}}}},
)
def artifact_preview(
    artifact_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    data, _ = review.artifact_bytes(settings, user, artifact_id)
    with tempfile.TemporaryDirectory(prefix="preview-") as work:
        pdf_path, png_path = Path(work) / "a.pdf", Path(work) / "a.png"
        pdf_path.write_bytes(data)
        render_page_png(pdf_path, png_path, dpi=110)
        png = png_path.read_bytes()
    return Response(content=png, media_type="image/png")


@router.post("/runs/{run_id}/ats/check", **_ACCEPTED)
def ats_check(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    """Queue any missing, failed, or out-of-date ATS checks for the run's current versions."""
    enforce_limit(settings, rate_limit.ATS_CHECK_USER, str(user.id))
    job_requests.request_ats_check(settings, user, run_id)
    return _load_run_out(settings, user, run_id)


@router.post("/runs/{run_id}/approve", response_model=RunOut, responses=_ERRORS)
def approve(
    run_id: uuid.UUID,
    body: ApproveRequest,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    review.approve(
        settings, user, run_id, body.draft_version, body.artifact_id, body.artifact_sha256
    )
    return _load_run_out(settings, user, run_id)


@router.get("/approvals", response_model=list[ApprovalOut], responses=_ERRORS)
def approvals(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> list[ApprovalOut]:
    with user_db(settings, user) as db:
        rows = db.execute(
            select(
                Approval, Draft.run_id, JobPosting.title, JobPosting.company, JobPosting.source_url
            )
            .join(Draft, Draft.id == Approval.draft_id)
            .join(TailoringRun, TailoringRun.id == Draft.run_id)
            .join(JobPostingVersion, JobPostingVersion.id == TailoringRun.posting_version_id)
            .join(JobPosting, JobPosting.id == JobPostingVersion.posting_id)
            .where(Approval.owner_id == user.id)
            .order_by(Approval.approved_at.desc())
        ).all()
        return [
            ApprovalOut(
                id=a.id,
                run_id=run_id,
                job_title=title,
                company=company,
                source_url=source_url,
                filename=a.filename,
                draft_version=a.draft_version,
                artifact_sha256=a.artifact_sha256,
                approved_at=a.approved_at,
            )
            for a, run_id, title, company, source_url in rows
        ]


@router.get(
    "/approvals/{approval_id}/download",
    responses={**_ERRORS, 200: {"content": {"application/pdf": {}}}},
)
def download(
    approval_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    data, filename = review.approved_download(settings, user, approval_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Remembered formatting preferences (explicit consent) -----------------------


@router.get("/profile/preferences", response_model=PreferencesOut, responses=_ERRORS)
def get_preferences(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> PreferencesOut:
    ensure_profile(settings, user.id)
    with user_db(settings, user) as db:
        profile = db.scalars(select(Profile).where(Profile.user_id == user.id)).one()
        return PreferencesOut(
            preferences=FormatPreferences.model_validate(profile.preferences or {}),
            lock_version=profile.lock_version,
        )


@router.put("/profile/preferences", response_model=PreferencesOut, responses=_ERRORS)
def remember_preferences(
    body: PreferencesUpdate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> PreferencesOut:
    """Only called when the user chooses "remember for future resumes"."""
    ensure_profile(settings, user.id)
    with user_db(settings, user) as db:
        profile = (
            db.execute(
                update(Profile)
                .where(Profile.user_id == user.id, Profile.lock_version == body.expected_version)
                .values(
                    preferences=body.preferences.model_dump(mode="json"),
                    lock_version=Profile.lock_version + 1,
                    updated_at=func.now(),
                )
                .returning(Profile)
            )
            .scalars()
            .first()
        )
        if profile is None:
            raise ApiError(
                409, "conflict", "Your settings changed in another tab. Reload to continue."
            )
        return PreferencesOut(
            preferences=FormatPreferences.model_validate(profile.preferences),
            lock_version=profile.lock_version,
        )
