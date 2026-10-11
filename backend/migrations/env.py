"""Alembic environment: runs migrations as the schema owner (MIGRATION_DATABASE_URL).

The runtime app uses a different, least-privilege role (DATABASE_URL).
"""

from pathlib import Path

from alembic import context
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool

from app.db.models import SCHEMA, Base


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore"
    )
    migration_database_url: SecretStr


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # Only compare our own schema; Supabase manages auth/storage/etc.
    schema = getattr(obj, "schema", None) or getattr(getattr(obj, "table", None), "schema", None)
    return schema == SCHEMA


def run_migrations_online() -> None:
    url = MigrationSettings().migration_database_url.get_secret_value()  # type: ignore[call-arg]
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            include_schemas=True,
            include_object=include_object,
            version_table_schema=SCHEMA,
            compare_server_default=False,
        )
        with context.begin_transaction():
            context.execute("CREATE SCHEMA IF NOT EXISTS app")
            context.run_migrations()


run_migrations_online()
