"""Milestone 6d: bounded automatic ATS improvement (ADR 0008), end to end.

Runs against the local Supabase stack with the inline job runner. A scripted
fake model (reporting itself as "openai" so budgets and costs apply) and the
demo provider stand in for the real model; every fact here is fictional.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.ai.client import DemoClient, LLMError
from app.ai.pricing import Usage
from app.ai.schemas import BulletOut, EntryOut, ImprovePatchOut, TailoredDraftOut
from app.config import Settings
from app.tailoring.improve import STOP_REASONS
from tests.test_isolation import _settings_or_skip, new_account, reset_rate_limits
from tests.test_tailoring_flow import ScriptedClient, create_job, make_app, prepared_account

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def base_settings() -> Settings:
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def app(base_settings):
    return make_app(base_settings, llm_provider="demo")


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    from app.documents.fixtures import build_fixture_master

    return build_fixture_master(tmp_path_factory.mktemp("m6d") / "master.docx").read_bytes()


@pytest.fixture(scope="module")
def engine(base_settings):
    engine = create_engine(base_settings.database_url.get_secret_value())  # runtime role
    yield engine
    engine.dispose()


def thin_draft(payload):
    """Uses only the dashboard fact, leaving out the SQL and Python facts."""
    entry, fact = next(
        (e, f) for e in payload["entries"] for f in e["facts"] if f["text"].startswith("Created a")
    )
    return TailoredDraftOut(
        headline="Data Analyst",
        summary=None,
        entries=[
            EntryOut(
                entry_ref=entry["ref"],
                bullets=[
                    BulletOut(text=fact["text"], evidence_ids=[fact["id"]], requirement_ids=[])
                ],
            )
        ],
        skills=[],
        omitted_evidence_ids=[],
    )


def tableau_claim(payload):
    """An improvement that claims an unconfirmed tool."""
    entry = payload["entries"][0]
    fact = entry["facts"][0]
    return ImprovePatchOut(
        entries=[
            EntryOut(
                entry_ref=entry["ref"],
                bullets=[
                    BulletOut(
                        text="Built Tableau dashboards with SQL and Python.",
                        evidence_ids=[fact["id"]],
                        requirement_ids=["R2"],
                    )
                ],
            )
        ]
    )


def times_out(payload):
    raise LLMError("timeout", Usage(input_tokens=500))


def draft_run(monkeypatch, account, client):
    monkeypatch.setattr("app.ai.client.client_for", lambda settings: client)
    run = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    response = account.client.post(f"/api/v1/runs/{run['id']}/draft")
    assert response.status_code == 202, response.text
    return response.json()


def job_of(run, kind):
    return next(j for j in run["jobs"] if j["kind"] == kind)


def versions(account, run):
    return account.client.get(f"/api/v1/runs/{run['id']}/draft/versions").json()


def test_supported_gaps_are_closed_automatically(monkeypatch, app, docx_bytes):
    account = prepared_account(app, docx_bytes)
    client = ScriptedClient([thin_draft], improve=DemoClient._improve)
    run = draft_run(monkeypatch, account, client)

    draft = run["draft"]
    assert (draft["version"], draft["created_by"]) == (2, "improve")
    assert draft["validation"]["passed"] is True
    text = str(draft["content"])
    assert "Python and SQL scripts" in text and "Technical: SQL, Python" in text
    for unsupported in ("Tableau", "Snowflake", "Kubernetes"):
        assert unsupported not in text  # gaps the facts don't support stay gaps

    [attempt] = run["improvements"]
    assert (attempt["attempt"], attempt["status"], attempt["outcome"]) == (1, "saved", "improved")
    assert (attempt["base_version"], attempt["new_version"]) == (1, 2)
    assert attempt["score_after"] > attempt["score_before"]
    assert run["improvements_left"] == 1
    job = job_of(run, "improve")
    assert job["status"] == "succeeded" and job["result"]["saved_versions"] == [2]
    # No supported gap left, so the second attempt was never started (or paid for).
    assert job["result"]["stopped"] == "exhausted" and job["result"]["version"] == 2
    assert run["usage"]["calls"] == 3  # analysis, draft, one improvement

    improve_payload = client.payloads[-1]
    assert [e["ref"] for e in improve_payload["entries"]] == [
        e["entry_ref"] for e in improve_payload["current_draft"]["entries"]
    ]  # a concise patch request: only the entry holding supporting facts
    assert all(t["supporting_fact_ids"] for t in improve_payload["targets"])
    sent = str(improve_payload)
    for private in ("Jordan Example", "jordan@example.com", account.email, str(account.id)):
        assert private not in sent

    history = versions(account, run)
    assert [(v["version"], v["created_by"]) for v in history] == [
        (2, "improve"),
        (1, "generation"),
    ]
    assert history[0]["ats_score"] > history[1]["ats_score"]


def test_unsupported_suggestions_are_not_saved(monkeypatch, app, docx_bytes):
    account = prepared_account(app, docx_bytes)
    run = draft_run(monkeypatch, account, ScriptedClient([thin_draft], improve=tableau_claim))
    assert (run["draft"]["version"], run["draft"]["created_by"]) == (1, "generation")
    assert "Tableau" not in str(run["draft"]["content"])
    [attempt] = run["improvements"]
    assert (attempt["status"], attempt["outcome"], attempt["new_version"]) == (
        "rejected",
        "unsupported_changes",
        None,
    )
    assert job_of(run, "improve")["result"]["stopped"] == "unsupported_changes"
    assert run["improvements_left"] == 1


def test_attempts_are_capped_per_run_across_retries(monkeypatch, app, docx_bytes):
    account = prepared_account(app, docx_bytes)
    run = draft_run(monkeypatch, account, ScriptedClient([thin_draft], improve=times_out))
    # Attempt 1 and 2 time out (transient: the job retries); the third job attempt
    # finds both slots used and stops without calling the model again.
    assert [(a["attempt"], a["status"], a["outcome"]) for a in run["improvements"]] == [
        (1, "failed", "timeout"),
        (2, "failed", "timeout"),
    ]
    job = job_of(run, "improve")
    assert (job["status"], job["attempts"]) == ("succeeded", 3)
    assert job["result"]["stopped"] == "limit_reached"
    assert run["usage"]["calls"] == 4  # analysis, draft, two improvement calls
    assert run["draft"]["version"] == 1 and run["improvements_left"] == 0

    refused = account.client.post(
        f"/api/v1/runs/{run['id']}/draft/improve", json={"expected_version": 1}
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "improve_limit"


def test_manual_retry_after_edits_and_privacy(app, docx_bytes):
    account = prepared_account(app, docx_bytes)
    run = account.client.post("/api/v1/runs", json={"job_id": create_job(account)}).json()
    run = account.client.post(f"/api/v1/runs/{run['id']}/draft").json()
    # Demo drafts use every fact, so there is nothing supported left to add.
    assert run["draft"]["created_by"] == "generation" and run["improvements"] == []
    assert job_of(run, "improve")["result"]["stopped"] in {"exhausted", "no_gaps"}
    assert set(STOP_REASONS) >= {"exhausted", "no_gaps", "limit_reached"}

    content = run["draft"]["content"]
    entry = next(
        e for e in content["entries"] if any("SQL scripts" in b["text"] for b in e["bullets"])
    )
    index = next(i for i, b in enumerate(entry["bullets"]) if "SQL scripts" in b["text"])
    skill = next(i for i, s in enumerate(content["skills"]) if s["text"].startswith("Technical"))
    url = f"/api/v1/runs/{run['id']}"
    edited = account.client.post(
        f"{url}/draft/edits",
        json={
            "expected_version": run["draft"]["version"],
            "operations": [
                {"op": "remove_bullet", "entry_ref": entry["ref"], "index": index},
                {"op": "remove_skill", "index": skill},
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    version = edited.json()["draft"]["version"]

    stale = account.client.post(f"{url}/draft/improve", json={"expected_version": version - 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    bob = new_account(app)
    assert (
        bob.client.post(f"{url}/draft/improve", json={"expected_version": version}).status_code
        == 404
    )

    improved = account.client.post(f"{url}/draft/improve", json={"expected_version": version})
    assert improved.status_code == 202, improved.text
    body = improved.json()
    assert (body["draft"]["version"], body["draft"]["created_by"]) == (version + 1, "improve")
    assert "SQL scripts" in str(body["draft"]["content"])  # the removed fact, word for word
    assert body["improvements"][0]["outcome"] == "improved" and body["improvements_left"] == 1
    assert Decimal(body["usage"]["cost_usd"]) == 0  # demo mode is free


def test_attempt_rows_stay_narrow_for_the_runtime_role(monkeypatch, app, docx_bytes, engine):
    account = prepared_account(app, docx_bytes)
    run = draft_run(monkeypatch, account, ScriptedClient([thin_draft], improve=tableau_claim))
    bob = new_account(app)

    def as_owner(conn, owner_id):
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(owner_id)})

    with engine.begin() as conn:
        as_owner(conn, bob.id)
        assert conn.execute(text("SELECT count(*) FROM app.improvement_attempts")).scalar() == 0
    for statement, error in (
        ("UPDATE app.improvement_attempts SET targets = '[]'::jsonb", "permission denied"),
        ("UPDATE app.improvement_attempts SET attempt = 1", "permission denied"),
        ("DELETE FROM app.improvement_attempts", "permission denied"),
        (
            "INSERT INTO app.improvement_attempts (owner_id, run_id, job_id, attempt, base_version)"
            " SELECT owner_id, run_id, job_id, 3, 1 FROM app.improvement_attempts LIMIT 1",
            "attempt_range",
        ),
    ):
        with pytest.raises(Exception, match=error), engine.begin() as conn:
            as_owner(conn, account.id)
            conn.execute(text(statement))
    assert len(run["improvements"]) == 1
