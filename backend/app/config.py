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
# Git-ignored; one folder per pinned revision (see app/retrieval/embedder.py).
_MODEL_DIR = Path(__file__).resolve().parents[1] / ".models" / "all-MiniLM-L6-v2"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
_DEV_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]
# Every secret placeholder in .env.example contains this ("replace-me", "replace-with-...").
_PLACEHOLDER = "replace-"

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
    # Local containers only: the issuer in tokens when this process reaches
    # Supabase under another name (host.docker.internal). Refused in production.
    supabase_jwt_issuer: str | None = None
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
    # A reservation not settled within this time (crash mid-call) is returned.
    reservation_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    # --- Durable jobs (ADR 0007). "worker": a separate `python -m app.worker`
    # process runs them. "inline": the request runs its own job through the same
    # code before answering (tests only; refused in production).
    job_runner: Literal["worker", "inline"] = "worker"
    job_max_attempts: int = Field(default=3, ge=1, le=5)
    job_lease_seconds: int = Field(default=60, ge=10, le=600)
    # Retry n waits backoff * 2**(n-1) seconds (transient model, storage, or DB errors).
    job_retry_backoff_seconds: float = Field(default=5.0, ge=0, le=300)
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    max_active_jobs_per_user: int = Field(default=4, ge=1, le=20)
    max_running_jobs_per_user: int = Field(default=1, ge=1, le=8)
    # Signed download URLs handed to a job (Supabase signed uploads last 2 hours).
    grant_ttl_seconds: int = Field(default=3600, ge=300, le=7200)
    # Bounded automatic ATS improvement after each draft (ADR 0008): at most two
    # attempts per run, each within the usual spending limits. Off: users can
    # still start it from the run page.
    auto_improve: bool = True
    # Manual application tracker: entries per user (abuse guard for free text).
    max_applications_per_user: int = Field(default=1000, ge=1, le=10_000)

    # --- Retrieval (milestone 7, ADR 0010). Embeddings run locally on CPU from
    # a pinned snapshot fetched by `python -m scripts.fetch_embedding_model`;
    # nothing is downloaded at runtime. "full": send every confirmed fact (the
    # baseline). "hybrid": per-requirement exact + full-text + semantic search,
    # with full-context fallback. "auto": full context while the profile fits
    # both retrieval_full_context_max_chars and max_context_chars, hybrid above.
    # The default equals max_context_chars: measured retrieval drops supporting
    # facts and saves under a tenth of a cent per draft on profiles that fit,
    # so it is used only when the full profile would otherwise be cut.
    retrieval_mode: Literal["auto", "full", "hybrid"] = "auto"
    retrieval_full_context_max_chars: int = Field(default=60_000, ge=0, le=400_000)
    retrieval_top_k: int = Field(default=4, ge=1, le=20)
    embedding_model_dir: Path = _MODEL_DIR
    embedding_threads: int = Field(default=2, ge=1, le=8)

    # --- Hosting (milestone 8, ADR 0012). The built frontend (`npm run build`
    # output) served by this app, so pages and /api share one origin. Unset in
    # development, where the Vite server proxies /api instead.
    frontend_dist: Path | None = None
    # The worker touches this file every loop; `python -m app.worker --check`
    # (the container health check) fails when it goes stale.
    worker_heartbeat_file: Path | None = None

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

    @field_validator("frontend_dist")
    @classmethod
    def _validate_frontend_dist(cls, dist: Path | None) -> Path | None:
        # Fail at startup, not on the first page request (middleware is built lazily).
        if dist is not None and not (dist / "index.html").is_file():
            raise ValueError("FRONTEND_DIST must be a frontend build folder with index.html")
        return dist

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
            if self.supabase_jwt_issuer is not None:
                raise ValueError("SUPABASE_JWT_ISSUER is for local containers, not production")
            if self.llm_provider == "demo":
                raise ValueError("the demo AI provider is not allowed in production")
            if self.job_runner != "worker":
                raise ValueError("production runs jobs in the worker (JOB_RUNNER=worker)")
            required = {
                "DATABASE_URL": self.database_url,
                "SUPABASE_ANON_KEY": self.supabase_anon_key,
            }
            for name, secret in required.items():
                if secret is None:
                    raise ValueError(f"production requires {name}")
            secrets = {**required, "OPENAI_API_KEY": self.openai_api_key}
            for name, secret in secrets.items():
                if secret is not None and _PLACEHOLDER in secret.get_secret_value():
                    raise ValueError(f"production {name} still holds the .env.example placeholder")
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
    def auth_url(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def jwt_issuer(self) -> str:
        return self.supabase_jwt_issuer or self.auth_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
