"""Milestone 6b: saved ATS reports, staleness, and the approval gate.

Needs the local Supabase stack and the Docker converter image (real PDFs are
rendered and their extracted text is scored). No real model is called.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.documents.convert import resolve_backend
from app.documents.extract import plain_text
from app.documents.fixtures import build_fixture_master
from app.storage import StorageUnavailableError
from tests.test_isolation import _settings_or_skip, new_account, reset_rate_limits
from tests.test_review_flow import drafted, render
from tests.test_tailoring_flow import create_job, make_app, prepared_account

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(resolve_backend() is None, reason="PDF converter not available"),
]


@pytest.fixture(scope="module")
def base_settings() -> Settings:
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def demo_app(base_settings):
    return make_app(base_settings, llm_provider="demo")


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("m6b") / "master.docx").read_bytes()


def approve_body(artifact):
    return {
        "draft_version": artifact["draft_version"],
        "artifact_id": artifact["id"],
        "artifact_sha256": artifact["sha256"],
    }


def first_bullet_edit(run):
    entry = next(e for e in run["draft"]["content"]["entries"] if len(e["bullets"]) >= 2)
    return {
        "expected_version": run["draft"]["version"],
        "operations": [{"op": "move_bullet", "entry_ref": entry["ref"], "index": 0, "to": 1}],
    }


def test_master_text_keeps_every_line(docx_bytes):
    lines = plain_text(docx_bytes, "docx").splitlines()
    assert "Jordan Example" in lines[0]  # header lines are part of what an ATS reads
    assert any(line.startswith("Built Python and SQL scripts") for line in lines)


def test_every_version_and_pdf_gets_a_report(demo_app, docx_bytes):
    account, run = drafted(demo_app, docx_bytes)
    ats = run["ats"]
    version = run["draft"]["version"]

    assert ats["rubric_version"] == "ats-rubric/v1"
    baseline, draft = ats["baseline"], ats["draft"]
    assert baseline["status"] == "complete" and baseline["master_version_id"]
    assert draft["status"] == "complete" and draft["draft_version"] == version
    assert isinstance(draft["score"], int) and 0 <= draft["score"] <= 100
    assert draft["score"] == draft["result"]["score"]
    assert {r["status"] for r in draft["result"]["requirements"]} <= {
        "direct",
        "related",
        "missing",
        "uncertain",
    }
    assert ats["final_pdf"] is None  # nothing rendered yet
    comparison = ats["draft_vs_baseline"]
    assert comparison["compatible"] is True
    assert comparison["delta"] == draft["score"] - baseline["score"]

    history = account.client.get(f"{account.run_url}/draft/versions").json()
    assert (history[0]["ats_status"], history[0]["ats_score"]) == ("complete", draft["score"])

    # A direct edit creates a new version, and the new version is checked too.
    edited = account.client.post(f"{account.run_url}/draft/edits", json=first_bullet_edit(run))
    assert edited.status_code == 200, edited.text
    run = edited.json()
    assert run["ats"]["draft"]["status"] == "complete"
    assert run["ats"]["draft"]["draft_version"] == version + 1

    # Rendering checks the exact PDF's text.
    rendered = render(account)
    artifact, pdf_report = rendered["artifact"], rendered["ats"]["final_pdf"]
    assert pdf_report["status"] == "complete"
    assert pdf_report["artifact_id"] == artifact["id"]
    assert pdf_report["draft_version"] == artifact["draft_version"]
    assert rendered["ats"]["pdf_vs_baseline"]["compatible"] is True
    passages = [m["where"] for r in pdf_report["result"]["requirements"] for m in r["matches"]]
    assert any(w.endswith("(PDF)") or w == "PDF text" for w in passages)

    # Any later change makes that PDF's check stale; it has no number to show.
    changed = account.client.post(f"{account.run_url}/draft/edits", json=first_bullet_edit(run))
    stale = changed.json()["ats"]["final_pdf"]
    assert stale["status"] == "stale" and stale["score"] is None and stale["result"] is None
    assert stale["draft_version"] == artifact["draft_version"]
    old = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert old.status_code == 409 and old.json()["error"]["code"] == "stale_approval"

    artifact = render(account)["artifact"]
    approved = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert approved.status_code == 200, approved.text


def test_failed_pdf_check_blocks_approval_until_rechecked(monkeypatch, demo_app, docx_bytes):
    account, _ = drafted(demo_app, docx_bytes)

    def broken(*args, **kwargs):
        raise RuntimeError("scoring bug")

    monkeypatch.setattr("app.ats.reports.pdf_passages", broken)
    rendered = render(account)
    artifact, report = rendered["artifact"], rendered["ats"]["final_pdf"]
    assert artifact["passed"] is True  # layout checks are separate from the ATS check
    assert report["status"] == "failed" and report["score"] is None and report["message"]
    refused = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ats_check_failed"

    monkeypatch.undo()
    checked = account.client.post(f"{account.run_url}/ats/check")
    assert checked.status_code == 200, checked.text
    report = checked.json()["ats"]["final_pdf"]
    assert report["status"] == "complete" and report["artifact_id"] == artifact["id"]
    again = account.client.post(f"{account.run_url}/ats/check").json()["ats"]["final_pdf"]
    assert again["checked_at"] == report["checked_at"]  # complete reports are not redone
    approved = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert approved.status_code == 200, approved.text


def test_baseline_failure_never_fails_the_run(monkeypatch, demo_app, docx_bytes):
    account = prepared_account(demo_app, docx_bytes)
    job_id = create_job(account)

    def unavailable(self, path):
        raise StorageUnavailableError("down")

    monkeypatch.setattr("app.tailoring.runs.UserStorage.download", unavailable)
    run = account.client.post("/api/v1/runs", json={"job_id": job_id}).json()
    assert run["status"] in {"ready", "awaiting_confirmation"}
    baseline = run["ats"]["baseline"]
    assert baseline["status"] == "failed" and baseline["score"] is None
    assert "could not be read" in baseline["message"]

    monkeypatch.undo()
    run = account.client.post(f"/api/v1/runs/{run['id']}/ats/check").json()
    assert run["ats"]["baseline"]["status"] == "complete"


def test_rubric_upgrade_requires_a_fresh_check(monkeypatch, demo_app, docx_bytes):
    account, _ = drafted(demo_app, docx_bytes)
    artifact = render(account)["artifact"]

    monkeypatch.setattr("app.ats.reports.RUBRIC_VERSION", "ats-rubric/v999-test")
    run = account.client.get(account.run_url).json()
    for kind in ("baseline", "draft", "final_pdf"):
        assert run["ats"][kind]["status"] == "stale", kind
        assert run["ats"][kind]["score"] is None
    assert run["ats"]["draft_vs_baseline"] is None
    refused = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ats_check_stale"

    run = account.client.post(f"{account.run_url}/ats/check").json()
    assert {run["ats"][k]["status"] for k in ("baseline", "draft", "final_pdf")} == {"complete"}
    approved = account.client.post(f"{account.run_url}/approve", json=approve_body(artifact))
    assert approved.status_code == 200, approved.text


def test_reports_are_private_and_insert_only(demo_app, docx_bytes, base_settings):
    alice, run = drafted(demo_app, docx_bytes)
    bob = new_account(demo_app)
    assert bob.client.post(f"{alice.run_url}/ats/check").status_code == 404
    assert bob.client.get(alice.run_url).status_code == 404

    engine = create_engine(base_settings.database_url.get_secret_value())
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(bob.id)})
            visible = conn.execute(
                text("SELECT count(*) FROM app.ats_reports WHERE run_id = :r"), {"r": run["id"]}
            ).scalar_one()
            assert visible == 0
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(alice.id)})
            mine = conn.execute(
                text("SELECT count(*) FROM app.ats_reports WHERE run_id = :r"), {"r": run["id"]}
            ).scalar_one()
            assert mine >= 2  # baseline and first draft
        for statement in (
            "UPDATE app.ats_reports SET score = 100",
            "DELETE FROM app.ats_reports",
        ):
            with pytest.raises(Exception, match="permission denied"), engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.user_id', :u, true)"), {"u": str(alice.id)}
                )
                conn.execute(text(statement))
        # Bob cannot attach a report to Alice's run, even naming himself as owner.
        with pytest.raises(Exception, match="violates|row-level security"), engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(bob.id)})
            conn.execute(
                text(
                    "INSERT INTO app.ats_reports (owner_id, run_id, analysis_id, kind, "
                    "master_version_id, status, error_code, rubric_version, analysis_fingerprint) "
                    "SELECT :b, :r, gen_random_uuid(), 'master_baseline', gen_random_uuid(), "
                    "'failed', 'x', 'v', repeat('0', 64)"
                ),
                {"b": bob.id, "r": run["id"]},
            )
    finally:
        engine.dispose()
