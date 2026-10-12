"""Milestone 6e: the manual application tracker against the local Supabase stack.

Covers the API (create, list, edit, delete with optimistic concurrency), two
users' separation at the API and database layers (as the real runtime role),
the per-user entry cap under concurrency, and linking an approved resume.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.documents.convert import resolve_backend
from app.documents.fixtures import build_fixture_master
from tests.test_isolation import _settings_or_skip, as_user, new_account, reset_rate_limits
from tests.test_review_flow import drafted, render
from tests.test_tailoring_flow import make_app

pytestmark = pytest.mark.integration
URL = "/api/v1/applications"
ENTRY = {
    "company": "Fictional Retail Co.",
    "role": "Data Analyst",
    "job_url": "https://jobs.example.com/fictional/123",
    "applied_on": (date.today() - timedelta(days=10)).isoformat(),
    "status": "applied",
    "notes": "Referred by a fictional friend.\nRecruiter screen next week.",
}


@pytest.fixture(scope="module")
def settings():
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def app(settings):
    return make_app(settings, llm_provider="demo")


@pytest.fixture(scope="module")
def runtime_engine(settings):
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


def create(account, **overrides):
    response = account.client.post(URL, json={**ENTRY, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_edit_delete_with_optimistic_concurrency(app):
    account = new_account(app)
    assert account.client.get(URL).json() == []

    minimal = create(account, job_url=None, applied_on=None, notes=None, status="planned")
    full = create(account)
    assert full["company"] == ENTRY["company"] and full["notes"] == ENTRY["notes"]
    assert full["job_url"] == ENTRY["job_url"] and full["applied_on"] == ENTRY["applied_on"]
    assert full["lock_version"] == 1 and full["resume"] is None
    assert minimal["status"] == "planned" and minimal["applied_on"] is None

    # Most recent first: no date counts as the day it was added (today).
    listed = account.client.get(URL).json()
    assert [e["id"] for e in listed] == [minimal["id"], full["id"]]
    only_planned = account.client.get(f"{URL}?status=planned").json()
    assert [e["id"] for e in only_planned] == [minimal["id"]]
    assert account.client.get(f"{URL}/{full['id']}").json() == full

    entry_url = f"{URL}/{full['id']}"
    edited = account.client.put(
        entry_url, json={**ENTRY, "status": "interviewing", "notes": "", "expected_version": 1}
    )
    assert edited.status_code == 200, edited.text
    edited = edited.json()
    assert edited["status"] == "interviewing" and edited["notes"] is None
    assert edited["lock_version"] == 2 and edited["created_at"] == full["created_at"]
    assert datetime.fromisoformat(edited["updated_at"]) > datetime.fromisoformat(full["updated_at"])

    # A stale tab can neither overwrite nor delete the newer version.
    stale = account.client.put(entry_url, json={**ENTRY, "status": "offer", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    assert account.client.delete(f"{entry_url}?expected_version=1").status_code == 409
    assert account.client.get(entry_url).json()["status"] == "interviewing"

    assert account.client.delete(f"{entry_url}?expected_version=2").status_code == 204
    assert account.client.get(entry_url).status_code == 404
    assert account.client.delete(f"{entry_url}?expected_version=2").status_code == 404
    missing = account.client.put(
        f"{URL}/{uuid.uuid4()}", json={**ENTRY, "expected_version": 1}
    ).json()
    assert missing["error"]["code"] == "not_found"
    assert [e["id"] for e in account.client.get(URL).json()] == [minimal["id"]]


def test_each_user_sees_and_changes_only_their_own_tracker(app):
    alice, bob = new_account(app), new_account(app)
    entry = create(alice)
    entry_url = f"{URL}/{entry['id']}"

    assert bob.client.get(URL).json() == []
    assert bob.client.get(entry_url).status_code == 404
    put = bob.client.put(entry_url, json={**ENTRY, "status": "rejected", "expected_version": 1})
    assert put.status_code == 404
    assert bob.client.delete(f"{entry_url}?expected_version=1").status_code == 404
    bob_entry = create(bob, company="Another Fictional Co.")

    assert alice.client.get(entry_url).json() == entry
    assert [e["id"] for e in alice.client.get(URL).json()] == [entry["id"]]
    assert [e["id"] for e in bob.client.get(URL).json()] == [bob_entry["id"]]


def test_runtime_role_is_confined_to_its_own_rows(app, runtime_engine):
    alice, bob = new_account(app), new_account(app)
    entry = create(alice)
    with runtime_engine.begin() as conn:  # no identity: nothing visible
        assert conn.execute(text("SELECT count(*) FROM app.applications")).scalar_one() == 0
    with runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM app.applications WHERE owner_id = :a"), {"a": alice.id}
            ).scalar_one()
            == 0
        )
        changed = conn.execute(
            text("UPDATE app.applications SET status = 'rejected' WHERE id = :id"),
            {"id": entry["id"]},
        )
        removed = conn.execute(
            text("DELETE FROM app.applications WHERE id = :id"), {"id": entry["id"]}
        )
        assert changed.rowcount == 0 and removed.rowcount == 0
    with pytest.raises(DBAPIError, match="row-level security"), runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        conn.execute(
            text(
                "INSERT INTO app.applications (owner_id, company, role, status) "
                "VALUES (:a, 'X', 'Y', 'applied')"
            ),
            {"a": alice.id},
        )
    for statement in (
        "UPDATE app.applications SET owner_id = :b",
        "UPDATE app.applications SET created_at = now()",
        "UPDATE app.applications SET id = gen_random_uuid()",
    ):
        with (
            pytest.raises(DBAPIError, match="permission denied"),
            runtime_engine.begin() as conn,
        ):
            as_user(conn, alice.id)
            conn.execute(text(statement), {"b": bob.id})
    with pytest.raises(DBAPIError, match="check constraint"), runtime_engine.begin() as conn:
        as_user(conn, alice.id)
        conn.execute(
            text("UPDATE app.applications SET job_url = 'javascript:alert(1)' WHERE id = :id"),
            {"id": entry["id"]},
        )
    assert alice.client.get(f"{URL}/{entry['id']}").json() == entry


def test_entry_cap_holds_under_concurrent_requests(settings):
    app = make_app(settings, llm_provider="demo", max_applications_per_user=3)
    account = new_account(app)

    def add(i: int) -> int:
        client = TestClient(app, raise_server_exceptions=False, headers=account.client.headers)
        client.cookies.update(account.client.cookies)
        return client.post(URL, json={**ENTRY, "role": f"Analyst {i}"}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(add, range(6)))
    assert codes == [201, 201, 201, 409, 409, 409]
    full = account.client.post(URL, json=ENTRY).json()["error"]
    assert full["code"] == "tracker_full" and "3" in full["message"]
    assert len(account.client.get(URL).json()) == 3


@pytest.mark.skipif(resolve_backend() is None, reason="PDF converter not available")
def test_entries_link_only_the_owners_approved_resumes(app, runtime_engine, tmp_path):
    docx = build_fixture_master(tmp_path / "master.docx").read_bytes()
    alice, _ = drafted(app, docx)
    artifact = render(alice)["artifact"]
    approved = alice.client.post(
        f"{alice.run_url}/approve",
        json={
            "draft_version": artifact["draft_version"],
            "artifact_id": artifact["id"],
            "artifact_sha256": artifact["sha256"],
        },
    )
    assert approved.status_code == 200, approved.text
    [approval] = alice.client.get("/api/v1/approvals").json()
    assert "source_url" in approval

    entry = create(alice, approval_id=approval["id"])
    assert entry["resume"] == {
        "approval_id": approval["id"],
        "run_id": approval["run_id"],
        "filename": artifact["filename"],
        "draft_version": artifact["draft_version"],
        "approved_at": approval["approved_at"],
    }

    bob = new_account(app)
    for approval_id in (approval["id"], str(uuid.uuid4())):
        refused = bob.client.post(URL, json={**ENTRY, "approval_id": approval_id})
        assert refused.status_code == 422
        assert refused.json()["error"]["fields"][0]["loc"] == ["body", "approval_id"]
    bob_entry = create(bob)
    moved = bob.client.put(
        f"{URL}/{bob_entry['id']}",
        json={**ENTRY, "approval_id": approval["id"], "expected_version": 1},
    )
    assert moved.status_code == 422
    # Even bypassing the API, the composite foreign key refuses the cross-owner link.
    with pytest.raises(DBAPIError, match="foreign key"), runtime_engine.begin() as conn:
        as_user(conn, bob.id)
        conn.execute(
            text("UPDATE app.applications SET approval_id = :ap WHERE id = :id"),
            {"ap": approval["id"], "id": bob_entry["id"]},
        )

    # Unlinking keeps the approved resume itself.
    unlinked = alice.client.put(
        f"{URL}/{entry['id']}", json={**ENTRY, "approval_id": None, "expected_version": 1}
    ).json()
    assert unlinked["resume"] is None
    assert alice.client.get(f"/api/v1/approvals/{approval['id']}/download").status_code == 200
