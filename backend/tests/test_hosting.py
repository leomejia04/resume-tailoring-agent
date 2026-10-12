"""Milestone 8 hosting pieces: same-origin frontend, readiness, production
settings, and the worker heartbeat (ADR 0012)."""

from __future__ import annotations

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import health, worker
from app.main import create_app
from tests.conftest import PRODUCTION, make_settings

INDEX = (
    '<!doctype html><title>Resume Tailor</title><script type="module" src="/assets/app-1a2b.js">'
    "</script>"
)


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "assets" / "app-1a2b.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def site(dist) -> TestClient:
    return TestClient(create_app(make_settings(frontend_dist=dist)), raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/", "/runs/123", "/reset-password", "/tracker?status=applied"])
def test_client_routes_load_the_app_page(site, path):
    response = site.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_hashed_assets_are_cached_and_missing_ones_are_404(site):
    asset = site.get("/assets/app-1a2b.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert "javascript" in asset.headers["content-type"]
    assert site.get("/favicon.svg").headers["cache-control"] == "public, max-age=3600"
    missing = site.get("/assets/other.js")
    assert missing.status_code == 404
    assert "Resume Tailor" not in missing.text


@pytest.mark.parametrize(
    "path",
    ["/assets/../index.html", "/assets/..%2f..%2fpyproject.toml", "/../.env", "/%2e%2e/.env"],
)
def test_only_build_files_are_served(site, path):
    response = site.get(path)
    # Either the app page or a 404; never a file from outside the build.
    assert response.status_code in {200, 404}
    assert response.text in {INDEX, "Not found"}


def test_api_paths_keep_api_behavior(site):
    health_response = site.get("/api/v1/health")
    assert health_response.json()["status"] == "ok"
    assert "default-src 'none'" in health_response.headers["content-security-policy"]
    unknown = site.get("/api/v1/nope")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    assert site.get("/api").status_code == 404
    # Wrong method on a real API route stays 405, not the app page.
    assert site.get("/api/v1/jobs/preview").status_code == 405


def test_non_get_requests_never_get_the_page(site):
    response = site.post("/runs/123", headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code in {403, 404, 405}
    assert response.text != INDEX


def test_frontend_dist_without_index_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="FRONTEND_DIST"):
        make_settings(frontend_dist=tmp_path)


def test_no_frontend_by_default(client):
    assert client.get("/").status_code == 404


@pytest.mark.parametrize("missing", ["database_url", "supabase_anon_key"])
def test_production_requires_connection_settings(missing):
    settings = {**PRODUCTION}
    del settings[missing]
    with pytest.raises(ValidationError, match=missing.upper()):
        make_settings(**settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("supabase_anon_key", "replace-with-publishable-key"),
        ("database_url", "postgresql+psycopg://app_backend:replace-me@db.example.invalid/postgres"),
        ("openai_api_key", "replace-me"),
    ],
)
def test_production_refuses_placeholder_secrets(field, value):
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(**{**PRODUCTION, field: value})


def test_issuer_override_is_local_only():
    settings = make_settings(
        supabase_url="http://host.docker.internal:54321",
        supabase_jwt_issuer="http://127.0.0.1:54321/auth/v1",
    )
    assert settings.jwt_issuer == "http://127.0.0.1:54321/auth/v1"
    assert settings.auth_url == "http://host.docker.internal:54321/auth/v1"
    assert make_settings().jwt_issuer == make_settings().auth_url
    with pytest.raises(ValidationError, match="SUPABASE_JWT_ISSUER"):
        make_settings(**PRODUCTION, supabase_jwt_issuer="https://other.example/auth/v1")


def test_placeholders_are_fine_outside_production():
    make_settings(supabase_anon_key="replace-with-publishable-key")


def test_production_settings_fixture_is_valid():
    assert make_settings(**PRODUCTION).app_env == "production"


@pytest.fixture
def fresh_readiness():
    health.reset_cache()
    yield
    health.reset_cache()


def test_ready_is_503_without_a_database(fresh_readiness):
    client = TestClient(create_app(make_settings()))
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "failed", "migrations": "failed"},
    }


def test_ready_results_are_cached(fresh_readiness, monkeypatch):
    calls = []

    def probe(settings):
        calls.append(1)
        return health.ReadyResponse(status="ok", checks={"database": "ok", "migrations": "ok"})

    monkeypatch.setattr(health, "_probe", probe)
    client = TestClient(create_app(make_settings()))
    for _ in range(5):
        assert client.get("/api/v1/health/ready").status_code == 200
    assert len(calls) == 1


def test_ready_against_the_local_stack(fresh_readiness):
    from tests.test_isolation import _settings_or_skip

    # Runs as app_backend, which may read only app.alembic_version (migration 0010).
    response = TestClient(create_app(_settings_or_skip())).get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "migrations": "ok"}}


def test_migration_state():
    head, known = health._revisions()
    assert head == "0011" and "0001" in known
    assert health.migration_state(head) == "ok"
    assert health.migration_state("0010") == "behind"
    assert health.migration_state(None) == "behind"
    # A newer release migrated first; this (older) image keeps serving.
    assert health.migration_state("0099") == "ok"


def test_heartbeat_round_trip(tmp_path):
    settings = make_settings(worker_heartbeat_file=tmp_path / "beat")
    assert not worker.check_heartbeat(settings)
    alive = threading.Thread(target=time.sleep, args=(1,))
    alive.start()
    worker.beat(settings, [alive])
    assert worker.check_heartbeat(settings)
    later = time.time() + worker.HEARTBEAT_MAX_AGE_SECONDS + 1
    assert not worker.check_heartbeat(settings, now=later)
    alive.join()


def test_heartbeat_not_written_when_a_job_thread_died(tmp_path):
    settings = make_settings(worker_heartbeat_file=tmp_path / "beat")
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    worker.beat(settings, [dead])
    assert not (tmp_path / "beat").exists()


def test_heartbeat_check_handles_garbage_and_unset(tmp_path):
    path = tmp_path / "beat"
    path.write_text("not a number", encoding="utf-8")
    assert not worker.check_heartbeat(make_settings(worker_heartbeat_file=path))
    assert not worker.check_heartbeat(make_settings())


def test_worker_stops_unhealthy_when_a_job_thread_dies(monkeypatch):
    def run_once(settings, worker_id):
        raise KeyboardInterrupt  # not an Exception: escapes the loop and ends the thread

    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(worker, "maintain", lambda settings: (0, 0))
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(worker, "MAINTENANCE_SECONDS", 0.05)
    stop = threading.Event()
    assert worker.serve(make_settings(worker_concurrency=1), stop) is False
    assert stop.is_set()


def test_worker_stops_healthy_on_request(monkeypatch):
    monkeypatch.setattr(worker, "run_once", lambda settings, worker_id: False)
    monkeypatch.setattr(worker, "maintain", lambda settings: (0, 0))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert worker.serve(make_settings(worker_concurrency=1, worker_poll_seconds=0.1), stop)


def test_worker_check_flag_exit_codes(tmp_path, monkeypatch):
    settings = make_settings(worker_heartbeat_file=tmp_path / "beat")
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    with pytest.raises(SystemExit) as stale:
        worker.main(["--check"])
    assert stale.value.code == 1
    (tmp_path / "beat").write_text(f"{time.time():.0f}", encoding="utf-8")
    with pytest.raises(SystemExit) as fresh:
        worker.main(["--check"])
    assert fresh.value.code == 0
