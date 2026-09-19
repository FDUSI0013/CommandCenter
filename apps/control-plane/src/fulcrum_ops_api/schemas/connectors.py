"""Request and response contracts for Connector & MCP Governance.

The connector list is a governance surface rather than a CRUD form, and one
rule shapes every model here: ``status = "Blocked"`` can never be reached by an
ordinary write. A block has to carry who imposed it, when and why, so
``POST /connectors/{id}/block`` is the only door into that state and it demands
a reason. Create and update refuse the value outright.

``used_by_agents`` and ``agents`` are not columns on the connector row - they
come from the grant table - so :meth:`ConnectorRead.from_model` takes them as an
argument rather than reading them off the ORM object.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Annotated, Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field, field_validator
from sqlalchemy import inspect as sa_inspect

from ..models.registry import (
    AccessLevel,
    Connector,
    ConnectorStatus,
    ConnectorType,
    DataClassification,
    RiskLevel,
)

#: Authentication modes the registry recognises. Stored as free text on the
#: model; constrained here so the console dropdown and the API agree.
AuthMode = Literal["OAuth 2.0", "API Key", "Managed Identity", "Service Principal"]

#: Verdict of a reachability probe, in the vocabulary the console status pill
#: already renders.
TestStatus = Literal["Healthy", "Warning", "Unreachable", "Blocked"]

ConnectorName = Annotated[str, Field(min_length=1, max_length=160)]
ProviderName = Annotated[str, Field(min_length=1, max_length=120)]
EndpointUrl = Annotated[str, Field(min_length=1, max_length=512)]
Scope = Annotated[str, Field(min_length=1, max_length=120)]
ScopeList = Annotated[list[Scope], Field(max_length=50)]
BlockReason = Annotated[str, Field(min_length=3, max_length=1000)]
OwnerId = Annotated[str, Field(min_length=1, max_length=36)]


class ConnectorAgentRef(BaseModel):
    """One agent holding a grant on a connector - the cross-link each row shows."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class ConnectorRead(BaseModel):
    """A governed connector, tool, data source or MCP server."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    connector_type: ConnectorType
    provider: str | None = None
    risk_level: RiskLevel
    status: ConnectorStatus
    access: AccessLevel
    data_classification: DataClassification
    scopes: list[str] = Field(default_factory=list)
    endpoint_url: str | None = None
    auth_mode: str | None = None

    used_by_agents: int = Field(0, description="How many agents hold a grant on this connector.")
    agents: list[ConnectorAgentRef] = Field(
        default_factory=list, description="The agents behind that count, sorted by name."
    )

    blocked_at: dt.datetime | None = None
    blocked_by: str | None = None
    blocked_reason: str | None = None

    last_used_at: dt.datetime | None = None
    owner_user_id: str | None = None

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    @computed_field(description="True while the connector is blocked for every agent.")
    @property
    def is_blocked(self) -> bool:
        return self.status is ConnectorStatus.BLOCKED

    @computed_field(description="True when the connector carries data past the tenant boundary.")
    @property
    def is_external(self) -> bool:
        return self.data_classification is not DataClassification.INTERNAL

    @classmethod
    def from_model(
        cls, connector: Connector, agents: Sequence[ConnectorAgentRef] = ()
    ) -> ConnectorRead:
        """Build a row, attaching the grants that were joined alongside it.

        The count is taken from those same joined rows, so the number on the row
        can never disagree with the names beside it.

        Only mapped *columns* are read off the instance. ``Connector.agents`` is
        a relationship with the same name as this schema's ``agents`` field, so
        validating the instance directly would make Pydantic touch it — and an
        unloaded relationship touched in async context raises ``MissingGreenlet``
        rather than lazy-loading. The grants come from the caller's join instead,
        which is the only source that can be trusted to be complete.
        """
        columns = sa_inspect(connector).mapper.column_attrs
        data = {attr.key: getattr(connector, attr.key) for attr in columns}
        read = cls.model_validate(data)
        read.agents = list(agents)
        read.used_by_agents = len(read.agents)
        return read


class _ConnectorWrite(BaseModel):
    """Validation shared by create and update."""

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("endpoint_url", check_fields=False)
    @classmethod
    def _http_endpoint(cls, value: str | None) -> str | None:
        """Only http(s) endpoints can be probed, so only those may be registered.

        The prefix alone is not enough: ``https://erp.internal:port/`` has it,
        and so does the form's own ``https://…`` placeholder pasted back in.
        Those registered cleanly and then could never be tested. The value is
        parsed by the same library that will later open it, so what is accepted
        here is exactly what the probe can address.
        """
        if value is None:
            return None
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ValueError(f"must be a valid http(s) URL ({exc})") from None
        if not url.host:
            raise ValueError("must be a valid http(s) URL with a host name")
        if url.port is not None and not 0 < url.port < 65536:
            raise ValueError("must be a valid http(s) URL (the port is out of range)")
        return value

    @field_validator("scopes", check_fields=False)
    @classmethod
    def _tidy_scopes(cls, value: list[str] | None) -> list[str] | None:
        """Drop blanks and duplicates; a scope list is a set with a display order."""
        if value is None:
            return None
        seen: set[str] = set()
        scopes: list[str] = []
        for scope in value:
            if scope and scope not in seen:
                seen.add(scope)
                scopes.append(scope)
        return scopes

    @field_validator("status", check_fields=False)
    @classmethod
    def _blocking_is_not_an_edit(cls, value: ConnectorStatus | None) -> ConnectorStatus | None:
        if value is ConnectorStatus.BLOCKED:
            raise ValueError(
                "a block is recorded with who, when and why - "
                "use POST /connectors/{id}/block instead"
            )
        return value


class ConnectorCreate(_ConnectorWrite):
    """Register a tool, API, data source or MCP server for governance."""

    name: ConnectorName
    connector_type: ConnectorType = Field(
        validation_alias=AliasChoices("connector_type", "type"),
        description="Shape of the integration; MCP servers are governed hardest.",
    )
    provider: ProviderName | None = None
    risk_level: RiskLevel = Field(
        RiskLevel.LOW, validation_alias=AliasChoices("risk_level", "risk")
    )
    status: ConnectorStatus = ConnectorStatus.ACTIVE
    access: AccessLevel = Field(
        AccessLevel.READ, description="What a grant on this connector lets an agent do."
    )
    data_classification: DataClassification = Field(
        DataClassification.INTERNAL,
        validation_alias=AliasChoices("data_classification", "classification"),
        description="Highest classification of data the connector is cleared to carry.",
    )
    scopes: ScopeList = Field(default_factory=list)
    endpoint_url: EndpointUrl | None = None
    auth_mode: AuthMode | None = None
    owner_user_id: OwnerId | None = Field(
        None, description="Defaults to the caller when a person registers the connector."
    )


class ConnectorUpdate(_ConnectorWrite):
    """Partial edit. Only the fields present in the body are written.

    ``expected_updated_at`` makes the edit optimistic: send back the
    ``updated_at`` the row carried when it was loaded and the API refuses the
    write if somebody else has changed it since.
    """

    name: ConnectorName | None = None
    connector_type: ConnectorType | None = Field(
        None, validation_alias=AliasChoices("connector_type", "type")
    )
    provider: ProviderName | None = None
    risk_level: RiskLevel | None = Field(
        None, validation_alias=AliasChoices("risk_level", "risk")
    )
    status: ConnectorStatus | None = None
    access: AccessLevel | None = None
    data_classification: DataClassification | None = Field(
        None, validation_alias=AliasChoices("data_classification", "classification")
    )
    scopes: ScopeList | None = None
    endpoint_url: EndpointUrl | None = None
    auth_mode: AuthMode | None = None
    owner_user_id: OwnerId | None = None
    expected_updated_at: dt.datetime | None = Field(
        None, description="Concurrency token: the updated_at the client last saw."
    )


class ConnectorBlockRequest(BaseModel):
    """Why a connector is being blocked. Kept on the row and in the audit trail."""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: BlockReason = Field(
        description="Recorded verbatim; agents calling the connector then fail closed."
    )


class ConnectorGrantRequest(BaseModel):
    """Grant of this connector to one agent — the edge the access graph draws."""

    model_config = ConfigDict(str_strip_whitespace=True)

    agent_id: str = Field(min_length=1, max_length=36, description="Agent in this workspace.")


class ConnectorTestResult(BaseModel):
    """Outcome of an unauthenticated reachability probe against the endpoint."""

    connector_id: str
    name: str
    ok: bool
    status: TestStatus
    message: str
    latency_ms: int | None = None
    http_status: int | None = None
    endpoint_url: str | None = None
    checked_at: dt.datetime


class ConnectorSummary(BaseModel):
    """KPI cards above the connector table, plus the counts its tab bar shows."""

    total: int
    active: int
    external: int
    high_risk: int
    blocked: int
    mcp_servers: int
    tools: int
    data_sources: int
    active_percent: float = Field(description="Active as a percentage of total; 0 when empty.")
    external_percent: float = Field(description="External as a percentage of total; 0 when empty.")
