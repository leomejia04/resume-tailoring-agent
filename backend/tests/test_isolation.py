"""Integration tests against the local Supabase stack (`npx supabase start`).

They prove tenant isolation at every layer: the API, the database (as the real
least-privilege runtime role), and Supabase Storage. Skipped automatically when
the stack or .env configuration is unavailable.
"""

from __future__ import annotations

import io
import re
import secrets
import uuid
import zipfile
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.auth.cookies import cookie_names
from app.config import Settings
from app.documents.fixtures import build_fixture_master
from app.main import create_app
from tests.conftest import browser_client

pytestmark = pytest.mark.integration
EMAIL_DOMAIN = "example.test"


def _settings_or_skip() -> Settings:
    # Routine tests never reach the paid model, even when .env holds a real key, and
    # run each request's job inline (the worker loop has its own tests).
    settings = Settings().model_copy(
        update={"openai_api_key": None, "llm_provider": "demo", "job_runner": "inline"}
    )
    if settings.database_url is None or settings.supabase_anon_key is None:
        pytest.skip("local Supabase settings not configured in .env")
    try:
        httpx.get(
            f"{settings.supabase_url}/auth/v1/health",
            headers={"apikey": settings.supabase_anon_key.get_secret_value()},
            timeout=3,
        ).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("local Supabase stack is not running")
    return settings


@dataclass
class Account:
    client: TestClient
    id: uuid.UUID
    email: str
    password: str

    @property
    def access_token(self) -> str:
        return self.client.cookies[cookie_names(self.client.app.state.settings).access]


def reset_rate_limits(settings: Settings) -> None:
    """Test-only: start fresh windows so creating many accounts does not trip limits."""
    engine = create_engine(settings.database_url.get_secret_value())
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app.rate_limit_counters"))
    engine.dispose()


@pytest.fixture(scope="module")
def settings() -> Settings:
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


def new_account(app) -> Account:
    reset_rate_limits(app.state.settings)
    client = browser_client(app)
    email = f"isolation-{secrets.token_hex(6)}@{EMAIL_DOMAIN}"
    password = secrets.token_urlsafe(18)
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return Account(client, uuid.UUID(response.json()["user"]["id"]), email, password)


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("iso") / "master.docx").read_bytes()


@pytest.fixture(scope="module")
def alice(app, docx_bytes):
    account = new_account(app)
    response = account.client.post(
        "/api/v1/masters",
        files={"file": ("Alice Resume.docx", docx_bytes, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    account.master = response.json()["master"]  # type: ignore[attr-defined]
    return account


@pytest.fixture(scope="module")
def bob(app):
    return new_account(app)


def upload(account: Account, data: bytes, name: str = "resume.docx"):
    return account.client.post(
        "/api/v1/masters", files={"file": (name, data, "application/octet-stream")}
    )


# --- API layer -----------------------------------------------------------------


def test_session_cookies_are_httponly_and_strict(app):
    client = browser_client(app)
    email = f"isolation-{secrets.token_hex(6)}@{EMAIL_DOMAIN}"
    response = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": secrets.token_urlsafe(18)}
    )
    assert response.status_code == 201
    session_cookies = [
        c for c in response.headers.get_list("set-cookie") if "rt_access" in c or "rt_refresh" in c
    ]
    assert len(session_cookies) == 2
    for cookie in session_cookies:
        assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    assert "access_token" not in response.text  # tokens never reach JavaScript
    assert client.get("/api/v1/auth/me").json()["email"] == email


def test_owner_workflow(alice, docx_bytes):
    master = alice.master  # type: ignore[attr-defined]
    assert master["version_number"] == 1 and master["evidence_count"] == 11
    assert master["original_filename"] == "Alice Resume.docx"

    again = upload(alice, docx_bytes, "copy.docx")
    assert again.status_code == 200 and again.json()["duplicate"] is True

    evidence = alice.client.get("/api/v1/evidence").json()
    assert len(evidence) == 11 and {e["verification_status"] for e in evidence} == {"unverified"}

    file = alice.client.get(f"/api/v1/masters/{master['id']}/file")
    assert file.status_code == 200 and file.content == docx_bytes
    assert "attachment" in file.headers["content-disposition"]


def test_evidence_versions_are_immutable_and_concurrency_safe(alice):
    item = next(
        e
        for e in alice.client.get("/api/v1/evidence").json()
        if e["suggested_type"] == "internship"
    )
    url = f"/api/v1/evidence/{item['id']}/versions"
    confirmed = alice.client.post(
        url,
        json={
            "expected_version": 1,
            "experience_type": "internship",
            "verification_status": "verified",
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["version"] == 2 and confirmed.json()["change_source"] == "extracted"

    stale = alice.client.post(
        url,
        json={
            "expected_version": 1,
            "experience_type": "internship",
            "verification_status": "rejected",
        },
    )
    assert stale.status_code == 409

    edited = alice.client.post(
        url,
        json={
            "expected_version": 2,
            "text": "Built Python and SQL scripts reconciling weekly sales extracts for 3 regions.",
            "experience_type": "internship",
            "verification_status": "verified",
        },
    )
    assert edited.status_code == 201
    body = edited.json()
    assert body["change_source"] == "user" and body["metrics"] == ["3"]
    assert body["source_text"].startswith("Built Python and SQL scripts that reconciled")

    history = alice.client.get(url).json()
    assert [v["version"] for v in history] == [3, 2, 1]
    assert history[-1]["text"] == history[-1]["source_text"]

    unclassified = alice.client.post(
        url,
        json={
            "expected_version": 3,
            "experience_type": "unclassified",
            "verification_status": "verified",
        },
    )
    assert unclassified.status_code == 422


def test_other_user_cannot_see_or_touch_anything(alice, bob):
    master_id = alice.master["id"]  # type: ignore[attr-defined]
    evidence_id = alice.client.get("/api/v1/evidence").json()[0]["id"]
    before = alice.client.get(f"/api/v1/evidence/{evidence_id}/versions").json()

    assert bob.client.get("/api/v1/masters").json() == []
    assert bob.client.get("/api/v1/evidence").json() == []
    assert bob.client.get(f"/api/v1/evidence?master_version_id={master_id}").json() == []
    for path in (
        f"/api/v1/masters/{master_id}",
        f"/api/v1/masters/{master_id}/file",
        f"/api/v1/evidence/{evidence_id}/versions",
    ):
        response = bob.client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"

    attack = bob.client.post(
        f"/api/v1/evidence/{evidence_id}/versions",
        json={
            "expected_version": 1,
            "experience_type": "professional",
            "verification_status": "verified",
        },
    )
    assert attack.status_code == 404
    assert alice.client.get(f"/api/v1/evidence/{evidence_id}/versions").json() == before

    assert bob.client.get("/api/v1/profile").json()["email"] == bob.email


def test_profile_updates_use_optimistic_concurrency(alice):
    profile = alice.client.get("/api/v1/profile").json()
    payload = {
        "expected_version": profile["lock_version"],
        "display_name": "Alice Example",
        "filename_prefix": "AliceExample",
    }
    first = alice.client.put("/api/v1/profile", json=payload)
    assert first.status_code == 200 and first.json()["lock_version"] == profile["lock_version"] + 1
    assert alice.client.put("/api/v1/profile", json=payload).status_code == 409
    bad = alice.client.put(
        "/api/v1/profile",
        json={
            **payload,
            "expected_version": first.json()["lock_version"],
            "filename_prefix": "../x",
        },
    )
    assert bad.status_code == 422


def test_hostile_uploads_are_rejected(alice, docx_bytes):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/vbaProject.bin", b"macro")
    macro = upload(alice, out.getvalue())
    assert macro.status_code == 422 and macro.json()["error"]["code"] == "unsupported_content"

    huge = upload(alice, b"PK\x03\x04" + b"0" * (6 * 1024 * 1024))
    assert huge.status_code == 413
    assert len(alice.client.get("/api/v1/masters").json()) == 1


def test_missing_or_forged_session_is_rejected(app):
    anonymous = browser_client(app)
    assert anonymous.get("/api/v1/masters").json()["error"]["code"] == "unauthenticated"
    anonymous.cookies.set(cookie_names(app.state.settings).access, "eyJhbGciOiJub25lIn0.e30.")
    assert anonymous.get("/api/v1/masters").status_code == 401


def test_logout_revokes_the_refresh_token(app):
    account = new_account(app)
    names = cookie_names(app.state.settings)
    old_refresh = account.client.cookies[names.refresh]
    assert account.client.post("/api/v1/auth/logout").status_code == 204
    assert names.access not in account.client.cookies
    account.client.cookies.set(names.refresh, old_refresh)
    replay = account.client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "session_expired"


def test_refresh_rotates_the_session(app):
    account = new_account(app)
    names = cookie_names(app.state.settings)
    old_refresh = account.client.cookies[names.refresh]
    refreshed = account.client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert account.client.cookies[names.refresh] != old_refresh
    assert account.client.get("/api/v1/auth/me").status_code == 200


def test_login_is_rate_limited_per_email(app):
    account = new_account(app)
    client = browser_client(app)
    statuses = [
        client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": "wrong-password-1"}
        ).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


def test_password_reset_flow_through_email(app, settings):
    account = new_account(app)
    client = browser_client(app)
    accepted = client.post("/api/v1/auth/password-reset", json={"email": account.email})
    assert accepted.status_code == 202
    unknown = client.post(
        "/api/v1/auth/password-reset",
        json={"email": f"nobody-{secrets.token_hex(4)}@{EMAIL_DOMAIN}"},
    )
    assert (
        unknown.status_code == 202 and unknown.json() == accepted.json()
    )  # no account enumeration

    mailpit = "http://127.0.0.1:54324"
    messages = httpx.get(
        f"{mailpit}/api/v1/search", params={"query": f"to:{account.email}"}, timeout=5
    ).json()
    assert messages["messages"], "reset email was not delivered to Mailpit"
    message = httpx.get(
        f"{mailpit}/api/v1/message/{messages['messages'][0]['ID']}", timeout=5
    ).json()
    link = re.search(r'href="([^"]+reset-password#[^"]+)"', message["HTML"]).group(1)
    assert link.startswith("http://127.0.0.1:5173/reset-password#token_hash=")
    token_hash = re.search(r"token_hash=([A-Za-z0-9_\-]+)", link).group(1)

    new_password = secrets.token_urlsafe(18)
    done = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token_hash": token_hash, "password": new_password},
    )
    assert done.status_code == 200, done.text
    reused = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token_hash": token_hash, "password": new_password},
    )
    assert reused.status_code == 400

    fresh = browser_client(app)
    assert (
        fresh.post(
            "/api/v1/auth/login", json={"email": account.email, "password": account.password}
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/api/v1/auth/login", json={"email": account.email, "password": new_password}
        ).status_code
        == 200
    )


# --- Database layer (as the real runtime role) ------------------------------------


def as_user(conn, user_id: uuid.UUID | None) -> None:
    conn.execute(
        text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""}
    )


def test_rls_hides_rows_without_or_with_the_wrong_identity(runtime_engine, alice, bob):
    with runtime_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM app.master_versions")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM app.evidence_versions")).scalar_one() == 0
    with runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM app.master_versions WHERE owner_id = :a"),
                {"a": alice.id},
            ).scalar_one()
            == 0
        )
        moved = conn.execute(
            text("UPDATE app.evidence_records SET current_version = 99 WHERE owner_id = :a"),
            {"a": alice.id},
        )
        assert moved.rowcount == 0
    with runtime_engine.begin() as conn:
        as_user(conn, alice.id)
        assert conn.execute(text("SELECT count(*) FROM app.master_versions")).scalar_one() == 1


def test_rls_rejects_rows_claiming_another_owner(runtime_engine, alice, bob):
    with pytest.raises(DBAPIError, match="row-level security"), runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        conn.execute(text("INSERT INTO app.profiles (user_id) VALUES (:a)"), {"a": alice.id})


def test_cross_owner_references_are_impossible(runtime_engine, alice, bob):
    master_id = alice.master["id"]  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError), runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        conn.execute(
            text(
                "INSERT INTO app.evidence_records (owner_id, master_version_id, position) "
                "VALUES (:b, :m, 0)"
            ),
            {"b": bob.id, "m": master_id},
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.master_versions SET sha256 = repeat('0', 64)",
        "UPDATE app.evidence_versions SET text = 'rewritten'",
        "DELETE FROM app.evidence_versions",
        "DELETE FROM app.master_versions",
        "UPDATE app.evidence_records SET master_version_id = gen_random_uuid()",
        # Readable since 0010 (readiness check; no user data), never writable.
        "UPDATE app.alembic_version SET version_num = '0001'",
        "DELETE FROM app.alembic_version",
        "INSERT INTO app.alembic_version VALUES ('9999')",
        "SET ROLE authenticated",
        "SET ROLE service_role",
        "CREATE TABLE app.sneaky (id int)",
        "ALTER TABLE app.master_versions DISABLE ROW LEVEL SECURITY",
    ],
)
def test_runtime_role_cannot_rewrite_history_or_escalate(runtime_engine, alice, statement):
    with (
        pytest.raises(DBAPIError, match="permission denied|must be owner"),
        runtime_engine.begin() as conn,
    ):
        as_user(conn, alice.id)
        conn.execute(text(statement))


def test_identity_never_leaks_to_the_next_request(app, runtime_engine, alice):
    # The pool has one connection, so the next transaction reuses it.
    with runtime_engine.begin() as conn:
        as_user(conn, alice.id)
    with runtime_engine.begin() as conn:
        assert conn.execute(text("SELECT app.current_user_id()")).scalar_one() is None
        assert conn.execute(text("SELECT count(*) FROM app.master_versions")).scalar_one() == 0


# --- Storage and Supabase API surface ------------------------------------------


def test_storage_policies_isolate_objects(settings, alice, bob, docx_bytes):
    base = f"{settings.supabase_url}/storage/v1/object"
    apikey = settings.supabase_anon_key.get_secret_value()
    path = f"{alice.id}/{alice.master['id']}.docx"  # type: ignore[attr-defined]

    def headers(account):
        return {"apikey": apikey, "Authorization": f"Bearer {account.access_token}"}

    own = httpx.get(f"{base}/authenticated/masters/{path}", headers=headers(alice), timeout=10)
    assert own.status_code == 200 and own.content == docx_bytes
    stolen = httpx.get(f"{base}/authenticated/masters/{path}", headers=headers(bob), timeout=10)
    assert stolen.status_code in (400, 403, 404) and stolen.content != docx_bytes
    planted = httpx.post(
        f"{base}/masters/{alice.id}/planted.docx",
        content=docx_bytes,
        headers={**headers(bob), "Content-Type": "application/pdf"},
        timeout=10,
    )
    assert planted.status_code in (400, 403)
    public = httpx.get(f"{base}/public/masters/{path}", timeout=10)
    assert public.status_code in (400, 404)


def test_supabase_rest_api_does_not_expose_the_app_schema(settings, alice):
    apikey = settings.supabase_anon_key.get_secret_value()
    for profile_header in ({"Accept-Profile": "app"}, {}):
        response = httpx.get(
            f"{settings.supabase_url}/rest/v1/master_versions",
            headers={
                "apikey": apikey,
                "Authorization": f"Bearer {alice.access_token}",
                **profile_header,
            },
            timeout=10,
        )
        assert response.status_code >= 400
        assert alice.master["id"] not in response.text  # type: ignore[attr-defined]
