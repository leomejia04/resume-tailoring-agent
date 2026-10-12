"""Tailoring workflow as ordinary Python functions.

Each step uses short database transactions and never holds one open during a
model call: reserve budget and record the new state, commit, call the model,
then settle cost and store results in a new transaction. Run states and legal
transitions are explicit, and the steps run in the durable worker (ADR 0007):
requests create runs and queue jobs; ``analyze_run`` and ``generate_draft``
resume safely when a crashed attempt is retried. Errors propagate to the job
runner, which decides between a bounded retry and marking the run failed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.budget import BudgetExceededError, reserve, settle
from app.ai.client import LLMClient, LLMError, LLMResult, user_hash
from app.ai.pricing import Usage, cost_usd, worst_case_cost_usd
from app.ai.schemas import JobAnalysisOut, TailoredDraftOut
from app.api.errors import ApiError
from app.ats import reports
from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import (
    ConfirmationQuestion,
    Draft,
    DraftVersion,
    EvidenceRecord,
    EvidenceVersion,
    JobAnalysis,
    JobPosting,
    JobPostingVersion,
    MasterVersion,
    Profile,
    TailoringRun,
)
from app.documents.extract import find_metrics, plain_text
from app.jobs import queue
from app.render.format import FormatPreferences
from app.retrieval.service import retrieve_for_draft
from app.storage import (
    SignedObject,
    StorageObjectMissingError,
    StorageRejectedError,
    StorageUnavailableError,
)
from app.tailoring.analysis import find_gaps, normalize_analysis, question_for
from app.tailoring.context import build_context, draft_payload, subset_context
from app.tailoring.models import AnalysisContent
from app.tailoring.validate import check, contains_term, finalize

logger = logging.getLogger("app.tailoring")
T = TypeVar("T", bound=BaseModel)
# Character budget for the "every confirmed fact" view used for omissions.
ALL_FACTS = 10**9

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"analyzing", "failed", "cancelled"},
    "analyzing": {"awaiting_confirmation", "ready", "failed", "cancelled"},
    "awaiting_confirmation": {"ready", "drafting", "cancelled"},
    "ready": {"drafting", "cancelled"},
    "drafting": {"validating", "failed", "cancelled"},
    # A retried draft job resumes from validating if the last attempt crashed there.
    "validating": {"awaiting_review", "drafting", "failed", "cancelled"},
    "awaiting_review": {"drafting", "approved", "cancelled"},
    # A new draft version after approval returns to review; drafting again is allowed.
    "approved": {"drafting", "awaiting_review", "cancelled"},
    "failed": {"analyzing", "drafting", "cancelled"},
    "cancelled": set(),
}


def _not_found(what: str = "That item") -> ApiError:
    return ApiError(404, "not_found", f"{what} was not found.")


def transition(
    db: Session, run: TailoringRun, new_status: str, error_code: str | None = None
) -> None:
    """Move a run to a new state if legal; conditional on lock_version (no stale writers)."""
    if new_status not in TRANSITIONS[run.status]:
        raise ApiError(
            409, "invalid_state", f"This run cannot go from {run.status} to {new_status}."
        )
    moved = db.execute(
        update(TailoringRun)
        .where(TailoringRun.id == run.id, TailoringRun.lock_version == run.lock_version)
        .values(
            status=new_status,
            error_code=error_code,
            lock_version=run.lock_version + 1,
            updated_at=func.now(),
        )
    )
    if moved.rowcount != 1:
        raise ApiError(409, "conflict", "This run changed in another tab. Reload to continue.")
    run.status, run.error_code, run.lock_version = new_status, error_code, run.lock_version + 1


def _owned_run(db: Session, user: VerifiedUser, run_id: uuid.UUID) -> TailoringRun:
    run = db.scalars(
        select(TailoringRun).where(TailoringRun.id == run_id, TailoringRun.owner_id == user.id)
    ).first()
    if run is None:
        raise _not_found("That tailoring run")
    return run


def run_master(db: Session, user: VerifiedUser, run: TailoringRun) -> MasterVersion | None:
    """The master a run renders from: the one chosen, else the newest."""
    query = select(MasterVersion).where(MasterVersion.owner_id == user.id)
    if run.master_version_id:
        query = query.where(MasterVersion.id == run.master_version_id)
    return db.scalars(query.order_by(MasterVersion.version_number.desc())).first()


def _verified_rows(db: Session, user: VerifiedUser) -> list[tuple[EvidenceRecord, EvidenceVersion]]:
    rows = db.execute(
        select(EvidenceRecord, EvidenceVersion)
        .join(
            EvidenceVersion,
            and_(
                EvidenceVersion.evidence_id == EvidenceRecord.id,
                EvidenceVersion.version == EvidenceRecord.current_version,
            ),
        )
        .where(
            EvidenceRecord.owner_id == user.id, EvidenceVersion.verification_status == "verified"
        )
    ).all()
    return [(r, v) for r, v in rows]


# --- Paid calls ----------------------------------------------------------------


def call_model[T: BaseModel](
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    *,
    run_id: uuid.UUID,
    operation: str,
    prompt_version: str,
    instructions: str,
    payload: dict[str, Any],
    schema: type[T],
    max_output_tokens: int,
) -> LLMResult[T]:
    """Reserve worst-case cost, call the model, then settle actual cost and log usage."""
    input_chars = len(instructions) + len(json.dumps(payload, ensure_ascii=False))
    estimate = (
        worst_case_cost_usd(input_chars, max_output_tokens, settings)
        if client.provider == "openai"
        else Decimal(0)
    )
    try:
        with user_db(settings, user) as db:
            reservation = reserve(db, settings, user.id, estimate)
    except BudgetExceededError as exc:
        scope = "your daily" if exc.scope == "user" else "today's application"
        raise ApiError(
            429,
            "budget_exceeded",
            f"This would exceed {scope} AI spending limit. Try again tomorrow.",
        ) from exc

    def record(usage, outcome: str, latency_ms: int, model: str) -> None:
        with user_db(settings, user) as db:
            settle(
                db,
                user.id,
                reservation,
                cost_usd(usage, settings),
                usage=usage,
                run_id=run_id,
                operation=operation,
                model=model,
                prompt_version=prompt_version,
                latency_ms=latency_ms,
                outcome=outcome,
            )

    try:
        result = client.parse(
            instructions=instructions,
            payload=payload,
            schema=schema,
            max_output_tokens=max_output_tokens,
            user_hash=user_hash(user.id),
        )
    except LLMError as exc:
        record(exc.usage or Usage(), exc.code, 0, client.model)
        raise
    record(result.usage, "success", result.latency_ms, result.model)
    return result


def fail_run(settings: Settings, user: VerifiedUser, run_id: uuid.UUID, code: str) -> None:
    """Mark a run failed after its job gave up (no-op if the run moved on or was cancelled)."""
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if "failed" in TRANSITIONS[run.status]:
            transition(db, run, "failed", code[:64])


# --- Jobs ----------------------------------------------------------------------


def create_job(
    settings: Settings,
    user: VerifiedUser,
    *,
    title: str | None,
    company: str | None,
    source_url: str | None,
    description: str,
) -> tuple[JobPosting, JobPostingVersion]:
    with user_db(settings, user) as db:
        posting = JobPosting(
            id=uuid.uuid4(), owner_id=user.id, title=title, company=company, source_url=source_url
        )
        db.add(posting)
        db.flush()
        version = JobPostingVersion(
            id=uuid.uuid4(),
            posting_id=posting.id,
            owner_id=user.id,
            version=1,
            description=description,
            description_sha256=hashlib.sha256(description.encode()).hexdigest(),
        )
        db.add(version)
        db.flush()
        db.refresh(posting)
        db.refresh(version)
        return posting, version


# --- Runs ----------------------------------------------------------------------


def _ensure_analysis(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    posting: JobPosting,
    version: JobPostingVersion,
) -> JobAnalysis:
    with user_db(settings, user) as db:
        cached = db.scalars(
            select(JobAnalysis).where(
                JobAnalysis.owner_id == user.id,
                JobAnalysis.posting_version_id == version.id,
                JobAnalysis.model == client.model,
                JobAnalysis.prompt_version == prompts.JOB_ANALYSIS_VERSION,
            )
        ).first()
        if cached is not None:
            return cached  # analyze each posting version once per model and prompt
    result = call_model(
        settings,
        user,
        client,
        run_id=run_id,
        operation="job_analysis",
        prompt_version=prompts.JOB_ANALYSIS_VERSION,
        instructions=prompts.JOB_ANALYSIS_INSTRUCTIONS,
        payload={
            "job_title": posting.title,
            "company": posting.company,
            "job_description": version.description,
        },
        schema=JobAnalysisOut,
        max_output_tokens=settings.analysis_max_output_tokens,
    )
    try:
        content = normalize_analysis(result.output, version.description)
    except ValidationError as exc:
        raise LLMError("invalid_output") from exc
    with user_db(settings, user) as db:
        analysis = JobAnalysis(
            id=uuid.uuid4(),
            owner_id=user.id,
            posting_version_id=version.id,
            model=client.model,
            prompt_version=prompts.JOB_ANALYSIS_VERSION,
            content=content.model_dump(mode="json"),
        )
        db.add(analysis)
        db.flush()
        return analysis


def create_run(
    db: Session,
    user: VerifiedUser,
    posting_id: uuid.UUID,
    master_version_id: uuid.UUID | None,
) -> TailoringRun:
    """Request side: a queued run for the posting's current version (analysis is a job)."""
    posting = db.scalars(
        select(JobPosting).where(JobPosting.id == posting_id, JobPosting.owner_id == user.id)
    ).first()
    if posting is None:
        raise _not_found("That job posting")
    if (
        master_version_id is not None
        and db.scalar(
            select(MasterVersion.id).where(
                MasterVersion.id == master_version_id, MasterVersion.owner_id == user.id
            )
        )
        is None
    ):
        raise _not_found("That resume version")
    version_id = db.scalars(
        select(JobPostingVersion.id).where(
            JobPostingVersion.posting_id == posting.id,
            JobPostingVersion.owner_id == user.id,
            JobPostingVersion.version == posting.current_version,
        )
    ).one()
    run = TailoringRun(
        id=uuid.uuid4(),
        owner_id=user.id,
        posting_version_id=version_id,
        master_version_id=master_version_id,
        status="queued",
    )
    db.add(run)
    db.flush()
    return run


def analyze_run(
    settings: Settings,
    user: VerifiedUser,
    client: LLMClient,
    run_id: uuid.UUID,
    master_grant: queue.Grant | None = None,
) -> str:
    """Worker side: analyze the posting, ask about gaps, and score the master baseline."""
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if run.status == "queued":
            transition(db, run, "analyzing")
        elif run.status != "analyzing":  # "analyzing" means a crashed attempt is resuming
            raise ApiError(409, "invalid_state", "This run is not waiting for analysis.")
        posting = db.scalars(
            select(JobPosting)
            .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
            .where(JobPostingVersion.id == run.posting_version_id)
        ).one()
        version = db.scalars(
            select(JobPostingVersion).where(JobPostingVersion.id == run.posting_version_id)
        ).one()
        db.expunge(posting)
        db.expunge(version)

    analysis = _ensure_analysis(settings, user, client, run_id, posting, version)
    content = AnalysisContent.model_validate(analysis.content)
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if run.status != "analyzing":  # cancelled while the model was answering
            raise ApiError(409, "invalid_state", "This run changed while it was analyzed.")
        db.execute(
            update(TailoringRun).where(TailoringRun.id == run_id).values(analysis_id=analysis.id)
        )
        verified_text = build_context(_verified_rows(db, user), settings.max_context_chars).all_text
        # Terms the user already said they have not used stay gaps but are not asked again.
        declined = {
            term.lower()
            for term in db.scalars(
                select(ConfirmationQuestion.term).where(
                    ConfirmationQuestion.owner_id == user.id,
                    ConfirmationQuestion.status == "declined",
                )
            )
        }
        gaps = [g for g in find_gaps(content, verified_text) if g.term.lower() not in declined]
        for gap in gaps:
            db.add(
                ConfirmationQuestion(
                    owner_id=user.id,
                    run_id=run_id,
                    requirement_id=gap.requirement.id,
                    requirement_text=gap.requirement.text[:500],
                    term=gap.term[:100],
                    question=question_for(gap)[:500],
                )
            )
        transition(db, run, "awaiting_confirmation" if gaps else "ready")
        status = run.status
    ensure_baseline(settings, user, run_id, master_grant)
    return status


def ensure_baseline(
    settings: Settings, user: VerifiedUser, run_id: uuid.UUID, master_grant: queue.Grant | None
) -> None:
    """Score the run's master once, as the baseline to compare drafts with.

    Reads the master through the job's signed URL, and only if that URL is for
    the master the run uses now. Informational only: a storage or parsing
    problem is stored as a failed report (shown with a retry), never as a
    failed run.
    """
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        inputs = reports.scoring_inputs(db, run)
        master = run_master(db, user, run)
        if (
            inputs is None
            or master is None
            or master_grant is None
            or master_grant.object_path != master.storage_path
            or not reports.needs_check(db, run, "master_baseline", master.id)
        ):
            return
        kind, master_id = master.file_kind, master.id
    text, error = None, None
    try:
        text = plain_text(SignedObject(settings, master_grant.url).download(), kind)
    except (StorageUnavailableError, StorageRejectedError, StorageObjectMissingError):
        error = "storage_unavailable"
    except Exception as exc:  # an upload that passed validation but cannot be re-read
        logger.error("master text failed run_id=%s error=%s", run_id, type(exc).__name__)
        error = "scoring_error"
    with user_db(settings, user) as db:
        reports.record_master(db, inputs, master_id, text, error)


def answer_question(
    settings: Settings,
    user: VerifiedUser,
    run_id: uuid.UUID,
    question_id: uuid.UUID,
    *,
    answer: str,
    experience_type: str | None,
    entry_evidence_id: uuid.UUID | None,
    description: str | None,
) -> ConfirmationQuestion:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        question = db.scalars(
            select(ConfirmationQuestion).where(
                ConfirmationQuestion.id == question_id,
                ConfirmationQuestion.run_id == run.id,
                ConfirmationQuestion.owner_id == user.id,
            )
        ).first()
        if question is None:
            raise _not_found("That question")
        if question.status != "open":
            raise ApiError(409, "conflict", "This question was already answered.")

        evidence_id = None
        if answer == "yes":
            if not description or not contains_term(description, question.term):
                raise ApiError(
                    422,
                    "validation_error",
                    f"Describe what you did with {question.term}, mentioning it by name.",
                )
            section = entry = date_text = None
            if entry_evidence_id is not None:
                row = db.execute(
                    select(EvidenceRecord, EvidenceVersion)
                    .join(
                        EvidenceVersion,
                        and_(
                            EvidenceVersion.evidence_id == EvidenceRecord.id,
                            EvidenceVersion.version == EvidenceRecord.current_version,
                        ),
                    )
                    .where(
                        EvidenceRecord.id == entry_evidence_id, EvidenceRecord.owner_id == user.id
                    )
                ).first()
                if row is None or row[1].verification_status != "verified":
                    raise _not_found("That experience entry")
                section, entry, date_text = row[1].section, row[1].entry, row[1].date_text
            position = (
                db.scalar(
                    select(func.count()).where(
                        EvidenceRecord.owner_id == user.id, EvidenceRecord.origin == "confirmation"
                    )
                )
                or 0
            )
            record = EvidenceRecord(
                id=uuid.uuid4(),
                owner_id=user.id,
                master_version_id=None,
                origin="confirmation",
                position=10_000 + position,
                current_version=1,
            )
            db.add(record)
            db.flush()
            db.add(
                EvidenceVersion(
                    evidence_id=record.id,
                    owner_id=user.id,
                    version=1,
                    text=description,
                    source_text=description,
                    section=section,
                    entry=entry,
                    date_text=date_text,
                    metrics=find_metrics(description),
                    experience_type=experience_type or "other",
                    suggested_type=experience_type or "other",
                    verification_status="verified",
                    change_source="confirmation",
                )
            )
            evidence_id = record.id
        new_status = {"yes": "confirmed", "no": "declined", "skip": "dismissed"}[answer]
        db.execute(
            update(ConfirmationQuestion)
            .where(ConfirmationQuestion.id == question.id)
            .values(status=new_status, evidence_id=evidence_id, answered_at=func.now())
        )
        db.flush()
        remaining = db.scalar(
            select(func.count()).where(
                ConfirmationQuestion.run_id == run.id, ConfirmationQuestion.status == "open"
            )
        )
        if remaining == 0 and run.status == "awaiting_confirmation":
            transition(db, run, "ready")
        db.refresh(question)
        return question


def generate_draft(
    settings: Settings, user: VerifiedUser, client: LLMClient, run_id: uuid.UUID
) -> int:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        if run.analysis_id is None:
            raise ApiError(409, "invalid_state", "Analyze the job posting before drafting.")
        analysis_id = run.analysis_id
        rows = _verified_rows(db, user)
        for record, version in rows:
            db.expunge(record)
            db.expunge(version)
        if not rows:
            raise ApiError(
                409, "no_confirmed_facts", "Confirm at least one fact on the Experience page first."
            )
        analysis = AnalysisContent.model_validate(
            db.scalars(select(JobAnalysis.content).where(JobAnalysis.id == analysis_id)).one()
        )
        posting = db.execute(
            select(JobPosting.title, JobPosting.company)
            .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
            .where(JobPostingVersion.id == run.posting_version_id)
        ).one()
        if run.status != "drafting":  # "drafting": a crashed attempt is resuming
            transition(db, run, "drafting")

    # Which facts the model reads: all of them, or a retrieved selection (ADR 0010).
    retrieved = retrieve_for_draft(settings, user, run_id, analysis_id, analysis, rows)
    everything = build_context(rows, ALL_FACTS)
    if retrieved.mode == "full":
        context = build_context(rows, settings.max_context_chars)
    else:
        context = subset_context(everything, retrieved.selected)
    payload = draft_payload(context, analysis, posting.title, posting.company)
    common = {
        "run_id": run_id,
        "prompt_version": prompts.TAILOR_VERSION,
        "schema": TailoredDraftOut,
        "max_output_tokens": settings.draft_max_output_tokens,
    }
    result = call_model(
        settings,
        user,
        client,
        operation="draft",
        instructions=prompts.TAILOR_INSTRUCTIONS,
        payload=payload,
        **common,
    )
    output = result.output
    checked = check(output, context, analysis)
    repaired = False
    errors = [i for i in checked.issues if i.severity == "error"]
    if errors:  # one bounded repair attempt, then remove whatever still fails
        repair_payload = {
            **payload,
            "previous_draft": output.model_dump(),
            "problems": [i.model_dump() for i in errors][:50],
        }
        result = call_model(
            settings,
            user,
            client,
            operation="draft_repair",
            instructions=prompts.TAILOR_INSTRUCTIONS + prompts.REPAIR_SUFFIX,
            payload=repair_payload,
            **common,
        )
        output = result.output
        checked = check(output, context, analysis)
        repaired = True

    content, report = finalize(output, context, analysis, checked, repaired, everything)
    with user_db(settings, user) as db:
        remembered = db.scalar(select(Profile.preferences).where(Profile.user_id == user.id))
        if remembered:
            content.format = FormatPreferences.model_validate(remembered)
        run = _owned_run(db, user, run_id)
        transition(db, run, "validating")
        draft = db.scalars(
            select(Draft).where(Draft.run_id == run_id, Draft.owner_id == user.id)
        ).first()
        if draft is None:
            draft = Draft(id=uuid.uuid4(), owner_id=user.id, run_id=run_id, current_version=1)
            db.add(draft)
            db.flush()
            number = 1
        else:
            number = draft.current_version + 1
            db.execute(update(Draft).where(Draft.id == draft.id).values(current_version=number))
        version_id = uuid.uuid4()
        db.add(
            DraftVersion(
                id=version_id,
                draft_id=draft.id,
                owner_id=user.id,
                version=number,
                content=content.model_dump(mode="json"),
                validation=report.model_dump(mode="json"),
                created_by="repair" if repaired else "generation",
                model=result.model,
                prompt_version=prompts.TAILOR_VERSION,
                retrieval_id=retrieved.retrieval_id,
            )
        )
        db.flush()
        inputs = reports.scoring_inputs(db, run)
        if inputs is not None:
            reports.record_draft(db, inputs, version_id, content)
        transition(db, run, "awaiting_review")
        return number


def cancel_run(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> None:
    with user_db(settings, user) as db:
        run = _owned_run(db, user, run_id)
        transition(db, run, "cancelled")
        queue.cancel_for_run(db, run_id)
