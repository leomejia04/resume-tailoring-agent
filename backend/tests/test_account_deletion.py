"""Account deletion against the local Supabase stack (ADR 0012)."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.api.errors import ApiError
from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import _ENV_FILE
from app.documents.fixtures import build_fixture_master
from app.jobs import queue
from app.main import create_app
from app.services import account
from tests.conftest import browser_client
from tests.test_isolation import (
    _settings_or_skip,
    as_user,
    new_account,
    reset_rate_limits,
    upload,
)

pytestmark = pytest.mark.integration


def _env_value(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    if not _ENV_FILE.is_file():
        return None
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


@pytest.fixture(scope="module")
def settings():
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def app(settings):
    return create_app(settings)


@pytest.fixture(scope="module")
def runtime_engine(settings):
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def admin_engine():
    # Read-only checks of auth and storage rows, which the runtime role cannot see.
    url = _env_value("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIGRATION_DATABASE_URL not configured")
    engine = create_engine(url, pool_size=1)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("del") / "master.docx").read_bytes()


def _storage(settings, acct, method: str, path: str, **kwargs) -> httpx.Response:
    headers = {
        "apikey": settings.supabase_anon_key.get_secret_value(),
        "Authorization": f"Bearer {acct.access_token}",
        **kwargs.pop("headers", {}),
    }
    url = f"{settings.supabase_url}/storage/v1/object/{path}"
    return httpx.request(method, url, headers=headers, timeout=10, **kwargs)


def _counts(admin_engine, user_id) -> tuple[int, int, int, int, int]:
    """auth user, masters, profile, deletion marker, stored objects."""
    with admin_engine.connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM auth.users WHERE id = :u), "
                    "(SELECT count(*) FROM app.master_versions WHERE owner_id = :u), "
                    "(SELECT count(*) FROM app.profiles WHERE user_id = :u), "
                    "(SELECT count(*) FROM app.account_deletions WHERE owner_id = :u), "
                    "(SELECT count(*) FROM storage.objects WHERE name LIKE :prefix)"
                ),
                {"u": user_id, "prefix": f"{user_id}/%"},
            ).one()
        )


def _delete(acct, password: str, confirmation: str = "DELETE") -> httpx.Response:
    return acct.client.post(
        "/api/v1/account/deletion", json={"password": password, "confirmation": confirmation}
    )


def test_deleting_an_account_removes_its_rows_files_and_sign_in(
    app, settings, admin_engine, docx_bytes
):
    alice, bob = new_account(app), new_account(app)
    for acct in (alice, bob):
        assert upload(acct, docx_bytes).status_code == 201
    # Rendered files live two folders deep, like real artifacts.
    nested = f"{alice.id}/{uuid.uuid4()}/{uuid.uuid4()}.pdf"
    created = _storage(
        settings, alice, "POST", f"artifacts/{nested}",
        content=b"%PDF-1.7 test", headers={"Content-Type": "application/pdf"},
    )  # fmt: skip
    assert created.status_code == 200, created.text

    # In normal use files stay immutable: the owner cannot delete them directly.
    master_id = alice.client.get("/api/v1/masters").json()[0]["id"]
    refused = _storage(
        settings, alice, "DELETE", "masters", json={"prefixes": [f"{alice.id}/{master_id}.docx"]}
    )
    assert refused.json() == []
    assert _counts(admin_engine, alice.id) == (1, 1, 1, 0, 2)

    wrong = _delete(alice, "not-the-password-123")
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "password_incorrect"
    assert _delete(alice, alice.password, confirmation="delete").status_code == 422
    assert _counts(admin_engine, alice.id) == (1, 1, 1, 0, 2)

    response = _delete(alice, alice.password)
    assert response.status_code == 204, response.text
    assert response.headers.get_list("set-cookie")  # session cookies cleared
    assert _counts(admin_engine, alice.id) == (0, 0, 0, 0, 0)

    reset_rate_limits(settings)
    login = browser_client(app).post(
        "/api/v1/auth/login", json={"email": alice.email, "password": alice.password}
    )
    assert login.status_code == 401
    # The other account is untouched.
    assert _counts(admin_engine, bob.id) == (1, 1, 1, 0, 1)
    assert len(bob.client.get("/api/v1/masters").json()) == 1


def test_active_jobs_block_deletion_before_anything_is_removed(
    app, admin_engine, docx_bytes, monkeypatch
):
    dave = new_account(app)
    assert upload(dave, docx_bytes).status_code == 201
    monkeypatch.setattr(account, "active_jobs", lambda db, owner_id: 1)
    response = _delete(dave, dave.password)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "jobs_active"
    assert _counts(admin_engine, dave.id) == (1, 1, 1, 0, 1)


def test_the_database_function_needs_identity_and_request(app, runtime_engine):
    carol = new_account(app)
    with pytest.raises(DBAPIError, match="no current user"), runtime_engine.begin() as conn:
        conn.execute(text("SELECT app.delete_current_account()"))
    with pytest.raises(DBAPIError, match="not requested"), runtime_engine.begin() as conn:
        as_user(conn, carol.id)
        conn.execute(text("SELECT app.delete_current_account()"))


def test_a_requested_deletion_refuses_new_jobs(app, settings, runtime_engine):
    erin = new_account(app)
    with runtime_engine.begin() as conn:
        as_user(conn, erin.id)
        conn.execute(
            text("INSERT INTO app.account_deletions (owner_id) VALUES (:u)"), {"u": erin.id}
        )
    user = VerifiedUser(id=erin.id, email=erin.email, access_token="")
    with pytest.raises(ApiError) as refused, user_db(settings, user) as db:
        queue.enqueue(db, settings, erin.id, uuid.uuid4(), "analyze")
    assert refused.value.code == "account_deleting"
