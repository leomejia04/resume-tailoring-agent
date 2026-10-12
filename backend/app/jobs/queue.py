"""The durable job queue in PostgreSQL (ADR 0007).

Requests enqueue jobs inside their own transaction. Workers claim them with
``app.claim_job`` (``FOR UPDATE SKIP LOCKED``, so several workers never take
the same job) and hold a lease they renew with heartbeats. Everything a job
does after claiming happens as its owner, under row-level security. Finishing
is fenced on ``locked_by``: a worker whose lease expired and was taken over
cannot overwrite the new attempt's outcome.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.config import Settings
from app.db.models import Job, JobGrant
from app.db.session import system_session, user_session

logger = logging.getLogger("app.jobs")

KIND_LANES = {
    "analyze": "workflow",
    "draft": "workflow",
    "regenerate": "workflow",
    "chat": "workflow",
    "improve": "workflow",
    "render": "render",
    "ats_check": "ats",
}
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class FollowUp:
    """A job queued when another succeeds, in the same transaction (so a crash
    cannot record the success and lose the follow-up)."""

    run_id: uuid.UUID
    kind: str
    params: dict[str, Any]
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class Claimed:
    job_id: uuid.UUID
    owner_id: uuid.UUID
    kind: str
    attempt: int


def enqueue(
    db: Session,
    settings: Settings,
    owner_id: uuid.UUID,
    run_id: uuid.UUID,
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    job_id: uuid.UUID | None = None,
) -> Job:
    """Queue a job in the caller's transaction; refuse duplicates and per-user overload."""
    # Serialize this owner's enqueues so the active-job count below cannot race.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"jobs:{owner_id}"}
    )
    active = db.scalar(select(func.count()).where(Job.owner_id == owner_id, Job.status.in_(ACTIVE)))
    if (active or 0) >= settings.max_active_jobs_per_user:
        raise ApiError(
            429, "too_many_jobs", "You have several tasks running. Wait for one to finish."
        )
    job = Job(
        id=job_id or uuid.uuid4(),
        owner_id=owner_id,
        run_id=run_id,
        kind=kind,
        lane=KIND_LANES[kind],
        status="queued",
        params=params or {},
        max_attempts=settings.job_max_attempts,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError as exc:  # uq_jobs_active_lane: same kind of work already active
        raise ApiError(409, "job_in_progress", "This is already in progress.") from exc
    return job


def add_grant(
    db: Session,
    job: Job,
    purpose: str,
    object_path: str,
    url: str,
    expires_at: datetime,
    attempt: int = 0,
) -> None:
    db.add(
        JobGrant(
            owner_id=job.owner_id,
            job_id=job.id,
            purpose=purpose,
            attempt=attempt,
            object_path=object_path,
            url=url,
            expires_at=expires_at,
        )
    )


def claim(settings: Settings, worker_id: str) -> Claimed | None:
    """Claim the next due job across all owners (the only cross-owner read)."""
    with system_session(settings) as db:
        row = db.execute(
            text("SELECT * FROM app.claim_job(:worker, :lease, :max_running)"),
            {
                "worker": worker_id,
                "lease": settings.job_lease_seconds,
                "max_running": settings.max_running_jobs_per_user,
            },
        ).first()
    if row is None:
        return None
    return Claimed(row.job_id, row.owner_id, row.kind, row.attempt)


def claim_own(settings: Settings, owner_id: uuid.UUID, job_id: uuid.UUID, worker_id: str) -> int:
    """Inline mode: the request claims its own job as the owner. Returns the attempt (0: none)."""
    with user_session(settings, owner_id) as db:
        attempt = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.owner_id == owner_id,
                Job.status == "queued",
                Job.attempts < Job.max_attempts,
            )
            .values(
                status="running",
                attempts=Job.attempts + 1,
                locked_by=worker_id[:64],
                lease_expires_at=func.now() + timedelta(seconds=settings.job_lease_seconds),
                heartbeat_at=func.now(),
                started_at=func.coalesce(Job.started_at, func.now()),
            )
            .returning(Job.attempts)
        )
    return attempt or 0


def heartbeat(
    settings: Settings, owner_id: uuid.UUID, job_id: uuid.UUID, worker_id: str
) -> tuple[bool, bool]:
    """Extend the lease. Returns (still ours, cancellation requested)."""
    with user_session(settings, owner_id) as db:
        cancel = db.scalar(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker_id[:64])
            .values(
                lease_expires_at=func.now() + timedelta(seconds=settings.job_lease_seconds),
                heartbeat_at=func.now(),
            )
            .returning(Job.cancel_requested)
        )
    return cancel is not None, bool(cancel)


def finish(
    settings: Settings,
    owner_id: uuid.UUID,
    job_id: uuid.UUID,
    worker_id: str,
    status: str,
    *,
    error_code: str | None = None,
    result: dict[str, Any] | None = None,
    retry_in: float | None = None,
    follow_up: FollowUp | None = None,
) -> bool:
    """Record an attempt's outcome if this worker still holds the job.

    ``status="queued"`` schedules a retry after ``retry_in`` seconds; terminal
    statuses also delete the job's storage grants. A ``follow_up`` is queued
    with a success; if limits refuse it, the success still stands.
    """
    values: dict[str, Any] = {
        "status": status,
        "lease_expires_at": None,
        "locked_by": None,
        "error_code": error_code,
    }
    if status == "queued":
        values["run_after"] = func.now() + timedelta(seconds=retry_in or 0)
    else:
        values |= {"finished_at": func.now(), "result": result}
    with user_session(settings, owner_id) as db:
        moved = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker_id[:64])
            .values(**values)
        ).rowcount
        if moved and status in TERMINAL:
            db.execute(delete(JobGrant).where(JobGrant.job_id == job_id))
        if moved and status == "succeeded" and follow_up is not None:
            try:
                enqueue(
                    db,
                    settings,
                    owner_id,
                    follow_up.run_id,
                    follow_up.kind,
                    follow_up.params,
                    job_id=follow_up.job_id,
                )
            except ApiError as exc:
                logger.info("follow-up skipped kind=%s code=%s", follow_up.kind, exc.code)
    return bool(moved)


def recover(settings: Settings) -> list[tuple[uuid.UUID, uuid.UUID, str, str]]:
    """Requeue (or fail, when out of attempts) running jobs whose lease expired."""
    with system_session(settings) as db:
        rows = db.execute(text("SELECT * FROM app.recover_expired_jobs()")).all()
    return [(r.job_id, r.owner_id, r.kind, r.status) for r in rows]


def release_expired_reservations(settings: Settings) -> int:
    with system_session(settings) as db:
        return int(db.scalar(text("SELECT app.release_expired_reservations()")) or 0)


def drop_grants(settings: Settings, owner_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with user_session(settings, owner_id) as db:
        db.execute(delete(JobGrant).where(JobGrant.job_id == job_id))


def cancel_for_run(db: Session, run_id: uuid.UUID) -> None:
    """Cancel queued jobs now; ask running ones to stop at their next checkpoint."""
    cancelled = db.scalars(
        update(Job)
        .where(Job.run_id == run_id, Job.status == "queued")
        .values(status="cancelled", finished_at=func.now())
        .returning(Job.id)
    ).all()
    if cancelled:
        db.execute(delete(JobGrant).where(JobGrant.job_id.in_(cancelled)))
    db.execute(
        update(Job)
        .where(Job.run_id == run_id, Job.status == "running")
        .values(cancel_requested=True)
    )


@dataclass(frozen=True)
class Grant:
    url: str  # relative signed URL; a capability, never log it
    object_path: str


class GrantExpiredError(RuntimeError):
    """The job waited longer than its storage permission lasts; the user must retry."""


def grant(
    settings: Settings, owner_id: uuid.UUID, job_id: uuid.UUID, purpose: str, attempt: int = 0
) -> Grant | None:
    """The job's signed URL for one purpose; raises GrantExpiredError past its expiry."""
    with user_session(settings, owner_id) as db:
        row = db.execute(
            select(JobGrant.url, JobGrant.object_path, JobGrant.expires_at).where(
                JobGrant.job_id == job_id,
                JobGrant.purpose == purpose,
                JobGrant.attempt == attempt,
            )
        ).first()
    if row is None:
        return None
    if row.expires_at <= datetime.now(UTC):
        raise GrantExpiredError(purpose)
    return Grant(row.url, row.object_path)


def latest_by_kind(db: Session, run_id: uuid.UUID) -> list[Job]:
    """The most recent job of each kind for a run (what the UI needs to show progress)."""
    jobs = db.scalars(
        select(Job).where(Job.run_id == run_id).order_by(Job.created_at.desc()).limit(60)
    ).all()
    latest: dict[str, Job] = {}
    for job in jobs:
        latest.setdefault(job.kind, job)
    return list(latest.values())
