"""User-owned account deletion (ADR 0012).

Three steps, each safe to repeat if a later one fails:

1. Under the owner's job lock, refuse while any job is queued or running,
   then record the request (``app.account_deletions``). From then on no new
   job can be queued, and Storage lets the owner delete their own objects.
2. Delete every object under the owner's folder in both buckets with the
   user's own session (never a service key), and confirm none is left.
3. ``app.delete_current_account()`` deletes the auth user; foreign-key
   cascades remove every app row (masters, facts, jobs, drafts, reports,
   artifacts' rows, vectors, retrieval caches, tracker, usage).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.auth.dependencies import unavailable, user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import AccountDeletion, Job
from app.jobs.queue import ACTIVE, lock_owner_jobs
from app.storage import StorageRejectedError, StorageUnavailableError, UserStorage

logger = logging.getLogger("app.account")

JOBS_ACTIVE = ApiError(
    409,
    "jobs_active",
    "Some tasks are still running. Wait for them to finish or cancel them, then try again.",
)


def active_jobs(db: Session, owner_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).where(Job.owner_id == owner_id, Job.status.in_(ACTIVE)))


def delete_account(settings: Settings, user: VerifiedUser) -> None:
    with user_db(settings, user) as db:
        lock_owner_jobs(db, user.id)
        if active_jobs(db, user.id):
            raise JOBS_ACTIVE
        db.execute(insert(AccountDeletion).values(owner_id=user.id).on_conflict_do_nothing())

    try:
        for bucket in (settings.storage_bucket, settings.artifacts_bucket):
            storage = UserStorage(settings, user.access_token, bucket)
            paths = storage.list_tree(str(user.id))
            if paths:
                storage.delete(paths)
            if storage.list_tree(str(user.id)):
                raise StorageUnavailableError("objects remain after deletion")
    except (StorageUnavailableError, StorageRejectedError) as exc:
        logger.error("account deletion: file removal failed error=%s", type(exc).__name__)
        raise unavailable("Account deletion") from exc

    try:
        with user_db(settings, user) as db:
            deleted = db.scalar(text("SELECT app.delete_current_account()"))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55006":
            raise JOBS_ACTIVE from exc
        raise
    if not deleted:
        logger.warning("account deletion: auth user was already gone")
    logger.info("account deleted")
