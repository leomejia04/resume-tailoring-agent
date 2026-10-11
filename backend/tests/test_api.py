import asyncio
import json
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import PRODUCTION, TRUSTED_ORIGIN, browser_client, make_settings, with_fake_user

POSTING = """Data Analyst - Fictional Retail Co.

About the Role
Fictional Retail Co. is hiring a data analyst to support store operations.

Responsibilities:
- Build weekly sales dashboards in Power BI
- Reconcile inventory extracts with SQL

Required Qualifications:
- Experience with SQL and Excel
- Clear written communication

Preferred Qualifications:
- Python or R
- Retail analytics experience

Benefits
Health insurance and a hybrid schedule.
"""


def make_client(**overrides) -> TestClient:
    """A signed-in (fake user) browser client with CSRF set up."""
    app = create_app(make_settings(**overrides))
    with_fake_user(app)
    return browser_client(app)


def assert_error(response, status: int, code: str) -> dict:
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    return body


def test_health_has_request_id_and_security_headers(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers


def test_hsts_only_in_production():
    response = make_client(**PRODUCTION).get("/api/v1/health")
    assert "max-age" in response.headers["strict-transport-security"]


def test_preview_detects_sections_and_saves_nothing(client):
    response = client.post(
        "/api/v1/jobs/preview",
        json={
            "title": "Data Analyst",
            "company": "Fictional Retail Co.",
            "source_url": "https://jobs.example.com/123",
            "description": POSTING,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["saved"] is False
    assert body["company"] == "Fictional Retail Co."
    kinds = [(s["kind"], s["heading"]) for s in body["sections"]]
    assert kinds == [
        ("about_role", "About the Role"),
        ("responsibilities", "Responsibilities"),
        ("required", "Required Qualifications"),
        ("preferred", "Preferred Qualifications"),
        ("benefits", "Benefits"),
    ]
    assert body["word_count"] > 40
    assert [w["code"] for w in body["warnings"]] == ["short_description"]


def test_blank_optional_fields_become_null(client):
    response = client.post(
        "/api/v1/jobs/preview",
        json={"title": "  ", "company": "", "source_url": "", "description": POSTING},
    )
    assert response.status_code == 200
    assert response.json()["title"] is None
    assert response.json()["source_url"] is None


def test_missing_description_reports_field(client):
    body = assert_error(client.post("/api/v1/jobs/preview", json={}), 422, "validation_error")
    assert body["fields"][0]["loc"] == ["body", "description"]


def test_validation_errors_do_not_echo_submitted_values(client):
    secret = "PRIVATE-RESUME-TEXT-" * 20  # longer than the 200-char title limit
    response = client.post("/api/v1/jobs/preview", json={"title": secret, "description": POSTING})
    assert_error(response, 422, "validation_error")
    assert "PRIVATE-RESUME-TEXT" not in response.text


@pytest.mark.parametrize(
    ("payload", "field", "fragment"),
    [
        ({"description": "too short"}, "description", "at least 40 characters"),
        ({"description": "https://jobs.example.com/123"}, "description", "opening links"),
        ({"description": POSTING, "source_url": "ftp://example.com/x"}, "source_url", "URL"),
        (
            {"description": POSTING, "source_url": "https://user:pw@example.com/x"},
            "source_url",
            "username or password",
        ),
        ({"description": POSTING, "owner_id": "someone-else"}, "owner_id", "Extra inputs"),
    ],
)
def test_invalid_inputs_are_rejected(client, payload, field, fragment):
    body = assert_error(client.post("/api/v1/jobs/preview", json=payload), 422, "validation_error")
    messages = {e["loc"][-1]: e["message"] for e in body["fields"]}
    assert fragment.lower() in messages[field].lower()


def test_malformed_json_is_a_validation_error(client):
    response = client.post(
        "/api/v1/jobs/preview",
        content=b'{"description": ',
        headers={"content-type": "application/json"},
    )
    body = assert_error(response, 422, "validation_error")
    assert body["fields"][0]["message"] == "Request body is not valid JSON."


def test_declared_oversized_body_is_rejected_early():
    client = make_client(max_request_bytes=2048)
    response = client.post("/api/v1/jobs/preview", json={"description": "x" * 5000})
    assert_error(response, 413, "payload_too_large")


def test_streamed_oversized_body_without_length_is_rejected():
    """Chunked uploads have no Content-Length; bytes are counted as they arrive."""
    app = create_app(Settings(_env_file=None, max_request_bytes=2048))
    with_fake_user(app)
    csrf = "c" * 43  # double-submit: any value works if cookie and header match
    chunks = [b'{"description": "' + b"x" * 1500, b"x" * 1500 + b'"}']
    sent: list[dict] = []

    async def receive():
        body = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/jobs/preview",
        "raw_path": b"/api/v1/jobs/preview",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"host", b"testserver"),
            (b"origin", TRUSTED_ORIGIN.encode()),
            (b"cookie", f"rt_csrf={csrf}".encode()),
            (b"x-csrf-token", csrf.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )
    assert start["status"] == 413
    assert body["error"]["code"] == "payload_too_large"


def test_unknown_route_and_wrong_method(client):
    assert_error(client.get("/api/v1/nope"), 404, "not_found")
    assert_error(client.get("/api/v1/jobs/preview"), 405, "method_not_allowed")


def test_unhandled_error_is_safe_and_logged(caplog):
    app = create_app(Settings(_env_file=None))

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("database password is hunter2")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.request"):
        response = client.get("/api/v1/boom")
    body = assert_error(response, 500, "internal_error")
    assert "hunter2" not in response.text
    assert body["message"] == "Something went wrong on our side."
    assert response.headers["x-content-type-options"] == "nosniff"
    assert any(body["request_id"] in record.getMessage() for record in caplog.records)


def test_no_cors_headers_by_default(client):
    response = client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_configured_origin():
    client = make_client(cors_allowed_origins="http://localhost:5173")
    allowed = client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-request-id" in allowed.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-credentials" not in allowed.headers

    other = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_cors_preflight():
    client = make_client(cors_allowed_origins="http://localhost:5173")
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    ok = client.options(
        "/api/v1/jobs/preview", headers={"Origin": "http://localhost:5173", **preflight}
    )
    assert ok.status_code == 200
    assert "POST" in ok.headers["access-control-allow-methods"]
    denied = client.options(
        "/api/v1/jobs/preview", headers={"Origin": "https://evil.example", **preflight}
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_documents_the_real_error_envelope(client):
    spec = client.get("/api/openapi.json").json()
    responses = spec["paths"]["/api/v1/jobs/preview"]["post"]["responses"]
    assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert "HTTPValidationError" not in json.dumps(spec)


def test_docs_disabled_in_production():
    client = make_client(**PRODUCTION)
    assert client.get("/api/openapi.json").status_code == 404
    assert client.get("/api/docs").status_code == 404


def test_preview_requires_sign_in():
    app = create_app(make_settings())
    response = browser_client(app).post("/api/v1/jobs/preview", json={"description": POSTING})
    assert_error(response, 401, "unauthenticated")


def test_state_changes_need_csrf_token_and_trusted_origin(app):
    with_fake_user(app)
    plain = TestClient(app, raise_server_exceptions=False, headers={"Origin": TRUSTED_ORIGIN})
    no_token = plain.post("/api/v1/jobs/preview", json={"description": POSTING})
    assert_error(no_token, 403, "csrf_failed")

    client = browser_client(app)
    wrong = client.post(
        "/api/v1/jobs/preview", json={"description": POSTING}, headers={"X-CSRF-Token": "x" * 43}
    )
    assert_error(wrong, 403, "csrf_failed")

    foreign = client.post(
        "/api/v1/jobs/preview",
        json={"description": POSTING},
        headers={"Origin": "https://evil.example"},
    )
    assert_error(foreign, 403, "csrf_failed")

    ok = client.post("/api/v1/jobs/preview", json={"description": POSTING})
    assert ok.status_code == 200


def test_referer_is_used_when_origin_is_absent(app):
    with_fake_user(app)
    client = browser_client(app)
    del client.headers["Origin"]
    good = client.post(
        "/api/v1/jobs/preview",
        json={"description": POSTING},
        headers={"Referer": f"{TRUSTED_ORIGIN}/jobs/new"},
    )
    assert good.status_code == 200
    missing = client.post("/api/v1/jobs/preview", json={"description": POSTING})
    assert_error(missing, 403, "csrf_failed")


def test_csrf_cookie_is_readable_but_strict(app):
    response = TestClient(app).get("/api/v1/auth/csrf")
    cookie = response.headers["set-cookie"].lower()
    assert "rt_csrf=" in cookie
    assert "samesite=strict" in cookie
    assert "httponly" not in cookie  # the frontend must read it to echo it
