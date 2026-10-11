"""Run one claimed job: heartbeat, execute, classify the outcome, then retry or finish.

Transient failures (model timeouts and rate limits, storage, converter, or
database hiccups) are retried with exponential backoff until the job's
attempts run out. Everything else fails at once with a short error code; the
handler's ``on_failure`` then leaves the run in an honest state. Logs carry
job IDs, kinds, and exception types only, never resume content or URLs.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.ai.client import LLMError
from app.api.errors import ApiError
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import Job, TailoringRun
from app.db.session import user_session
from app.documents.convert import ConversionError, ConversionUnavailableError
from app.jobs import queue
from app.jobs.handlers import HANDLERS, on_failure
from app.storage import StorageObjectMissingError, StorageRejectedError, StorageUnavailableError

logger = logging.getLogger("app.jobs")

RETRYABLE = frozenset(
    {
        "timeout",
        "unavailable",
        "rate_limited",
        "storage_unavailable",
        "converter_unavailable",
        "conversion_failed",
        "database_unavailable",
        "service_unavailable",
    }
)


class JobCancelledError(Exception):
    pass


def job_owner(owner_id: uuid.UUID) -> VerifiedUser:
    """The identity a job acts as: its owner, authorized by the session that queued it.

    It carries no token, so storage is reachable only through the job's grants;
    anything else fails closed.
    """
    return VerifiedUser(id=owner_id, email=None, access_token="")


@dataclass
class JobContext:
    settings: Settings
    job_id: uuid.UUID
    owner_id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    params: dict[str, Any]
    attempt: int
    max_attempts: int
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def owner(self) -> VerifiedUser:
        return job_owner(self.owner_id)

    def grant(self, purpose: str, attempt: int = 0) -> queue.Grant | None:
        return queue.grant(self.settings, self.owner_id, self.job_id, purpose, attempt)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise JobCancelledError


def classify(exc: BaseException) -> str:
    if isinstance(exc, JobCancelledError):
        return "cancelled"
    if isinstance(exc, LLMError):
        return exc.code
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, queue.GrantExpiredError):
        return "grant_expired"
    if isinstance(exc, StorageRejectedError):
        return "storage_rejected"  # e.g. an expired signed URL: retrying cannot help
    if isinstance(exc, (StorageUnavailableError, StorageObjectMissingError)):
        return "storage_unavailable"
    if isinstance(exc, ConversionUnavailableError):
        return "converter_unavailable"
    if isinstance(exc, ConversionError):
        return "conversion_failed"
    if isinstance(exc, OperationalError):
        return "database_unavailable"
    return "internal_error"


def _load(settings: Settings, job_id: uuid.UUID, owner_id: uuid.UUID, worker_id: str) -> Job | None:
    with user_session(settings, owner_id) as db:
        job = db.scalars(
            select(Job).where(
                Job.id == job_id, Job.status == "running", Job.locked_by == worker_id[:64]
            )
        ).first()
        if job is not None:
            db.expunge(job)
        return job


def _run_cancelled(settings: Settings, owner_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    with user_session(settings, owner_id) as db:
        return (
            db.scalar(select(TailoringRun.status).where(TailoringRun.id == run_id)) == "cancelled"
        )


def context_for(settings: Settings, job: Job) -> JobContext:
    return JobContext(
        settings=settings,
        job_id=job.id,
        owner_id=job.owner_id,
        run_id=job.run_id,
        kind=job.kind,
        params=dict(job.params or {}),
        attempt=job.attempts,
        max_attempts=job.max_attempts,
    )


def execute(settings: Settings, job_id: uuid.UUID, owner_id: uuid.UUID, worker_id: str) -> str:
    """Run a job this worker has claimed; returns the job's status afterwards."""
    job = _load(settings, job_id, owner_id, worker_id)
    if job is None:
        return "lost"
    ctx = context_for(settings, job)
    stop, lost = threading.Event(), threading.Event()

    def beat() -> None:
        while not stop.wait(max(settings.job_lease_seconds / 3, 1)):
            try:
                ours, cancel = queue.heartbeat(settings, owner_id, job_id, worker_id)
            except Exception as exc:  # a missed beat is fine; the lease covers two more
                logger.warning("heartbeat missed job_id=%s error=%s", job_id, type(exc).__name__)
                continue
            if cancel or not ours:
                ctx.cancel.set()
            if not ours:
                lost.set()
                return

    heart = threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True)
    heart.start()
    started = time.perf_counter()
    result: dict[str, Any] | None = None
    code: str | None = None
    try:
        ctx.check_cancelled()
        result = HANDLERS[ctx.kind](ctx)
    except Exception as exc:
        code = classify(exc)
        if code == "internal_error":
            logger.error(
                "job error kind=%s job_id=%s error=%s", ctx.kind, job_id, type(exc).__name__
            )
    finally:
        stop.set()
        heart.join()

    if lost.is_set():
        logger.warning("job lease lost kind=%s job_id=%s", ctx.kind, job_id)
        return "lost"
    if code is None:
        status = "succeeded"
        finished = queue.finish(settings, owner_id, job_id, worker_id, status, result=result)
    elif (
        code == "cancelled" or ctx.cancel.is_set() or _run_cancelled(settings, owner_id, ctx.run_id)
    ):
        status = "cancelled"
        finished = queue.finish(settings, owner_id, job_id, worker_id, status)
    elif code in RETRYABLE and ctx.attempt < ctx.max_attempts:
        status = "queued"
        delay = settings.job_retry_backoff_seconds * 2 ** (ctx.attempt - 1)
        finished = queue.finish(
            settings, owner_id, job_id, worker_id, status, error_code=code, retry_in=delay
        )
    else:
        status = "failed"
        finished = queue.finish(settings, owner_id, job_id, worker_id, status, error_code=code)
        if finished:
            on_failure(ctx, code)
    logger.info(
        "job %s kind=%s job_id=%s attempt=%s/%s code=%s duration_ms=%d",
        status if finished else "lost",
        ctx.kind,
        job_id,
        ctx.attempt,
        ctx.max_attempts,
        code,
        (time.perf_counter() - started) * 1000,
    )
    return status if finished else "lost"


def after_recovery(settings: Settings, job_id: uuid.UUID, owner_id: uuid.UUID, status: str) -> None:
    """A worker died holding this job. Clean up if the queue gave up on it."""
    if status not in ("failed", "cancelled"):
        return  # requeued: the next attempt runs normally
    queue.drop_grants(settings, owner_id, job_id)
    if status == "failed":
        with user_session(settings, owner_id) as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            db.expunge(job)
        on_failure(context_for(settings, job), "lease_expired")


def run_inline(settings: Settings, job_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    """JOB_RUNNER=inline: run the request's own job now, retrying immediately."""
    worker_id = f"inline-{uuid.uuid4().hex[:12]}"
    for _ in range(settings.job_max_attempts):
        if not queue.claim_own(settings, owner_id, job_id, worker_id):
            return
        if execute(settings, job_id, owner_id, worker_id) != "queued":
            return
