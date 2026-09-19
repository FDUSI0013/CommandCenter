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
    # Per *process*. Every uvicorn worker opens its own pool, so the ceiling on
    # the server is workers x (pool_size + max_overflow) and that product has to
    # stay under Postgres's max_connections with room for the scheduler, psql and
    # the backup. 4 x (8 + 8) = 64 against a server configured for 200. It was
    # 4 x 30 = 120 against 100, which is how a busy hour ended in "sorry, too
    # many clients already" rather than in a queue.
    database_pool_size: int = 8
    database_max_overflow: int = 8
    # How long a request waits for a pooled connection before giving up. The
    # library default is 30 s, which turns an exhausted pool into a 30 s hang on
    # every request -- including the health probe -- instead of a fast failure.
    database_pool_timeout_seconds: float = 5.0
    database_echo: bool = False

    # ---- observability engine (private, never exposed to clients) --------
    engine_base_url: str = "http://localhost:8085"
    engine_workspace: str = "default"
    engine_api_key: str | None = None
    engine_timeout_seconds: float = 30.0
    # The aggregate reads a screen waits on (stats, metric series, cost) get a
    # shorter leash than writes, exports and evaluations: a person is watching,
    # the console stops listening at 30 s, and a rollup that has not answered in
    # this long will not be read by anyone when it does.
    engine_read_timeout_seconds: float = 12.0
    # All the engine calls behind ONE screen request, together. Under the
    # console's 30 s so the server is always the one that answers. See
    # engine.deadline().
    engine_fanout_deadline_seconds: float = 25.0
    engine_connect_timeout_seconds: float = 5.0
    # Wait for a free pooled connection. Deliberately short: see EngineClient.
    engine_pool_timeout_seconds: float = 3.0
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

    # ---- run screens: what is remembered between requests ----------------
    # See services/telemetry_cache.py. Seconds; zero switches a memory off.
    # How long "which projects were written to, and when" is trusted.
    runs_activity_cache_seconds: float = 2.0
    # How long one project's rows for one window are shared between the table,
    # the KPI row and the live-stream subscribers that all want them at once.
    runs_scan_cache_seconds: float = 4.0
    # How long a finished KPI row is served before it is folded again.
    runs_summary_cache_seconds: float = 20.0

    # ---- metrics rollups: what is remembered between requests -------------
    # See services/metrics.py. How long one engine measurement (a project's
    # stats, tokens or metric series for one window) is shared between the KPI
    # row, the charts, the breakdown tables and the quota screens that all ask
    # for it at once. Seconds; zero switches the memory off.
    metrics_cache_seconds: float = 60.0
    # How many of those measurements one worker runs against the store at a
    # time. One Metrics page used to put 4N+6 aggregations on it at once.
    metrics_engine_concurrency: int = 6

    # ---- Agent Detail: its telemetry reads --------------------------------
    # See services/agents.py. How long one agent's 30-day counters are served
    # from memory; the registry opens the first row's detail on every visit and
    # the page reloads itself after every action. Zero switches it off.
    agent_stats_cache_seconds: float = 30.0
    # One telemetry read for the page may take this long before the page is
    # served without it. The page's buttons must not wait on a slow store.
    agent_detail_read_timeout_seconds: float = 8.0

    # ---- Configuration Center: the Usage tab's telemetry reads ------------
    # See services/configurations.py. How long one bound project's run counters
    # are served from memory. A "Production" environment configuration binds
    # every production agent, and each click on the tab used to ask the store
    # for a 30-day aggregate per agent. Zero switches it off.
    configuration_usage_cache_seconds: float = 60.0

    # ---- RAG & Knowledge Governance: the retrieval-span scan ---------------
    # See services/knowledge.py. How long one workspace's retrieval telemetry,
    # reduced to what the screen reads, is shared between every source's
    # grounding panel and documents modal. The store is asked the same question
    # whichever source is selected, so clicking down the table used to repeat a
    # ten-project scan per click. A sync always reads afresh. Zero switches it off.
    knowledge_scan_cache_seconds: float = 60.0

    # ---- Prompt Studio: the owning agents' run counters --------------------
    # See services/prompts.py. How long one agent project's 30-day run and
    # error counts are served from memory. The table, its KPI row and the CSV
    # each asked the store for one aggregate per agent, on every load, every
    # keystroke in the search box and after every lifecycle click. Zero
    # switches it off.
    prompt_stats_cache_seconds: float = 60.0

    # ---- Memory & State: purges and thread counts --------------------------
    # See services/memory.py. How long one retention purge may keep deleting
    # before it stops, audits what it removed and answers "run me again". Under
    # the console's 30 s, so the request that did the deleting is always the one
    # that writes the audit row.
    memory_purge_budget_seconds: float = 20.0
    # How long one project's thread counts (all, and active in the last day) are
    # served from memory. The KPI row and every thread-backed store row read
    # them, on every load and after every action. Zero switches it off.
    memory_counts_cache_seconds: float = 45.0

    # ---- inline guardrail checks on the ingest path -----------------------
    # One scanner call may take this long...
    guardrail_check_timeout_seconds: float = 8.0
    # ...and all of a batch's checks together may take this long, after which
    # the batch is stored unevaluated rather than held. Telemetry is a record of
    # something that already happened; a slow checker must not cost the record.
    guardrail_batch_budget_seconds: float = 12.0
    # A validation the scanner cannot run is not asked for again for this long.
    guardrail_suspend_seconds: float = 300.0

    # ---- ingest -----------------------------------------------------------
    ingest_max_batch_spans: int = 1000
    ingest_max_body_bytes: int = 8 * 1024 * 1024
    # How long one ingest request may run, governance and the hand-off to the
    # telemetry store together, before it answers 503 "retry". It has to sit
    # under the SDKs' own 30 s HTTP timeout: a reporter that gives up first
    # re-sends a batch this service is still working on, and the copies pile up
    # on a store that is already slow.
    ingest_budget_seconds: float = 25.0

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
    # Counted per worker process (core/ratelimit.py): with N workers a caller
    # meets a ceiling somewhere between 1x and Nx these figures, depending on
    # how its connections land. They stop a runaway loop; quota does the metering.
    rate_limit_enabled: bool = True
    rate_limit_ingest_per_minute: int = 12_000
    rate_limit_read_per_minute: int = 1_200
    # NOT READ BY ANYTHING. This service has no Redis client, so there is no
    # shared limiter to switch on; the compose file has always set this and it
    # has always been ignored. Kept only so those deployments still start.
    redis_url: str | None = None

    # ---- inline content checker ------------------------------------------
    # The guardrail scanner is its own service; unset means the engine's own
    # base URL is assumed to front it (true for the test double, not for the
    # compose stack, where the scanner has its own hostname and port).
    engine_checker_url: str | None = None

    # ---- background scheduler --------------------------------------------
    # The platform's own clock: export schedules, test-suite cadences, SLA
    # expiry, mute expiry and secret-status recomputation all hang off it.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 30.0
    # Two jobs on that clock measure rather than decide, so they run on their
    # own, slower cadence (services/quota.py). Rolling measured cost into the
    # budgets asks the telemetry store one rollup per budget period; recording
    # the platform's own capacity readings at 15 minutes keeps a month of them
    # well inside what the capacity table is prepared to read.
    budget_sweep_interval_seconds: float = 600.0
    capacity_sweep_interval_seconds: float = 900.0

    # ---- connector reachability probes -------------------------------------
    # "Test Connection" makes this process open a URL somebody typed in. It
    # refuses to call a private, loopback or link-local address -- from inside
    # the container network those are the unpublished services beside it and
    # the instance metadata endpoint. Turn this on only where the connectors
    # being governed really do live on the control plane's own network.
    connector_probe_allow_private: bool = False

    # ---- audit trail: hash-chain verification -------------------------------
    # See services/audit.py. Verifying replays every audit row the workspace
    # has, and the console asks on every visit to the Audit Trail tab. How long
    # one replay's answer is served to everyone who asks. A hard expiry, not a
    # key on "newest row": an edit to an old row changes no such key, and
    # catching that edit is the point. Seconds; zero switches the memory off.
    audit_verify_cache_seconds: float = 60.0

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
