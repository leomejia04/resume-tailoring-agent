"""User-owned account deletion (ADR 0012).

The backend never holds the service-role key, so deleting a Supabase auth
user (which cascades to every app row) goes through one narrow function:

* ``app.account_deletions``: a marker row the backend inserts after the user
  re-enters their password. While it exists, new jobs are refused.
* Storage DELETE policies on ``masters`` and ``artifacts``: owners may delete
  objects in their own folder only while their marker exists, so files stay
  immutable in normal use. The check runs through
  ``app_storage.account_deletion_requested()`` (SECURITY DEFINER, own schema)
  because the ``authenticated`` role has no access to ``app``.
* ``app.delete_current_account()``: SECURITY DEFINER, owned by the migration
  role (the only role that may delete from ``auth.users``; Supabase does not
  let it grant that). It deletes exactly the user named by
  ``app.current_user_id()`` (set per transaction from the verified session),
  only when the marker exists and no job is queued or running, after
  returning that user's open budget reservations. Only app_backend may
  execute it.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELETE_ACCOUNT = """
CREATE FUNCTION app.delete_current_account()
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  uid uuid := app.current_user_id();
BEGIN
  IF uid IS NULL THEN
    RAISE EXCEPTION 'no current user' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM app.account_deletions AS d WHERE d.owner_id = uid) THEN
    RAISE EXCEPTION 'account deletion was not requested' USING ERRCODE = '42501';
  END IF;
  IF EXISTS (SELECT 1 FROM app.jobs AS j
             WHERE j.owner_id = uid AND j.status IN ('queued', 'running')) THEN
    RAISE EXCEPTION 'jobs are still active' USING ERRCODE = '55006';
  END IF;
  -- Open reservations also count against the app-wide daily limit; return them.
  UPDATE app.spend_global AS s
  SET reserved_usd = greatest(s.reserved_usd - g.total, 0)
  FROM (SELECT b.day, sum(b.amount_usd) AS total FROM app.budget_reservations AS b
        WHERE b.owner_id = uid GROUP BY b.day) AS g
  WHERE s.day = g.day;
  DELETE FROM auth.users AS u WHERE u.id = uid;
  RETURN FOUND;
END
$fn$
"""

DELETION_REQUESTED = """
CREATE FUNCTION app_storage.account_deletion_requested()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
  SELECT EXISTS (SELECT 1 FROM app.account_deletions AS d WHERE d.owner_id = auth.uid())
$fn$
"""


def upgrade() -> None:
    op.create_table(
        "account_deletions",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["auth.users.id"],
            name=op.f("fk_account_deletions_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id", name=op.f("pk_account_deletions")),
        schema="app",
    )
    op.execute("REVOKE ALL ON app.account_deletions FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON app.account_deletions TO app_backend")
    op.execute("ALTER TABLE app.account_deletions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.account_deletions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY account_deletions_owner ON app.account_deletions FOR ALL TO app_backend "
        "USING (owner_id = app.current_user_id()) "
        "WITH CHECK (owner_id = app.current_user_id())"
    )

    op.execute(DELETE_ACCOUNT)
    op.execute("REVOKE ALL ON FUNCTION app.delete_current_account() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app.delete_current_account() TO app_backend")

    if not _has_supabase_storage():
        return
    op.execute("CREATE SCHEMA app_storage")
    op.execute("REVOKE ALL ON SCHEMA app_storage FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app_storage TO authenticated")
    op.execute(DELETION_REQUESTED)
    op.execute("REVOKE ALL ON FUNCTION app_storage.account_deletion_requested() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_storage.account_deletion_requested() TO authenticated"
    )
    op.execute(
        """
        CREATE POLICY owner_delete_on_account_deletion ON storage.objects
          FOR DELETE TO authenticated
          USING (bucket_id IN ('masters', 'artifacts')
                 AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
                 AND (SELECT app_storage.account_deletion_requested()))
        """
    )


def _has_supabase_storage() -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'storage') "
                "AND EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated')"
            )
        )
    )


def downgrade() -> None:
    if _has_supabase_storage():
        op.execute("DROP POLICY IF EXISTS owner_delete_on_account_deletion ON storage.objects")
    op.execute("DROP SCHEMA IF EXISTS app_storage CASCADE")
    op.execute("DROP FUNCTION app.delete_current_account()")
    op.drop_table("account_deletions", schema="app")
