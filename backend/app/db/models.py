"""SQLAlchemy models for the private ``app`` schema.

Ownership is enforced three times: application queries filter by owner,
composite foreign keys make cross-owner references impossible, and row-level
security policies (see migrations) restrict every row to ``app.current_user_id()``.
Immutable history (master versions, evidence versions) has no UPDATE/DELETE
grants for the runtime role at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "app"

EXPERIENCE_TYPES = ("professional", "internship", "academic", "personal", "other", "unclassified")
VERIFICATION_STATUSES = ("unverified", "verified", "rejected")
EVIDENCE_ORIGINS = ("master", "confirmation")
CHANGE_SOURCES = ("extracted", "user", "confirmation")
RUN_STATUSES = (
    "queued",
    "analyzing",
    "awaiting_confirmation",
    "ready",
    "drafting",
    "validating",
    "awaiting_review",
    "approved",
    "failed",
    "cancelled",
)
QUESTION_STATUSES = ("open", "confirmed", "declined", "dismissed")
DRAFT_SOURCES = ("generation", "repair", "edit", "chat", "improve")
ATS_REPORT_KINDS = ("master_baseline", "draft", "final_pdf")
# "standard": the shared layout (ADR 0011); "template": the master DOCX edited in place;
# "reconstructed": the pre-0011 rebuilt layout (kept for existing artifacts).
LAYOUTS = ("standard", "template", "reconstructed")
# "stale" is never stored: it is derived when a report no longer matches the run.
ATS_REPORT_STATUSES = ("pending", "complete", "failed")
JOB_KINDS = ("analyze", "draft", "regenerate", "chat", "improve", "render", "ats_check")
# At most one active job per run and lane: workflow jobs change the run or its draft.
JOB_LANES = ("workflow", "render", "ats")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
GRANT_PURPOSES = ("master_read", "artifact_read", "pdf_upload", "docx_upload")
IMPROVEMENT_STATUSES = ("running", "saved", "rejected", "failed")
APPLICATION_STATUSES = (
    "planned",
    "applied",
    "interviewing",
    "offer",
    "accepted",
    "rejected",
    "withdrawn",
)
# Automatic ATS improvement attempts per run; the attempt_range constraint enforces it.
MAX_IMPROVEMENT_ATTEMPTS = 2


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=SCHEMA,
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
        },
    )


# Supabase owns auth.users; this stub only lets foreign keys resolve. Migrations
# never create or alter it.
auth_users = Table(
    "users", Base.metadata, Column("id", UUID(as_uuid=True), primary_key=True), schema="auth"
)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _auth_user_fk() -> ForeignKey:
    # Deleting the Supabase auth user removes all of their rows.
    return ForeignKey("auth.users.id", ondelete="CASCADE")


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _auth_user_fk(), primary_key=True
    )
    display_name: Mapped[str | None] = mapped_column(String(100))
    # Prefix for downloaded PDF names, e.g. "LeonardoSuzana".
    filename_prefix: Mapped[str | None] = mapped_column(String(40))
    # Formatting defaults the user explicitly chose to remember.
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class MasterVersion(Base):
    __tablename__ = "master_versions"
    __table_args__ = (
        UniqueConstraint("owner_id", "version_number", name="uq_master_versions_owner_version"),
        UniqueConstraint("owner_id", "sha256", name="uq_master_versions_owner_sha256"),
        # Target for composite foreign keys that pin children to the same owner.
        UniqueConstraint("id", "owner_id", name="uq_master_versions_id_owner"),
        CheckConstraint(_in("file_kind", ("docx", "pdf")), name="file_kind"),
        CheckConstraint(_in("extraction_status", ("complete", "partial", "failed")), name="status"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("version_number > 0", name="version_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _auth_user_fk(), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Sanitized display label only; storage uses storage_path.
    original_filename: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    extraction_status: Mapped[str] = mapped_column(String(16), nullable=False)
    extraction_notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = _created_at()


class EvidenceRecord(Base):
    """A stable fact identity. Its content lives in immutable EvidenceVersion rows."""

    __tablename__ = "evidence_records"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_evidence_records_id_owner"),
        ForeignKeyConstraint(
            ["master_version_id", "owner_id"],
            ["app.master_versions.id", "app.master_versions.owner_id"],
            name="fk_evidence_records_master_owner",
        ),
        CheckConstraint("current_version > 0", name="current_version_positive"),
        CheckConstraint(_in("origin", EVIDENCE_ORIGINS), name="origin"),
        # Extracted facts come from a master; facts added by answering questions do not.
        CheckConstraint(
            "(origin = 'master') = (master_version_id IS NOT NULL)", name="origin_matches_master"
        ),
        Index("ix_evidence_records_owner_master", "owner_id", "master_version_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    master_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    origin: Mapped[str] = mapped_column(String(16), nullable=False, server_default="master")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()


class EvidenceVersion(Base):
    __tablename__ = "evidence_versions"
    __table_args__ = (
        UniqueConstraint("evidence_id", "version", name="uq_evidence_versions_evidence_version"),
        ForeignKeyConstraint(
            ["evidence_id", "owner_id"],
            ["app.evidence_records.id", "app.evidence_records.owner_id"],
            name="fk_evidence_versions_record_owner",
        ),
        CheckConstraint(_in("experience_type", EXPERIENCE_TYPES), name="experience_type"),
        CheckConstraint(_in("suggested_type", EXPERIENCE_TYPES), name="suggested_type"),
        CheckConstraint(
            _in("verification_status", VERIFICATION_STATUSES), name="verification_status"
        ),
        CheckConstraint(_in("change_source", CHANGE_SOURCES), name="change_source"),
        CheckConstraint(
            "verification_status <> 'verified' OR experience_type <> 'unclassified'",
            name="verified_needs_type",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(text) > 0", name="text_not_empty"),
        CheckConstraint(
            "jsonb_typeof(bold_terms) = 'array' AND jsonb_array_length(bold_terms) <= 30",
            name="bold_terms_array",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    # Exactly what the master contained; never rewritten.
    source_text: Mapped[str] = mapped_column(String(2000), nullable=False)
    section: Mapped[str | None] = mapped_column(String(100))
    entry: Mapped[str | None] = mapped_column(String(300))
    date_text: Mapped[str | None] = mapped_column(String(100))
    # Parts of a two-line entry header as the master shows them (protected like
    # entry and dates: copied from the master, never model-written; ADR 0011).
    organization: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(200))
    # Terms the master set in bold inside this fact (e.g. project technologies).
    bold_terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    metrics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    experience_type: Mapped[str] = mapped_column(String(16), nullable=False)
    suggested_type: Mapped[str] = mapped_column(String(16), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(16), nullable=False)
    change_source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class RateLimitCounter(Base):
    """Shared fixed-window counters (keys are hashes, never raw emails or IPs)."""

    __tablename__ = "rate_limit_counters"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


def json_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


# --- Milestone 4: jobs, analyses, runs, confirmations, drafts, usage -----------


def _owner_fk(columns: list[str], target: str, name: str) -> ForeignKeyConstraint:
    """Composite (id, owner_id) foreign key: a child can only reference its owner's rows."""
    return ForeignKeyConstraint(columns, [f"app.{target}.id", f"app.{target}.owner_id"], name=name)


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (UniqueConstraint("id", "owner_id", name="uq_job_postings_id_owner"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _auth_user_fk(), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()


class JobPostingVersion(Base):
    __tablename__ = "job_posting_versions"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_job_posting_versions_id_owner"),
        UniqueConstraint("posting_id", "version", name="uq_job_posting_versions_posting_version"),
        _owner_fk(
            ["posting_id", "owner_id"], "job_postings", "fk_job_posting_versions_posting_owner"
        ),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    description_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class JobAnalysis(Base):
    """Structured requirements for one posting version; cached per model and prompt version."""

    __tablename__ = "job_analyses"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_job_analyses_id_owner"),
        UniqueConstraint(
            "posting_version_id",
            "model",
            "prompt_version",
            name="uq_job_analyses_version_model_prompt",
        ),
        _owner_fk(
            ["posting_version_id", "owner_id"],
            "job_posting_versions",
            "fk_job_analyses_version_owner",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    posting_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class TailoringRun(Base):
    __tablename__ = "tailoring_runs"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_tailoring_runs_id_owner"),
        _owner_fk(
            ["posting_version_id", "owner_id"],
            "job_posting_versions",
            "fk_tailoring_runs_version_owner",
        ),
        _owner_fk(
            ["master_version_id", "owner_id"], "master_versions", "fk_tailoring_runs_master_owner"
        ),
        _owner_fk(["analysis_id", "owner_id"], "job_analyses", "fk_tailoring_runs_analysis_owner"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_tailoring_runs_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    posting_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    master_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class ConfirmationQuestion(Base):
    """A precise question about a requirement the user's confirmed facts do not cover."""

    __tablename__ = "confirmation_questions"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_confirmation_questions_id_owner"),
        UniqueConstraint(
            "run_id", "requirement_id", name="uq_confirmation_questions_run_requirement"
        ),
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_confirmation_questions_run_owner"),
        _owner_fk(
            ["evidence_id", "owner_id"],
            "evidence_records",
            "fk_confirmation_questions_evidence_owner",
        ),
        CheckConstraint(_in("status", QUESTION_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[str] = mapped_column(String(16), nullable=False)
    requirement_text: Mapped[str] = mapped_column(String(500), nullable=False)
    term: Mapped[str] = mapped_column(String(100), nullable=False)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_drafts_id_owner"),
        UniqueConstraint("run_id", name="uq_drafts_run_id"),
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_drafts_run_owner"),
        CheckConstraint("current_version > 0", name="current_version_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()


class DraftVersion(Base):
    """An immutable resume draft plus the validation report computed for it."""

    __tablename__ = "draft_versions"
    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="uq_draft_versions_draft_version"),
        UniqueConstraint("id", "owner_id", name="uq_draft_versions_id_owner"),
        _owner_fk(["draft_id", "owner_id"], "drafts", "fk_draft_versions_draft_owner"),
        _owner_fk(["retrieval_id", "owner_id"], "retrievals", "fk_draft_versions_retrieval_owner"),
        CheckConstraint(_in("created_by", DRAFT_SOURCES), name="created_by"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    # The fact selection a generated version's model context came from (milestone 7).
    retrieval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


class UsageEvent(Base):
    """One paid model call: tokens, computed cost, latency, and outcome."""

    __tablename__ = "usage_events"
    __table_args__ = (
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_usage_events_run_owner"),
        CheckConstraint("cost_usd >= 0", name="cost_nonnegative"),
        Index("ix_usage_events_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class SpendUser(Base):
    """Per-user daily spend: reserved (in-flight worst case) and settled actual cost."""

    __tablename__ = "spend_user"
    __table_args__ = (CheckConstraint("reserved_usd >= 0 AND spent_usd >= 0", name="nonnegative"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _auth_user_fk(), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")


class SpendGlobal(Base):
    """Application-wide daily spend for the spending cutoff (aggregate numbers only)."""

    __tablename__ = "spend_global"
    __table_args__ = (CheckConstraint("reserved_usd >= 0 AND spent_usd >= 0", name="nonnegative"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")


# --- Milestone 5: rendered artifacts, approvals, chat revisions ---------------


class Artifact(Base):
    """A rendered PDF (plus its internal DOCX) for one immutable draft version."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_artifacts_id_owner"),
        _owner_fk(
            ["draft_version_id", "owner_id"], "draft_versions", "fk_artifacts_draft_version_owner"
        ),
        CheckConstraint(_in("layout", LAYOUTS), name="layout"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        Index("ix_artifacts_owner_version", "owner_id", "draft_version_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    draft_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    docx_storage_path: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    layout: Mapped[str] = mapped_column(String(16), nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Approval(Base):
    """Binds one draft version to the exact bytes (hash) of its rendered PDF."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_approvals_artifact_id"),
        UniqueConstraint("id", "owner_id", name="uq_approvals_id_owner"),
        _owner_fk(["artifact_id", "owner_id"], "artifacts", "fk_approvals_artifact_owner"),
        _owner_fk(["draft_id", "owner_id"], "drafts", "fk_approvals_draft_owner"),
        CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        Index("ix_approvals_owner_created", "owner_id", "approved_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    approved_at: Mapped[datetime] = _created_at()


class AtsReport(Base):
    """An ATS match estimate for one immutable subject (ADR 0006). Insert-only.

    The subject is a master version (baseline), a draft version, or a rendered
    artifact (the exact final PDF). A report never changes; whether it is still
    current (or stale) is decided when reading, by comparing its subject and
    rubric version with the run's current ones.
    """

    __tablename__ = "ats_reports"
    __table_args__ = (
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_ats_reports_run_owner"),
        _owner_fk(["analysis_id", "owner_id"], "job_analyses", "fk_ats_reports_analysis_owner"),
        _owner_fk(
            ["master_version_id", "owner_id"], "master_versions", "fk_ats_reports_master_owner"
        ),
        _owner_fk(
            ["draft_version_id", "owner_id"], "draft_versions", "fk_ats_reports_draft_version_owner"
        ),
        _owner_fk(["artifact_id", "owner_id"], "artifacts", "fk_ats_reports_artifact_owner"),
        CheckConstraint(_in("kind", ATS_REPORT_KINDS), name="kind"),
        CheckConstraint(_in("status", ATS_REPORT_STATUSES), name="status"),
        CheckConstraint(
            "(kind = 'master_baseline' AND master_version_id IS NOT NULL"
            " AND draft_version_id IS NULL AND artifact_id IS NULL)"
            " OR (kind = 'draft' AND draft_version_id IS NOT NULL"
            " AND master_version_id IS NULL AND artifact_id IS NULL)"
            " OR (kind = 'final_pdf' AND artifact_id IS NOT NULL"
            " AND master_version_id IS NULL AND draft_version_id IS NULL)",
            name="one_subject",
        ),
        CheckConstraint(
            "(status = 'complete' AND result IS NOT NULL AND error_code IS NULL)"
            " OR (status <> 'complete' AND result IS NULL AND score IS NULL)",
            name="outcome",
        ),
        CheckConstraint("status <> 'failed' OR error_code IS NOT NULL", name="failed_has_code"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="score_range"),
        CheckConstraint("analysis_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_hex"),
        Index("ix_ats_reports_owner_run", "owner_id", "run_id", "created_at"),
        Index("ix_ats_reports_artifact", "artifact_id"),
        Index("ix_ats_reports_draft_version", "draft_version_id"),
        # At most one complete report per subject, analysis, and rubric version, so
        # concurrent checks cannot store duplicates (inserts use ON CONFLICT DO NOTHING).
        Index(
            "uq_ats_reports_complete_master",
            "master_version_id",
            "run_id",
            "analysis_id",
            "rubric_version",
            unique=True,
            postgresql_where=text("kind = 'master_baseline' AND status = 'complete'"),
        ),
        Index(
            "uq_ats_reports_complete_draft",
            "draft_version_id",
            "analysis_id",
            "rubric_version",
            unique=True,
            postgresql_where=text("kind = 'draft' AND status = 'complete'"),
        ),
        Index(
            "uq_ats_reports_complete_pdf",
            "artifact_id",
            "analysis_id",
            "rubric_version",
            unique=True,
            postgresql_where=text("kind = 'final_pdf' AND status = 'complete'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    master_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    draft_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # None unless complete, and None when nothing could be scored; never shown as 0.
    score: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rubric_version: Mapped[str] = mapped_column(String(50), nullable=False)
    analysis_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


# --- Milestone 6c: durable jobs, storage grants, budget reservations -----------


class Job(Base):
    """Durable background work for one run (ADR 0007).

    Queued by a request, claimed by a worker with a renewable lease, retried a
    bounded number of times, and finished as succeeded, failed, or cancelled.
    Cross-owner queue access happens only through ``app_queue`` functions.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_jobs_id_owner"),
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_jobs_run_owner"),
        CheckConstraint(_in("kind", JOB_KINDS), name="kind"),
        CheckConstraint(_in("lane", JOB_LANES), name="lane"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("max_attempts BETWEEN 1 AND 5", name="max_attempts_range"),
        CheckConstraint("attempts BETWEEN 0 AND max_attempts", name="attempts_bounded"),
        CheckConstraint(
            "(status = 'running') = (lease_expires_at IS NOT NULL AND locked_by IS NOT NULL)",
            name="lease_iff_running",
        ),
        CheckConstraint("status <> 'failed' OR error_code IS NOT NULL", name="failed_has_code"),
        Index(
            "uq_jobs_active_lane",
            "run_id",
            "lane",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_jobs_claim", "run_after", "created_at", postgresql_where=text("status = 'queued'")
        ),
        Index("ix_jobs_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("ix_jobs_owner_status", "owner_id", "status"),
        Index("ix_jobs_run_created", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    # Small, validated inputs (IDs and versions); never file contents or secrets.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = _created_at()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    error_code: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobGrant(Base):
    """A short-lived signed URL for exactly one storage object a job may read or write.

    Created in the request with the user's own session, so Storage policies
    decide what may be signed. It is a capability: never logged, never
    returned by the API, and deleted when the job finishes.
    """

    __tablename__ = "job_grants"
    __table_args__ = (
        _owner_fk(["job_id", "owner_id"], "jobs", "fk_job_grants_job_owner"),
        CheckConstraint(_in("purpose", GRANT_PURPOSES), name="purpose"),
        CheckConstraint("attempt BETWEEN 0 AND 5", name="attempt_range"),
        CheckConstraint("url LIKE '/object/%'", name="relative_url"),
        UniqueConstraint("job_id", "purpose", "attempt", name="uq_job_grants_job_purpose_attempt"),
        Index("ix_job_grants_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    # 0 for reads; uploads are per attempt because signed uploads never overwrite.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    object_path: Mapped[str] = mapped_column(String(200), nullable=False)
    # Relative to the Storage API base (the worker prefixes its own SUPABASE_URL).
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class BudgetReservation(Base):
    """One in-flight worst-case reservation. Deleted when settled or released;
    expired rows (a crash mid-call) are returned to the daily counters by the worker."""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        CheckConstraint("amount_usd >= 0", name="amount_nonnegative"),
        Index("ix_budget_reservations_expires", "expires_at"),
        Index("ix_budget_reservations_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatMessage(Base):
    """A revision instruction and the outcome summary (never raw model output)."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_chat_messages_run_owner"),
        CheckConstraint(_in("role", ("user", "assistant")), name="role"),
        Index("ix_chat_messages_owner_run", "owner_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String(2000), nullable=False)
    draft_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class ImprovementAttempt(Base):
    """One automatic ATS improvement attempt for a run (milestone 6d, ADR 0008).

    The row is inserted before any work starts, so the unique (run, attempt)
    pair and the attempt range cap attempts per run even across job retries
    and worker restarts. ``targets`` holds requirement IDs only.
    """

    __tablename__ = "improvement_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", name="uq_improvement_attempts_run_attempt"),
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_improvement_attempts_run_owner"),
        _owner_fk(["job_id", "owner_id"], "jobs", "fk_improvement_attempts_job_owner"),
        CheckConstraint(f"attempt BETWEEN 1 AND {MAX_IMPROVEMENT_ATTEMPTS}", name="attempt_range"),
        CheckConstraint(_in("status", IMPROVEMENT_STATUSES), name="status"),
        CheckConstraint("(status = 'saved') = (new_version IS NOT NULL)", name="saved_has_version"),
        CheckConstraint("(status = 'running') = (outcome IS NULL)", name="finished_has_outcome"),
        CheckConstraint(
            "score_before IS NULL OR score_before BETWEEN 0 AND 100", name="score_before_range"
        ),
        CheckConstraint(
            "score_after IS NULL OR score_after BETWEEN 0 AND 100", name="score_after_range"
        ),
        Index("ix_improvement_attempts_owner_run", "owner_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    new_version: Mapped[int | None] = mapped_column(Integer)
    targets: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    score_before: Mapped[int | None] = mapped_column(Integer)
    score_after: Mapped[int | None] = mapped_column(Integer)
    # Why the attempt ended: improved, no_improvement, unsupported_changes, ... (ADR 0008).
    outcome: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Milestone 6e: manual application tracker ------------------------------------


class Application(Base):
    """One job application the user tracks by hand (milestone 6e).

    Optionally points at one of the owner's approvals: the exact resume version
    (and PDF hash) that was sent. Entries are user notes, not evidence; they
    never feed tailoring. Deleting one is a hard delete the user confirms.
    """

    __tablename__ = "applications"
    __table_args__ = (
        _owner_fk(["approval_id", "owner_id"], "approvals", "fk_applications_approval_owner"),
        CheckConstraint(_in("status", APPLICATION_STATUSES), name="status"),
        CheckConstraint("length(btrim(company)) > 0", name="company_not_blank"),
        CheckConstraint("length(btrim(role)) > 0", name="role_not_blank"),
        # The API validates links fully; this only keeps other schemes out of the table.
        CheckConstraint("job_url IS NULL OR job_url ~ '^https?://'", name="job_url_http"),
        CheckConstraint("lock_version > 0", name="lock_version_positive"),
        Index("ix_applications_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False)
    job_url: Mapped[str | None] = mapped_column(String(2048))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    applied_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(4000))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


# --- Milestone 7: retrieval (ADR 0010) --------------------------------------------

RETRIEVAL_MODES = ("full", "hybrid")


class EvidenceChunk(Base):
    """Embedded text of one confirmed fact version: derived data, rebuilt at any time.

    ``context`` is the parent entry and section the fact belongs to; the vector
    embeds context and body together. Rows are tied to the exact fact version
    and to the embedding model revision, so an edited fact or a new model gets
    new rows and old ones are deleted. Removing the fact removes its chunks.
    """

    __tablename__ = "evidence_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evidence_id", "owner_id"],
            ["app.evidence_records.id", "app.evidence_records.owner_id"],
            name="fk_evidence_chunks_record_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "evidence_version"],
            ["app.evidence_versions.evidence_id", "app.evidence_versions.version"],
            name="fk_evidence_chunks_version",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "evidence_id",
            "evidence_version",
            "model",
            "model_revision",
            "chunk_index",
            name="uq_evidence_chunks_identity",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("length(body) > 0", name="body_not_empty"),
        CheckConstraint("dimension = 384", name="dimension"),
        Index("ix_evidence_chunks_owner_model", "owner_id", "model", "model_revision"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    context: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    # SHA-256 of the exact embedded text: identical text reuses the owner's vector.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized: Mapped[bool] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Retrieval(Base):
    """Which confirmed facts a draft's model context used, and why (insert-only).

    ``result`` holds per-requirement rankings (evidence IDs and scores per
    method), the selected IDs, and fallback notes: never fact text. Rows are a
    tenant-scoped cache: the same analysis, profile fingerprint (evidence IDs
    and versions), and configuration reuse the stored selection.
    """

    __tablename__ = "retrievals"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_retrievals_id_owner"),
        UniqueConstraint(
            "owner_id",
            "analysis_id",
            "profile_fingerprint",
            "config_fingerprint",
            name="uq_retrievals_cache_key",
        ),
        _owner_fk(["run_id", "owner_id"], "tailoring_runs", "fk_retrievals_run_owner"),
        _owner_fk(["analysis_id", "owner_id"], "job_analyses", "fk_retrievals_analysis_owner"),
        CheckConstraint(_in("mode", RETRIEVAL_MODES), name="mode"),
        CheckConstraint("facts_selected <= facts_total", name="selected_within_total"),
        Index("ix_retrievals_owner_run", "owner_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    # The run that first computed it; later runs may reuse it through the cache key.
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_revision: Mapped[str | None] = mapped_column(String(64))
    facts_total: Mapped[int] = mapped_column(Integer, nullable=False)
    facts_selected: Mapped[int] = mapped_column(Integer, nullable=False)
    chars_total: Mapped[int] = mapped_column(Integer, nullable=False)
    chars_selected: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


# --- Milestone 8: account deletion (ADR 0012) --------------------------------------


class AccountDeletion(Base):
    """Marks an account whose deletion has started (confirmed by the user's password).

    While it exists, new jobs are refused and Storage lets the owner delete
    their own objects; ``app.delete_current_account()`` then removes the auth
    user, and every row (this one too) goes with it through the cascades.
    """

    __tablename__ = "account_deletions"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _auth_user_fk(), primary_key=True
    )
    requested_at: Mapped[datetime] = _created_at()
