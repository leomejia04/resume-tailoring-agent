"""Application tracker input rules (milestone 6e) that need no database.

Integration tests with real accounts, RLS, and approvals live in
``test_tracker_flow.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.schemas.tracker import ApplicationInput, ApplicationUpdate
from tests.conftest import TRUSTED_ORIGIN, make_settings
from tests.test_api import assert_error

URL = "/api/v1/applications"
VALID = {"company": "Fictional Retail Co.", "role": "Data Analyst"}


def test_blank_optional_fields_become_null_and_status_defaults_to_applied():
    entry = ApplicationInput.model_validate(
        {**VALID, "job_url": " ", "approval_id": "", "applied_on": "", "notes": "  "}
    )
    assert entry.job_url is None and entry.approval_id is None
    assert entry.applied_on is None and entry.notes is None
    assert entry.status == "applied"


def test_notes_keep_line_breaks_and_text_is_trimmed():
    entry = ApplicationInput.model_validate(
        {"company": "  Fictional Co. ", "role": "Analyst", "notes": "Call 1\n\tfollow-up"}
    )
    assert entry.company == "Fictional Co."
    assert entry.notes == "Call 1\n\tfollow-up"


def test_tomorrow_is_allowed_for_time_zones_ahead_of_the_server():
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    assert ApplicationInput.model_validate({**VALID, "applied_on": tomorrow}).applied_on == tomorrow


@pytest.mark.parametrize(
    ("payload", "field", "fragment"),
    [
        ({"role": "Analyst"}, "company", "required"),
        ({**VALID, "company": "   "}, "company", "at least 1"),
        ({**VALID, "role": "x" * 201}, "role", "at most 200"),
        ({**VALID, "role": "Data\nAnalyst"}, "role", "control characters"),
        ({**VALID, "notes": "ok\x00"}, "notes", "control characters"),
        ({**VALID, "notes": "n" * 4001}, "notes", "at most 4000"),
        ({**VALID, "job_url": "javascript:alert(1)"}, "job_url", "URL"),
        ({**VALID, "job_url": "ftp://jobs.example.com/1"}, "job_url", "URL"),
        ({**VALID, "job_url": "https://u:p@jobs.example.com/1"}, "job_url", "username or password"),
        ({**VALID, "job_url": "https://example.com/" + "a" * 2040}, "job_url", "too long"),
        ({**VALID, "applied_on": "1999-12-31"}, "applied_on", "not in the future"),
        (
            {**VALID, "applied_on": (date.today() + timedelta(days=30)).isoformat()},
            "applied_on",
            "not in the future",
        ),
        ({**VALID, "status": "ghosted"}, "status", "Input should be"),
        ({**VALID, "approval_id": "not-a-uuid"}, "approval_id", "UUID"),
        ({**VALID, "owner_id": str(uuid.uuid4())}, "owner_id", "Extra inputs"),
        ({**VALID, "lock_version": 7}, "lock_version", "Extra inputs"),
    ],
)
def test_invalid_entries_are_rejected_by_the_api(client, payload, field, fragment):
    body = assert_error(client.post(URL, json=payload), 422, "validation_error")
    messages = {e["loc"][-1]: e["message"] for e in body["fields"]}
    assert fragment.lower() in messages[field].lower()


def test_validation_errors_do_not_echo_notes(client):
    secret = "PRIVATE-INTERVIEW-NOTE-" * 200
    response = client.post(URL, json={**VALID, "notes": secret})
    assert_error(response, 422, "validation_error")
    assert "PRIVATE-INTERVIEW-NOTE" not in response.text


def test_updates_and_deletes_must_name_the_version_they_replace(client):
    with pytest.raises(ValidationError):
        ApplicationUpdate.model_validate(VALID)
    entry_url = f"{URL}/{uuid.uuid4()}"
    body = assert_error(client.put(entry_url, json=VALID), 422, "validation_error")
    assert body["fields"][0]["loc"] == ["body", "expected_version"]
    body = assert_error(client.delete(entry_url), 422, "validation_error")
    assert body["fields"][0]["loc"] == ["query", "expected_version"]
    assert_error(client.get(f"{URL}?status=unknown"), 422, "validation_error")
    assert_error(client.get(f"{URL}/not-a-uuid"), 422, "validation_error")


def test_valid_entry_without_database_is_a_safe_503(client):
    assert_error(client.post(URL, json=VALID), 503, "service_unavailable")


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", URL), ("post", URL), ("get", f"{URL}/{uuid.uuid4()}")],
)
def test_tracker_requires_sign_in(method, path):
    app = create_app(make_settings())
    client = TestClient(app, raise_server_exceptions=False, headers={"Origin": TRUSTED_ORIGIN})
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    kwargs = {"json": VALID} if method == "post" else {}
    assert_error(getattr(client, method)(path, **kwargs), 401, "unauthenticated")


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", URL, {"json": VALID}),
        ("put", f"{URL}/{uuid.uuid4()}", {"json": {**VALID, "expected_version": 1}}),
        ("delete", f"{URL}/{uuid.uuid4()}?expected_version=1", {}),
    ],
)
def test_tracker_changes_need_csrf(client, method, path, kwargs):
    del client.headers["X-CSRF-Token"]
    assert_error(getattr(client, method)(path, **kwargs), 403, "csrf_failed")
