"""Typed application settings, read from environment variables or the repo-root .env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
_DEV_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]

OriginList = Annotated[list[str], NoDecode]


def _check_origins(origins: list[str]) -> list[str]:
    for origin in origins:
        if "*" in origin:
            raise ValueError("wildcard origins are not allowed")
        parts = urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError(f"origin must look like https://example.com: {origin!r}")
    return [origin.rstrip("/") for origin in origins]


def _is_public_https(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and f"{parts.hostname}" not in _LOCAL_HOSTS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    # Comma-separated exact origins (scheme://host[:port]). Empty means no CORS:
    # the frontend must be served from the same origin (e.g. via the Vite proxy).
    cors_allowed_origins: OriginList = Field(default_factory=list)
    # Origins whose pages may send state-changing requests (CSRF Origin check).
    # Empty means the local Vite dev origins outside production.
    trusted_origins: OriginList = Field(default_factory=list)
    max_request_bytes: int = Field(default=256 * 1024, ge=1024, le=10 * 1024 * 1024)
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

    supabase_url: str = "http://127.0.0.1:54321"
    # Supabase "anon"/publishable key: identifies the project; grants nothing by itself.
    supabase_anon_key: SecretStr | None = None
    storage_bucket: str = "masters"
    artifacts_bucket: str = "artifacts"
    # Runtime connection as the least-privilege app_backend role (never the service role).
    database_url: SecretStr | None = None

    # None means: Secure cookies in production, plain cookies on http://localhost.
    cookie_secure: bool | None = None
    refresh_cookie_max_age: int = Field(default=7 * 24 * 3600, ge=3600, le=30 * 24 * 3600)

    # --- Generative AI (server-side only). Unset provider: "openai" when a key
    # exists, otherwise AI features are disabled. "demo" is a transparent,
    # deterministic stand-in for local development and tests, never production.
    llm_provider: Literal["openai", "demo"] | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=100)
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "low"
    openai_timeout_seconds: float = Field(default=90, ge=5, le=300)
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    # Verify HTTPS against the operating system's certificates instead of the
    # bundled list, e.g. when local antivirus re-signs HTTPS. Verification stays on.
    use_os_trust_store: bool = False
    # USD per 1M tokens, from the model's official pricing page (verify before changing).
    price_input_per_mtok: Decimal = Decimal("0.20")
    price_cached_input_per_mtok: Decimal = Decimal("0.02")
    price_output_per_mtok: Decimal = Decimal("1.20")
    price_cache_write_multiplier: Decimal = Decimal("1.25")
    user_daily_budget_usd: Decimal = Field(default=Decimal("0.50"), ge=0, le=100)
    app_daily_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0, le=10_000)
    analysis_max_output_tokens: int = Field(default=6000, ge=500, le=64_000)
    draft_max_output_tokens: int = Field(default=10_000, ge=500, le=64_000)
    max_context_chars: int = Field(default=60_000, ge=5_000, le=400_000)

    @field_validator("cors_allowed_origins", "trusted_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("cors_allowed_origins", "trusted_origins")
    @classmethod
    def _validate_origins(cls, origins: list[str]) -> list[str]:
        return _check_origins(origins)

    @field_validator("supabase_url")
    @classmethod
    def _validate_supabase_url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query:
            raise ValueError("SUPABASE_URL must be an http(s) base URL")
        return url.rstrip("/")

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        if self.app_env == "production":
            for origin in self.cors_allowed_origins + self.trusted_origins:
                if not _is_public_https(origin):
                    raise ValueError("production origins must be HTTPS and not localhost")
            if not self.trusted_origins:
                raise ValueError("production requires TRUSTED_ORIGINS")
            if self.cookie_secure is False:
                raise ValueError("production requires Secure cookies")
            if not _is_public_https(self.supabase_url):
                raise ValueError("production SUPABASE_URL must be HTTPS")
            if self.llm_provider == "demo":
                raise ValueError("the demo AI provider is not allowed in production")
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        return self

    @property
    def effective_llm_provider(self) -> str:
        if self.llm_provider:
            return self.llm_provider
        return "openai" if self.openai_api_key is not None else "disabled"

    @property
    def secure_cookies(self) -> bool:
        return (
            self.cookie_secure if self.cookie_secure is not None else self.app_env == "production"
        )

    @property
    def effective_trusted_origins(self) -> frozenset[str]:
        base = self.trusted_origins or ([] if self.app_env == "production" else _DEV_ORIGINS)
        return frozenset(base + self.cors_allowed_origins)

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
