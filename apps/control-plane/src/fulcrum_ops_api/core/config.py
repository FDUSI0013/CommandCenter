"""Runtime configuration for the Fulcrum Ops control plane API.

Every setting is overridable by environment variable. Nothing in this file may
reference an upstream vendor: the observability engine is addressed only through
the neutral ``FULCRUM_OPS_ENGINE_*`` settings.
"""

from __future__ import annotations

import functools
import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FULCRUM_OPS_",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- service identity -------------------------------------------------
    environment: Environment = "local"
    service_name: str = "fulcrum-ops-control-plane"
    api_prefix: str = "/api/v1"
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"
    log_json: bool = False
    # Serve the console's static files from this process when set. The edge
    # proxy serves them in production; this makes a single container (and a
    # local dev run) self-sufficient, and puts the API on the console's own
    # origin so the browser needs no cross-origin exception.
    static_dir: str | None = None

    # ---- security ---------------------------------------------------------
    # Signing key for browser session JWTs. MUST be set in any non-local env.
    # Refused at startup outside local dev by require_production_hardening().
    secret_key: str = "dev-only-insecure-key-change-me"  # noqa: S105 — dev default, gated below
    session_ttl_minutes: int = 12 * 60
    # Fernet key used to encrypt secret material at rest (secrets vault,
    # connector credentials, provider keys). Generated on first boot locally.
    encryption_key: str | None = None
    # Comma separated list of allowed browser origins.
    cors_origins: str = "http://localhost:3005,http://localhost:5173"
    # Bootstrap owner account, created by `fulcrum-ops-api bootstrap`.
    bootstrap_email: str | None = None
    bootstrap_password: str | None = None

    # ---- our own domain database -----------------------------------------
    # SQLite for local development, Postgres in every deployed environment.
    database_url: str = "sqlite+aiosqlite:///./fulcrum-ops.db"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # ---- observability engine (private, never exposed to clients) --------
    engine_base_url: str = "http://localhost:8085"
    engine_workspace: str = "default"
    engine_api_key: str | None = None
    engine_timeout_seconds: float = 30.0
    engine_connect_timeout_seconds: float = 5.0
    engine_max_connections: int = 100
    engine_retries: int = 2
    # When false, telemetry reads/writes degrade gracefully instead of 502ing.
    engine_required: bool = True
    # Python package that the engine's metric runner exposes for user-defined
    # metric code (the mirrored guardrail evaluators import
    # `<library>.evaluation.metrics`). Deployment-specific and deliberately not
    # defaulted: the vendor's name never ships in this repository, so the real
    # value lives in the host's .env. Empty means guardrails are not mirrored
    # into the engine's rule store — inline enforcement is unaffected.
    engine_metric_library: str = ""

    # ---- ingest -----------------------------------------------------------
    ingest_max_batch_spans: int = 1000
    ingest_max_body_bytes: int = 8 * 1024 * 1024

    # ---- Prompt Studio execution -----------------------------------------
    # Running a prompt is the one place this service calls a model itself, so
    # it is off unless deliberately configured. Any OpenAI-compatible chat
    # completions endpoint works: OpenAI, Azure OpenAI, or a local gateway.
    # Unconfigured, the editor still renders and diffs; only Run is refused,
    # with a message that says which setting is missing.
    prompt_studio_endpoint: str | None = None
    prompt_studio_api_key: str | None = None
    prompt_studio_model: str = "gpt-4o-mini"
    prompt_studio_api_version: str | None = None
    prompt_studio_timeout_seconds: float = 60.0
    # Ceiling on one execution, so a runaway template cannot bill unbounded.
    prompt_studio_max_output_tokens: int = 1024

    @property
    def prompt_studio_enabled(self) -> bool:
        return bool(self.prompt_studio_endpoint and self.prompt_studio_api_key)
    ingest_queue_max_pending: int = 50_000

    # ---- rate limiting ----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_ingest_per_minute: int = 12_000
    rate_limit_read_per_minute: int = 1_200
    redis_url: str | None = None  # optional; in-process limiter when unset

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return ",".join(p.strip() for p in v.split(",") if p.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o for o in self.cors_origins.split(",") if o]

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    @property
    def engine_is_private(self) -> bool:
        """True when the telemetry engine is addressed on a network we control.

        The reference deployment puts the engine on a container network with no
        published port, reachable only as a compose service name. That is what
        makes it safe for the engine to run with its own authentication off: it
        is not addressable from anywhere a credential would protect it from.
        """
        host = urlsplit(self.engine_base_url).hostname or ""
        if host in {"localhost", "telemetry-engine"} or "." not in host:
            # A bare name resolves only inside the container network.
            return True
        try:
            return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host.endswith((".internal", ".local", ".svc", ".svc.cluster.local"))

    def require_production_hardening(self) -> list[str]:
        """Return a list of misconfigurations that must be fixed before prod."""
        problems: list[str] = []
        if self.environment == "production":
            if self.secret_key.startswith("dev-only"):
                problems.append("FULCRUM_OPS_SECRET_KEY is still the dev default")
            if not self.encryption_key:
                problems.append("FULCRUM_OPS_ENCRYPTION_KEY is not set")
            if self.database_url.startswith("sqlite"):
                problems.append("FULCRUM_OPS_DATABASE_URL must point at Postgres")
            # An engine credential is required only when the engine is somewhere
            # a credential would actually protect. Demanding one for a private
            # compose service would be theatre: there is no key to set, because
            # the engine runs with its authentication disabled precisely because
            # nothing outside the host can reach it. Pointing the adapter at a
            # public address is a different deployment, and that one needs a key.
            if not self.engine_api_key and not self.engine_is_private:
                problems.append(
                    "FULCRUM_OPS_ENGINE_API_KEY is not set, and "
                    f"FULCRUM_OPS_ENGINE_BASE_URL ({self.engine_base_url}) is not a private "
                    "address — an engine reachable over a public network must be authenticated"
                )
        return problems


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
