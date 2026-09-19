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
import ipaddress
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def is_link_local_host(host: str | None) -> bool:
    """True for an address literal in the link-local range.

    That range is where cloud hosts publish instance metadata and credentials.
    Nothing an operator integrates with lives there, and the probe reports the
    status and latency of whatever it is pointed at. Private ranges and internal
    names are deliberately *not* refused: an MCP server on the same network is
    an ordinary thing to connect.
    """
    try:
        address = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False  # a name, not an address literal
    # An IPv4 address carried inside an IPv6 one is routed as the IPv4 address:
    # ::ffff:169.254.169.254 is the metadata address by another spelling.
    carried = getattr(address, "ipv4_mapped", None)
    return (carried or address).is_link_local


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
        # A scheme and a netloc are not enough: ``https://erp.internal:port/``
        # has both, and so does the Add form's own ``https://…`` placeholder
        # pasted back in. Those saved cleanly and then could never be tested. The
        # value is parsed by the library that will later open it, so what is
        # accepted here is exactly what the probe can address.
        try:
            url = httpx.URL(raw.strip())
        except httpx.InvalidURL:
            raise ValueError(f"config.{key} must be a valid http(s) URL") from None
        if not url.host:
            raise ValueError(f"config.{key} must be a valid http(s) URL with a host name")
        # The library takes any integer at parse time and fails at connect.
        if url.port is not None and not 0 < url.port < 65536:
            raise ValueError(f"config.{key} must be a valid http(s) URL (the port is out of range)")
        if is_link_local_host(url.host) or is_link_local_host(parsed.hostname):
            raise ValueError(f"config.{key} must not be a link-local (instance metadata) address")
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
    note: str | None = Field(None, description="The operator's free text; a probe never writes it")
    status_detail: str | None = Field(
        None,
        description=(
            "What the last test or sync saw, when it was not a clean answer; null after a "
            "healthy one. Read-only: derived from the activity feed"
        ),
    )

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

    @model_validator(mode="after")
    def _syncs_today_means_today(self) -> ConnectionRead:
        """Report no syncs today for a tile that was last synced on another day.

        The stored counter is reset by the *next* sync, not by midnight, so on
        a day nobody has synced yet it still holds the previous day's total --
        and the tile said "Last Sync: 1d ago, Syncs Today 5". Deciding it here
        covers the list, the single read and every write that answers a tile.
        """
        synced = self.last_sync_at
        if synced is not None and synced.tzinfo is None:
            synced = synced.replace(tzinfo=dt.UTC)
        today = dt.datetime.now(dt.UTC).date()
        if synced is None or synced.astimezone(dt.UTC).date() != today:
            self.syncs_today = 0
        return self


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
    """What one sync found, and whether it counts as one."""

    connection_id: str
    name: str
    synced: bool = Field(
        True, description="False when the endpoint did not answer, so nothing was synchronised"
    )
    status: ActivityStatus = Field(
        ActivityStatus.SUCCESS, description="Warning when the endpoint answered 4xx/5xx"
    )
    synced_at: dt.datetime | None = Field(None, description="Null when nothing was synchronised")
    syncs_today: int
    linked_agent_count: int = Field(0, description="Agents registered on this platform")
    reachable: bool | None = Field(
        None, description="Null when no endpoint is configured, so nothing was probed"
    )
    http_status: int | None = None
    latency_ms: int | None = None
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
    runs: int | None = Field(
        description=(
            "Runs observed across those agents in the window; null when the store did "
            "not report them in time, which is not the same as none"
        )
    )
    tool_calls: list[ConnectionToolCall] = Field(default_factory=list)
    data_flows: list[ConnectionDataFlow] = Field(default_factory=list)
    truncated: bool = Field(
        False,
        description=(
            "True when an agent had more spans than its share of the scan cap, or the "
            "scan ran out of time: the totals are a floor"
        ),
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


