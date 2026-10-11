"""Write a local-development .env from `supabase status -o env` output.

Usage (from backend/):
    npx supabase status -o env | .venv/Scripts/python -m scripts.local_env

Reads the status on stdin, generates a random password for the least-privilege
app_backend role, and writes ../.env (refuses to overwrite an existing file).
Values are never printed. The service-role key is deliberately not copied: the
backend never uses it.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED = ("API_URL", "PUBLISHABLE_KEY", "DB_URL")


def parse_status(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise SystemExit(f"missing from supabase status output: {', '.join(missing)}")
    return values


def render_env(values: dict[str, str], app_db_password: str) -> str:
    db = urlsplit(values["DB_URL"])
    host = f"{db.hostname}:{db.port}"
    name = db.path.lstrip("/")
    return (
        "# Local development only. Generated from `supabase status`; never commit.\n"
        "APP_ENV=development\n"
        f"SUPABASE_URL={values['API_URL']}\n"
        f"SUPABASE_ANON_KEY={values['PUBLISHABLE_KEY']}\n"
        f"MIGRATION_DATABASE_URL=postgresql+psycopg://{db.username}:{db.password}@{host}/{name}\n"
        f"APP_DB_PASSWORD={app_db_password}\n"
        f"DATABASE_URL=postgresql+psycopg://app_backend:{app_db_password}@{host}/{name}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / ".env")
    args = parser.parse_args()
    if args.output.exists():
        print(f"{args.output} already exists; not overwriting.", file=sys.stderr)
        return 1
    content = render_env(parse_status(sys.stdin.read()), secrets.token_urlsafe(24))
    args.output.write_text(content, encoding="utf-8")
    keys = [line.split("=")[0] for line in content.splitlines() if "=" in line]
    print(f"wrote {args.output} with: {', '.join(keys)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
