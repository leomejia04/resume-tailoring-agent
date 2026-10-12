"""Manual application tracker (milestone 6e).

Each user keeps their own list of applications: company, role, posting link,
the approved resume they sent, date applied, status, and notes. Updates and
deletes name the version the client last saw, so a stale tab cannot
overwrite or remove a newer change. Nothing here calls an AI provider or
fetches the posting link.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import Date, cast, delete, func, select, text, update
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.auth.dependencies import current_user, get_app_settings, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import Application, Approval, Draft
from app.schemas.errors import ErrorResponse, FieldError
from app.schemas.tracker import (
    ApplicationInput,
    ApplicationOut,
    ApplicationStatus,
    ApplicationUpdate,
    TrackedResume,
)

router = APIRouter(prefix="/applications", tags=["tracker"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "That application was not found.")


def _stale() -> ApiError:
    return ApiError(
        409, "conflict", "This application changed in another tab. Reload to see the latest."
    )


def _query(user: VerifiedUser):
    return (
        select(Application, Approval, Draft.run_id)
        .outerjoin(Approval, Approval.id == Application.approval_id)
        .outerjoin(Draft, Draft.id == Approval.draft_id)
        .where(Application.owner_id == user.id)
    )


def _out(entry: Application, approval: Approval | None, run_id: uuid.UUID | None) -> ApplicationOut:
    resume = None
    if approval is not None and run_id is not None:
        resume = TrackedResume(
            approval_id=approval.id,
            run_id=run_id,
            filename=approval.filename,
            draft_version=approval.draft_version,
            approved_at=approval.approved_at,
        )
    return ApplicationOut(
        id=entry.id,
        company=entry.company,
        role=entry.role,
        job_url=entry.job_url,
        applied_on=entry.applied_on,
        status=entry.status,  # type: ignore[arg-type]
        notes=entry.notes,
        resume=resume,
        lock_version=entry.lock_version,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _load(db: Session, user: VerifiedUser, application_id: uuid.UUID) -> ApplicationOut:
    row = db.execute(_query(user).where(Application.id == application_id)).first()
    if row is None:
        raise _not_found()
    return _out(*row)


def _check_approval(db: Session, user: VerifiedUser, approval_id: uuid.UUID | None) -> None:
    """A clear field error instead of a foreign-key failure for unknown or foreign IDs."""
    if approval_id is None:
        return
    found = db.scalar(
        select(Approval.id).where(Approval.id == approval_id, Approval.owner_id == user.id)
    )
    if found is None:
        raise ApiError(
            422,
            "validation_error",
            "Some fields are missing or invalid.",
            fields=[
                FieldError(
                    loc=["body", "approval_id"],
                    message="Choose one of your approved resumes.",
                    type="approval_not_found",
                )
            ],
        )


def _values(body: ApplicationInput) -> dict:
    return {
        "company": body.company,
        "role": body.role,
        "job_url": str(body.job_url) if body.job_url else None,
        "approval_id": body.approval_id,
        "applied_on": body.applied_on,
        "status": body.status,
        "notes": body.notes,
    }


@router.get("", response_model=list[ApplicationOut], responses=_ERRORS)
def list_applications(
    application_status: ApplicationStatus | None = Query(default=None, alias="status"),
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[ApplicationOut]:
    """Your applications, most recent first (by date applied, else date added)."""
    query = _query(user)
    if application_status is not None:
        query = query.where(Application.status == application_status)
    query = query.order_by(
        func.coalesce(Application.applied_on, cast(Application.created_at, Date)).desc(),
        Application.created_at.desc(),
    )
    with user_db(settings, user) as db:
        return [_out(*row) for row in db.execute(query).all()]


@router.post(
    "", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED, responses=_ERRORS
)
def create_application(
    body: ApplicationInput,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> ApplicationOut:
    with user_db(settings, user) as db:
        # Serialize this user's inserts so two requests cannot both pass the count.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"applications:{user.id}"},
        )
        count = db.scalar(
            select(func.count()).select_from(Application).where(Application.owner_id == user.id)
        )
        if (count or 0) >= settings.max_applications_per_user:
            raise ApiError(
                409,
                "tracker_full",
                f"You can track up to {settings.max_applications_per_user} applications. "
                "Delete old entries to add more.",
            )
        _check_approval(db, user, body.approval_id)
        application = Application(owner_id=user.id, **_values(body))
        db.add(application)
        db.flush()
        return _load(db, user, application.id)


@router.get("/{application_id}", response_model=ApplicationOut, responses=_ERRORS)
def get_application(
    application_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> ApplicationOut:
    with user_db(settings, user) as db:
        return _load(db, user, application_id)


def _exists(db: Session, user: VerifiedUser, application_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(Application.id).where(
                Application.id == application_id, Application.owner_id == user.id
            )
        )
        is not None
    )


@router.put("/{application_id}", response_model=ApplicationOut, responses=_ERRORS)
def update_application(
    application_id: uuid.UUID,
    body: ApplicationUpdate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> ApplicationOut:
    """Replace every field of the version the client last saw."""
    with user_db(settings, user) as db:
        _check_approval(db, user, body.approval_id)
        moved = db.execute(
            update(Application)
            .where(
                Application.id == application_id,
                Application.owner_id == user.id,
                Application.lock_version == body.expected_version,
            )
            .values(
                **_values(body),
                lock_version=Application.lock_version + 1,
                updated_at=func.now(),
            )
        )
        if moved.rowcount != 1:
            raise _stale() if _exists(db, user, application_id) else _not_found()
        return _load(db, user, application_id)


@router.delete(
    "/{application_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ERRORS,
)
def delete_application(
    application_id: uuid.UUID,
    expected_version: int = Query(ge=1, description="lock_version the client last saw"),
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Permanently delete one entry. The approved resume it points at is kept."""
    with user_db(settings, user) as db:
        removed = db.execute(
            delete(Application).where(
                Application.id == application_id,
                Application.owner_id == user.id,
                Application.lock_version == expected_version,
            )
        )
        if removed.rowcount != 1:
            raise _stale() if _exists(db, user, application_id) else _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
