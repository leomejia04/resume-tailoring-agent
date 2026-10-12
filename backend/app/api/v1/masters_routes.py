"""Master resume uploads: immutable, versioned, private.

Upload order matters for partial failures: the database rows are written and
flushed inside an open transaction, then the file is stored, then the
transaction commits. If storage fails, the transaction rolls back and no row
exists. If the commit itself fails after storage succeeded, an unreferenced
object can remain; it is logged by ID for cleanup and is still private.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.api.errors import ApiError
from app.auth import rate_limit
from app.auth.dependencies import (
    current_user,
    enforce_limit,
    get_app_settings,
    unavailable,
    user_db,
)
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import EvidenceRecord, EvidenceVersion, MasterVersion, Profile
from app.documents.extract import ExtractionResult, extract_docx, extract_pdf
from app.documents.uploads import UploadRejectedError, validate_upload
from app.schemas.errors import ErrorResponse
from app.schemas.masters import MasterVersionOut, UploadResult
from app.storage import (
    StorageObjectMissingError,
    StorageRejectedError,
    StorageUnavailableError,
    UserStorage,
)

logger = logging.getLogger("app.masters")
router = APIRouter(prefix="/masters", tags=["masters"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 404, 413, 422, 429, 503)}


def _extract(kind: str, data: bytes) -> ExtractionResult:
    try:
        return extract_docx(data) if kind == "docx" else extract_pdf(data)
    except Exception:  # malformed-but-valid files must not fail the upload
        logger.warning("extraction failed for a %s upload", kind, exc_info=False)
        return ExtractionResult(
            [],
            ["We could not read the text of this file. You can still keep it as a version."],
            "failed",
        )


def _evidence_counts(db, owner_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(EvidenceRecord.master_version_id, func.count())
        .where(EvidenceRecord.owner_id == owner_id)
        .group_by(EvidenceRecord.master_version_id)
    )
    return dict(rows.all())


def _out(master: MasterVersion, count: int) -> MasterVersionOut:
    return MasterVersionOut(
        id=master.id,
        version_number=master.version_number,
        file_kind=master.file_kind,  # type: ignore[arg-type]
        size_bytes=master.size_bytes,
        sha256=master.sha256,
        original_filename=master.original_filename,
        extraction_status=master.extraction_status,  # type: ignore[arg-type]
        extraction_notes=list(master.extraction_notes or []),
        evidence_count=count,
        created_at=master.created_at,
    )


@router.post(
    "", response_model=UploadResult, status_code=status.HTTP_201_CREATED, responses=_ERRORS
)
def upload_master(
    response: Response,
    file: UploadFile = File(description="A .docx or .pdf resume"),
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> UploadResult:
    enforce_limit(settings, rate_limit.UPLOAD_USER, str(user.id))
    data = file.file.read(settings.max_upload_bytes + 1)
    try:
        upload = validate_upload(data, file.filename, settings.max_upload_bytes)
    except UploadRejectedError as exc:
        status_code = 413 if exc.code == "file_too_large" else 422
        raise ApiError(status_code, exc.code, exc.message) from exc

    extraction = _extract(upload.kind, data)
    master_id = uuid.uuid4()
    storage_path = f"{user.id}/{master_id}.{upload.kind}"
    stored = False
    try:
        with user_db(settings, user) as db:
            # Lock this user's profile row so concurrent uploads number versions safely.
            db.execute(insert(Profile).values(user_id=user.id).on_conflict_do_nothing())
            db.execute(select(Profile.user_id).where(Profile.user_id == user.id).with_for_update())

            existing = db.scalars(
                select(MasterVersion).where(
                    MasterVersion.owner_id == user.id, MasterVersion.sha256 == upload.sha256
                )
            ).first()
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                count = _evidence_counts(db, user.id).get(existing.id, 0)
                return UploadResult(master=_out(existing, count), duplicate=True)

            latest = db.scalar(
                select(func.max(MasterVersion.version_number)).where(
                    MasterVersion.owner_id == user.id
                )
            )
            master = MasterVersion(
                id=master_id,
                owner_id=user.id,
                version_number=(latest or 0) + 1,
                file_kind=upload.kind,
                content_type=upload.content_type,
                size_bytes=upload.size,
                sha256=upload.sha256,
                original_filename=upload.label,
                storage_path=storage_path,
                extraction_status=extraction.status,
                extraction_notes=extraction.notes,
            )
            db.add(master)
            db.flush()
            for candidate in extraction.candidates:
                record = EvidenceRecord(
                    id=uuid.uuid4(),
                    owner_id=user.id,
                    master_version_id=master_id,
                    position=candidate.position,
                    current_version=1,
                )
                db.add(record)
                db.flush()
                db.add(
                    EvidenceVersion(
                        evidence_id=record.id,
                        owner_id=user.id,
                        version=1,
                        text=candidate.text,
                        source_text=candidate.text,
                        section=candidate.section,
                        entry=candidate.entry,
                        date_text=candidate.date_text,
                        organization=candidate.organization,
                        location=candidate.location,
                        title=candidate.title,
                        bold_terms=candidate.bold_terms,
                        metrics=candidate.metrics,
                        experience_type="unclassified",
                        suggested_type=candidate.suggested_type,
                        verification_status="unverified",
                        change_source="extracted",
                    )
                )
            db.flush()

            UserStorage(settings, user.access_token).upload(storage_path, data, upload.content_type)
            stored = True
            db.refresh(master)
            result = UploadResult(master=_out(master, len(extraction.candidates)), duplicate=False)
    except (StorageUnavailableError, StorageRejectedError) as exc:
        logger.error("master upload storage failure master_id=%s", master_id)
        raise unavailable("File storage") from exc
    except Exception:
        if stored:
            logger.error("commit failed after storage write; orphan object master_id=%s", master_id)
        raise
    return result


@router.get("", response_model=list[MasterVersionOut], responses=_ERRORS)
def list_masters(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> list[MasterVersionOut]:
    with user_db(settings, user) as db:
        masters = db.scalars(
            select(MasterVersion)
            .where(MasterVersion.owner_id == user.id)
            .order_by(MasterVersion.version_number.desc())
        ).all()
        counts = _evidence_counts(db, user.id)
        return [_out(m, counts.get(m.id, 0)) for m in masters]


def _get_owned(db, user: VerifiedUser, master_id: uuid.UUID) -> MasterVersion:
    master = db.scalars(
        select(MasterVersion).where(
            MasterVersion.id == master_id, MasterVersion.owner_id == user.id
        )
    ).first()
    if master is None:
        # Same answer for "does not exist" and "belongs to someone else".
        raise ApiError(404, "not_found", "That resume version was not found.")
    return master


@router.get("/{master_id}", response_model=MasterVersionOut, responses=_ERRORS)
def get_master(
    master_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> MasterVersionOut:
    with user_db(settings, user) as db:
        master = _get_owned(db, user, master_id)
        return _out(master, _evidence_counts(db, user.id).get(master.id, 0))


@router.get(
    "/{master_id}/file", responses={**_ERRORS, 200: {"content": {"application/octet-stream": {}}}}
)
def download_master(
    master_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    with user_db(settings, user) as db:
        master = _get_owned(db, user, master_id)
        path, content_type, kind, label = (
            master.storage_path,
            master.content_type,
            master.file_kind,
            master.original_filename,
        )
    try:
        data = UserStorage(settings, user.access_token).download(path)
    except StorageObjectMissingError as exc:
        raise ApiError(404, "not_found", "That file is no longer available.") from exc
    except (StorageUnavailableError, StorageRejectedError) as exc:
        raise unavailable("File storage") from exc
    filename = label if label.lower().endswith(f".{kind}") else f"{label}.{kind}"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
