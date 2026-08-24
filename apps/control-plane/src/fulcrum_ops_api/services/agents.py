"""Agent Registry and Agent Detail business logic.

An agent row is the control plane's half of a 1:1 pairing with a telemetry
project. Registering an agent therefore does two things that must both succeed:
it inserts the governed row, and it ensures the project the agent's runs will
land in exists. The project is ensured *first* — if telemetry cannot be
provisioned we refuse the registration rather than create an agent whose runs
have nowhere to go.

Three invariants hold in every function below:

* every statement filters on ``principal.workspace_id``, and every telemetry
  read is addressed by the project id stored on a row we already loaded from
  this workspace, so a cross-tenant read is not expressible;
* status moves only through the transition table in :data:`STATUS_TRANSITIONS`;
  no endpoint assigns ``status`` directly;
* every state change writes an audit row naming the console screen it came
  from, inside the same transaction as the change.

Telemetry failures surface as :class:`TelemetryBackendUnavailable` with the
adapter's exception as the cause. Nothing here substitutes a number when the
engine cannot answer: the caller gets an error, not a plausible zero.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import difflib
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Final, TypeVar

from fastapi import Request
from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import (
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.base import new_id
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    EngineUnavailable,
    get_engine_client,
)
from ..models.governance import Policy, PolicyBinding, PolicyViolation
from ..models.identity import Role, User
from ..models.registry import (
    Agent,
    AgentConnector,
    AgentStatus,
    AgentType,
    Connector,
    EnvironmentType,
    Platform,
    PolicyStatus,
    RiskLevel,
)
from ..schemas.agents import (
    TEMPLATE_PREVIEW_CHARS,
    AgentClone,
    AgentConfiguration,
    AgentConnectorRead,
    AgentCreate,
    AgentDetail,
    AgentMetrics,
    AgentOwnerFacet,
    AgentPolicyBindingRead,
    AgentRead,
    AgentRunAccepted,
    AgentRunRequest,
    AgentRunStats,
    AgentsSummary,
    AgentUpdate,
    AgentVersionCreate,
    AgentVersionDiff,
    AgentVersionRead,
    DiffLine,
    MetadataChange,
    MetricPoint,
    MetricSeries,
    NamedScore,
    RetryPolicy,
    slugify,
)
from . import audit

T = TypeVar("T")

SOURCE_SCREEN: Final[str] = "Agent Registry"
SOURCE_SCREEN_DETAIL: Final[str] = "Agent Detail"
ENTITY_TYPE: Final[str] = "agent"

MAX_EXPORT_ROWS: Final[int] = 5_000

#: Window the detail screen's counters and latency chart cover.
STATS_WINDOW_DAYS: Final[int] = 30

#: Metric the engine reports run duration under, and the bucket width we ask
#: for. Thirty daily buckets is what the detail chart plots.
LATENCY_METRIC: Final[str] = "DURATION"
LATENCY_INTERVAL: Final[str] = "DAILY"

#: Versions are commits of the agent's system prompt, kept in the telemetry
#: engine's prompt library under the agent's project name. One prompt per
#: agent, so a commit list is a version history.
PROMPT_NAME_SUFFIX: Final[str] = "-system-prompt"

#: Upper bound on a version listing; a prompt with more commits than this is
#: paged by the engine and the diff endpoint resolves by commit anyway.
MAX_VERSIONS: Final[int] = 200

#: Legal status moves. Anything not listed is refused with 409.
STATUS_TRANSITIONS: Final[dict[AgentStatus, frozenset[AgentStatus]]] = {
    AgentStatus.PENDING_REVIEW: frozenset({AgentStatus.ACTIVE, AgentStatus.INACTIVE}),
    AgentStatus.ACTIVE: frozenset({AgentStatus.INACTIVE}),
    AgentStatus.INACTIVE: frozenset({AgentStatus.ACTIVE}),
}

#: Sort keys accepted by the registry table. Both the API's field names and the
#: console's column keys are honoured so the header cells can sort by their own
#: key without a translation table in the frontend.
SORTABLE: Final[dict[str, Any]] = {
    "name": Agent.name,
    "platform": Agent.platform,
    "source": Agent.platform,
    "agent_type": Agent.agent_type,
    "type": Agent.agent_type,
    "environment": Agent.environment,
    "env": Agent.environment,
    "status": Agent.status,
    "risk": Agent.risk,
    "policy_status": Agent.policy_status,
    "policy": Agent.policy_status,
    "team": Agent.team,
    "model": Agent.model,
    "health": Agent.health,
    "last_used_at": Agent.last_used_at,
    "last_used": Agent.last_used_at,
    "created_at": Agent.created_at,
    "updated_at": Agent.updated_at,
}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive instant as UTC; clients are not required to send an offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _scoped(principal: Principal) -> Select:
    return select(Agent).where(Agent.workspace_id == principal.workspace_id)


def _telemetry_error(exc: EngineError) -> Exception:
    """Translate an adapter failure into the API's error envelope.

    The adapter's exception is always kept as the cause, so the traceback still
    names what actually failed while the client sees a stable code.
    """
    if isinstance(exc, EngineNotFound):
        return NotFound("The telemetry store does not hold that record.")
    if isinstance(exc, EngineUnavailable):
        return TelemetryBackendUnavailable()
    if isinstance(exc, EngineBadRequest):
        return TelemetryBackendUnavailable(
            f"The telemetry store rejected the request (status {exc.status}).",
            code="telemetry_rejected",
        )
    return TelemetryBackendUnavailable()


async def _call(awaitable: Awaitable[T]) -> T:
    """Await one engine call, mapping its failures onto the API's errors."""
    try:
        return await awaitable
    except EngineError as exc:
        raise _telemetry_error(exc) from exc


def _client() -> EngineClient:
    try:
        return get_engine_client()
    except EngineError as exc:
        raise _telemetry_error(exc) from exc


def _project_name(principal: Principal, slug: str) -> str:
    """Project namespace for one agent.

    The workspace slug is globally unique, so prefixing it makes a collision
    between two tenants' agents impossible inside the shared engine namespace.
    """
    return f"{principal.workspace_slug}-{slug}"


def _prompt_name(agent: Agent) -> str:
    return f"{agent.engine_project_name or agent.slug}{PROMPT_NAME_SUFFIX}"


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the row array out of an engine envelope, whatever it is named."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("content", "items", "data", "results", "prompts", "versions", "projects"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _number(value: Any) -> float | None:
    """Coerce an engine scalar to a float, or None when it is not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _instant(value: Any) -> dt.datetime | None:
    """Parse an engine timestamp; unparseable values are dropped, not guessed."""
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return _as_utc(dt.datetime.fromisoformat(text))
    except ValueError:
        return None


def _iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("template", "text", "content", "prompt", "value"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
    return None


def _flatten_metrics(payload: Any) -> dict[str, float]:
    """Flatten an engine stats envelope into ``{metric name: value}``.

    Stats arrive either as a list of ``{"name": …, "value": …}`` objects or as
    plain numeric attributes on the row; both shapes are folded into one map so
    the callers below can look a metric up by name and get None when the engine
    did not report it.
    """
    flat: dict[str, float] = {}

    def absorb_value(name: str, value: Any) -> None:
        """File one attribute, walking into the engine's nested shapes.

        Stats rows nest freely — ``duration`` is ``{"p50": …, "p90": …}``,
        ``usage`` is ``{"total_tokens": …}``, ``error_count`` is
        ``{"count": …, "deviation": …}`` and ``feedback_scores`` is a list of
        named values — so nesting becomes a dotted name rather than being lost.
        """
        number = _number(value)
        if number is not None:
            flat.setdefault(name, number)
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                absorb_value(f"{name}.{str(key).strip().lower()}", nested)
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                nested_name = item.get("name")
                nested_value = _number(item.get("value"))
                if isinstance(nested_name, str) and nested_value is not None:
                    flat.setdefault(f"{name}.{nested_name.strip().lower()}", nested_value)

    def absorb(row: dict[str, Any]) -> None:
        stats = row.get("stats")
        if isinstance(stats, list):
            for item in stats:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                value = _number(item.get("value"))
                if value is not None:
                    flat.setdefault(name.strip().lower(), value)
                else:
                    absorb_value(name.strip().lower(), item.get("value"))
        for key, value in row.items():
            if key == "stats":
                continue
            absorb_value(key.strip().lower(), value)

    rows = _rows(payload)
    if rows:
        for row in rows:
            absorb(row)
    elif isinstance(payload, dict):
        absorb(payload)
    return flat


def _pick(metrics: dict[str, float], *names: str) -> float | None:
    """First metric present under any of ``names``, else a substring match."""
    for name in names:
        if name in metrics:
            return metrics[name]
    for name in names:
        for key, value in metrics.items():
            if name in key:
                return value
    return None


def _ms_to_seconds(value: float | None) -> float | None:
    """Engine durations are milliseconds; the console renders seconds."""
    return None if value is None else round(value / 1000.0, 3)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _owner_labels(
    session: AsyncSession, user_ids: set[str]
) -> dict[str, tuple[str, str]]:
    """Resolve owner ids to (display name, email) for the Owner column."""
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {user_id: (full_name, email) for user_id, full_name, email in rows}


def _metrics_of(agent: Agent) -> AgentMetrics | None:
    """Read the cached rolling metrics off the row.

    ``metrics_cache`` stores exactly :class:`AgentMetrics`' fields — it is
    written by :func:`get_detail` from the engine's answer — so a malformed or
    empty cache yields None and the console shows a dash.
    """
    cache = agent.metrics_cache
    if not isinstance(cache, dict) or not cache:
        return None
    payload = dict(cache)
    payload.setdefault("computed_at", agent.metrics_cached_at)
    try:
        return AgentMetrics.model_validate(payload)
    except ValueError:
        return None


async def read_agents(
    session: AsyncSession, agents: Sequence[Agent]
) -> list[AgentRead]:
    """Shape rows for the table, resolving owner names in one extra statement."""
    labels = await _owner_labels(session, {a.owner_user_id or "" for a in agents})
    reads: list[AgentRead] = []
    for agent in agents:
        name, email = labels.get(agent.owner_user_id or "", (None, None))
        reads.append(
            AgentRead(
                id=agent.id,
                name=agent.name,
                slug=agent.slug,
                description=agent.description,
                platform=Platform(agent.platform),
                agent_type=AgentType(agent.agent_type),
                environment=EnvironmentType(agent.environment),
                status=AgentStatus(agent.status),
                risk=RiskLevel(agent.risk),
                policy_status=PolicyStatus(agent.policy_status),
                owner_user_id=agent.owner_user_id,
                owner_name=name,
                owner_email=email,
                team=agent.team,
                tags=[str(tag) for tag in (agent.tags or [])],
                model=agent.model,
                prompt_version=agent.prompt_version,
                tools_enabled=agent.tools_enabled,
                policies_applied=agent.policies_applied,
                retries=agent.retries,
                memory_policy=agent.memory_policy,
                access_scope=agent.access_scope,
                last_used_at=agent.last_used_at,
                health=agent.health,
                engine_project_id=agent.engine_project_id,
                engine_project_name=agent.engine_project_name,
                is_provisioned=agent.is_provisioned,
                metrics=_metrics_of(agent),
                created_at=agent.created_at,
                updated_at=agent.updated_at,
                created_by=agent.created_by,
                updated_by=agent.updated_by,
            )
        )
    return reads


async def _owner_ids(session: AsyncSession, owner: str) -> list[str]:
    """Resolve the Owner filter value, which may be an id, an email or a name."""
    rows = (
        await session.execute(
            select(User.id).where(
                or_(User.id == owner, User.email == owner, User.full_name == owner)
            )
        )
    ).scalars().all()
    # An unmatched value must select nothing rather than everything.
    return list(rows) or [owner]


async def _list_stmt(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    platform: Platform | None,
    environment: EnvironmentType | None,
    status: AgentStatus | None,
    risk: RiskLevel | None,
    policy_status: PolicyStatus | None,
    owner: str | None,
    team: str | None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt,
        params,
        [Agent.name, Agent.description, Agent.platform, Agent.model, Agent.team],
    )
    stmt = apply_filters(
        stmt,
        {
            Agent.platform: platform.value if platform else None,
            Agent.environment: environment.value if environment else None,
            Agent.status: status.value if status else None,
            Agent.risk: risk.value if risk else None,
            Agent.policy_status: policy_status.value if policy_status else None,
            Agent.team: team,
            Agent.owner_user_id: (await _owner_ids(session, owner)) if owner else None,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=Agent.name, default_desc=False)


async def list_agents(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    platform: Platform | None = None,
    environment: EnvironmentType | None = None,
    status: AgentStatus | None = None,
    risk: RiskLevel | None = None,
    policy_status: PolicyStatus | None = None,
    owner: str | None = None,
    team: str | None = None,
) -> tuple[Sequence[Agent], int]:
    """One page of the workspace's agents, filtered the way the table is."""
    stmt = await _list_stmt(
        session,
        principal,
        params,
        platform=platform,
        environment=environment,
        status=status,
        risk=risk,
        policy_status=policy_status,
        owner=owner,
        team=team,
    )
    return await paginate(session, stmt, params)


async def export_agents(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    platform: Platform | None = None,
    environment: EnvironmentType | None = None,
    status: AgentStatus | None = None,
    risk: RiskLevel | None = None,
    policy_status: PolicyStatus | None = None,
    owner: str | None = None,
    team: str | None = None,
) -> Sequence[Agent]:
    """Every row the current filters select, capped so one click cannot pull the
    whole registry into memory."""
    stmt = await _list_stmt(
        session,
        principal,
        params,
        platform=platform,
        environment=environment,
        status=status,
        risk=risk,
        policy_status=policy_status,
        owner=owner,
        team=team,
    )
    return (await session.execute(stmt.limit(MAX_EXPORT_ROWS))).scalars().all()


async def get_agent(session: AsyncSession, principal: Principal, agent_id: str) -> Agent:
    """Load one agent, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as a row that never existed —
    a 403 would confirm the id is real.
    """
    agent = (
        await session.execute(_scoped(principal).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound(f"Agent '{agent_id}' does not exist.")
    return agent


async def summarise(session: AsyncSession, principal: Principal) -> AgentsSummary:
    """The six KPI cards, computed entirely in SQL.

    Policy Violations counts breaches recorded against this workspace in the
    last 30 days, and is compared with the 30 days before that; the two windows
    are the only honest way to render the card's delta.
    """
    now = _now()
    window_start = now - dt.timedelta(days=30)
    previous_start = now - dt.timedelta(days=60)
    workspace = Agent.workspace_id == principal.workspace_id

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(Agent.id).label("total"),
                _count_where(Agent.created_at >= window_start).label("added"),
                _count_where(Agent.status == AgentStatus.ACTIVE.value).label("active"),
                _count_where(Agent.status == AgentStatus.INACTIVE.value).label("inactive"),
                _count_where(Agent.risk == RiskLevel.HIGH.value).label("high_risk"),
                _count_where(
                    (Agent.risk == RiskLevel.HIGH.value) & (Agent.created_at >= window_start)
                ).label("high_risk_added"),
                _count_where(
                    (Agent.policy_status == PolicyStatus.APPROVAL_REQUIRED.value)
                    | (Agent.status == AgentStatus.PENDING_REVIEW.value)
                ).label("pending_approval"),
                _count_where(Agent.engine_project_id.is_not(None)).label("provisioned"),
            ).where(workspace)
        )
    ).one()

    violations = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(case((PolicyViolation.occurred_at >= window_start, 1), else_=0)),
                    0,
                ).label("current"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (PolicyViolation.occurred_at >= previous_start)
                                & (PolicyViolation.occurred_at < window_start),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("previous"),
            ).where(
                PolicyViolation.workspace_id == principal.workspace_id,
                PolicyViolation.occurred_at >= previous_start,
            )
        )
    ).one()

    grouped = (
        await session.execute(
            select(Agent.owner_user_id, func.count(Agent.id))
            .where(workspace)
            .group_by(Agent.owner_user_id)
        )
    ).all()
    labels = await _owner_labels(session, {owner or "" for owner, _ in grouped})
    owners = [
        AgentOwnerFacet(
            owner_user_id=owner_id,
            name=labels.get(owner_id or "", ("Unassigned", None))[0] or "Unassigned",
            email=labels.get(owner_id or "", (None, None))[1],
            agent_count=int(count),
        )
        for owner_id, count in grouped
    ]
    owners.sort(key=lambda facet: facet.name.lower())

    total = int(totals.total or 0)
    active = int(totals.active or 0)
    inactive = int(totals.inactive or 0)
    return AgentsSummary(
        total=total,
        total_added_30d=int(totals.added or 0),
        active=active,
        active_percent=round(active / total * 100, 1) if total else 0.0,
        high_risk=int(totals.high_risk or 0),
        high_risk_added_30d=int(totals.high_risk_added or 0),
        policy_violations_30d=int(violations.current or 0),
        policy_violations_previous_30d=int(violations.previous or 0),
        pending_approval=int(totals.pending_approval or 0),
        inactive=inactive,
        inactive_percent=round(inactive / total * 100, 1) if total else 0.0,
        provisioned=int(totals.provisioned or 0),
        owners=owners,
    )


# ---------------------------------------------------------------------------
# Detail payload
# ---------------------------------------------------------------------------


async def _linked_connectors(
    session: AsyncSession, principal: Principal, agent_id: str
) -> list[AgentConnectorRead]:
    """The Tools & Connectors tab: grants joined to the connectors they grant."""
    rows = (
        await session.execute(
            select(AgentConnector, Connector)
            .join(Connector, Connector.id == AgentConnector.connector_id)
            .where(
                AgentConnector.workspace_id == principal.workspace_id,
                AgentConnector.agent_id == agent_id,
            )
            .order_by(Connector.name.asc())
        )
    ).all()
    return [
        AgentConnectorRead(
            grant_id=grant.id,
            connector_id=connector.id,
            name=connector.name,
            connector_type=connector.connector_type,
            provider=connector.provider,
            risk_level=RiskLevel(connector.risk_level),
            status=connector.status,
            access=connector.access,
            data_classification=connector.data_classification,
            scopes=[str(scope) for scope in (connector.scopes or [])],
            endpoint_url=connector.endpoint_url,
            auth_mode=connector.auth_mode,
            last_used_at=connector.last_used_at,
            granted_at=grant.granted_at,
            granted_by=grant.granted_by,
            is_blocked=connector.is_blocked,
        )
        for grant, connector in rows
    ]


async def _policy_bindings(
    session: AsyncSession, principal: Principal, agent_id: str
) -> list[AgentPolicyBindingRead]:
    """Policies explicitly bound to this agent, for the Risk & Policy summary."""
    rows = (
        await session.execute(
            select(PolicyBinding, Policy)
            .join(Policy, Policy.id == PolicyBinding.policy_id)
            .where(
                PolicyBinding.workspace_id == principal.workspace_id,
                PolicyBinding.agent_id == agent_id,
            )
            .order_by(Policy.name.asc())
        )
    ).all()
    return [
        AgentPolicyBindingRead(
            binding_id=binding.id,
            policy_id=policy.id,
            name=policy.name,
            category=policy.category,
            scope=policy.scope,
            scope_label=policy.scope_label,
            risk_level=RiskLevel(policy.risk_level),
            status=policy.status,
            enforcement=policy.enforcement,
            version=policy.version,
            violations_30d=policy.violations_30d,
            bound_at=binding.bound_at,
            bound_by=binding.bound_by,
        )
        for binding, policy in rows
    ]


def _empty_stats(window_start: dt.datetime, window_end: dt.datetime) -> AgentRunStats:
    return AgentRunStats(window_start=window_start, window_end=window_end)


def _empty_series() -> MetricSeries:
    return MetricSeries(name="Average latency", unit="seconds", interval=LATENCY_INTERVAL)


async def _run_stats(
    client: EngineClient,
    agent: Agent,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> AgentRunStats:
    """Counters for one agent's project over the window, read from the engine."""
    payload = await _call(
        client.get_project_stats(
            name=agent.engine_project_name,
            from_time=window_start,
            to_time=window_end,
            size=20,
        )
    )
    rows = _rows(payload)
    matching = [
        row
        for row in rows
        if not agent.engine_project_id
        or row.get("project_id") in (agent.engine_project_id, None)
    ]
    metrics = _flatten_metrics(matching or payload)

    run_count = int(_pick(metrics, "trace_count", "count", "traces") or 0)
    error_count = int(_pick(metrics, "error_count.count", "error_count", "errors", "failed") or 0)
    # No bare "duration" alias: the stats row nests percentiles under it, and a
    # substring match would report a p50 as the average.
    duration_avg = _pick(metrics, "duration.avg", "duration_avg", "avg_duration")
    scores = [
        NamedScore(name=name, value=value)
        for name, value in sorted(metrics.items())
        if name.startswith("feedback_scores.") or name.startswith("feedback_score.")
    ]

    # `usage.*` and `span_count` are per-trace AVERAGES in this endpoint; only
    # the `usage_sum.*` / `total_estimated_cost_sum` spellings are totals.
    tokens = _pick(metrics, "usage_sum.total_tokens")
    if tokens is None:
        prompt = _pick(metrics, "usage_sum.prompt_tokens")
        completion = _pick(metrics, "usage_sum.completion_tokens")
        if prompt is not None or completion is not None:
            tokens = (prompt or 0.0) + (completion or 0.0)
    span_average = _pick(metrics, "span_count", "spans")

    return AgentRunStats(
        window_start=window_start,
        window_end=window_end,
        run_count=run_count,
        span_count=(
            int(round(span_average * run_count)) if span_average is not None else None
        ),
        error_count=error_count,
        success_rate=(
            round((run_count - error_count) / run_count * 100, 1) if run_count else None
        ),
        avg_duration_seconds=_ms_to_seconds(duration_avg),
        p50_duration_seconds=_ms_to_seconds(_pick(metrics, "duration.p50", "duration_p50")),
        p90_duration_seconds=_ms_to_seconds(_pick(metrics, "duration.p90", "duration_p90")),
        p99_duration_seconds=_ms_to_seconds(_pick(metrics, "duration.p99", "duration_p99")),
        total_tokens=int(tokens or 0),
        total_cost=round(
            _pick(metrics, "total_estimated_cost_sum", "total_cost") or 0.0, 4
        ),
        feedback_scores=scores,
    )


async def _latency_series(
    client: EngineClient,
    agent: Agent,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> MetricSeries:
    """Daily average latency for the detail screen's chart."""
    if not agent.engine_project_id:
        return _empty_series()
    payload = await _call(
        client.get_project_metrics(
            agent.engine_project_id,
            metric_type=LATENCY_METRIC,
            interval=LATENCY_INTERVAL,
            interval_start=window_start,
            interval_end=window_end,
        )
    )
    rows = _rows(payload)
    chosen: list[dict[str, Any]] = []
    for row in rows:
        data = row.get("data") or row.get("points")
        if isinstance(data, list) and data:
            chosen = [point for point in data if isinstance(point, dict)]
            # Prefer an average/p50 line when the engine returns several.
            name = str(row.get("name") or "").lower()
            if "avg" in name or "p50" in name or "duration" in name:
                break
    points = [
        MetricPoint(
            at=at,
            value=_ms_to_seconds(_number(point.get("value"))),
        )
        for point in chosen
        if (at := _instant(point.get("time") or point.get("timestamp") or point.get("at")))
        is not None
    ]
    series = _empty_series()
    series.points = points
    return series


def _configuration(agent: Agent, owner_name: str | None, tools: list[str]) -> AgentConfiguration:
    """The manifest the Configuration tab prints and Export Configuration downloads."""
    return AgentConfiguration(
        agent_id=agent.id,
        name=agent.name,
        source_platform=Platform(agent.platform),
        type=AgentType(agent.agent_type),
        owner=owner_name,
        environment=EnvironmentType(agent.environment),
        status=AgentStatus(agent.status),
        risk_level=RiskLevel(agent.risk),
        policy_status=PolicyStatus(agent.policy_status),
        model=agent.model,
        prompt_version=agent.prompt_version,
        allowed_tools=tools,
        memory_policy=agent.memory_policy,
        retry_policy=RetryPolicy(max_retries=agent.retries),
        access_scope=agent.access_scope,
        telemetry_project=agent.engine_project_name,
        exported_at=_now(),
    )


def _cache_metrics(agent: Agent, stats: AgentRunStats) -> bool:
    """Fold the engine's answer into the row's cache; True when it changed.

    The registry list renders hundreds of rows without one engine call per row,
    which is only possible if something keeps that cache warm; opening an agent
    is the natural moment, so a detail read refreshes it.

    The write is skipped when the numbers are unchanged. Rewriting an identical
    cache would move ``updated_at`` and make another editor's optimistic
    concurrency guard fail for no reason.
    """
    computed_at = _now()
    metrics = AgentMetrics(
        runs_30d=stats.run_count,
        success_rate_30d=stats.success_rate,
        avg_latency_seconds=stats.avg_duration_seconds,
        tokens_30d=stats.total_tokens,
        cost_30d=stats.total_cost,
        eval_score=next(
            (score.value for score in stats.feedback_scores if "eval" in score.name.lower()),
            None,
        ),
        computed_at=computed_at,
    )
    cache = metrics.model_dump(mode="json")
    # Counters the engine does not report stay at whatever the previous refresh
    # recorded rather than being reset to zero.
    previous = agent.metrics_cache if isinstance(agent.metrics_cache, dict) else {}
    for key in ("tool_calls_30d", "escalations_30d", "violations_30d"):
        if not cache.get(key) and previous.get(key):
            cache[key] = previous[key]

    comparable = {k: v for k, v in cache.items() if k != "computed_at"}
    if previous and {k: v for k, v in previous.items() if k != "computed_at"} == comparable:
        return False

    agent.metrics_cache = cache
    agent.metrics_cached_at = computed_at
    return True


async def get_detail(
    session: AsyncSession, principal: Principal, agent_id: str
) -> AgentDetail:
    """Everything the nine tabs need, in one response.

    The three telemetry reads run concurrently; an unprovisioned agent skips
    them entirely and reports ``telemetry_available=False`` rather than an
    empty-looking set of zeros.
    """
    agent = await get_agent(session, principal, agent_id)
    window_end = _now()
    window_start = window_end - dt.timedelta(days=STATS_WINDOW_DAYS)

    connectors = await _linked_connectors(session, principal, agent.id)
    policies = await _policy_bindings(session, principal, agent.id)

    telemetry_available = agent.is_provisioned
    if telemetry_available:
        client = _client()
        stats, series, versions = await asyncio.gather(
            _run_stats(client, agent, window_start, window_end),
            _latency_series(client, agent, window_start, window_end),
            _versions(client, agent),
        )
        if _cache_metrics(agent, stats):
            await session.flush()
            # ``updated_at`` is a server-side onupdate: the UPDATE expired it
            # rather than refetching it, so read it back before the row is
            # serialised.
            await session.refresh(agent)
    else:
        stats = _empty_stats(window_start, window_end)
        series = _empty_series()
        versions = []

    read = (await read_agents(session, [agent]))[0]
    return AgentDetail(
        agent=read,
        configuration=_configuration(
            agent, read.owner_name, [connector.name for connector in connectors]
        ),
        connectors=connectors,
        policies=policies,
        versions=versions,
        stats=stats,
        latency_series=series,
        telemetry_available=telemetry_available,
    )


async def export_configuration(
    session: AsyncSession, principal: Principal, agent_id: str
) -> AgentConfiguration:
    """The Export Configuration payload: the manifest and nothing else."""
    agent = await get_agent(session, principal, agent_id)
    connectors = await _linked_connectors(session, principal, agent.id)
    read = (await read_agents(session, [agent]))[0]
    return _configuration(agent, read.owner_name, [c.name for c in connectors])


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _unique_slug(session: AsyncSession, principal: Principal, name: str) -> str:
    """Slug for a new agent, probing forward until the workspace has room."""
    base = slugify(name)[:150]
    taken = set(
        (
            await session.execute(
                select(Agent.slug).where(
                    Agent.workspace_id == principal.workspace_id,
                    Agent.slug.like(f"{base}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise Conflict(f"Too many agents are already named like '{name}'.")


async def _ensure_project(principal: Principal, slug: str) -> tuple[str | None, str]:
    """Get-or-create the telemetry project this agent's runs will land in."""
    name = _project_name(principal, slug)
    project = await _call(_client().ensure_project(name))
    project_id = project.get("id") if isinstance(project, dict) else None
    project_name = project.get("name") if isinstance(project, dict) else None
    return (
        str(project_id) if project_id else None,
        str(project_name) if project_name else name,
    )


async def create_agent(
    session: AsyncSession,
    principal: Principal,
    payload: AgentCreate,
    *,
    request: Request | None = None,
) -> Agent:
    """Register an agent and provision its telemetry project.

    The project is ensured before the row is inserted: an agent whose runs have
    nowhere to land is worse than a failed registration. The row starts Pending
    Review — only an explicit activation, which checks the policy verdict, may
    make it Active. Requires the admin role.
    """
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(_scoped(principal).where(Agent.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"An agent named '{payload.name}' already exists.")

    slug = await _unique_slug(session, principal, payload.name)
    project_id, project_name = await _ensure_project(principal, slug)

    agent = Agent(
        workspace_id=principal.workspace_id,
        name=payload.name,
        slug=slug,
        description=payload.description,
        platform=payload.platform.value,
        agent_type=payload.agent_type.value,
        environment=payload.environment.value,
        status=AgentStatus.PENDING_REVIEW.value,
        risk=payload.risk.value,
        policy_status=PolicyStatus.ALLOWED.value,
        owner_user_id=payload.owner_user_id,
        team=payload.team,
        tags=list(payload.tags),
        model=payload.model,
        prompt_version=payload.prompt_version,
        tools_enabled=payload.tools_enabled,
        policies_applied=payload.policies_applied,
        retries=payload.retries,
        memory_policy=payload.memory_policy,
        access_scope=payload.access_scope,
        engine_project_id=project_id,
        engine_project_name=project_name,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(agent)
    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(f"An agent named '{payload.name}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="agent.created",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Registered {agent.platform} agent in {agent.environment}",
        metadata={"slug": slug, "telemetry_project": project_name},
        request=request,
    )
    await session.flush()
    # Server-side timestamp defaults land on the row, not on the instance;
    # refresh explicitly so the caller never triggers implicit IO while
    # serialising it.
    await session.refresh(agent)
    return agent


async def update_agent(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    payload: AgentUpdate,
    *,
    request: Request | None = None,
) -> Agent:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.ADMIN)
    agent = await get_agent(session, principal, agent_id)

    if payload.expected_updated_at is not None:
        current = agent.updated_at
        expected = _as_utc(payload.expected_updated_at)
        # One second of slack: clients round-trip the timestamp through JSON.
        if current is not None and abs((_as_utc(current) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{agent.name}' was changed by someone else. Reload and try again."
            )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return agent

    if "name" in changes and changes["name"] != agent.name:
        clash = (
            await session.execute(
                _scoped(principal).where(Agent.name == changes["name"], Agent.id != agent.id)
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"An agent named '{changes['name']}' already exists.")

    for field, value in changes.items():
        # Vocabulary enums are stored as their rendered label, not the member.
        setattr(agent, field, getattr(value, "value", value))
    agent.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"An agent named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="agent.updated",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN_DETAIL,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    # ``updated_at`` is a server-side onupdate: the UPDATE expired it rather
    # than refetching it, so read it back before the caller serialises the row.
    await session.refresh(agent)
    return agent


async def delete_agent(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove an agent, its connector grants and its policy bindings.

    Refused while the agent is Active: an agent still accepting runs must be
    deactivated first, which is the moment operators are told about. Telemetry
    already recorded stays in the engine — it is the historical record, and
    deleting the registration does not un-happen the runs. Requires admin.
    """
    principal.require(Role.ADMIN)
    agent = await get_agent(session, principal, agent_id)

    if agent.status == AgentStatus.ACTIVE.value:
        raise PreconditionFailed(
            f"'{agent.name}' is active. Deactivate it before removing the registration."
        )

    label = agent.name
    await audit.record(
        session,
        principal=principal,
        action="agent.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=label,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed {agent.platform} agent from the registry",
        metadata={"telemetry_project": agent.engine_project_name},
        request=request,
    )
    # Policy bindings carry no foreign key to agents, so they are removed here
    # rather than by a cascade.
    await session.execute(
        delete(PolicyBinding).where(
            PolicyBinding.workspace_id == principal.workspace_id,
            PolicyBinding.agent_id == agent.id,
        )
    )
    await session.delete(agent)
    await session.flush()


async def clone_agent(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    payload: AgentClone,
    *,
    request: Request | None = None,
) -> Agent:
    """Copy an agent into a new registration with its own telemetry project.

    The clone starts Pending Review with no run history of its own: it has not
    been through governance review, whatever its source had. Requires admin.
    """
    principal.require(Role.ADMIN)
    source = await get_agent(session, principal, agent_id)

    name = payload.name or f"{source.name} (Copy)"
    clash = (
        await session.execute(_scoped(principal).where(Agent.name == name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"An agent named '{name}' already exists.")

    slug = await _unique_slug(session, principal, name)
    project_id, project_name = await _ensure_project(principal, slug)

    clone = Agent(
        workspace_id=principal.workspace_id,
        name=name,
        slug=slug,
        description=source.description,
        platform=source.platform,
        agent_type=source.agent_type,
        environment=payload.environment.value,
        status=AgentStatus.PENDING_REVIEW.value,
        risk=source.risk,
        policy_status=PolicyStatus.ALLOWED.value,
        owner_user_id=source.owner_user_id,
        team=source.team,
        tags=list(source.tags or []),
        model=source.model,
        prompt_version=source.prompt_version,
        tools_enabled=source.tools_enabled,
        policies_applied=source.policies_applied,
        retries=source.retries,
        memory_policy=source.memory_policy,
        access_scope=source.access_scope,
        engine_project_id=project_id,
        engine_project_name=project_name,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(clone)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"An agent named '{name}' already exists.") from exc

    copied_connectors = 0
    if payload.copy_connectors:
        grants = (
            (
                await session.execute(
                    select(AgentConnector).where(
                        AgentConnector.workspace_id == principal.workspace_id,
                        AgentConnector.agent_id == source.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for grant in grants:
            session.add(
                AgentConnector(
                    workspace_id=principal.workspace_id,
                    agent_id=clone.id,
                    connector_id=grant.connector_id,
                    granted_at=_now(),
                    granted_by=principal.actor,
                )
            )
        copied_connectors = len(grants)

    copied_policies = 0
    if payload.copy_policies:
        bindings = (
            (
                await session.execute(
                    select(PolicyBinding).where(
                        PolicyBinding.workspace_id == principal.workspace_id,
                        PolicyBinding.agent_id == source.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for binding in bindings:
            session.add(
                PolicyBinding(
                    workspace_id=principal.workspace_id,
                    policy_id=binding.policy_id,
                    agent_id=clone.id,
                    bound_at=_now(),
                    bound_by=principal.actor,
                )
            )
        copied_policies = len(bindings)

    await audit.record(
        session,
        principal=principal,
        action="agent.cloned",
        entity_type=ENTITY_TYPE,
        entity_id=clone.id,
        entity_label=clone.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Cloned from '{source.name}' into {clone.environment} with "
            f"{copied_connectors} connector grant(s) and {copied_policies} policy binding(s)"
        ),
        metadata={
            "source_agent_id": source.id,
            "connectors": copied_connectors,
            "policies": copied_policies,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(clone)
    return clone


async def _transition(
    session: AsyncSession,
    principal: Principal,
    agent: Agent,
    target: AgentStatus,
    *,
    reason: str | None,
    request: Request | None,
) -> Agent:
    """Move an agent's status, enforcing the transition table and its gates."""
    current = AgentStatus(agent.status)
    if current == target:
        raise Conflict(f"'{agent.name}' is already {target.value.lower()}.")
    if target not in STATUS_TRANSITIONS.get(current, frozenset()):
        raise Conflict(
            f"'{agent.name}' cannot move from {current.value} to {target.value}."
        )

    if target is AgentStatus.ACTIVE:
        if not agent.is_provisioned:
            raise PreconditionFailed(
                f"'{agent.name}' has no telemetry project. Re-register it before activating."
            )
        if agent.policy_status == PolicyStatus.BLOCKED.value:
            raise PreconditionFailed(
                f"'{agent.name}' is blocked by policy and cannot be activated."
            )
        if agent.policy_status == PolicyStatus.APPROVAL_REQUIRED.value:
            raise PreconditionFailed(
                f"'{agent.name}' needs an approved request before it can be activated."
            )

    agent.status = target.value
    agent.updated_by = principal.actor
    await session.flush()

    verb = "activated" if target is AgentStatus.ACTIVE else "deactivated"
    await audit.record(
        session,
        principal=principal,
        action=f"agent.{verb}",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN,
        detail=reason or f"{current.value} to {target.value}",
        metadata={"from": current.value, "to": target.value},
        request=request,
    )
    await session.flush()
    await session.refresh(agent)
    return agent


async def activate_agent(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> Agent:
    """Let the agent accept runs again. Requires the operator role."""
    principal.require(Role.OPERATOR)
    agent = await get_agent(session, principal, agent_id)
    return await _transition(
        session, principal, agent, AgentStatus.ACTIVE, reason=reason, request=request
    )


async def deactivate_agent(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> Agent:
    """Stop the agent accepting new runs. In-flight runs are unaffected."""
    principal.require(Role.OPERATOR)
    agent = await get_agent(session, principal, agent_id)
    return await _transition(
        session, principal, agent, AgentStatus.INACTIVE, reason=reason, request=request
    )


async def trigger_run(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    payload: AgentRunRequest,
    *,
    request: Request | None = None,
) -> AgentRunAccepted:
    """Start one execution of the agent from the console.

    The invocation is recorded as an open trace in the telemetry engine — the
    run appears on Live Runs as ``Running`` immediately — and the agent's
    runtime completes it through the ingest API when it finishes. The metadata
    written here is exactly what Live Runs reads back: tenant, environment,
    platform, risk and who pressed the button.

    Refused unless the agent is active, provisioned and permitted by policy.
    Requires the operator role.
    """
    principal.require(Role.OPERATOR)
    agent = await get_agent(session, principal, agent_id)

    if agent.status != AgentStatus.ACTIVE.value:
        raise PreconditionFailed(
            f"'{agent.name}' is {AgentStatus(agent.status).value.lower()} and cannot be run."
        )
    if not agent.is_provisioned:
        raise PreconditionFailed(f"'{agent.name}' has no telemetry project to record into.")
    if agent.policy_status == PolicyStatus.BLOCKED.value:
        raise PreconditionFailed(f"'{agent.name}' is blocked by policy and cannot be run.")
    if agent.policy_status == PolicyStatus.APPROVAL_REQUIRED.value:
        raise PreconditionFailed(
            f"'{agent.name}' requires an approved request before it can be run."
        )

    started_at = _now()
    run_id = new_id()
    session_id = payload.session_id or f"ses-{new_id()}"
    metadata: dict[str, Any] = {
        **payload.metadata,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "environment": agent.environment,
        "platform": agent.platform,
        "model": agent.model,
        "risk": agent.risk,
        "tenant": principal.workspace_slug,
        "triggered_by": principal.actor,
        "source": SOURCE_SCREEN_DETAIL,
    }
    trace: dict[str, Any] = {
        "id": run_id,
        "project_name": agent.engine_project_name,
        "name": agent.name,
        "start_time": _iso(started_at),
        "thread_id": session_id,
        "metadata": metadata,
        "tags": [agent.environment, agent.platform],
    }
    if payload.input:
        trace["input"] = {"input": payload.input}

    await _call(_client().create_traces_batch([trace]))

    agent.last_used_at = started_at
    agent.updated_by = principal.actor
    await audit.record(
        session,
        principal=principal,
        action="agent.run_triggered",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN_DETAIL,
        detail=f"Run {run_id} started manually",
        metadata={"run_id": run_id, "session_id": session_id},
        request=request,
    )
    await session.flush()

    return AgentRunAccepted(
        run_id=run_id,
        agent_id=agent.id,
        agent_name=agent.name,
        started_at=started_at,
        telemetry_project=agent.engine_project_name,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _version_row(row: dict[str, Any], *, current_commit: str | None) -> AgentVersionRead:
    """Map one engine prompt commit onto the Version History entry."""
    commit = row.get("commit") or row.get("id")
    label = (
        row.get("version")
        or row.get("version_number")
        or (str(commit)[:8] if commit else "unversioned")
    )
    template = _text(row.get("template")) or ""
    status = row.get("status")
    is_current = bool(current_commit and str(commit) == current_commit)
    return AgentVersionRead(
        version=str(label),
        commit=str(commit) if commit else None,
        status=str(status) if status else ("Current" if is_current else "Superseded"),
        change_description=(
            row.get("change_description") or row.get("description") or row.get("message")
        ),
        author=row.get("created_by") or row.get("author") or row.get("last_updated_by"),
        created_at=_instant(row.get("created_at") or row.get("last_updated_at")),
        is_current=is_current,
        template_preview=template[:TEMPLATE_PREVIEW_CHARS] or None,
        token_count=(
            int(count) if (count := _number(row.get("token_count"))) is not None else None
        ),
    )


async def _prompt_row(client: EngineClient, agent: Agent) -> dict[str, Any] | None:
    """The engine prompt that holds this agent's version history, if it exists."""
    payload = await _call(
        client.list_prompts(
            name=_prompt_name(agent), project_id=agent.engine_project_id, size=5
        )
    )
    wanted = _prompt_name(agent)
    for row in _rows(payload):
        if str(row.get("name") or "") == wanted:
            return row
    return None


async def _raw_versions(client: EngineClient, agent: Agent) -> list[dict[str, Any]]:
    prompt = await _prompt_row(client, agent)
    if prompt is None or not prompt.get("id"):
        return []
    payload = await _call(client.list_versions(str(prompt["id"]), size=MAX_VERSIONS))
    return _rows(payload)


async def _versions(client: EngineClient, agent: Agent) -> list[AgentVersionRead]:
    """Version history, newest first, with the agent's live version marked."""
    rows = await _raw_versions(client, agent)
    if not rows:
        return []
    current = _current_commit(rows, agent)
    versions = [_version_row(row, current_commit=current) for row in rows]
    versions.sort(key=lambda v: (v.created_at is None, v.created_at), reverse=True)
    return versions


def _current_commit(rows: list[dict[str, Any]], agent: Agent) -> str | None:
    """Which commit the agent is actually running.

    ``Agent.prompt_version`` is the version label the registry shows, so it is
    matched first; otherwise the newest commit is the live one.
    """
    if agent.prompt_version:
        for row in rows:
            label = row.get("version") or row.get("version_number")
            if label and str(label) == agent.prompt_version:
                commit = row.get("commit") or row.get("id")
                return str(commit) if commit else None
    newest: dict[str, Any] | None = None
    newest_at: dt.datetime | None = None
    for row in rows:
        created = _instant(row.get("created_at") or row.get("last_updated_at"))
        if created is not None and (newest_at is None or created > newest_at):
            newest, newest_at = row, created
    if newest is None and rows:
        newest = rows[0]
    if newest is None:
        return None
    commit = newest.get("commit") or newest.get("id")
    return str(commit) if commit else None


async def list_versions(
    session: AsyncSession, principal: Principal, agent_id: str
) -> list[AgentVersionRead]:
    """The Version History pipe on the Model & Prompt tab."""
    agent = await get_agent(session, principal, agent_id)
    if not agent.is_provisioned:
        return []
    return await _versions(_client(), agent)


async def create_version(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    payload: AgentVersionCreate,
    *,
    request: Request | None = None,
) -> AgentVersionRead:
    """Commit a new version of the agent's system prompt. Requires admin."""
    principal.require(Role.ADMIN)
    agent = await get_agent(session, principal, agent_id)
    if not agent.is_provisioned:
        raise PreconditionFailed(
            f"'{agent.name}' has no telemetry project, so it cannot hold prompt versions."
        )

    client = _client()
    created = await _call(
        client.create_version(
            _prompt_name(agent),
            {
                "template": payload.template,
                "change_description": payload.change_description,
                "metadata": payload.metadata,
            },
            project_name=agent.engine_project_name,
        )
    )
    row = created if isinstance(created, dict) else {}
    version = _version_row(row, current_commit=str(row.get("commit") or row.get("id") or ""))
    version.is_current = payload.make_current

    if payload.make_current:
        agent.prompt_version = version.version[:24]
        agent.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="agent.version_created",
        entity_type=ENTITY_TYPE,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN_DETAIL,
        detail=payload.change_description or f"Committed prompt version {version.version}",
        metadata={"version": version.version, "commit": version.commit},
        request=request,
    )
    await session.flush()
    return version


def _match_version(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """Resolve a version coordinate, which may be a commit id or a label."""
    for row in rows:
        commit = str(row.get("commit") or row.get("id") or "")
        label = str(row.get("version") or row.get("version_number") or "")
        if wanted in (commit, label) or (commit and commit.startswith(wanted)):
            return row
    return None


async def diff_versions(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    *,
    from_version: str,
    to_version: str,
) -> AgentVersionDiff:
    """What the Compare Versions modal renders: a real line diff of two commits."""
    if from_version == to_version:
        raise ValidationFailed("Choose two different versions to compare.")

    agent = await get_agent(session, principal, agent_id)
    if not agent.is_provisioned:
        raise PreconditionFailed(f"'{agent.name}' has no prompt versions to compare.")

    rows = await _raw_versions(_client(), agent)
    if not rows:
        raise NotFound(f"'{agent.name}' has no recorded prompt versions.")

    left = _match_version(rows, from_version)
    right = _match_version(rows, to_version)
    if left is None:
        raise NotFound(f"Version '{from_version}' does not exist for this agent.")
    if right is None:
        raise NotFound(f"Version '{to_version}' does not exist for this agent.")

    current = _current_commit(rows, agent)
    left_text = (_text(left.get("template")) or "").splitlines()
    right_text = (_text(right.get("template")) or "").splitlines()

    lines: list[DiffLine] = []
    added = removed = 0
    for raw in difflib.unified_diff(left_text, right_text, lineterm="", n=3):
        if raw.startswith(("---", "+++")):
            continue
        if raw.startswith("@@"):
            lines.append(DiffLine(op="@", text=raw))
        elif raw.startswith("+"):
            added += 1
            lines.append(DiffLine(op="+", text=raw[1:]))
        elif raw.startswith("-"):
            removed += 1
            lines.append(DiffLine(op="-", text=raw[1:]))
        else:
            lines.append(DiffLine(op=" ", text=raw[1:] if raw.startswith(" ") else raw))

    left_meta = left.get("metadata") if isinstance(left.get("metadata"), dict) else {}
    right_meta = right.get("metadata") if isinstance(right.get("metadata"), dict) else {}
    metadata_changes = [
        MetadataChange(key=key, before=left_meta.get(key), after=right_meta.get(key))
        for key in sorted({*left_meta, *right_meta})
        if left_meta.get(key) != right_meta.get(key)
    ]

    return AgentVersionDiff(
        agent_id=agent.id,
        from_version=_version_row(left, current_commit=current),
        to_version=_version_row(right, current_commit=current),
        identical=not lines and not metadata_changes,
        added_lines=added,
        removed_lines=removed,
        diff=lines,
        metadata_changes=metadata_changes,
    )
