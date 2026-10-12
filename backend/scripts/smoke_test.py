"""End-to-end smoke test over HTTP against a running instance of the app (ADR 0012).

Usage (from backend/):
    .venv/Scripts/python -m scripts.smoke_test --base-url http://127.0.0.1:8002
    .venv/Scripts/python -m scripts.smoke_test --base-url http://127.0.0.1:8002 --delete-account

Walks the main flow as a browser would, with a fictional master and posting:
sign up (or sign in with SMOKE_EMAIL/SMOKE_PASSWORD when the project requires
email confirmation), upload the master, confirm its facts, enter a job, draft,
wait for the background worker, render, approve the exact PDF, download it,
and check the filename and hash. With --delete-account it then deletes the
account and confirms sign-in fails. Uses AI exactly as the deployment is
configured: against a real OpenAI key each run costs a few tenths of a cent,
bounded by the app's spending limits. Prints step names and timings only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

import httpx

from app.documents.fixtures import build_fixture_master

POSTING = """Data Analyst - Fictional Retail Co.

Responsibilities:
- Build weekly sales reporting with SQL and Python

Required Qualifications:
- Experience with SQL and Python
- Tableau dashboards for store managers

Preferred Qualifications:
- Snowflake data warehouse
"""
JOB_WAIT_SECONDS = 600


class SmokeError(RuntimeError):
    pass


def _ok(response: httpx.Response, *expected: int) -> dict:
    if response.status_code not in (expected or (200,)):
        code = ""
        with contextlib.suppress(ValueError):
            code = response.json().get("error", {}).get("code", "")
        raise SmokeError(
            f"{response.request.method} {response.url.path} -> {response.status_code} {code}"
        )
    return response.json() if response.content else {}


class Browser:
    def __init__(self, base_url: str) -> None:
        origin = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=f"{origin}/api/v1", timeout=60, headers={"Origin": origin}
        )
        token = _ok(self.client.get("/auth/csrf"))["csrf_token"]
        self.client.headers["X-CSRF-Token"] = token

    def wait_idle(self, run_id: str) -> dict:
        deadline = time.monotonic() + JOB_WAIT_SECONDS
        while True:
            run = _ok(self.client.get(f"/runs/{run_id}"))
            jobs = run.get("jobs", [])
            failed = [j for j in jobs if j["status"] == "failed"]
            if failed:
                raise SmokeError(f"job {failed[0]['kind']} failed: {failed[0]['error_code']}")
            if not any(j["status"] in ("queued", "running") for j in jobs):
                return run
            if time.monotonic() > deadline:
                raise SmokeError("background jobs did not finish (is the worker running?)")
            time.sleep(1.5)


def run(base_url: str, *, delete_account: bool) -> None:
    timings: list[tuple[str, float]] = []
    started = time.monotonic()

    def step(name: str) -> None:
        nonlocal started
        now = time.monotonic()
        timings.append((name, now - started))
        print(f"ok  {name:<34} {now - started:6.1f}s", flush=True)
        started = now

    browser = Browser(base_url)
    c = browser.client
    email = os.environ.get("SMOKE_EMAIL")
    password = os.environ.get("SMOKE_PASSWORD")
    if email and password:
        _ok(c.post("/auth/login", json={"email": email, "password": password}))
    else:
        email = f"smoke-{secrets.token_hex(5)}@example.test"
        password = secrets.token_urlsafe(18)
        signup = _ok(c.post("/auth/signup", json={"email": email, "password": password}), 201, 202)
        if signup.get("status") != "signed_in":
            raise SmokeError("email confirmation required: set SMOKE_EMAIL and SMOKE_PASSWORD")
    step("signed in")

    with tempfile.TemporaryDirectory() as tmp:
        docx = build_fixture_master(Path(tmp) / "master.docx").read_bytes()
    _ok(c.post("/masters", files={"file": ("master.docx", docx, "application/octet-stream")}), 201)
    for item in _ok(c.get("/evidence")):
        kind = item["suggested_type"] if item["suggested_type"] != "unclassified" else "other"
        _ok(
            c.post(
                f"/evidence/{item['id']}/versions",
                json={
                    "expected_version": 1,
                    "experience_type": kind,
                    "verification_status": "verified",
                },
            ),
            201,
        )
    profile = _ok(c.get("/profile"))
    _ok(
        c.put(
            "/profile",
            json={
                "expected_version": profile["lock_version"],
                "display_name": "Jordan Example",
                "filename_prefix": "JordanExample",
            },
        )
    )
    step("master uploaded, facts confirmed")

    job = _ok(
        c.post(
            "/jobs",
            json={
                "title": "Data Analyst",
                "company": "Fictional Retail Co.",
                "description": POSTING,
            },
        ),
        201,
    )
    run_out = _ok(c.post("/runs", json={"job_id": job["id"]}), 200, 201, 202)
    run_id = run_out["id"]
    browser.wait_idle(run_id)
    step("job analyzed")
    _ok(c.post(f"/runs/{run_id}/draft"), 200, 202)
    run_out = browser.wait_idle(run_id)
    if run_out["draft"] is None:
        raise SmokeError(f"no draft (run status {run_out['status']})")
    step("drafted (incl. automatic improvement)")

    _ok(c.post(f"/runs/{run_id}/render"), 202)
    run_out = browser.wait_idle(run_id)
    artifact = run_out["artifact"]
    if not artifact or not artifact["passed"] or artifact["page_count"] != 1:
        raise SmokeError("rendered PDF missing or failed its checks")
    step("rendered and checked")

    approval = _ok(
        c.post(
            f"/runs/{run_id}/approve",
            json={
                "draft_version": artifact["draft_version"],
                "artifact_id": artifact["id"],
                "artifact_sha256": artifact["sha256"],
            },
        )
    )["approval"]
    download = c.get(f"/approvals/{approval['id']}/download")
    if download.status_code != 200:
        raise SmokeError(f"download -> {download.status_code}")
    if hashlib.sha256(download.content).hexdigest() != artifact["sha256"]:
        raise SmokeError("downloaded PDF does not match the approved hash")
    expected = "JordanExample_Data_Analyst_Fictional_Retail_Co_CV.pdf"
    if expected not in download.headers.get("content-disposition", ""):
        raise SmokeError("download filename does not follow the naming rule")
    step("approved and downloaded")

    if delete_account:
        _ok(c.post("/account/deletion", json={"password": password, "confirmation": "DELETE"}), 204)
        again = Browser(base_url).client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        if again.status_code != 401:
            raise SmokeError("deleted account can still sign in")
        step("account deleted")
    print(f"\nSmoke test passed ({sum(t for _, t in timings):.0f}s).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--delete-account", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args.base_url, delete_account=args.delete_account)
    except (SmokeError, httpx.HTTPError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
