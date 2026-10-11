"""Experience bank: review, confirm, edit, or reject extracted facts.

Every change creates a new immutable version; nothing is overwritten. A
version can only be added on top of the version the client last saw.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, select, update

from app.api.errors import ApiError
from app.auth.dependencies import current_user, get_app_settings, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import EvidenceRecord, EvidenceVersion
from app.documents.extract import find_metrics
from app.schemas.errors import ErrorResponse
from app.schemas.evidence import EvidenceOut, EvidenceUpdate

router = APIRouter(prefix="/evidence", tags=["evidence"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 404, 409, 422, 503)}


def _out(record: EvidenceRecord, version: EvidenceVersion) -> EvidenceOut:
    return EvidenceOut(
        id=record.id,
        master_version_id=record.master_version_id,
        position=record.position,
        version=version.version,
        text=version.text,
        source_text=version.source_text,
        section=version.section,
        entry=version.entry,
        date_text=version.date_text,
        metrics=list(version.metrics or []),
        experience_type=version.experience_type,  # type: ignore[arg-type]
        suggested_type=version.suggested_type,  # type: ignore[arg-type]
        verification_status=version.verification_status,  # type: ignore[arg-type]
        change_source=version.change_source,  # type: ignore[arg-type]
        updated_at=version.created_at,
    )


def _current(user: VerifiedUser):
    return (
        select(EvidenceRecord, EvidenceVersion)
        .join(
            EvidenceVersion,
            and_(
                EvidenceVersion.evidence_id == EvidenceRecord.id,
                EvidenceVersion.version == EvidenceRecord.current_version,
            ),
        )
        .where(EvidenceRecord.owner_id == user.id)
    )


@router.get("", response_model=list[EvidenceOut], responses=_ERRORS)
def list_evidence(
    master_version_id: uuid.UUID | None = None,
    verification_status: Literal["unverified", "verified", "rejected"] | None = Query(
        default=None, alias="status"
    ),
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[EvidenceOut]:
    query = _current(user)
    if master_version_id is not None:
        query = query.where(EvidenceRecord.master_version_id == master_version_id)
    if verification_status is not None:
        query = query.where(EvidenceVersion.verification_status == verification_status)
    query = query.order_by(EvidenceRecord.master_version_id, EvidenceRecord.position)
    with user_db(settings, user) as db:
        return [_out(record, version) for record, version in db.execute(query).all()]


@router.get("/{evidence_id}/versions", response_model=list[EvidenceOut], responses=_ERRORS)
def evidence_history(
    evidence_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[EvidenceOut]:
    with user_db(settings, user) as db:
        record = db.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.id == evidence_id, EvidenceRecord.owner_id == user.id
            )
        ).first()
        if record is None:
            raise ApiError(404, "not_found", "That item was not found.")
        versions = db.scalars(
            select(EvidenceVersion)
            .where(EvidenceVersion.evidence_id == evidence_id, EvidenceVersion.owner_id == user.id)
            .order_by(EvidenceVersion.version.desc())
        ).all()
        return [_out(record, v) for v in versions]


@router.post(
    "/{evidence_id}/versions",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def add_evidence_version(
    evidence_id: uuid.UUID,
    body: EvidenceUpdate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> EvidenceOut:
    with user_db(settings, user) as db:
        row = db.execute(_current(user).where(EvidenceRecord.id == evidence_id)).first()
        if row is None:
            raise ApiError(404, "not_found", "That item was not found.")
        record, current = row
        stale = ApiError(
            409, "conflict", "This item changed in another tab. Reload to see the latest version."
        )
        if record.current_version != body.expected_version:
            raise stale
        # Conditional update: succeeds only if nobody added a version meanwhile.
        moved = db.execute(
            update(EvidenceRecord)
            .where(
                EvidenceRecord.id == evidence_id,
                EvidenceRecord.owner_id == user.id,
                EvidenceRecord.current_version == body.expected_version,
            )
            .values(current_version=body.expected_version + 1)
        )
        if moved.rowcount != 1:
            raise stale
        text = body.text if body.text is not None else current.text
        version = EvidenceVersion(
            evidence_id=evidence_id,
            owner_id=user.id,
            version=body.expected_version + 1,
            text=text,
            source_text=current.source_text,
            section=current.section,
            entry=current.entry,
            date_text=current.date_text,
            metrics=find_metrics(text),
            experience_type=body.experience_type,
            suggested_type=current.suggested_type,
            verification_status=body.verification_status,
            # Provenance: wording differs from the master means the user wrote it.
            change_source="extracted" if text == current.source_text else "user",
        )
        db.add(version)
        db.flush()
        db.refresh(record)
        db.refresh(version)
        return _out(record, version)
