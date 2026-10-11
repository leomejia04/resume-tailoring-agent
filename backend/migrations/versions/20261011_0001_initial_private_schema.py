"""Private app schema: profiles, master versions, evidence, RLS, storage.

Creates the least-privilege runtime role ``app_backend`` (password from the
APP_DB_PASSWORD environment variable at migration time), tables owned by the
migration role, row-level security restricting every row to
``app.current_user_id()``, column-level grants that make history immutable,
and a private Supabase Storage bucket with per-owner folder policies.

Revision ID: 0001
Revises:
Create Date: 2026-10-11
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
EXPERIENCE_TYPES = "('professional', 'internship', 'academic', 'personal', 'other', 'unclassified')"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OWNED_TABLES = {
    "profiles": "user_id",
    "master_versions": "owner_id",
    "evidence_records": "owner_id",
    "evidence_versions": "owner_id",
}


class _RoleSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )
    app_db_password: SecretStr


def _auth_fk(column: str = "owner_id") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], ["auth.users.id"], ondelete="CASCADE")


def _created_at(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False)


def _create_role() -> None:
    password = _RoleSettings().app_db_password.get_secret_value()  # type: ignore[call-arg]
    if len(password) < 16:
        raise RuntimeError("APP_DB_PASSWORD must be at least 16 characters")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_backend') THEN
            CREATE ROLE app_backend LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOREPLICATION NOBYPASSRLS;
          END IF;
        END $$
        """
    )
    # Bind the password as a parameter, then let format(%L) quote it server-side.
    op.get_bind().execute(
        sa.text("SELECT set_config('app.migration_role_password', :pw, true)"), {"pw": password}
    )
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE format('ALTER ROLE app_backend PASSWORD %L',
                         current_setting('app.migration_role_password'));
        END $$
        """
    )
    op.execute("SELECT set_config('app.migration_role_password', '', true)")
    op.execute("ALTER ROLE app_backend SET statement_timeout = '15s'")
    op.execute("ALTER ROLE app_backend SET idle_in_transaction_session_timeout = '30s'")


def upgrade() -> None:
    op.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
    _create_role()

    op.execute(
        """
        CREATE FUNCTION app.current_user_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$
        """
    )

    op.create_table(
        "profiles",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("display_name", sa.String(100)),
        sa.Column("filename_prefix", sa.String(40)),
        sa.Column("lock_version", sa.Integer, server_default="1", nullable=False),
        _created_at(),
        _created_at("updated_at"),
        sa.PrimaryKeyConstraint("user_id", name="pk_profiles"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["auth.users.id"], ondelete="CASCADE", name="fk_profiles_user_id"
        ),
        schema="app",
    )

    op.create_table(
        "master_versions",
        _uuid_pk(),
        sa.Column("owner_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("file_kind", sa.String(8), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(120), nullable=False),
        sa.Column("storage_path", sa.String(200), nullable=False),
        sa.Column("extraction_status", sa.String(16), nullable=False),
        sa.Column("extraction_notes", JSONB, server_default="[]", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_master_versions"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["auth.users.id"], ondelete="CASCADE", name="fk_master_versions_owner_id"
        ),
        sa.UniqueConstraint("owner_id", "version_number", name="uq_master_versions_owner_version"),
        sa.UniqueConstraint("owner_id", "sha256", name="uq_master_versions_owner_sha256"),
        sa.UniqueConstraint("id", "owner_id", name="uq_master_versions_id_owner"),
        sa.UniqueConstraint("storage_path", name="uq_master_versions_storage_path"),
        sa.CheckConstraint("file_kind IN ('docx', 'pdf')", name="ck_master_versions_file_kind"),
        sa.CheckConstraint(
            "extraction_status IN ('complete', 'partial', 'failed')",
            name="ck_master_versions_status",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_master_versions_size_positive"),
        sa.CheckConstraint("version_number > 0", name="ck_master_versions_version_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_master_versions_sha256_hex"),
        schema="app",
    )
    op.create_index("ix_master_versions_owner_id", "master_versions", ["owner_id"], schema="app")

    op.create_table(
        "evidence_records",
        _uuid_pk(),
        sa.Column("owner_id", UUID, nullable=False),
        sa.Column("master_version_id", UUID, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("current_version", sa.Integer, server_default="1", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_records"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["auth.users.id"], ondelete="CASCADE", name="fk_evidence_records_owner_id"
        ),
        sa.ForeignKeyConstraint(
            ["master_version_id", "owner_id"],
            ["app.master_versions.id", "app.master_versions.owner_id"],
            name="fk_evidence_records_master_owner",
        ),
        sa.UniqueConstraint("id", "owner_id", name="uq_evidence_records_id_owner"),
        sa.CheckConstraint(
            "current_version > 0", name="ck_evidence_records_current_version_positive"
        ),
        schema="app",
    )
    op.create_index(
        "ix_evidence_records_owner_master",
        "evidence_records",
        ["owner_id", "master_version_id"],
        schema="app",
    )

    op.create_table(
        "evidence_versions",
        _uuid_pk(),
        sa.Column("evidence_id", UUID, nullable=False),
        sa.Column("owner_id", UUID, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("text", sa.String(2000), nullable=False),
        sa.Column("source_text", sa.String(2000), nullable=False),
        sa.Column("section", sa.String(100)),
        sa.Column("entry", sa.String(300)),
        sa.Column("date_text", sa.String(100)),
        sa.Column("metrics", JSONB, server_default="[]", nullable=False),
        sa.Column("experience_type", sa.String(16), nullable=False),
        sa.Column("suggested_type", sa.String(16), nullable=False),
        sa.Column("verification_status", sa.String(16), nullable=False),
        sa.Column("change_source", sa.String(16), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_versions"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["auth.users.id"],
            ondelete="CASCADE",
            name="fk_evidence_versions_owner_id",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id", "owner_id"],
            ["app.evidence_records.id", "app.evidence_records.owner_id"],
            name="fk_evidence_versions_record_owner",
        ),
        sa.UniqueConstraint("evidence_id", "version", name="uq_evidence_versions_evidence_version"),
        sa.CheckConstraint(
            f"experience_type IN {EXPERIENCE_TYPES}", name="ck_evidence_versions_experience_type"
        ),
        sa.CheckConstraint(
            f"suggested_type IN {EXPERIENCE_TYPES}", name="ck_evidence_versions_suggested_type"
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'verified', 'rejected')",
            name="ck_evidence_versions_verification_status",
        ),
        sa.CheckConstraint(
            "change_source IN ('extracted', 'user')", name="ck_evidence_versions_change_source"
        ),
        sa.CheckConstraint(
            "verification_status <> 'verified' OR experience_type <> 'unclassified'",
            name="ck_evidence_versions_verified_needs_type",
        ),
        sa.CheckConstraint("version > 0", name="ck_evidence_versions_version_positive"),
        sa.CheckConstraint("length(text) > 0", name="ck_evidence_versions_text_not_empty"),
        schema="app",
    )

    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket_key", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("bucket_key", "window_start", name="pk_rate_limit_counters"),
        schema="app",
    )

    # --- Privileges: nothing for PUBLIC or Supabase API roles; minimal for app_backend.
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
          FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
              EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA app FROM %I', r);
              EXECUTE format('REVOKE ALL ON SCHEMA app FROM %I', r);
            END IF;
          END LOOP;
        END $$
        """
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app.current_user_id() FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app TO app_backend")
    op.execute("GRANT EXECUTE ON FUNCTION app.current_user_id() TO app_backend")
    op.execute("GRANT SELECT, INSERT ON app.profiles TO app_backend")
    op.execute(
        "GRANT UPDATE (display_name, filename_prefix, lock_version, updated_at) "
        "ON app.profiles TO app_backend"
    )
    op.execute("GRANT SELECT, INSERT ON app.master_versions TO app_backend")
    op.execute("GRANT SELECT, INSERT ON app.evidence_records TO app_backend")
    op.execute("GRANT UPDATE (current_version) ON app.evidence_records TO app_backend")
    op.execute("GRANT SELECT, INSERT ON app.evidence_versions TO app_backend")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.rate_limit_counters TO app_backend")

    # --- Row-level security, forced even for the table owner.
    for table, owner_column in OWNED_TABLES.items():
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table} FOR ALL TO app_backend "
            f"USING ({owner_column} = app.current_user_id()) "
            f"WITH CHECK ({owner_column} = app.current_user_id())"
        )
    op.execute("ALTER TABLE app.rate_limit_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.rate_limit_counters FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY rate_limit_counters_backend ON app.rate_limit_counters FOR ALL "
        "TO app_backend USING (true) WITH CHECK (true)"
    )

    # --- Private storage bucket; each user may only use the folder named after their ID.
    storage_sql = f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT FROM pg_namespace WHERE nspname = 'storage') THEN
            INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
            VALUES ('masters', 'masters', false, 5242880,
                    ARRAY['{DOCX_MIME}', 'application/pdf'])
            ON CONFLICT (id) DO NOTHING;
            CREATE POLICY masters_owner_insert ON storage.objects FOR INSERT TO authenticated
              WITH CHECK (bucket_id = 'masters'
                          AND (storage.foldername(name))[1] = (SELECT auth.uid())::text);
            CREATE POLICY masters_owner_select ON storage.objects FOR SELECT TO authenticated
              USING (bucket_id = 'masters'
                     AND (storage.foldername(name))[1] = (SELECT auth.uid())::text);
          END IF;
        END $$
        """  # noqa: S608 - interpolates only the DOCX_MIME constant
    op.execute(storage_sql)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT FROM pg_namespace WHERE nspname = 'storage') THEN
            DROP POLICY IF EXISTS masters_owner_insert ON storage.objects;
            DROP POLICY IF EXISTS masters_owner_select ON storage.objects;
          END IF;
        END $$
        """
    )
    for table in (
        "rate_limit_counters",
        "evidence_versions",
        "evidence_records",
        "master_versions",
        "profiles",
    ):
        op.drop_table(table, schema="app")
    op.execute("DROP FUNCTION IF EXISTS app.current_user_id()")
    op.execute("REVOKE ALL ON SCHEMA app FROM app_backend")
