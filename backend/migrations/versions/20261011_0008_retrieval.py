"""Retrieval: fact embeddings and retrieval provenance (milestone 7, ADR 0010).

- Enables pgvector in Supabase's ``extensions`` schema (where hosted Supabase
  installs it) and lets the runtime role resolve the ``vector`` type and its
  operators there: USAGE on the schema plus the role's search_path. Nobody
  but the schema owner can create objects in it, so the search path cannot be
  hijacked by the runtime role.
- ``app.evidence_chunks``: derived embeddings per confirmed fact version.
  The runtime role may read, insert, and delete its own rows; never update
  (a changed fact gets new rows). Cascades from the fact and its version.
- ``app.retrievals``: insert-only record of which facts a draft's model
  context used and why (IDs and scores, no fact text); also the cache key.
- ``draft_versions.retrieval_id``: the selection a generated version used.

No approximate (HNSW/IVFFlat) index: per-user exact search over a few
hundred rows is fast, and approximate indexes wait for measurement.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11 22:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_BACKEND_SEARCH_PATH = '"$user", public, extensions'


REVOKE_SUPABASE_ROLES = """
DO $$
DECLARE r text; t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['evidence_chunks', 'retrievals'] LOOP
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('REVOKE ALL ON app.%I FROM %I', t, r);
      END IF;
    END LOOP;
  END LOOP;
END $$
"""


def _owner_rls(table: str) -> None:
    op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_owner ON app.{table} FOR ALL TO app_backend "
        "USING (owner_id = app.current_user_id()) "
        "WITH CHECK (owner_id = app.current_user_id())"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS extensions")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions")
    op.execute("GRANT USAGE ON SCHEMA extensions TO app_backend")
    op.execute(f"ALTER ROLE app_backend SET search_path = {APP_BACKEND_SEARCH_PATH}")

    op.create_table(
        "evidence_chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("context", sa.String(length=500), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("model_revision", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False),
        sa.Column("embedding", Vector(dim=384), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(body) > 0", name=op.f("ck_evidence_chunks_body_not_empty")),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_evidence_chunks_chunk_index_nonnegative")
        ),
        sa.CheckConstraint("dimension = 384", name=op.f("ck_evidence_chunks_dimension")),
        sa.ForeignKeyConstraint(
            ["evidence_id", "evidence_version"],
            ["app.evidence_versions.evidence_id", "app.evidence_versions.version"],
            name="fk_evidence_chunks_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id", "owner_id"],
            ["app.evidence_records.id", "app.evidence_records.owner_id"],
            name="fk_evidence_chunks_record_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["auth.users.id"],
            name=op.f("fk_evidence_chunks_owner_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_chunks")),
        sa.UniqueConstraint(
            "evidence_id",
            "evidence_version",
            "model",
            "model_revision",
            "chunk_index",
            name="uq_evidence_chunks_identity",
        ),
        schema="app",
    )
    op.create_index(
        "ix_evidence_chunks_owner_model",
        "evidence_chunks",
        ["owner_id", "model", "model_revision"],
        unique=False,
        schema="app",
    )

    op.create_table(
        "retrievals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("profile_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_revision", sa.String(length=64), nullable=True),
        sa.Column("facts_total", sa.Integer(), nullable=False),
        sa.Column("facts_selected", sa.Integer(), nullable=False),
        sa.Column("chars_total", sa.Integer(), nullable=False),
        sa.Column("chars_selected", sa.Integer(), nullable=False),
        sa.Column("runtime_ms", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "facts_selected <= facts_total", name=op.f("ck_retrievals_selected_within_total")
        ),
        sa.CheckConstraint("mode IN ('full', 'hybrid')", name=op.f("ck_retrievals_mode")),
        sa.ForeignKeyConstraint(
            ["analysis_id", "owner_id"],
            ["app.job_analyses.id", "app.job_analyses.owner_id"],
            name="fk_retrievals_analysis_owner",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["auth.users.id"], name=op.f("fk_retrievals_owner_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "owner_id"],
            ["app.tailoring_runs.id", "app.tailoring_runs.owner_id"],
            name="fk_retrievals_run_owner",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrievals")),
        sa.UniqueConstraint("id", "owner_id", name="uq_retrievals_id_owner"),
        sa.UniqueConstraint(
            "owner_id",
            "analysis_id",
            "profile_fingerprint",
            "config_fingerprint",
            name="uq_retrievals_cache_key",
        ),
        schema="app",
    )
    op.create_index(
        "ix_retrievals_owner_run", "retrievals", ["owner_id", "run_id"], unique=False, schema="app"
    )

    op.add_column(
        "draft_versions", sa.Column("retrieval_id", sa.UUID(), nullable=True), schema="app"
    )
    op.create_foreign_key(
        "fk_draft_versions_retrieval_owner",
        "draft_versions",
        "retrievals",
        ["retrieval_id", "owner_id"],
        ["id", "owner_id"],
        source_schema="app",
        referent_schema="app",
    )

    for table in ("evidence_chunks", "retrievals"):
        op.execute(f"REVOKE ALL ON app.{table} FROM PUBLIC")
        _owner_rls(table)
    op.execute(REVOKE_SUPABASE_ROLES)
    op.execute("GRANT SELECT, INSERT, DELETE ON app.evidence_chunks TO app_backend")
    op.execute("GRANT SELECT, INSERT ON app.retrievals TO app_backend")


def downgrade() -> None:
    op.drop_constraint(
        "fk_draft_versions_retrieval_owner", "draft_versions", schema="app", type_="foreignkey"
    )
    op.drop_column("draft_versions", "retrieval_id", schema="app")
    op.drop_index("ix_retrievals_owner_run", table_name="retrievals", schema="app")
    op.drop_table("retrievals", schema="app")
    op.drop_index("ix_evidence_chunks_owner_model", table_name="evidence_chunks", schema="app")
    op.drop_table("evidence_chunks", schema="app")
    op.execute("ALTER ROLE app_backend RESET search_path")
    op.execute("REVOKE USAGE ON SCHEMA extensions FROM app_backend")
    # Fails (RESTRICT) rather than dropping anything else that uses pgvector.
    op.execute("DROP EXTENSION IF EXISTS vector")
