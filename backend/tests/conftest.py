"""Shared test helpers.

``browser_client`` behaves like our frontend: it sends a trusted Origin and the
double-submit CSRF token. ``fake_user`` replaces session verification for
tests that do not need a real Supabase stack.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import current_user
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.main import create_app

TRUSTED_ORIGIN = "http://127.0.0.1:5173"
PRODUCTION = {
    "app_env": "production",
    "trusted_origins": "https://app.example.com",
    "supabase_url": "https://project.supabase.co",
    # Fictional; nothing in these tests connects to them.
    "supabase_anon_key": "fictional-publishable-key",
    "database_url": "postgresql+psycopg://app_backend:fictional@db.example.invalid:5432/postgres",
}


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def browser_client(app: FastAPI, origin: str = TRUSTED_ORIGIN) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False, headers={"Origin": origin})
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    return client


def with_fake_user(app: FastAPI, user_id: uuid.UUID | None = None) -> VerifiedUser:
    user = VerifiedUser(id=user_id or uuid.uuid4(), email="jordan@example.com", access_token="fake")
    app.dependency_overrides[current_user] = lambda: user
    return user


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A signed-in (fake) browser client for routes that need no database."""
    with_fake_user(app)
    return browser_client(app)
