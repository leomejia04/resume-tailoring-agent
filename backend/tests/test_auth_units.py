"""Token verification, cookies, and auth-route behavior without a live Supabase."""

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth import gotrue
from app.auth.cookies import cookie_names, set_session_cookies
from app.auth.gotrue import AuthSession
from app.auth.tokens import TokenError, TokenVerifier
from app.main import create_app
from tests.conftest import PRODUCTION, browser_client, make_settings

ISSUER = "http://127.0.0.1:54321/auth/v1"


class StaticJWKClient:
    """Stands in for PyJWKClient so tests never fetch keys over the network."""

    def __init__(self, public_key) -> None:
        self._key = jwt.PyJWK.from_dict(
            {
                **json.loads(jwt.algorithms.ECAlgorithm.to_jwk(public_key)),
                "alg": "ES256",
                "kid": "k1",
            }
        )

    def get_signing_key_from_jwt(self, token: str):
        if jwt.get_unverified_header(token).get("kid") != "k1":
            raise jwt.PyJWKClientError("unknown kid")
        return self._key


@pytest.fixture(scope="module")
def keypair():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def verifier(keypair):
    return TokenVerifier(ISSUER, jwk_client=StaticJWKClient(keypair.public_key()))


def claims(**overrides):
    now = int(time.time())
    base = {
        "sub": str(uuid.uuid4()),
        "aud": "authenticated",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 600,
        "role": "authenticated",
        "email": "jordan@example.com",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign(keypair, payload, kid="k1", alg="ES256"):
    return jwt.encode(payload, keypair, algorithm=alg, headers={"kid": kid})


def test_valid_token(verifier, keypair):
    payload = claims()
    user = verifier.verify(sign(keypair, payload))
    assert str(user.id) == payload["sub"]
    assert user.email == "jordan@example.com"
    assert "fake" not in repr(user) and user.access_token not in repr(user)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"aud": "anon"}, "invalid"),
        ({"iss": "https://evil.example/auth/v1"}, "invalid"),
        ({"role": "service_role"}, "invalid"),
        ({"is_anonymous": True}, "invalid"),
        ({"sub": "not-a-uuid"}, "invalid"),
        ({"sub": None}, "invalid"),
        ({"exp": None}, "invalid"),
    ],
)
def test_rejected_claims(verifier, keypair, overrides, reason):
    with pytest.raises(TokenError) as excinfo:
        verifier.verify(sign(keypair, claims(**overrides)))
    assert excinfo.value.reason == reason


def test_rejects_other_keys_and_symmetric_or_unsigned_tokens(verifier, keypair):
    other = ec.generate_private_key(ec.SECP256R1())
    forged = [
        sign(other, claims()),  # right kid, wrong key
        sign(keypair, claims(), kid="unknown"),
        jwt.encode(
            claims(), "shared-secret-shared-secret-1234", algorithm="HS256", headers={"kid": "k1"}
        ),
        jwt.encode(claims(), None, algorithm="none", headers={"kid": "k1"}),
        "not.a.jwt",
        "",
        "x" * 9000,
    ]
    for token in forged:
        with pytest.raises(TokenError):
            verifier.verify(token)


def test_cookie_flags_in_production():
    settings = make_settings(**PRODUCTION)
    names = cookie_names(settings)
    assert names.access == "__Host-rt_access"
    from starlette.responses import Response

    response = Response()
    session = AuthSession("a" * 40, "r" * 40, 3600, uuid.uuid4(), "j@example.com")
    set_session_cookies(response, settings, session)
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        lowered = cookie.lower()
        assert "httponly" in lowered and "secure" in lowered
        assert "samesite=strict" in lowered and "path=/" in lowered
        assert "domain=" not in lowered


def test_login_failure_is_generic_and_sets_no_cookies(monkeypatch):
    app = create_app(make_settings(supabase_anon_key="publishable-test-key"))
    monkeypatch.setattr("app.api.v1.auth_routes.enforce_limit", lambda *a: None)

    def refuse(self, email, password):
        raise gotrue.AuthServiceError(400, "invalid_credentials", "Invalid login credentials")

    monkeypatch.setattr(gotrue.SupabaseAuthClient, "sign_in", refuse)
    response = browser_client(app).post(
        "/api/v1/auth/login", json={"email": "jordan@example.com", "password": "whatever-123"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Email or password is incorrect."
    assert "rt_access" not in response.headers.get("set-cookie", "")


def test_auth_service_outage_is_a_safe_503(monkeypatch):
    app = create_app(make_settings(supabase_anon_key="publishable-test-key"))
    monkeypatch.setattr("app.api.v1.auth_routes.enforce_limit", lambda *a: None)

    def down(self, email, password):
        raise gotrue.AuthUnavailableError("connection refused to 10.0.0.5")

    monkeypatch.setattr(gotrue.SupabaseAuthClient, "sign_in", down)
    response = browser_client(app).post(
        "/api/v1/auth/login", json={"email": "jordan@example.com", "password": "whatever-123"}
    )
    assert response.status_code == 503
    assert "10.0.0.5" not in response.text


def test_rate_limit_failure_closes_sensitive_routes():
    # No DATABASE_URL: limits cannot be enforced, so login refuses instead of allowing.
    app = create_app(make_settings(supabase_anon_key="publishable-test-key"))
    response = browser_client(app).post(
        "/api/v1/auth/login", json={"email": "jordan@example.com", "password": "whatever-123"}
    )
    assert response.status_code == 503


def test_signup_validates_before_calling_auth():
    app = create_app(make_settings(supabase_anon_key="publishable-test-key"))
    response = browser_client(app).post(
        "/api/v1/auth/signup", json={"email": "not-an-email", "password": "Qz7-unique"}
    )
    assert response.status_code == 422
    fields = {e["loc"][-1] for e in response.json()["error"]["fields"]}
    assert fields == {"email", "password"}
    assert "Qz7-unique" not in response.text
