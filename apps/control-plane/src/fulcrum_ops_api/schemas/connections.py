"""Wire contracts for the Connection Center.

A connection is one platform integration tile: Foundry, Copilot Studio, Purview,
Key Vault, an MCP server, a vector store. The row itself never holds credential
material — only ``credential_secret_id``, a pointer into the secrets vault — so
nothing in this module can leak one. ``config`` may still carry operator-entered
keys by accident, so every read redacts the well-known credential-shaped keys
before the payload leaves the process.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.registry import ConnectionStatus, HealthState

#: A metadata pair is rendered verbatim under the tile title as label/value.
MetadataPair = tuple[str, str]

#: Config keys the connection test probes, in priority order. The first one
#: present and non-empty is the URL we call.
ENDPOINT_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "endpoint",
    "endpoint_url",
    "url",
    "base_url",
    "resource_uri",
)

#: Substrings that mark a config key as credential-shaped. Matching values are
#: replaced on read; the stored value is untouched.
SENSITIVE_CONFIG_MARKERS: Final[tuple[str, ...]] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "connection_string",
    "access_key",
    "private_key",
    "passphrase",
    "sas",
)

REDACTED: Final[str] = "***redacted***"

MAX_METADATA_PAIRS: Final[int] = 12


class ActivityStatus(str, enum.Enum):
    """Outcome vocabulary of the connection activity feed.

    Lives here rather than on the model because the column stores a plain
    string and this is the only place that vocabulary is enforced.
    """

    SUCCESS = "Success"
    WARNING = "Warning"
    ERROR = "Error"


def is_sensitive_key(key: str) -> bool:
    """True when a config key looks like it holds credential material."""
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_CONFIG_MARKERS)


def redact_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Replace credential-shaped values so a read can never hand one back."""
    if not config:
        return {}
    return {
        key: (REDACTED if is_sensitive_key(key) and value not in (None, "") else value)
        for key, value in config.items()
    }


def _validate_endpoints(config: dict[str, Any]) -> dict[str, Any]:
    """Reject an endpoint the probe could not call, at the edge rather than later."""
    for key in ENDPOINT_CONFIG_KEYS:
        raw = config.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"config.{key} must be a non-empty URL string")
        parsed = urlparse(raw.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"config.{key} must be an absolute http(s) URL")
    return config


def _normalise_pairs(pairs: list[MetadataPair]) -> list[MetadataPair]:
    cleaned: list[MetadataPair] = []
    for label, value in pairs:
        label = label.strip()
        if not label:
            raise ValueError("metadata pair labels cannot be blank")
        cleaned.append((label, value.strip()))
    return cleaned


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class ConnectionRead(BaseModel):
    """One tile on the Connection Center."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    logo_key: str | None = None

    status: ConnectionStatus
    health: HealthState

    metadata_pairs: list[list[str]] = Field(
        default_factory=list, description="Ordered [label, value] pairs shown on the tile"
    )
    last_sync_at: dt.datetime | None = None
    latency_ms: int | None = Field(None, description="Latency of the last successful probe")
    linked_agent_count: int = 0
    syncs_today: int = 0
    note: str | None = None

    config: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret settings; credential-shaped keys are redacted"
    )
    credential_secret_id: str | None = Field(
        None, description="Pointer into the secrets vault; the material itself never travels"
    )
    enabled: bool = True

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    @field_validator("config", mode="after")
    @classmethod
    def _redact(cls, value: dict[str, Any]) -> dict[str, Any]:
        return redact_config(value)


class ConnectionActivityRead(BaseModel):
    """One row of the append-only sync/error feed."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    connection_id: str
    connection_name: str | None = Field(
        None, description="Denormalised for the feed table, which shows the tile name"
    )
    connection_logo_key: str | None = None
    occurred_at: dt.datetime
    event: str
    status: ActivityStatus
    details: str | None = None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """Register an integration.

    Status and health are deliberately not accepted: a new row starts
    disconnected and only a real probe (``POST /connections/{id}/test``) may
    declare it healthy.
    """

    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(
        min_length=1, max_length=48, description="Integration type, e.g. 'MCP Server'"
    )
    logo_key: str | None = Field(None, max_length=40)
    metadata_pairs: list[MetadataPair] = Field(
        default_factory=list, max_length=MAX_METADATA_PAIRS
    )
    note: str | None = Field(None, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)
    credential_secret_id: str | None = Field(None, max_length=36)
    enabled: bool = True

    @field_validator("name", "kind")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("config")
    @classmethod
    def _check_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_endpoints(value)

    @field_validator("metadata_pairs")
    @classmethod
    def _check_pairs(cls, value: list[MetadataPair]) -> list[MetadataPair]:
        return _normalise_pairs(value)


class ConnectionUpdate(BaseModel):
    """Partial update. Every field is optional; omitted fields are left alone."""

    name: str | None = Field(None, min_length=1, max_length=120)
    kind: str | None = Field(None, min_length=1, max_length=48)
    logo_key: str | None = Field(None, max_length=40)
    status: ConnectionStatus | None = None
    health: HealthState | None = None
    metadata_pairs: list[MetadataPair] | None = Field(None, max_length=MAX_METADATA_PAIRS)
    note: str | None = Field(None, max_length=500)
    config: dict[str, Any] | None = None
    credential_secret_id: str | None = Field(None, max_length=36)
    enabled: bool | None = None
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name", "kind")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("config")
    @classmethod
    def _check_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _validate_endpoints(value)

    @field_validator("metadata_pairs")
    @classmethod
    def _check_pairs(cls, value: list[MetadataPair] | None) -> list[MetadataPair] | None:
        return None if value is None else _normalise_pairs(value)


# ---------------------------------------------------------------------------
# Action results
# ---------------------------------------------------------------------------


class ConnectionTestOutcome(BaseModel):
    """What one probe measured, and what it did to the tile."""

    connection_id: str
    name: str
    reachable: bool
    status: ConnectionStatus
    health: HealthState
    latency_ms: int | None = Field(
        None, description="Round trip in milliseconds; null if unreachable"
    )
    http_status: int | None = None
    detail: str


class ConnectionSyncOutcome(BaseModel):
    """What one sync recorded."""

    connection_id: str
    name: str
    synced_at: dt.datetime
    syncs_today: int
    detail: str


class BulkActionSummary(BaseModel):
    """Counts returned by the test-all / sync-all fan-outs."""

    requested: int = 0
    succeeded: int = 0
    warned: int = 0
    failed: int = 0
    skipped: int = 0


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class ConnectionHealthSlice(BaseModel):
    """One segment of the Connection Health Overview donut."""

    health: HealthState
    count: int
    percent: float = Field(description="Share of all connections, 0-100, one decimal")


class ConnectionsSummary(BaseModel):
    """The five KPI cards plus the donut, in one round trip."""

    total: int
    connected: int
    warning: int
    disconnected: int
    enabled: int
    disabled: int
    last_sync_at: dt.datetime | None = Field(
        None, description="Most recent sync across every connection in the workspace"
    )
    avg_latency_ms: float | None = None
    syncs_today: int = 0
    linked_agents: int = 0
    health_breakdown: list[ConnectionHealthSlice] = Field(default_factory=list)
class ConnectionToolCall(BaseModel):
    """One tool the agents on this connection actually called."""

    name: str
    calls: int
    errors: int = 0
    error_rate: float | None = Field(None, description="Percent of calls that failed")
    avg_duration_ms: float | None = None
    last_called_at: dt.datetime | None = None


class ConnectionDataFlow(BaseModel):
    """Retrieval and data access observed through this connection."""

    name: str
    operations: int
    documents: int | None = Field(None, description="Retrieved documents, when reported")
    avg_duration_ms: float | None = None
    last_seen_at: dt.datetime | None = None


class ConnectionTraffic(BaseModel):
    """What has actually flowed through one connection.

    Derived from the spans of the agents running on this connection's platform,
    not from anything declared on the connection itself. An empty result means
    no agent on this platform reported a tool or data span in the window, which
    is different from the connection being unhealthy.
    """

    connection_id: str
    name: str
    kind: str
    window_days: int
    agents: int = Field(description="Agents on this platform that could report")
    runs: int = Field(description="Runs observed across those agents in the window")
    tool_calls: list[ConnectionToolCall] = Field(default_factory=list)
    data_flows: list[ConnectionDataFlow] = Field(default_factory=list)
    truncated: bool = Field(
        False, description="True when the span scan hit its cap and totals are a floor"
    )
    attributable: bool = Field(
        True,
        description=(
            "False when this kind of connection has no agent platform to match, so "
            "traffic cannot be attributed to it at all"
        ),
    )
    note: str | None = Field(
        None, description="Why the result is empty, when emptiness needs explaining"
    )


