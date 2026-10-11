from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ats import AtsOverview
from app.schemas.jobs import JobInputRequest
from app.tailoring.models import AnalysisContent, DraftContent, ValidationReport


class JobCreate(JobInputRequest):
    """Same validation as the preview: pasted text, optional title/company/link."""


class JobOut(BaseModel):
    id: uuid.UUID
    title: str | None
    company: str | None
    source_url: str | None
    version: int
    created_at: datetime


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    master_version_id: uuid.UUID | None = None


class QuestionOut(BaseModel):
    id: uuid.UUID
    requirement_id: str
    requirement_text: str
    term: str
    question: str
    status: Literal["open", "confirmed", "declined", "dismissed"]
    evidence_id: uuid.UUID | None


class QuestionAnswer(BaseModel):
    """'yes' needs context: a bare confirmation never becomes a professional claim."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: Literal["yes", "no", "skip"]
    experience_type: (
        Literal["professional", "internship", "academic", "personal", "other"] | None
    ) = None
    entry_evidence_id: uuid.UUID | None = None
    description: str | None = Field(default=None, min_length=15, max_length=500)

    @model_validator(mode="after")
    def _context_for_yes(self) -> QuestionAnswer:
        if self.answer == "yes" and (self.experience_type is None or self.description is None):
            raise ValueError("say what kind of experience it was and describe what you did")
        return self


class SourceFact(BaseModel):
    text: str
    entry: str | None
    experience_type: str


class DraftOut(BaseModel):
    version: int
    content: DraftContent
    validation: ValidationReport
    created_by: str
    model: str | None
    prompt_version: str | None
    created_at: datetime
    sources: dict[uuid.UUID, SourceFact]


class ArtifactOut(BaseModel):
    id: uuid.UUID
    draft_version: int
    sha256: str
    page_count: int
    layout: Literal["template", "reconstructed"]
    passed: bool
    checks: dict
    filename: str
    created_at: datetime


class CurrentApproval(BaseModel):
    id: uuid.UUID
    draft_version: int
    artifact_id: uuid.UUID
    filename: str
    approved_at: datetime


class JobStatusOut(BaseModel):
    """Background work for a run (the latest of each kind); the UI polls while one is active."""

    id: uuid.UUID
    kind: Literal["analyze", "draft", "regenerate", "chat", "render", "ats_check"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    max_attempts: int
    error_code: str | None
    result: dict | None
    created_at: datetime
    finished_at: datetime | None


class RunUsage(BaseModel):
    calls: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    error_code: str | None
    lock_version: int
    job: JobOut
    master_version_id: uuid.UUID | None
    analysis: AnalysisContent | None
    analysis_model: str | None
    questions: list[QuestionOut]
    draft: DraftOut | None
    artifact: ArtifactOut | None = None
    approval: CurrentApproval | None = None
    # ATS match estimate reports; None until the posting has been analyzed.
    ats: AtsOverview | None = None
    jobs: list[JobStatusOut] = Field(default_factory=list)
    usage: RunUsage
    created_at: datetime
    updated_at: datetime


class RunSummary(BaseModel):
    id: uuid.UUID
    status: str
    job_title: str | None
    company: str | None
    created_at: datetime


class AIStatus(BaseModel):
    provider: Literal["openai", "demo", "disabled"]
    model: str | None
    reasoning_effort: str | None
    disclosure: str
    user_daily_budget_usd: Decimal
    spent_today_usd: Decimal
