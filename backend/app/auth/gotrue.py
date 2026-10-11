"""Minimal client for the Supabase Auth (GoTrue) REST API.

The browser never talks to Supabase Auth directly: the backend exchanges
credentials for tokens and keeps them in HttpOnly cookies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class AuthUnavailableError(RuntimeError):
    """Supabase Auth could not be reached or is not configured."""


class AuthServiceError(RuntimeError):
    def __init__(self, status: int, error_code: str | None, message: str | None) -> None:
        super().__init__(error_code or f"status {status}")
        self.status = status
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class AuthSession:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    user_id: uuid.UUID
    email: str | None


def _session_from(data: dict[str, Any]) -> AuthSession:
    user = data.get("user") or {}
    return AuthSession(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
        expires_in=int(data.get("expires_in") or 3600),
        user_id=uuid.UUID(str(user["id"])),
        email=user.get("email"),
    )


class SupabaseAuthClient:
    def __init__(self, settings: Settings) -> None:
        if settings.supabase_anon_key is None:
            raise AuthUnavailableError("SUPABASE_ANON_KEY is not set")
        self._base = f"{settings.supabase_url}/auth/v1"
        self._apikey = settings.supabase_anon_key.get_secret_value()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"apikey": self._apikey}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.request(
                    method, f"{self._base}{path}", json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise AuthUnavailableError("auth service unreachable") from exc
        data: dict[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                data = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                data = {}
        if response.status_code >= 500:
            raise AuthUnavailableError(f"auth service error {response.status_code}")
        if response.status_code >= 400:
            code = data.get("error_code") or data.get("error")
            message = data.get("msg") or data.get("error_description")
            raise AuthServiceError(
                response.status_code,
                str(code) if code else None,
                str(message) if message else None,
            )
        return data

    def sign_up(self, email: str, password: str) -> AuthSession | None:
        """Returns a session, or None when the project requires email confirmation."""
        data = self._request("POST", "/signup", json={"email": email, "password": password})
        return _session_from(data) if data.get("access_token") else None

    def sign_in(self, email: str, password: str) -> AuthSession:
        data = self._request(
            "POST",
            "/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        return _session_from(data)

    def refresh(self, refresh_token: str) -> AuthSession:
        data = self._request(
            "POST",
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        return _session_from(data)

    def sign_out(self, access_token: str) -> None:
        self._request("POST", "/logout", params={"scope": "local"}, access_token=access_token)

    def request_password_reset(self, email: str) -> None:
        self._request("POST", "/recover", json={"email": email})

    def verify_recovery(self, token_hash: str) -> AuthSession:
        data = self._request("POST", "/verify", json={"type": "recovery", "token_hash": token_hash})
        return _session_from(data)

    def update_password(self, access_token: str, password: str) -> None:
        self._request("PUT", "/user", json={"password": password}, access_token=access_token)
