"""Entry header details and the standard layout (ADR 0011).

- ``evidence_versions``: ``organization``, ``location``, ``title`` (the parts
  of a two-line entry header, copied from the master and never model-written)
  and ``bold_terms`` (terms the master set in bold inside the fact). Existing
  rows keep NULL / ``[]``; re-uploading a master fills them. app_backend's
  table-level SELECT/INSERT grants already cover the new columns.
- ``artifacts.layout`` also allows ``standard``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-12 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence_versions", sa.Column("organization", sa.String(length=200)), schema="app"
    )
    op.add_column("evidence_versions", sa.Column("location", sa.String(length=100)), schema="app")
    op.add_column("evidence_versions", sa.Column("title", sa.String(length=200)), schema="app")
    op.add_column(
        "evidence_versions",
        sa.Column(
            "bold_terms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        schema="app",
    )
    op.create_check_constraint(
        op.f("ck_evidence_versions_bold_terms_array"),
        "evidence_versions",
        "jsonb_typeof(bold_terms) = 'array' AND jsonb_array_length(bold_terms) <= 30",
        schema="app",
    )
    op.drop_constraint(op.f("ck_artifacts_layout"), "artifacts", schema="app", type_="check")
    op.create_check_constraint(
        op.f("ck_artifacts_layout"),
        "artifacts",
        "layout IN ('standard', 'template', 'reconstructed')",
        schema="app",
    )


def downgrade() -> None:
    # Fails if standard-layout artifacts exist (they are insert-only history).
    op.drop_constraint(op.f("ck_artifacts_layout"), "artifacts", schema="app", type_="check")
    op.create_check_constraint(
        op.f("ck_artifacts_layout"),
        "artifacts",
        "layout IN ('template', 'reconstructed')",
        schema="app",
    )
    op.drop_constraint(
        op.f("ck_evidence_versions_bold_terms_array"),
        "evidence_versions",
        schema="app",
        type_="check",
    )
    for column in ("bold_terms", "title", "location", "organization"):
        op.drop_column("evidence_versions", column, schema="app")
