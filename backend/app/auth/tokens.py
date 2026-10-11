"""Verify Supabase access tokens (JWTs) locally with the project's public keys.

Only asymmetric algorithms are accepted, so a token signed with a shared
secret, or with "alg": "none", can never pass. Issuer, audience, expiry, and
subject are all required.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from functools import lru_cache

import jwt

ALLOWED_ALGORITHMS = ["ES256", "RS256"]
AUDIENCE = "authenticated"
MAX_TOKEN_LENGTH = 8192


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "expired" or "invalid"


@dataclass(frozen=True)
class VerifiedUser:
    id: uuid.UUID
    email: str | None
    access_token: str = field(repr=False)


class TokenVerifier:
    def __init__(self, issuer: str, jwk_client: jwt.PyJWKClient | None = None) -> None:
        self.issuer = issuer
        self._jwks = jwk_client or jwt.PyJWKClient(
            f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=600, timeout=5
        )

    def verify(self, token: str) -> VerifiedUser:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise TokenError("invalid")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=AUDIENCE,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("expired") from exc
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
            raise TokenError("invalid") from exc

        if claims.get("role") != "authenticated" or claims.get("is_anonymous"):
            raise TokenError("invalid")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise TokenError("invalid") from exc
        email = claims.get("email")
        return VerifiedUser(
            id=user_id, email=email if isinstance(email, str) else None, access_token=token
        )


@lru_cache(maxsize=4)
def verifier_for(issuer: str) -> TokenVerifier:
    return TokenVerifier(issuer)
