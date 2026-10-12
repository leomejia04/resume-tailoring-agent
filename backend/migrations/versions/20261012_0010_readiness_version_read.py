"""Let the runtime role read the schema version (readiness check, ADR 0012).

``GET /api/v1/health/ready`` compares ``app.alembic_version`` with the newest
migration shipped in the image, so a host does not route traffic to an app
whose database is behind. app_backend gets SELECT on that one table only.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON app.alembic_version TO app_backend")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON app.alembic_version FROM app_backend")
