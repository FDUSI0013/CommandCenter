"""Wire contracts for the Agent Registry and the nine-tab Agent Detail screen.

An agent is the control-plane half of a 1:1 pairing with a telemetry project:
identity, ownership, risk and configuration live in our ``agents`` table, while
every run counter and prompt version is read back from the telemetry engine at
request time. That split is visible in the schemas below —
:class:`AgentRead` is pure database state, :class:`AgentRunStats` and
:class:`AgentVersionRead` are engine state, and
:class:`AgentDetail` is the one payload that carries both so the detail screen
opens in a single round trip.

Rolling metrics on :class:`AgentRead` come from ``metrics_cache`` — the last
engine answer, stamped with the moment it was computed. The list view renders
from that cache instead of fanning out one engine call per row; a null cache
means the numbers have not been computed yet and the console shows a dash
rather than a zero.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.registry import (
    AgentStatus,
    AgentType,
    EnvironmentType,
    Platform,
    PolicyStatus,
    RiskLevel,
)

#: Longest prompt body accepted on a version commit. Templates above this are a
#: sign something other than a prompt is being versioned.
MAX_TEMPLATE_CHARS: Final[int] = 200_000

#: How much of a version's template travels with the version list. The full
#: body is only sent by the diff endpoint, which is what needs it.
TEMPLATE_PREVIEW_CHARS: Final[int] = 400

MAX_TAGS: Final[int] = 20

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Reduce a display name to the stable id the telemetry project is keyed on."""
    slug = _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")
    return slug or "agent"


def _clean_tags(tags: list[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate, preserve author order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in tags:
        trimmed = tag.strip()
        if not trimmed or trimmed.lower() in seen:
            continue
        seen.add(trimmed.lower())
        cleaned.append(trimmed)
    return cleaned


# ---------------------------------------------------------------------------
# Engine-derived building blocks
# ---------------------------------------------------------------------------


class NamedScore(BaseModel):
    """One feedback/evaluation score averaged over the window."""

    name: str
    value: float


class AgentMetrics(BaseModel):
    """The rolling 30-day numbers the registry row and detail header show.

    Every field is nullable on purpose: an agent that has never run has no
    success rate, and reporting 0% would be a different — and wrong — claim.

    Two sources meet here. The run figures come from the telemetry engine and
    are null until it has been read for this agent. ``violations_30d`` and
    ``escalations_30d`` are counted from our own violation records, so on the
    detail payload they are always present and always current, whatever the
    telemetry store is doing.
    """

    runs_30d: int | None = None
    success_rate_30d: float | None = Field(None, description="Percent, 0-100")
    avg_latency_seconds: float | None = Field(
        None,
        description=(
            "Mean run duration when the telemetry engine reports one; otherwise the "
            "median, which is the figure it does measure"
        ),
    )
    p50_latency_seconds: float | None = Field(None, description="Median run duration")
    tokens_30d: int | None = None
    cost_30d: float | None = None
    tool_calls_30d: int | None = Field(None, description="Not measured yet; always null")
    escalations_30d: int | None = Field(
        None, description="Violations in the window that escalated or required approval"
    )
    violations_30d: int | None = Field(
        None, description="Policy violations recorded against the agent in the window"
    )
    eval_score: float | None = Field(None, description="Mean evaluation score, 0-1")
    computed_at: dt.datetime | None = Field(
        None, description="When these numbers were last read from the telemetry engine"
    )


class AgentRunStats(BaseModel):
    """Run counters for one agent over a window, read from the telemetry engine.

    Names the engine did not report come back null rather than zero, so the
    detail screen can tell "no data" from "no runs".
    """

    window_start: dt.datetime
    window_end: dt.datetime
    run_count: int = 0
    span_count: int | None = None
    error_count: int = 0
    success_rate: float | None = Field(None, description="Percent, 0-100")
    avg_duration_seconds: float | None = None
    p50_duration_seconds: float | None = None
    p90_duration_seconds: float | None = None
    p99_duration_seconds: float | None = None
    total_tokens: int = 0
    total_cost: float = 0.0
    feedback_scores: list[NamedScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class AgentRead(BaseModel):
    """One row of the Agent Registry table, and the header of Agent Detail.

    Column keys map to the table's columns as: ``name`` (Agent Name),
    ``platform`` (Source), ``agent_type`` (Type), ``environment``, ``status``,
    ``risk``, ``policy_status`` (Policy Status), ``owner_name``/``team``
    (Owner), ``last_used_at`` (Last Used).
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    description: str | None = None

    platform: Platform
    agent_type: AgentType
    environment: EnvironmentType
    status: AgentStatus
    risk: RiskLevel
    policy_status: PolicyStatus = Field(
        description=(
            "Blocked and Approval Required are the stored gate and refuse activation "
            "and Run Agent. Warned is also reported for an Allowed agent whose runs a "
            "policy intervened in during the last 30 days"
        )
    )
    strongest_enforcement_30d: str | None = Field(
        None,
        description=(
            "Strongest enforcement recorded against the agent's runs in the last 30 "
            "days (Block, Require Approval, Escalate, Mask, Route, Throttle or Warn); "
            "null when policy has not intervened. Informational: it is not a gate"
        ),
    )

    owner_user_id: str | None = None
    owner_name: str | None = Field(None, description="Resolved from the owning user")
    owner_email: str | None = None
    team: str | None = None
    tags: list[str] = Field(default_factory=list)

    model: str | None = None
    prompt_version: str | None = None
    tools_enabled: int = 0
    policies_applied: int = 0
    retries: int = 1
    memory_policy: str | None = None
    access_scope: str | None = None

    last_used_at: dt.datetime | None = None
    health: int | None = Field(
        None,
        description=(
            "Composite health score, 0-100: the 30-day success rate, less 5 per policy "
            "violation in the window (at most 30), less 10 per blocked connector grant. "
            "Null until the agent has reported a run"
        ),
    )

    engine_project_id: str | None = Field(
        None, description="Telemetry project this agent's runs land in"
    )
    engine_project_name: str | None = None
    is_provisioned: bool = Field(
        False, description="True once a telemetry project exists for the agent"
    )

    metrics: AgentMetrics | None = Field(
        None, description="Last computed rolling metrics; null before the first run"
    )

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class AgentConnectorRead(BaseModel):
    """One row of the Tools & Connectors tab: a grant joined to its connector."""

    grant_id: str = Field(description="Id of the agent-to-connector grant")
    connector_id: str
    name: str
    connector_type: str
    provider: str | None = None
    risk_level: RiskLevel
    status: str
    access: str = Field(description="Read, Read-Write or Admin")
    data_classification: str
    scopes: list[str] = Field(default_factory=list)
    endpoint_url: str | None = None
    auth_mode: str | None = None
    last_used_at: dt.datetime | None = None
    granted_at: dt.datetime | None = None
    granted_by: str | None = None
    is_blocked: bool = False


class AgentPolicyBindingRead(BaseModel):
    """One policy attached to this agent, for the Risk & Policy summary."""

    binding_id: str
    policy_id: str
    name: str
    category: str
    scope: str
    scope_label: str | None = None
    risk_level: RiskLevel
    status: str
    enforcement: str
    version: str
    violations_30d: int = 0
    bound_at: dt.datetime | None = None
    bound_by: str | None = None


class AgentVersionRead(BaseModel):
    """One commit of the agent's system prompt, as the Version History pipe shows it."""

    version: str = Field(description="Version label, e.g. 'v2.3.1'")
    commit: str | None = Field(None, description="Immutable commit id of this version")
    status: str = Field("Approved", description="Publication state of the commit")
    change_description: str | None = None
    author: str | None = None
    created_at: dt.datetime | None = None
    is_current: bool = False
    template_preview: str | None = Field(
        None, description=f"First {TEMPLATE_PREVIEW_CHARS} characters of the prompt body"
    )
    token_count: int | None = None


class DiffLine(BaseModel):
    """One line of the Compare Versions diff."""

    op: str = Field(description="' ' unchanged, '-' removed, '+' added, '@' hunk header")
    text: str


class MetadataChange(BaseModel):
    """One metadata key that differs between the two compared versions."""

    key: str
    before: Any = None
    after: Any = None


class AgentVersionDiff(BaseModel):
    """What the Compare Versions modal renders."""

    agent_id: str
    from_version: AgentVersionRead
    to_version: AgentVersionRead
    identical: bool
    added_lines: int = 0
    removed_lines: int = 0
    diff: list[DiffLine] = Field(default_factory=list)
    metadata_changes: list[MetadataChange] = Field(default_factory=list)


class RetryPolicy(BaseModel):
    """Retry block of the exported configuration manifest."""

    max_retries: int
    backoff: str = "exponential"


class AgentConfiguration(BaseModel):
    """The manifest the Configuration tab prints and Export Configuration downloads.

    Field names are the manifest's own, not the database's: this document is
    consumed by deployment tooling, so it stays stable even if a column is
    renamed underneath it.
    """

    agent_id: str
    name: str
    source_platform: Platform
    type: AgentType
    owner: str | None = None
    environment: EnvironmentType
    status: AgentStatus
    risk_level: RiskLevel
    policy_status: PolicyStatus
    model: str | None = None
    prompt_version: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    memory_policy: str | None = None
    retry_policy: RetryPolicy
    access_scope: str | None = None
    telemetry_project: str | None = None
    exported_at: dt.datetime


class AgentDetail(BaseModel):
    """Everything the nine tabs need, in one response."""

    agent: AgentRead
    configuration: AgentConfiguration
    connectors: list[AgentConnectorRead] = Field(default_factory=list)
    policies: list[AgentPolicyBindingRead] = Field(default_factory=list)
    versions: list[AgentVersionRead] = Field(default_factory=list)
    stats: AgentRunStats | None = Field(
        None,
        description=(
            "The window's run counters. Null when the telemetry store could not be "
            "read for this response (see `telemetry_error`): not measured, which is "
            "a different claim from zero"
        ),
    )
    telemetry_available: bool = Field(
        True,
        description="False when the agent has no telemetry project yet; stats are empty",
    )
    telemetry_error: str | None = Field(
        None,
        description=(
            "Set when the agent has a telemetry project but the store failed or was "
            "too slow for this response. The rest of the payload is our own data and "
            "is complete; `stats` is null and/or `versions` is empty, and "
            "`agent.metrics` holds the last figures that were measured, dated by "
            "`computed_at`"
        ),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class AgentCreate(BaseModel):
    """Register an agent.

    ``status`` and ``policy_status`` are deliberately not accepted. A new agent
    starts Pending Review and Allowed; status moves only through
    activate/deactivate, and the policy verdict is written by the policy engine.
    """

    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(None, max_length=2000)
    platform: Platform
    agent_type: AgentType = AgentType.PRO_CODE
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    risk: RiskLevel = RiskLevel.LOW
    owner_user_id: str | None = Field(None, max_length=36)
    team: str | None = Field(None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    model: str | None = Field(None, max_length=80)
    prompt_version: str | None = Field(None, max_length=24)
    tools_enabled: int = Field(0, ge=0, le=500)
    policies_applied: int = Field(0, ge=0, le=500)
    retries: int = Field(1, ge=0, le=10)
    memory_policy: str | None = Field(None, max_length=48)
    access_scope: str | None = Field(None, max_length=48)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        return _clean_tags(value)


class AgentUpdate(BaseModel):
    """Partial update. Omitted fields are left alone.

    Status is not settable here — use activate/deactivate, which enforce the
    transition rules and record why the agent moved.
    """

    name: str | None = Field(None, min_length=1, max_length=160)
    description: str | None = Field(None, max_length=2000)
    platform: Platform | None = None
    agent_type: AgentType | None = None
    environment: EnvironmentType | None = None
    risk: RiskLevel | None = None
    policy_status: PolicyStatus | None = None
    owner_user_id: str | None = Field(None, max_length=36)
    team: str | None = Field(None, max_length=120)
    tags: list[str] | None = Field(None, max_length=MAX_TAGS)
    model: str | None = Field(None, max_length=80)
    prompt_version: str | None = Field(None, max_length=24)
    tools_enabled: int | None = Field(None, ge=0, le=500)
    policies_applied: int | None = Field(None, ge=0, le=500)
    retries: int | None = Field(None, ge=0, le=10)
    memory_policy: str | None = Field(None, max_length=48)
    access_scope: str | None = Field(None, max_length=48)
    health: int | None = Field(None, ge=0, le=100)
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value)


class AgentClone(BaseModel):
    """Copy an agent into a fresh registration.

    The clone always starts Pending Review in a non-production environment: a
    copy has not been through governance review, whatever its source had.
    """

    name: str | None = Field(
        None, min_length=1, max_length=160, description="Defaults to '<source> (Copy)'"
    )
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    copy_connectors: bool = Field(
        True, description="Copy the source agent's connector grants onto the clone"
    )
    copy_policies: bool = Field(
        True, description="Copy the source agent's explicit policy bindings"
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class AgentStatusChange(BaseModel):
    """Reason recorded with an activate or deactivate."""

    reason: str | None = Field(None, max_length=500)


class AgentRunRequest(BaseModel):
    """Trigger one execution of the agent from the console."""

    input: str | None = Field(
        None, max_length=32_000, description="Prompt to run; omit for a bare smoke run"
    )
    session_id: str | None = Field(
        None, max_length=64, description="Groups this run into an existing conversation"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extra attributes stamped on the run's trace"
    )


class AgentRunAccepted(BaseModel):
    """The run the console can now follow on Live Runs or Replay Studio."""

    run_id: str
    agent_id: str
    agent_name: str
    status: str = Field("Running", description="Runs open in this state until completion")
    started_at: dt.datetime
    telemetry_project: str | None = None
    session_id: str | None = None


class AgentVersionCreate(BaseModel):
    """Commit a new version of the agent's system prompt."""

    template: str = Field(min_length=1, max_length=MAX_TEMPLATE_CHARS)
    change_description: str | None = Field(None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    make_current: bool = Field(
        True, description="Point the agent's prompt_version at this commit"
    )

    @field_validator("template")
    @classmethod
    def _trim_template(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class AgentOwnerFacet(BaseModel):
    """One entry of the registry's Owner filter, with its row count."""

    owner_user_id: str | None = None
    name: str
    email: str | None = None
    agent_count: int


class AgentsSummary(BaseModel):
    """The six KPI cards above the registry table.

    Deltas are only present where a real comparison exists: agents carry a
    creation timestamp, and violations carry an occurrence timestamp, so those
    windows are computed. Nothing here is estimated.
    """

    total: int
    total_added_30d: int = Field(0, description="Agents registered in the last 30 days")

    active: int
    active_percent: float = Field(0.0, description="Share of all agents, 0-100")

    high_risk: int
    high_risk_added_30d: int = 0

    policy_violations_30d: int
    policy_violations_previous_30d: int = 0

    pending_approval: int = Field(
        description="Approval Required policy status, or Pending Review status"
    )

    inactive: int
    inactive_percent: float = 0.0

    provisioned: int = Field(0, description="Agents with a telemetry project")
    owners: list[AgentOwnerFacet] = Field(
        default_factory=list, description="Options for the Owner filter"
    )
