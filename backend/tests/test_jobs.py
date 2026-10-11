"""Milestone 6c: the durable job queue and worker (ADR 0007).

Integration tests use the local Supabase stack and the real worker functions
(``run_once``, ``maintain``, ``drain``); rendering needs the converter image.
A "crash" is simulated by claiming a job and never finishing it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.ai.budget import reserve, settle
from app.ai.pricing import Usage
from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.documents.convert import resolve_backend
from app.documents.fixtures import build_fixture_master
from app.jobs import queue, runner
from app.storage import SignedObject, StorageUnavailableError
from app.worker import drain, maintain
from tests.conftest import make_settings
from tests.test_isolation import _settings_or_skip, new_account, reset_rate_limits
from tests.test_tailoring_flow import create_job, make_app, prepared_account

# --- Units (no database) -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/object/sign/x?token=1",
        "//attacker.example/object/sign/x?token=1",
        "/object/sign/masters/a.docx",  # no token
        "/rest/v1/profiles?token=1",  # not a Storage object path
    ],
)
def test_signed_urls_must_be_relative_storage_paths(url):
    with pytest.raises(StorageUnavailableError):
        SignedObject(make_settings(), url)


def test_inline_jobs_are_refused_in_production():
    with pytest.raises(ValueError, match="JOB_RUNNER=worker"):
        make_settings(
            app_env="production",
            trusted_origins="https://app.example.com",
            supabase_url="https://project.supabase.co",
            job_runner="inline",
        )


def test_only_transient_failures_are_retried():
    from app.ai.client import LLMError
    from app.api.errors import ApiError

    assert runner.classify(LLMError("timeout")) in runner.RETRYABLE
    assert runner.classify(StorageUnavailableError("down")) in runner.RETRYABLE
    for permanent in (
        LLMError("refused"),
        ApiError(429, "budget_exceeded", "x"),
        ApiError(409, "conflict", "x"),
        queue.GrantExpiredError("pdf_upload"),
        KeyError("bug"),
    ):
        assert runner.classify(permanent) not in runner.RETRYABLE


# --- Integration -------------------------------------------------------------


@pytest.fixture(scope="module")
def base_settings() -> Settings:
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def worker_app(base_settings):
    return make_app(base_settings, job_runner="worker", job_retry_backoff_seconds=0)


@pytest.fixture(scope="module")
def settings(worker_app) -> Settings:
    return worker_app.state.settings


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("m6c") / "master.docx").read_bytes()


@pytest.fixture(scope="module")
def engine(base_settings):
    engine = create_engine(base_settings.database_url.get_secret_value())  # runtime role
    yield engine
    engine.dispose()


def as_owner(conn, owner_id) -> None:
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(owner_id)})


def job_of(run, kind):
    return next(j for j in run["jobs"] if j["kind"] == kind)


@pytest.mark.integration
@pytest.mark.skipif(resolve_backend() is None, reason="PDF converter not available")
def test_worker_runs_queued_work_end_to_end(worker_app, settings, docx_bytes, engine):
    drain(settings)  # start from an empty queue
    account = prepared_account(worker_app, docx_bytes)
    created = account.client.post("/api/v1/runs", json={"job_id": create_job(account)})
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["status"] == "queued" and job_of(run, "analyze")["status"] == "queued"
    url = f"/api/v1/runs/{run['id']}"

    assert drain(settings) == 1
    run = account.client.get(url).json()
    assert run["status"] in {"awaiting_confirmation", "ready"}
    assert job_of(run, "analyze")["status"] == "succeeded"
    assert run["ats"]["baseline"]["status"] == "complete"  # read through a signed URL

    first = account.client.post(f"{url}/draft")
    assert first.status_code == 202 and job_of(first.json(), "draft")["status"] == "queued"
    duplicate = account.client.post(f"{url}/draft")
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "job_in_progress"
    drain(settings)
    run = account.client.get(url).json()
    assert run["status"] == "awaiting_review" and run["draft"]["version"] == 1

    rendered = account.client.post(f"{url}/render")
    assert rendered.status_code == 202 and rendered.json()["artifact"] is None
    render_job = job_of(rendered.json(), "render")
    drain(settings)
    run = account.client.get(url).json()
    assert run["artifact"]["passed"] is True
    assert job_of(run, "render")["result"]["artifact_id"] == run["artifact"]["id"]
    assert "token=" not in str(run)  # signed URLs never leave the server
    with engine.begin() as conn:
        as_owner(conn, account.id)
        left = conn.execute(
            text("SELECT count(*) FROM app.job_grants WHERE job_id = :j"), {"j": render_job["id"]}
        ).scalar_one()
    assert left == 0  # grants are deleted when the job finishes
    approved = account.client.post(
        f"{url}/approve",
        json={
            "draft_version": run["artifact"]["draft_version"],
            "artifact_id": run["artifact"]["id"],
            "artifact_sha256": run["artifact"]["sha256"],
        },
    )
    assert approved.status_code == 200, approved.text


@pytest.mark.integration
def test_crashed_worker_is_recovered_and_fenced(worker_app, settings, docx_bytes, engine):
    drain(settings)
    account = prepared_account(worker_app, docx_bytes)
    run = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    job_id = uuid.UUID(job_of(run, "analyze")["id"])

    crashed = queue.claim(settings, "crashed-worker")  # claimed, then the process "dies"
    assert crashed is not None and crashed.job_id == job_id and crashed.attempt == 1
    assert drain(settings) == 0  # nobody else may take a leased job
    with engine.begin() as conn:
        as_owner(conn, account.id)
        conn.execute(
            text(
                "UPDATE app.jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"
            ),
            {"j": job_id},
        )
    recovered, _ = maintain(settings)
    assert recovered >= 1
    run = account.client.get(f"/api/v1/runs/{run['id']}").json()
    job = job_of(run, "analyze")
    assert (job["status"], job["attempts"], job["error_code"]) == ("queued", 1, "lease_expired")
    with engine.begin() as conn:  # recovery backs off a few seconds; make the retry due now
        as_owner(conn, account.id)
        conn.execute(text("UPDATE app.jobs SET run_after = now() WHERE id = :j"), {"j": job_id})

    assert drain(settings) == 1
    run = account.client.get(f"/api/v1/runs/{run['id']}").json()
    assert job_of(run, "analyze")["status"] == "succeeded"
    assert job_of(run, "analyze")["attempts"] == 2
    assert run["status"] in {"awaiting_confirmation", "ready"}
    # The crashed worker waking up later cannot overwrite the new outcome.
    assert not queue.finish(
        settings, account.id, job_id, "crashed-worker", "failed", error_code="x"
    )


@pytest.mark.integration
def test_a_job_that_keeps_crashing_fails_the_run_honestly(worker_app, settings, docx_bytes, engine):
    drain(settings)
    account = prepared_account(worker_app, docx_bytes)
    run = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    job_id = uuid.UUID(job_of(run, "analyze")["id"])
    for _ in range(settings.job_max_attempts):
        claimed = queue.claim(settings, "flaky-worker")
        assert claimed is not None and claimed.job_id == job_id
        with engine.begin() as conn:
            as_owner(conn, account.id)
            conn.execute(
                text(
                    "UPDATE app.jobs SET lease_expires_at = now() - interval '1 second', "
                    "run_after = now() WHERE id = :j"
                ),
                {"j": job_id},
            )
        maintain(settings)
        with engine.begin() as conn:  # recovery backs off; make the retry due now
            as_owner(conn, account.id)
            conn.execute(text("UPDATE app.jobs SET run_after = now() WHERE id = :j"), {"j": job_id})
    run = account.client.get(f"/api/v1/runs/{run['id']}").json()
    job = job_of(run, "analyze")
    assert (job["status"], job["attempts"]) == ("failed", settings.job_max_attempts)
    assert run["status"] == "failed" and run["error_code"] == "analysis_lease_expired"
    with engine.begin() as conn:
        as_owner(conn, account.id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM app.job_grants WHERE job_id = :j"), {"j": job_id}
            ).scalar_one()
            == 0
        )


@pytest.mark.integration
def test_cancel_stops_queued_and_running_jobs(worker_app, settings, docx_bytes):
    drain(settings)
    account = prepared_account(worker_app, docx_bytes)
    queued = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    cancelled = account.client.post(f"/api/v1/runs/{queued['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert job_of(cancelled, "analyze")["status"] == "cancelled"
    drain(settings)
    still = account.client.get(f"/api/v1/runs/{queued['id']}").json()
    assert job_of(still, "analyze")["status"] == "cancelled" and still["analysis"] is None

    running = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    claimed = queue.claim(settings, "busy-worker")
    assert claimed is not None and str(claimed.job_id) == job_of(running, "analyze")["id"]
    account.client.post(f"/api/v1/runs/{running['id']}/cancel")
    # The worker reaches its next checkpoint and stops instead of saving anything.
    assert runner.execute(settings, claimed.job_id, account.id, "busy-worker") == "cancelled"
    after = account.client.get(f"/api/v1/runs/{running['id']}").json()
    assert after["status"] == "cancelled" and after["analysis"] is None


@pytest.mark.integration
def test_active_jobs_per_user_are_limited(base_settings, docx_bytes):
    app = make_app(base_settings, job_runner="worker", max_active_jobs_per_user=2)
    drain(app.state.settings)
    account = prepared_account(app, docx_bytes)
    job_id = create_job(account)
    for _ in range(2):
        assert account.client.post("/api/v1/runs", json={"job_id": job_id}).status_code == 202
    third = account.client.post("/api/v1/runs", json={"job_id": job_id})
    assert third.status_code == 429 and third.json()["error"]["code"] == "too_many_jobs"
    assert len(account.client.get("/api/v1/runs").json()) == 2  # the refused run was rolled back
    drain(app.state.settings)


@pytest.mark.integration
def test_expired_reservations_are_returned_exactly_once(base_settings, worker_app, engine):
    account = new_account(worker_app)
    overrides = {"user_daily_budget_usd": Decimal("1.00"), "app_daily_budget_usd": Decimal("10000")}
    settings = Settings(_env_file=None, **{**base_settings.model_dump(), **overrides})
    user = VerifiedUser(id=account.id, email=None, access_token="")

    def reserved() -> Decimal:
        with engine.begin() as conn:
            as_owner(conn, account.id)
            return conn.execute(
                text("SELECT coalesce(sum(reserved_usd), 0) FROM app.spend_user")
            ).scalar_one()

    with user_db(settings, user) as db:
        reservation = reserve(db, settings, user.id, Decimal("0.40"))
    assert reserved() == Decimal("0.40")
    with engine.begin() as conn:  # the process "crashed" long ago: backdate the row
        as_owner(conn, account.id)
        conn.execute(
            text("DELETE FROM app.budget_reservations WHERE id = :r"), {"r": reservation.id}
        )
        conn.execute(
            text(
                "INSERT INTO app.budget_reservations (id, owner_id, day, amount_usd, expires_at) "
                "VALUES (:r, :o, :d, 0.40, now() - interval '1 minute')"
            ),
            {"r": reservation.id, "o": account.id, "d": reservation.day},
        )
    _, released = maintain(settings)
    assert released >= 1 and reserved() == Decimal("0")
    # A late settle records the cost but never subtracts the reservation twice.
    with user_db(settings, user) as db:
        settle(
            db,
            user.id,
            reservation,
            Decimal("0.01"),
            usage=Usage(),
            run_id=None,
            operation="test",
            model="test",
            prompt_version="test",
            latency_ms=0,
            outcome="success",
        )
    assert reserved() == Decimal("0")


@pytest.mark.integration
@pytest.mark.skipif(resolve_backend() is None, reason="PDF converter not available")
def test_expired_grants_fail_without_retrying(monkeypatch, worker_app, settings, docx_bytes):
    drain(settings)
    account = prepared_account(worker_app, docx_bytes)
    run = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    url = f"/api/v1/runs/{run['id']}"
    drain(settings)
    account.client.post(f"{url}/draft")
    drain(settings)
    account.client.post(f"{url}/render")

    real = datetime

    class ThreeHoursLater(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(hours=3)

    monkeypatch.setattr("app.jobs.queue.datetime", ThreeHoursLater)
    drain(settings)
    job = job_of(account.client.get(url).json(), "render")
    assert (job["status"], job["attempts"], job["error_code"]) == ("failed", 1, "grant_expired")


@pytest.mark.integration
def test_queue_privileges_stay_narrow(worker_app, docx_bytes, engine):
    alice = prepared_account(worker_app, docx_bytes)
    run = alice.client.post("/api/v1/runs", json={"job_id": create_job(alice)}).json()
    bob = new_account(worker_app)
    with engine.begin() as conn:
        as_owner(conn, bob.id)
        assert conn.execute(text("SELECT count(*) FROM app.jobs")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM app.job_grants")).scalar_one() == 0
        moved = conn.execute(
            text("UPDATE app.jobs SET cancel_requested = true WHERE run_id = :r"), {"r": run["id"]}
        )
        assert moved.rowcount == 0
    for statement in (
        "SET ROLE app_queue",
        "UPDATE app.jobs SET owner_id = gen_random_uuid()",
        "UPDATE app.jobs SET params = '{}'::jsonb",
        "UPDATE app.job_grants SET url = '/object/x?token=1'",
        "UPDATE app.budget_reservations SET amount_usd = 0",
    ):
        with pytest.raises(Exception, match="permission denied"), engine.begin() as conn:
            as_owner(conn, alice.id)
            conn.execute(text(statement))
    with engine.begin() as conn:
        for role in ("anon", "authenticated", "service_role"):
            can = conn.execute(
                text(
                    "SELECT has_function_privilege("
                    ":r, 'app.claim_job(text,integer,integer)', 'EXECUTE')"
                ),
                {"r": role},
            ).scalar_one()
            assert can is False, role
    drain(worker_app.state.settings)
