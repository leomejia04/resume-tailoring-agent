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
DRAFT_SOURCES = ("generation", "repair", "edit", "chat")


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
        CheckConstraint(_in("layout", ("template", "reconstructed")), name="layout"),
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
