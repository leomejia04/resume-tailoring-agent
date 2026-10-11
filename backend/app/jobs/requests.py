"""Request side of every background action (ADR 0007).

Each function validates ownership, state, and versions synchronously (so the
user gets an immediate 404/409), signs URLs for exactly the storage objects
the job will need using the user's own session, and queues the job in the
same transaction. Nothing slow or paid happens here; with JOB_RUNNER=inline
the job then runs before the response, through the same runner the worker uses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import client as ai_client
from app.api.errors import ApiError
from app.auth.dependencies import unavailable, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import Artifact, ChatMessage, Draft, DraftVersion, Job, TailoringRun
from app.jobs import queue, runner
from app.storage import (
    StorageObjectMissingError,
    StorageRejectedError,
    StorageUnavailableError,
    UserStorage,
)
from app.tailoring import review, runs

DRAFTABLE = {"awaiting_confirmation", "ready", "awaiting_review", "approved", "failed"}
# Supabase signed upload URLs last two hours; stop using them a little earlier.
UPLOAD_GRANT_SECONDS = 2 * 3600 - 300


def _require_ai(settings: Settings) -> None:
    try:
        configured = ai_client.client_for(settings) is not None
    except ai_client.LLMError as exc:
        raise ApiError(503, "ai_unavailable", "AI features are not configured.") from exc
    if not configured:
        raise ApiError(503, "ai_unavailable", "AI features are not configured on this server yet.")


def _dispatch(settings: Settings, owner_id: uuid.UUID, job_id: uuid.UUID) -> None:
    if settings.job_runner == "inline":
        runner.run_inline(settings, job_id, owner_id)


def _sign(action, *args):  # noqa: ANN001, ANN202 - small wrapper mapping storage errors
    try:
        return action(*args)
    except (StorageUnavailableError, StorageRejectedError) as exc:
        raise unavailable("File storage") from exc


def _grant_master(
    db: Session,
    settings: Settings,
    user: VerifiedUser,
    run: TailoringRun,
    job: Job,
    *,
    required: bool,
) -> None:
    master = runs.run_master(db, user, run)
    if master is None:
        return
    storage = UserStorage(settings, user.access_token)
    try:
        url = _sign(storage.sign_download, master.storage_path, settings.grant_ttl_seconds)
    except StorageObjectMissingError as exc:
        if required:
            raise ApiError(
                409, "master_missing", "Your master file could not be found. Upload it again."
            ) from exc
        return
    expires = datetime.now(UTC) + timedelta(seconds=settings.grant_ttl_seconds - 60)
    queue.add_grant(db, job, "master_read", master.storage_path, url, expires)


def start_analysis(
    settings: Settings,
    user: VerifiedUser,
    posting_id: uuid.UUID,
    master_version_id: uuid.UUID | None,
) -> uuid.UUID:
    _require_ai(settings)
    with user_db(settings, user) as db:
        run = runs.create_run(db, user, posting_id, master_version_id)
        job = queue.enqueue(db, settings, user.id, run.id, "analyze")
        _grant_master(db, settings, user, run, job, required=False)
        run_id, job_id = run.id, job.id
    _dispatch(settings, user.id, job_id)
    return run_id


def request_draft(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> None:
    _require_ai(settings)
    with user_db(settings, user) as db:
        run = runs._owned_run(db, user, run_id)
        if run.status not in DRAFTABLE or run.analysis_id is None:
            raise ApiError(409, "invalid_state", "This run can't be drafted right now.")
        if not runs._verified_rows(db, user):
            raise ApiError(
                409, "no_confirmed_facts", "Confirm at least one fact on the Experience page first."
            )
        job_id = queue.enqueue(db, settings, user.id, run_id, "draft").id
    _dispatch(settings, user.id, job_id)


def _current_version(db: Session, user: VerifiedUser, run_id: uuid.UUID, expected: int):  # noqa: ANN202
    run = runs._owned_run(db, user, run_id)
    draft, version, content = review._draft_state(db, user, run)
    if draft.current_version != expected:
        raise ApiError(
            409, "conflict", "This draft changed in another tab. Reload to see the latest version."
        )
    return run, version, content


def request_regenerate(
    settings: Settings, user: VerifiedUser, run_id: uuid.UUID, entry_ref: str, expected: int
) -> None:
    _require_ai(settings)
    with user_db(settings, user) as db:
        _, _, content = _current_version(db, user, run_id, expected)
        if entry_ref not in {e.ref for e in content.entries}:
            raise ApiError(404, "not_found", "That section of the draft was not found.")
        params = {"entry_ref": entry_ref, "expected_version": expected}
        job_id = queue.enqueue(db, settings, user.id, run_id, "regenerate", params).id
    _dispatch(settings, user.id, job_id)


def request_chat(
    settings: Settings, user: VerifiedUser, run_id: uuid.UUID, message: str, expected: int
) -> None:
    _require_ai(settings)
    with user_db(settings, user) as db:
        _current_version(db, user, run_id, expected)
        chat = ChatMessage(
            id=uuid.uuid4(), owner_id=user.id, run_id=run_id, role="user", content=message
        )
        params = {"message_id": str(chat.id), "expected_version": expected}
        job_id = queue.enqueue(db, settings, user.id, run_id, "chat", params).id
        db.add(chat)
    _dispatch(settings, user.id, job_id)


def request_render(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> None:
    """Render the version current now. Each attempt gets its own artifact paths,
    because signed uploads can create an object but never overwrite one."""
    with user_db(settings, user) as db:
        run = runs._owned_run(db, user, run_id)
        _, version, _ = review._draft_state(db, user, run)
        artifact_ids = [uuid.uuid4() for _ in range(settings.job_max_attempts)]
        params = {"version": version.version, "artifact_ids": [str(a) for a in artifact_ids]}
        job = queue.enqueue(db, settings, user.id, run_id, "render", params)
        _grant_master(db, settings, user, run, job, required=True)
        storage = UserStorage(settings, user.access_token, settings.artifacts_bucket)
        expires = datetime.now(UTC) + timedelta(seconds=UPLOAD_GRANT_SECONDS)
        for attempt, artifact_id in enumerate(artifact_ids, start=1):
            base = f"{user.id}/{version.id}/{artifact_id}"
            for purpose, path in (("pdf_upload", f"{base}.pdf"), ("docx_upload", f"{base}.docx")):
                url = _sign(storage.sign_upload, path)
                queue.add_grant(db, job, purpose, path, url, expires, attempt)
        job_id = job.id
    _dispatch(settings, user.id, job_id)


def request_ats_check(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> None:
    with user_db(settings, user) as db:
        run = runs._owned_run(db, user, run_id)
        if run.analysis_id is None:
            raise ApiError(409, "invalid_state", "Analyze the job posting first.")
        job = queue.enqueue(db, settings, user.id, run_id, "ats_check")
        _grant_master(db, settings, user, run, job, required=False)
        artifact = db.execute(
            select(Artifact.storage_path)
            .join(DraftVersion, DraftVersion.id == Artifact.draft_version_id)
            .join(Draft, Draft.id == DraftVersion.draft_id)
            .where(
                Draft.run_id == run_id,
                DraftVersion.version == Draft.current_version,
                Artifact.owner_id == user.id,
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        ).first()
        if artifact is not None:
            storage = UserStorage(settings, user.access_token, settings.artifacts_bucket)
            try:
                url = _sign(
                    storage.sign_download, artifact.storage_path, settings.grant_ttl_seconds
                )
            except StorageObjectMissingError:
                url = None  # the check records it as unreadable
            if url:
                expires = datetime.now(UTC) + timedelta(seconds=settings.grant_ttl_seconds - 60)
                queue.add_grant(db, job, "artifact_read", artifact.storage_path, url, expires)
        job_id = job.id
    _dispatch(settings, user.id, job_id)
