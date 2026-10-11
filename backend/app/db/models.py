"""SQLAlchemy models for the private ``app`` schema.

Ownership is enforced three times: application queries filter by owner,
composite foreign keys make cross-owner references impossible, and row-level
security policies (see migrations) restrict every row to ``app.current_user_id()``.
Immutable history (master versions, evidence versions) has no UPDATE/DELETE
grants for the runtime role at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "app"

EXPERIENCE_TYPES = ("professional", "internship", "academic", "personal", "other", "unclassified")
VERIFICATION_STATUSES = ("unverified", "verified", "rejected")


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
        Index("ix_evidence_records_owner_master", "owner_id", "master_version_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _auth_user_fk(), nullable=False)
    master_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
        CheckConstraint(_in("change_source", ("extracted", "user")), name="change_source"),
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
