"""Jobs, tailoring runs, contextual confirmations, and drafts.

Model calls run synchronously in the request for this baseline; the durable
worker in milestone 6 will call the same workflow functions. Paid routes are
rate-limited per user and protected by the atomic daily spending limits.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, func, select

from app.ai.budget import today
from app.ai.client import LLMClient, LLMError, client_for
from app.api.errors import ApiError
from app.auth import rate_limit
from app.auth.dependencies import current_user, enforce_limit, get_app_settings, user_db
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
    SpendUser,
    TailoringRun,
    UsageEvent,
)
from app.schemas.errors import ErrorResponse
from app.schemas.tailoring import (
    AIStatus,
    DraftOut,
    JobCreate,
    JobOut,
    QuestionAnswer,
    QuestionOut,
    RunCreate,
    RunOut,
    RunSummary,
    RunUsage,
    SourceFact,
)
from app.tailoring import runs
from app.tailoring.models import AnalysisContent, DraftContent, ValidationReport

router = APIRouter(tags=["tailoring"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 404, 409, 422, 429, 503)}

DISCLOSURE = (
    "To analyze a posting and draft your resume, we send the job posting text and the facts "
    "you confirmed on the Experience page (with their job or project lines and dates) to "
    "OpenAI. We do not send your name, email, phone number, or files. Requests are sent with "
    "storage turned off; OpenAI states that API data is not used to train its models by "
    "default and that abuse-monitoring logs may be kept for up to 30 days."
)


def _client(settings: Settings) -> LLMClient:
    try:
        client = client_for(settings)
    except LLMError as exc:
        raise ApiError(503, "ai_unavailable", "AI features are not configured.") from exc
    if client is None:
        raise ApiError(503, "ai_unavailable", "AI features are not configured on this server yet.")
    return client


def _job_out(posting: JobPosting) -> JobOut:
    return JobOut(
        id=posting.id,
        title=posting.title,
        company=posting.company,
        source_url=posting.source_url,
        version=posting.current_version,
        created_at=posting.created_at,
    )


@router.get("/ai/status", response_model=AIStatus, responses=_ERRORS)
def ai_status(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> AIStatus:
    provider = settings.effective_llm_provider
    with user_db(settings, user) as db:
        spent = db.scalar(
            select(SpendUser.spent_usd).where(
                SpendUser.owner_id == user.id, SpendUser.day == today()
            )
        )
    return AIStatus(
        provider=provider,  # type: ignore[arg-type]
        model=None
        if provider == "disabled"
        else ("demo-deterministic-v1" if provider == "demo" else settings.openai_model),
        reasoning_effort=settings.openai_reasoning_effort if provider == "openai" else None,
        disclosure=DISCLOSURE,
        user_daily_budget_usd=settings.user_daily_budget_usd,
        spent_today_usd=spent or Decimal(0),
    )


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
def create_job(
    body: JobCreate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> JobOut:
    posting, _ = runs.create_job(
        settings,
        user,
        title=body.title,
        company=body.company,
        source_url=str(body.source_url) if body.source_url else None,
        description=body.description,
    )
    return _job_out(posting)


@router.get("/jobs", response_model=list[JobOut], responses=_ERRORS)
def list_jobs(
    user: VerifiedUser = Depends(current_user), settings: Settings = Depends(get_app_settings)
) -> list[JobOut]:
    with user_db(settings, user) as db:
        postings = db.scalars(
            select(JobPosting)
            .where(JobPosting.owner_id == user.id)
            .order_by(JobPosting.created_at.desc())
        ).all()
        return [_job_out(p) for p in postings]


def _run_out(db, user: VerifiedUser, run: TailoringRun) -> RunOut:
    posting = db.execute(
        select(JobPosting)
        .join(JobPostingVersion, JobPostingVersion.posting_id == JobPosting.id)
        .where(JobPostingVersion.id == run.posting_version_id, JobPosting.owner_id == user.id)
    ).scalar_one()
    analysis_row = (
        db.execute(select(JobAnalysis).where(JobAnalysis.id == run.analysis_id)).scalar_one()
        if run.analysis_id
        else None
    )
    questions = db.scalars(
        select(ConfirmationQuestion)
        .where(ConfirmationQuestion.run_id == run.id, ConfirmationQuestion.owner_id == user.id)
        .order_by(ConfirmationQuestion.created_at, ConfirmationQuestion.requirement_id)
    ).all()
    draft_out = None
    draft = db.scalars(
        select(Draft).where(Draft.run_id == run.id, Draft.owner_id == user.id)
    ).first()
    if draft is not None:
        version = db.scalars(
            select(DraftVersion).where(
                DraftVersion.draft_id == draft.id, DraftVersion.version == draft.current_version
            )
        ).one()
        content = DraftContent.model_validate(version.content)
        cited = {i for e in content.entries for b in e.bullets for i in b.evidence_ids}
        cited |= {i for s in content.skills for i in s.evidence_ids}
        sources = {}
        if cited:
            for record, ev in db.execute(
                select(EvidenceRecord, EvidenceVersion)
                .join(
                    EvidenceVersion,
                    and_(
                        EvidenceVersion.evidence_id == EvidenceRecord.id,
                        EvidenceVersion.version == EvidenceRecord.current_version,
                    ),
                )
                .where(EvidenceRecord.owner_id == user.id, EvidenceRecord.id.in_(cited))
            ):
                sources[record.id] = SourceFact(
                    text=ev.text, entry=ev.entry, experience_type=ev.experience_type
                )
        draft_out = DraftOut(
            version=version.version,
            content=content,
            validation=ValidationReport.model_validate(version.validation),
            created_by=version.created_by,
            model=version.model,
            prompt_version=version.prompt_version,
            created_at=version.created_at,
            sources=sources,
        )
    usage = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        ).where(UsageEvent.run_id == run.id, UsageEvent.owner_id == user.id)
    ).one()
    return RunOut(
        id=run.id,
        status=run.status,
        error_code=run.error_code,
        lock_version=run.lock_version,
        job=_job_out(posting),
        master_version_id=run.master_version_id,
        analysis=AnalysisContent.model_validate(analysis_row.content) if analysis_row else None,
        analysis_model=analysis_row.model if analysis_row else None,
        questions=[
            QuestionOut(
                id=q.id,
                requirement_id=q.requirement_id,
                requirement_text=q.requirement_text,
                term=q.term,
                question=q.question,
                status=q.status,  # type: ignore[arg-type]
                evidence_id=q.evidence_id,
            )
            for q in questions
        ],
        draft=draft_out,
        usage=RunUsage(
            calls=usage[0],
            cost_usd=Decimal(usage[1]),
            input_tokens=usage[2],
            output_tokens=usage[3],
        ),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _load_run_out(settings: Settings, user: VerifiedUser, run_id: uuid.UUID) -> RunOut:
    with user_db(settings, user) as db:
        run = db.scalars(
            select(TailoringRun).where(TailoringRun.id == run_id, TailoringRun.owner_id == user.id)
        ).first()
        if run is None:
            raise ApiError(404, "not_found", "That tailoring run was not found.")
        return _run_out(db, user, run)


@router.post("/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
def create_run(
    body: RunCreate,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    client = _client(settings)
    enforce_limit(settings, rate_limit.AI_RUN_USER, str(user.id))
    run_id = runs.start_run(settings, user, client, body.job_id, body.master_version_id)
    return _load_run_out(settings, user, run_id)


@router.get("/runs", response_model=list[RunSummary], responses=_ERRORS)
def list_runs(
    job_id: uuid.UUID | None = Query(default=None),
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[RunSummary]:
    query = (
        select(TailoringRun, JobPosting)
        .join(JobPostingVersion, JobPostingVersion.id == TailoringRun.posting_version_id)
        .join(JobPosting, JobPosting.id == JobPostingVersion.posting_id)
        .where(TailoringRun.owner_id == user.id)
        .order_by(TailoringRun.created_at.desc())
    )
    if job_id is not None:
        query = query.where(JobPosting.id == job_id)
    with user_db(settings, user) as db:
        return [
            RunSummary(
                id=r.id,
                status=r.status,
                job_title=p.title,
                company=p.company,
                created_at=r.created_at,
            )
            for r, p in db.execute(query).all()
        ]


@router.get("/runs/{run_id}", response_model=RunOut, responses=_ERRORS)
def get_run(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    return _load_run_out(settings, user, run_id)


@router.post(
    "/runs/{run_id}/questions/{question_id}", response_model=QuestionOut, responses=_ERRORS
)
def answer_question(
    run_id: uuid.UUID,
    question_id: uuid.UUID,
    body: QuestionAnswer,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> QuestionOut:
    q = runs.answer_question(
        settings,
        user,
        run_id,
        question_id,
        answer=body.answer,
        experience_type=body.experience_type,
        entry_evidence_id=body.entry_evidence_id,
        description=body.description,
    )
    return QuestionOut(
        id=q.id,
        requirement_id=q.requirement_id,
        requirement_text=q.requirement_text,
        term=q.term,
        question=q.question,
        status=q.status,  # type: ignore[arg-type]
        evidence_id=q.evidence_id,
    )


@router.post("/runs/{run_id}/draft", response_model=RunOut, responses=_ERRORS)
def draft(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    client = _client(settings)
    enforce_limit(settings, rate_limit.AI_DRAFT_USER, str(user.id))
    runs.generate_draft(settings, user, client, run_id)
    return _load_run_out(settings, user, run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunOut, responses=_ERRORS)
def cancel(
    run_id: uuid.UUID,
    user: VerifiedUser = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> RunOut:
    runs.cancel_run(settings, user, run_id)
    return _load_run_out(settings, user, run_id)
