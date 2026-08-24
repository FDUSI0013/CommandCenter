"""Quota, cost and capacity.

This module owns three things the rest of the platform depends on:

* **Enforcement.** ``check_quota``, ``enforce_quota`` and ``record_usage`` are
  the request-path entry points. Ingest calls them before accepting telemetry
  and licensing calls them alongside its entitlement checks, so they are written
  to be cheap, to write only when they genuinely advance state, and never to
  fail open silently — a ``Block`` quota that is breached raises
  :class:`QuotaExceeded` and nothing else decides otherwise.
* **Attribution.** Spend, tokens and calls are measured by the telemetry engine
  and attributed to a model, a service class, a team or a budget scope using the
  agent registry. The attribution rule is stated on every function that applies
  one; no number is apportioned by a guess.
* **Evaluation.** Budgets and quotas carry thresholds. Crossing one moves the
  row's status and raises an alert through ``services.alerts``, deduplicated on
  the row and the state it entered so a breach that persists for a week is one
  alert, not a thousand.

Every statement filters on the caller's workspace, and every mutation writes an
audit row naming this screen.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import math
import statistics
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, QuotaExceeded, ValidationFailed
from ..engine import get_engine_client
from ..models.governance import AuditEvent
from ..models.identity import Role
from ..models.operations import (
    Alert,
    AlertSeverity,
    Budget,
    CapacityRecord,
    CapacityStatus,
    LimitPeriod,
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
    QuotaResource,
)
from ..schemas.approvals import ApprovalImpact, ApprovalRequestCreate, ApprovalRisk
from ..schemas.metrics import (
    MetricInterval,
    MetricKpi,
    MetricPoint,
    MetricsSeriesResponse,
    MetricWindow,
    SeriesMetric,
)
from ..schemas.quota import (
    CRITICAL_PERCENT,
    WATCH_PERCENT,
    BudgetCreate,
    BudgetUpdate,
    CapacityHealth,
    ForecastPoint,
    InsightSeverity,
    QuotaCheck,
    QuotaCreate,
    QuotaForecast,
    QuotaIncreaseRequest,
    QuotaPeriod,
    QuotaSummary,
    QuotaUpdate,
    QuotaUsageMetric,
    health_for,
)
from . import alerts, approvals, audit
from . import metrics as telemetry

SOURCE_SCREEN: Final[str] = "Quota, Cost & Capacity"
ENTITY_QUOTA: Final[str] = "quota"
ENTITY_BUDGET: Final[str] = "budget"

#: A quota with no explicit reset instant is rolled onto the calendar period it
#: declares, so "Monthly" means the calendar month everywhere on the screen.
DAY: Final[dt.timedelta] = dt.timedelta(days=1)

#: The cadence read as a noun, for sentences like "10M tokens per month".
PERIOD_NOUN: Final[dict[LimitPeriod, str]] = {
    LimitPeriod.MONTHLY: "month",
    LimitPeriod.QUARTERLY: "quarter",
    LimitPeriod.ANNUAL: "year",
}

#: Capacity readings older than this are not shown: the table reports
#: measurements, not memories.
CAPACITY_HISTORY_DAYS: Final[int] = 30
MAX_CAPACITY_READINGS: Final[int] = 20_000
MAX_TREND_POINTS: Final[int] = 24

#: Width of every sparkline on the screen — capacity and team spend alike.
TREND_DAYS: Final[int] = 7

#: Ranked lists the screen shows are short by design.
TOP_DRIVER_LIMIT: Final[int] = 10

#: A forecast needs enough history to fit a line through.
MIN_FORECAST_POINTS: Final[int] = 4
#: Residuals beyond this many standard deviations are reported as anomalies.
ANOMALY_SIGMA: Final[float] = 3.0
#: z for a two-sided 95% interval.
CONFIDENCE_Z: Final[float] = 1.96

#: Unit-cost movement worth telling an operator about.
UNIT_COST_MOVE_PERCENT: Final[float] = 15.0
#: A ceiling this far below its limit is over-provisioned rather than safe.
IDLE_UTILISATION_PERCENT: Final[float] = 20.0
#: Enough of the period must have elapsed before a pace projection means anything.
MIN_ELAPSED_FRACTION: Final[float] = 0.05

#: Statuses a caller may set by hand. Warning and Exceeded belong to the
#: evaluator: a limit is in trouble because of what was measured, not because
#: somebody typed it.
SETTABLE_STATUSES: Final[frozenset[LimitStatus]] = frozenset(
    {LimitStatus.ACTIVE, LimitStatus.DISABLED}
)
#: Quotas in these states are not consulted by the enforcement path.
INERT_STATUSES: Final[tuple[str, ...]] = (
    LimitStatus.DISABLED.value,
    LimitStatus.EXPIRED.value,
)

QUOTA_SORTABLE: Final[dict[str, Any]] = {
    "name": Quota.name,
    "resource": Quota.resource,
    "scope": Quota.scope,
    "limit_value": Quota.limit_value,
    "used_value": Quota.used_value,
    "status": Quota.status,
    "enforcement": Quota.enforcement,
    "period": Quota.period,
    "resets_at": Quota.resets_at,
    "created_at": Quota.created_at,
    "updated_at": Quota.updated_at,
}

BUDGET_SORTABLE: Final[dict[str, Any]] = {
    "name": Budget.name,
    "scope": Budget.scope,
    "amount_usd": Budget.amount_usd,
    "spent_usd": Budget.spent_usd,
    "status": Budget.status,
    "period_start": Budget.period_start,
    "period_end": Budget.period_end,
    "created_at": Budget.created_at,
    "updated_at": Budget.updated_at,
}

#: How a billed model name is classified into a service family for the Cost by
#: Service donut. First match wins; the rules are substrings of the model name
#: the engine billed, so the grouping only ever regroups measured cost.
SERVICE_RULES: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("embedding", "Embedding Service", ("embed",)),
    ("speech", "Speech Service", ("whisper", "tts", "speech", "transcribe", "audio")),
    ("image", "Image Service", ("dall", "diffusion", "image-gen")),
    ("retrieval", "Vector & Retrieval", ("rerank", "retrieval", "search")),
    ("inference", "Model Inference", ()),
)
UNCLASSIFIED_SERVICE: Final[tuple[str, str]] = ("other", "Other Services")

#: Icon key per capacity family, matched as a substring of ``resource_type``.
CAPACITY_ICONS: Final[tuple[tuple[str, str], ...]] = (
    ("gpu", "cpu"),
    ("cpu", "cpu"),
    ("memory", "hardDrive"),
    ("ram", "hardDrive"),
    ("disk", "database"),
    ("storage", "database"),
    ("network", "wifi"),
    ("bandwidth", "wifi"),
)
DEFAULT_CAPACITY_ICON: Final[str] = "activity"

QUOTA_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("name", "Quota Type"),
    ("resource", "Resource"),
    ("scope", "Scope"),
    ("used_value", "Used"),
    ("limit_value", "Limit"),
    ("unit", "Unit"),
    ("utilization_percent", "Utilization"),
    ("health", "Status"),
    ("enforcement", "Enforcement"),
    ("resets_label", "Resets"),
]

BUDGET_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("name", "Budget"),
    ("scope", "Scope"),
    ("period", "Period"),
    ("spent_usd", "SpentUSD"),
    ("amount_usd", "BudgetUSD"),
    ("utilization_percent", "Utilization"),
    ("projected_spend_usd", "ProjectedUSD"),
    ("status", "Status"),
    ("resets_label", "Resets"),
]

TEAM_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("team", "Team"),
    ("spend_usd", "SpendMTD"),
    ("share_display", "PercentOfTotal"),
    ("tokens", "Tokens"),
    ("api_calls", "APICalls"),
    ("avg_cost_per_1k_tokens", "AvgCostPer1K"),
]

CAPACITY_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("name", "Resource"),
    ("resource_type", "Type"),
    ("region", "Region"),
    ("used", "Used"),
    ("provisioned", "Provisioned"),
    ("unit", "Unit"),
    ("utilization_percent", "Utilization"),
    ("headroom", "Headroom"),
    ("status", "Status"),
    ("measured_at", "Measured"),
]

MODEL_COST_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("model", "Model"),
    ("agent_count", "Agents"),
    ("cost_usd", "CostUSD"),
    ("previous_cost_usd", "PriorCostUSD"),
    ("cost_delta_percent", "CostDeltaPercent"),
    ("tokens", "Tokens"),
    ("api_calls", "APICalls"),
    ("cost_per_1k_tokens", "CostPer1K"),
    ("unit_cost_delta_percent", "UnitCostDeltaPercent"),
]

SERVICE_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("label", "Service"),
    ("cost_usd", "CostUSD"),
    ("share_percent", "PercentOfTotal"),
    ("model_count", "Models"),
]

DRIVER_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("label", "Driver"),
    ("cost_usd", "CostUSD"),
    ("share_display", "PercentOfTotal"),
]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive instant as UTC; clients are not required to send an offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _add_months(moment: dt.datetime, months: int) -> dt.datetime:
    """Month arithmetic on a period boundary, which is always day one."""
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    return moment.replace(year=year, month=month, day=1)


def period_bounds(period: LimitPeriod, at: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Start and end of the calendar period containing ``at``.

    Periods are calendar-aligned so "Monthly" means the same thing to a budget,
    to a quota and to the person reading the screen.
    """
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is LimitPeriod.ANNUAL:
        start = midnight.replace(month=1, day=1)
        return start, _add_months(start, 12)
    if period is LimitPeriod.QUARTERLY:
        start = midnight.replace(month=(at.month - 1) // 3 * 3 + 1, day=1)
        return start, _add_months(start, 3)
    start = midnight.replace(day=1)
    return start, _add_months(start, 1)


def resolve_period(period: QuotaPeriod, *, at: dt.datetime | None = None) -> telemetry.Range:
    """Map the screen's period picker onto a measurement window."""
    if period is QuotaPeriod.MTD:
        return telemetry.month_to_date(at=at)
    return telemetry.resolve_window(_WINDOW_FOR[period], at=at)


_WINDOW_FOR: Final[dict[QuotaPeriod, MetricWindow]] = {
    QuotaPeriod.LAST_24H: MetricWindow.LAST_24H,
    QuotaPeriod.LAST_7D: MetricWindow.LAST_7D,
    QuotaPeriod.LAST_30D: MetricWindow.LAST_30D,
    QuotaPeriod.LAST_90D: MetricWindow.LAST_90D,
}


def _days_until(moment: dt.datetime | None, *, at: dt.datetime | None = None) -> int | None:
    if moment is None:
        return None
    remaining = _as_utc(moment) - (at or _now())
    return max(0, math.ceil(remaining.total_seconds() / DAY.total_seconds()))


def _resets_label(days: int | None) -> str | None:
    if days is None:
        return None
    if days <= 0:
        return "Resets today"
    if days == 1:
        return "Resets tomorrow"
    return f"Resets in {days} days"


def _utilisation(used: float | None, limit: float | None) -> float:
    if not limit:
        return 0.0
    return round(float(used or 0.0) / float(limit) * 100, 1)


# ---------------------------------------------------------------------------
# Quotas — reads
# ---------------------------------------------------------------------------


def _scoped_quotas(principal: Principal) -> Select:
    return select(Quota).where(Quota.workspace_id == principal.workspace_id)


def quota_payload(quota: Quota, *, at: dt.datetime | None = None) -> dict[str, Any]:
    """One quota as the table renders it, with the derived chip and countdown."""
    utilization = _utilisation(quota.used_value, quota.limit_value)
    health = health_for(utilization)
    days = _days_until(quota.resets_at, at=at)
    return {
        "id": quota.id,
        "name": quota.name,
        "resource": quota.resource,
        "scope": quota.scope,
        "scope_ref": quota.scope_ref,
        "limit_value": float(quota.limit_value or 0.0),
        "used_value": float(quota.used_value or 0.0),
        "unit": quota.unit,
        "used_display": telemetry.compact(float(quota.used_value or 0.0)),
        "limit_display": telemetry.compact(float(quota.limit_value or 0.0)),
        "utilization_percent": utilization,
        "remaining_value": round(
            max(0.0, float(quota.limit_value or 0.0) - float(quota.used_value or 0.0)), 4
        ),
        "enforcement": quota.enforcement,
        "period": quota.period,
        "status": quota.status,
        "health": health,
        "health_color": health.color,
        "resets_at": quota.resets_at,
        "resets_in_days": days,
        "resets_label": _resets_label(days),
        "created_at": quota.created_at,
        "updated_at": quota.updated_at,
        "created_by": quota.created_by,
        "updated_by": quota.updated_by,
    }


async def list_quotas(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    resource: QuotaResource | None = None,
    scope: LimitScope | None = None,
    status: LimitStatus | None = None,
    enforcement: QuotaEnforcement | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the workspace's quotas, filtered the way the table is."""
    stmt = _scoped_quotas(principal)
    stmt = apply_search(stmt, params, [Quota.name, Quota.unit, Quota.scope_ref])
    stmt = apply_filters(
        stmt,
        {
            Quota.resource: resource.value if resource else None,
            Quota.scope: scope.value if scope else None,
            Quota.status: status.value if status else None,
            Quota.enforcement: enforcement.value if enforcement else None,
        },
    )
    stmt = apply_sort(stmt, params, QUOTA_SORTABLE, default=Quota.name, default_desc=False)
    rows, total = await paginate(session, stmt, params)
    return [quota_payload(row) for row in rows], total


async def all_quota_rows(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    """Every quota, unpaged — used by the summary, the insights and the CSV."""
    rows = (
        (await session.execute(_scoped_quotas(principal).order_by(Quota.name.asc())))
        .scalars()
        .all()
    )
    return [quota_payload(row) for row in rows]


async def get_quota(session: AsyncSession, principal: Principal, quota_id: str) -> Quota:
    """Load one quota, or raise :class:`NotFound`.

    A quota in another workspace answers 404 exactly as a quota that never
    existed would; a 403 would confirm the id is real.
    """
    quota = (
        await session.execute(_scoped_quotas(principal).where(Quota.id == quota_id))
    ).scalar_one_or_none()
    if quota is None:
        raise NotFound(f"Quota '{quota_id}' does not exist.")
    return quota


# ---------------------------------------------------------------------------
# Quotas — evaluation and enforcement
# ---------------------------------------------------------------------------


def _status_for(utilization: float, current: str) -> LimitStatus:
    """Durable state of a limit at a given utilisation.

    Disabled and expired are terminal until a person changes them: a disabled
    quota that is over its ceiling is still disabled.
    """
    if current in INERT_STATUSES:
        return LimitStatus(current)
    if utilization >= 100:
        return LimitStatus.EXCEEDED
    if utilization >= WATCH_PERCENT:
        return LimitStatus.WARNING
    return LimitStatus.ACTIVE


def _refresh_period(quota: Quota, at: dt.datetime) -> bool:
    """Roll a quota onto its next period once the reset instant has passed.

    Called only from the write paths — usage recording and enforcement — so a
    read never mutates a row. The window therefore rolls the first time the
    quota is consulted after it expires, which is the moment it matters.
    """
    if quota.resets_at is None or at < _as_utc(quota.resets_at):
        return False
    quota.used_value = 0.0
    quota.resets_at = period_bounds(LimitPeriod(quota.period), at)[1]
    if quota.status in (LimitStatus.WARNING.value, LimitStatus.EXCEEDED.value):
        quota.status = LimitStatus.ACTIVE.value
    return True


async def _evaluate_quota(
    session: AsyncSession, quota: Quota, *, request: Request | None = None
) -> LimitStatus:
    """Move a quota's status to match its usage and alert on an escalation.

    An alert is raised only when the state actually changes, and it is
    deduplicated on the quota and the state it entered, so a ceiling that stays
    breached produces one alert with a rising occurrence count.
    """
    previous = quota.status
    utilization = _utilisation(quota.used_value, quota.limit_value)
    status = _status_for(utilization, previous)
    quota.status = status.value
    if status.value == previous or status not in (LimitStatus.WARNING, LimitStatus.EXCEEDED):
        return status

    breached = status is LimitStatus.EXCEEDED
    blocking = quota.enforcement == QuotaEnforcement.BLOCK.value
    await alerts.raise_alert(
        session,
        workspace_id=quota.workspace_id,
        title=(
            f"Quota exceeded: {quota.name}"
            if breached
            else f"Quota approaching limit: {quota.name}"
        ),
        description=(
            f"{quota.name} is at {utilization:.1f}% of "
            f"{telemetry.compact(float(quota.limit_value or 0.0))} {quota.unit}."
        ),
        source=SOURCE_SCREEN,
        severity=(
            AlertSeverity.CRITICAL
            if breached and blocking
            else AlertSeverity.HIGH
            if breached
            else AlertSeverity.MEDIUM
        ),
        dedupe_key=f"quota:{quota.id}:{status.value}",
        source_entity_type=ENTITY_QUOTA,
        source_entity_id=quota.id,
        metadata={
            "utilization_percent": utilization,
            "limit_value": float(quota.limit_value or 0.0),
            "used_value": float(quota.used_value or 0.0),
            "unit": quota.unit,
            "enforcement": quota.enforcement,
        },
        request=request,
    )
    return status


async def _applicable_quotas(
    session: AsyncSession,
    workspace_id: str,
    resource: QuotaResource,
    scope_ref: str | None,
) -> list[Quota]:
    """Every live quota that governs this resource for this caller.

    A workspace-scoped quota always applies. A narrower one applies only when
    the caller named the target it binds to, so an unattributed call is governed
    by the workspace ceiling alone rather than by every team's ceiling at once.
    """
    workspace_wide = Quota.scope == LimitScope.WORKSPACE.value
    applies = (
        or_(workspace_wide, Quota.scope_ref == scope_ref) if scope_ref else workspace_wide
    )
    stmt = (
        select(Quota)
        .where(
            Quota.workspace_id == workspace_id,
            Quota.resource == resource.value,
            Quota.status.not_in(INERT_STATUSES),
            applies,
        )
        .order_by(Quota.name.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _verdict(
    quotas: Sequence[Quota], resource: QuotaResource, amount: float
) -> QuotaCheck:
    """The tightest applicable quota, and whether the amount may be consumed."""
    if not quotas:
        return QuotaCheck(allowed=True, resource=resource)

    def _projected(quota: Quota) -> float:
        return float(quota.used_value or 0.0) + amount

    tightest = max(quotas, key=lambda q: _utilisation(_projected(q), q.limit_value))
    blocked = [
        quota
        for quota in quotas
        if quota.enforcement == QuotaEnforcement.BLOCK.value
        and quota.limit_value
        and _projected(quota) > float(quota.limit_value)
    ]
    offender = blocked[0] if blocked else tightest
    projected = _projected(offender)
    utilization = _utilisation(projected, offender.limit_value)
    return QuotaCheck(
        allowed=not blocked,
        resource=resource,
        quota_id=offender.id,
        quota_name=offender.name,
        limit_value=float(offender.limit_value or 0.0),
        used_value=float(offender.used_value or 0.0),
        projected_value=projected,
        remaining_value=round(max(0.0, float(offender.limit_value or 0.0) - projected), 4),
        utilization_percent=utilization,
        enforcement=QuotaEnforcement(offender.enforcement),
        status=LimitStatus(offender.status),
        health=health_for(utilization),
        reason=(
            f"'{offender.name}' allows {telemetry.compact(float(offender.limit_value or 0.0))} "
            f"{offender.unit} per {PERIOD_NOUN[LimitPeriod(offender.period)]} and this "
            f"request would take it to {telemetry.compact(projected)}."
            if blocked
            else None
        ),
    )


async def check_quota(
    session: AsyncSession,
    workspace_id: str,
    *,
    resource: QuotaResource,
    amount: float = 0.0,
    scope_ref: str | None = None,
) -> QuotaCheck:
    """Would consuming ``amount`` of ``resource`` be allowed? Never writes.

    This is the read the ingest path and the licensing checks call before doing
    work. It takes a ``workspace_id`` rather than a principal because most
    callers are machine paths with no user attached. A workspace with no quota
    for the resource is allowed: absence of a ceiling is not a ceiling of zero.
    """
    quotas = await _applicable_quotas(session, workspace_id, resource, scope_ref)
    return _verdict(quotas, resource, amount)


async def enforce_quota(
    session: AsyncSession,
    workspace_id: str,
    *,
    resource: QuotaResource,
    amount: float = 0.0,
    scope_ref: str | None = None,
    request: Request | None = None,
) -> QuotaCheck:
    """Check, and raise :class:`QuotaExceeded` when a Block quota refuses.

    ``Warn`` and ``Log`` quotas never raise: the traffic is admitted and the
    breach is recorded, which is the difference between a guardrail and a
    report. Expired periods are rolled here, so enforcement is always measured
    against the current window.
    """
    quotas = await _applicable_quotas(session, workspace_id, resource, scope_ref)
    at = _now()
    rolled = False
    for quota in quotas:
        if _refresh_period(quota, at):
            rolled = True
    if rolled:
        await session.flush()

    verdict = _verdict(quotas, resource, amount)
    if not verdict.allowed:
        raise QuotaExceeded(
            verdict.reason or "The workspace has exhausted this quota.",
            details={
                "quota_id": verdict.quota_id,
                "resource": resource.value,
                "limit_value": verdict.limit_value,
                "used_value": verdict.used_value,
                "requested": amount,
            },
        )
    return verdict


async def record_usage(
    session: AsyncSession,
    workspace_id: str,
    *,
    resource: QuotaResource,
    amount: float,
    scope_ref: str | None = None,
    request: Request | None = None,
) -> QuotaCheck:
    """Count consumption against every quota that governs it.

    Called by the ingest path once the work has actually happened. Each matching
    quota advances, its period rolls if it was due, and its status is
    re-evaluated — which is where a threshold alert is raised.
    """
    if amount < 0:
        raise ValidationFailed("Recorded usage cannot be negative.")
    quotas = await _applicable_quotas(session, workspace_id, resource, scope_ref)
    at = _now()
    for quota in quotas:
        _refresh_period(quota, at)
        quota.used_value = float(quota.used_value or 0.0) + amount
        await _evaluate_quota(session, quota, request=request)
    await session.flush()
    return _verdict(quotas, resource, 0.0)


# ---------------------------------------------------------------------------
# Quotas — writes
# ---------------------------------------------------------------------------


async def create_quota(
    session: AsyncSession,
    principal: Principal,
    payload: QuotaCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Register a ceiling. It starts empty and Active; the enforcer fills it in."""
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(_scoped_quotas(principal).where(Quota.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A quota named '{payload.name}' already exists.")

    at = _now()
    quota = Quota(
        workspace_id=principal.workspace_id,
        name=payload.name,
        resource=payload.resource.value,
        scope=payload.scope.value,
        scope_ref=payload.scope_ref,
        limit_value=payload.limit_value,
        used_value=0.0,
        unit=payload.unit,
        period=payload.period.value,
        enforcement=payload.enforcement.value,
        status=LimitStatus.ACTIVE.value,
        resets_at=payload.resets_at or period_bounds(payload.period, at)[1],
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(quota)
    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(f"A quota named '{payload.name}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="quota.created",
        entity_type=ENTITY_QUOTA,
        entity_id=quota.id,
        entity_label=quota.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{payload.resource.value} ceiling of "
            f"{telemetry.compact(payload.limit_value)} {payload.unit} "
            f"per {payload.period.value.lower()}, enforcement {payload.enforcement.value}"
        ),
        metadata={
            "resource": payload.resource.value,
            "limit_value": payload.limit_value,
            "enforcement": payload.enforcement.value,
            "scope": payload.scope.value,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(quota)
    return quota_payload(quota)


async def update_quota(
    session: AsyncSession,
    principal: Principal,
    quota_id: str,
    payload: QuotaUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Amend a ceiling, then re-evaluate it against the usage already counted."""
    principal.require(Role.ADMIN)
    quota = await get_quota(session, principal, quota_id)

    if payload.expected_updated_at is not None and quota.updated_at is not None:
        expected = _as_utc(payload.expected_updated_at)
        # One second of slack: clients round-trip the timestamp through JSON.
        if abs((_as_utc(quota.updated_at) - expected).total_seconds()) > 1:
            raise Conflict(f"'{quota.name}' was changed by someone else. Reload and try again.")

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return quota_payload(quota)

    if payload.status is not None and payload.status not in SETTABLE_STATUSES:
        raise ValidationFailed(
            f"'{payload.status.value}' is set by the threshold evaluator, not by hand.",
            details={"settable": sorted(status.value for status in SETTABLE_STATUSES)},
        )
    if "name" in changes and changes["name"] != quota.name:
        clash = (
            await session.execute(
                _scoped_quotas(principal).where(
                    Quota.name == changes["name"], Quota.id != quota.id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A quota named '{changes['name']}' already exists.")

    for field, value in changes.items():
        setattr(quota, field, value.value if hasattr(value, "value") else value)
    if payload.period is not None and payload.resets_at is None:
        # A new cadence needs a new reset instant, or the old one would outlive it.
        quota.resets_at = period_bounds(payload.period, _now())[1]
    quota.updated_by = principal.actor

    # A raised ceiling can clear a breach and a lowered one can create it, so the
    # status is always recomputed rather than left where the caller found it.
    if quota.status not in INERT_STATUSES:
        await _evaluate_quota(session, quota, request=request)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A quota named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="quota.updated",
        entity_type=ENTITY_QUOTA,
        entity_id=quota.id,
        entity_label=quota.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    await session.refresh(quota)
    return quota_payload(quota)


async def delete_quota(
    session: AsyncSession,
    principal: Principal,
    quota_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a ceiling. Enforcement stops immediately; the audit row remains."""
    principal.require(Role.ADMIN)
    quota = await get_quota(session, principal, quota_id)
    await audit.record(
        session,
        principal=principal,
        action="quota.deleted",
        entity_type=ENTITY_QUOTA,
        entity_id=quota.id,
        entity_label=quota.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Removed {quota.resource} ceiling of "
            f"{telemetry.compact(float(quota.limit_value or 0.0))} {quota.unit}"
        ),
        request=request,
    )
    await session.delete(quota)
    await session.flush()


def _increase_risk(current: float, requested: float) -> ApprovalRisk:
    """Bigger asks need a heavier signature."""
    if current <= 0:
        return ApprovalRisk.HIGH
    growth = (requested - current) / current * 100
    if growth <= 25:
        return ApprovalRisk.LOW
    if growth <= 100:
        return ApprovalRisk.MEDIUM
    return ApprovalRisk.HIGH


async def request_increase(
    session: AsyncSession,
    principal: Principal,
    quota_id: str,
    payload: QuotaIncreaseRequest,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Route a bigger ceiling into the approvals queue.

    The quota is not touched: an increase is a decision, and the decision is
    recorded where every other governed decision lives. The approval payload
    carries everything an approver needs to apply it afterwards.
    """
    principal.require(Role.MEMBER)
    quota = await get_quota(session, principal, quota_id)

    current = float(quota.limit_value or 0.0)
    if payload.requested_limit <= current:
        raise ValidationFailed(
            f"'{quota.name}' already allows {telemetry.compact(current)} {quota.unit}; "
            "ask for a larger ceiling than the one in force.",
            details={"current_limit": current, "requested_limit": payload.requested_limit},
        )

    risk = _increase_risk(current, payload.requested_limit)
    delta = payload.requested_limit - current
    financial = (
        telemetry.money(delta, decimals=2)
        if quota.resource == QuotaResource.COST.value
        else None
    )
    created = await approvals.create_request(
        session,
        principal,
        ApprovalRequestCreate(
            action="Quota Increase",
            action_detail=(
                f"{quota.name}: {telemetry.compact(current)} → "
                f"{telemetry.compact(payload.requested_limit)} {quota.unit}"
            ),
            resource=quota.name,
            risk=risk,
            reason=payload.reason,
            source=SOURCE_SCREEN,
            payload={
                "quota_id": quota.id,
                "resource": quota.resource,
                "unit": quota.unit,
                "current_limit": current,
                "requested_limit": payload.requested_limit,
                "utilization_percent": _utilisation(quota.used_value, quota.limit_value),
                "justification_ticket": payload.justification_ticket,
            },
            impact=ApprovalImpact(financial=financial),
        ),
        request=request,
    )

    await audit.record(
        session,
        principal=principal,
        action="quota.increase_requested",
        entity_type=ENTITY_QUOTA,
        entity_id=quota.id,
        entity_label=quota.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Requested {telemetry.compact(payload.requested_limit)} {quota.unit} "
            f"(from {telemetry.compact(current)}) — {created.request_ref}"
        ),
        metadata={
            "request_ref": created.request_ref,
            "requested_limit": payload.requested_limit,
            "risk": risk.value,
        },
        request=request,
    )
    await session.flush()
    return {
        "request_ref": created.request_ref,
        "request_id": created.id,
        "status": created.status.value,
        "risk": risk.value,
        "sla_due_at": created.sla_due_at.isoformat() if created.sla_due_at else None,
        "current_limit": current,
        "requested_limit": payload.requested_limit,
        "unit": quota.unit,
    }


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def _scoped_budgets(principal: Principal) -> Select:
    return select(Budget).where(Budget.workspace_id == principal.workspace_id)


def _projection(budget: Budget, at: dt.datetime) -> float | None:
    """Period-end spend at the pace observed so far.

    Returns ``None`` until enough of the period has elapsed for a pace to mean
    anything: projecting a month from its first hour is arithmetic, not insight.
    """
    start = _as_utc(budget.period_start)
    end = _as_utc(budget.period_end)
    total = (end - start).total_seconds()
    if total <= 0:
        return None
    elapsed = (min(at, end) - start).total_seconds()
    if elapsed <= 0 or elapsed / total < MIN_ELAPSED_FRACTION:
        return None
    return round(float(budget.spent_usd or 0.0) * total / elapsed, 2)


def budget_payload(budget: Budget, *, at: dt.datetime | None = None) -> dict[str, Any]:
    """One budget as the bar renders it, with the pace projection beside it."""
    moment = at or _now()
    amount = float(budget.amount_usd or 0.0)
    spent = float(budget.spent_usd or 0.0)
    utilization = _utilisation(spent, amount)
    health = health_for(utilization)
    days = _days_until(budget.period_end, at=moment)
    projected = _projection(budget, moment)
    return {
        "id": budget.id,
        "name": budget.name,
        "scope": budget.scope,
        "scope_ref": budget.scope_ref,
        "period": budget.period,
        "amount_usd": amount,
        "spent_usd": spent,
        "currency": budget.currency,
        "utilization_percent": utilization,
        "remaining_usd": round(max(0.0, amount - spent), 2),
        "spent_display": telemetry.money(spent),
        "amount_display": telemetry.money(amount),
        "warn_threshold_percent": budget.warn_threshold_percent,
        "hard_threshold_percent": budget.hard_threshold_percent,
        "period_start": budget.period_start,
        "period_end": budget.period_end,
        "resets_in_days": days,
        "resets_label": _resets_label(days),
        "status": budget.status,
        "health": health,
        "health_color": health.color,
        "projected_spend_usd": projected,
        "on_pace_to_breach": bool(projected is not None and amount and projected > amount),
        "owner_user_id": budget.owner_user_id,
        "created_at": budget.created_at,
        "updated_at": budget.updated_at,
    }


async def list_budgets(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    scope: LimitScope | None = None,
    status: LimitStatus | None = None,
    period: LimitPeriod | None = None,
    active_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """One page of budgets. ``active_only`` keeps the bars to the live period."""
    stmt = _scoped_budgets(principal)
    stmt = apply_search(stmt, params, [Budget.name, Budget.scope_ref])
    stmt = apply_filters(
        stmt,
        {
            Budget.scope: scope.value if scope else None,
            Budget.status: status.value if status else None,
            Budget.period: period.value if period else None,
        },
    )
    if active_only:
        at = _now()
        stmt = stmt.where(Budget.period_start <= at, Budget.period_end > at)
    stmt = apply_sort(stmt, params, BUDGET_SORTABLE, default=Budget.name, default_desc=False)
    rows, total = await paginate(session, stmt, params)
    return [budget_payload(row) for row in rows], total


async def current_budgets(session: AsyncSession, principal: Principal) -> list[Budget]:
    """Budgets whose period contains now and which are not switched off."""
    at = _now()
    stmt = (
        _scoped_budgets(principal)
        .where(
            Budget.period_start <= at,
            Budget.period_end > at,
            Budget.status != LimitStatus.DISABLED.value,
        )
        .order_by(Budget.name.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_budget(session: AsyncSession, principal: Principal, budget_id: str) -> Budget:
    budget = (
        await session.execute(_scoped_budgets(principal).where(Budget.id == budget_id))
    ).scalar_one_or_none()
    if budget is None:
        raise NotFound(f"Budget '{budget_id}' does not exist.")
    return budget


async def _evaluate_budget(
    session: AsyncSession, budget: Budget, *, request: Request | None = None
) -> LimitStatus:
    """Move a budget's status to match its spend and alert on an escalation."""
    previous = budget.status
    if previous == LimitStatus.DISABLED.value:
        return LimitStatus.DISABLED

    amount = float(budget.amount_usd or 0.0)
    spent = float(budget.spent_usd or 0.0)
    utilization = _utilisation(spent, amount)
    if _now() >= _as_utc(budget.period_end):
        status = LimitStatus.EXPIRED
    elif utilization >= budget.hard_threshold_percent:
        status = LimitStatus.EXCEEDED
    elif utilization >= budget.warn_threshold_percent:
        status = LimitStatus.WARNING
    else:
        status = LimitStatus.ACTIVE
    budget.status = status.value

    if status.value == previous or status not in (LimitStatus.WARNING, LimitStatus.EXCEEDED):
        return status

    breached = status is LimitStatus.EXCEEDED
    threshold = (
        budget.hard_threshold_percent if breached else budget.warn_threshold_percent
    )
    await alerts.raise_alert(
        session,
        workspace_id=budget.workspace_id,
        title=(
            f"Budget threshold exceeded: {budget.name}"
            if breached
            else f"Budget threshold reached: {budget.name}"
        ),
        description=(
            f"{budget.name} is at {utilization:.1f}% of {telemetry.money(amount)} "
            f"({telemetry.money(spent)} spent), past its {threshold:.0f}% threshold."
        ),
        source=SOURCE_SCREEN,
        severity=AlertSeverity.HIGH if breached else AlertSeverity.MEDIUM,
        dedupe_key=f"budget:{budget.id}:{status.value}",
        source_entity_type=ENTITY_BUDGET,
        source_entity_id=budget.id,
        metadata={
            "utilization_percent": utilization,
            "amount_usd": amount,
            "spent_usd": spent,
            "threshold_percent": threshold,
            "scope": budget.scope,
            "scope_ref": budget.scope_ref,
        },
        request=request,
    )
    return status


async def create_budget(
    session: AsyncSession,
    principal: Principal,
    payload: BudgetCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Open a budget for a period. Spend starts at zero until it is rolled up."""
    principal.require(Role.ADMIN)
    at = _now()
    default_start, default_end = period_bounds(payload.period, at)
    period_start = _as_utc(payload.period_start) if payload.period_start else default_start
    period_end = _as_utc(payload.period_end) if payload.period_end else default_end

    clash = (
        await session.execute(
            _scoped_budgets(principal).where(
                Budget.name == payload.name, Budget.period_start == period_start
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(
            f"A budget named '{payload.name}' already exists for the period starting "
            f"{period_start.date().isoformat()}."
        )

    budget = Budget(
        workspace_id=principal.workspace_id,
        name=payload.name,
        scope=payload.scope.value,
        scope_ref=payload.scope_ref,
        period=payload.period.value,
        amount_usd=payload.amount_usd,
        spent_usd=0.0,
        currency=payload.currency,
        warn_threshold_percent=payload.warn_threshold_percent,
        hard_threshold_percent=payload.hard_threshold_percent,
        period_start=period_start,
        period_end=period_end,
        owner_user_id=payload.owner_user_id,
        status=LimitStatus.ACTIVE.value,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(budget)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A budget named '{payload.name}' already exists for that period.") from exc

    await audit.record(
        session,
        principal=principal,
        action="budget.created",
        entity_type=ENTITY_BUDGET,
        entity_id=budget.id,
        entity_label=budget.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{telemetry.money(payload.amount_usd)} for {payload.period.value.lower()} "
            f"{period_start.date().isoformat()}, warn at "
            f"{payload.warn_threshold_percent:.0f}%"
        ),
        metadata={
            "amount_usd": payload.amount_usd,
            "scope": payload.scope.value,
            "warn_threshold_percent": payload.warn_threshold_percent,
            "hard_threshold_percent": payload.hard_threshold_percent,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(budget)
    return budget_payload(budget)


async def update_budget(
    session: AsyncSession,
    principal: Principal,
    budget_id: str,
    payload: BudgetUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Amend a budget — including the threshold editor — and re-evaluate it.

    Lowering a threshold under the spend already recorded is a breach the moment
    it is saved, so the evaluator runs before the response is built and the
    alert it raises lands in the same transaction as the edit.
    """
    principal.require(Role.ADMIN)
    budget = await get_budget(session, principal, budget_id)

    if payload.expected_updated_at is not None and budget.updated_at is not None:
        expected = _as_utc(payload.expected_updated_at)
        if abs((_as_utc(budget.updated_at) - expected).total_seconds()) > 1:
            raise Conflict(f"'{budget.name}' was changed by someone else. Reload and try again.")

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return budget_payload(budget)
    if payload.status is not None and payload.status not in SETTABLE_STATUSES:
        raise ValidationFailed(
            f"'{payload.status.value}' is set by the threshold evaluator, not by hand.",
            details={"settable": sorted(status.value for status in SETTABLE_STATUSES)},
        )

    warn = payload.warn_threshold_percent
    hard = payload.hard_threshold_percent
    warn_value = budget.warn_threshold_percent if warn is None else warn
    hard_value = budget.hard_threshold_percent if hard is None else hard
    if warn_value >= hard_value:
        raise ValidationFailed(
            "The warning threshold must sit below the hard threshold.",
            details={"warn_threshold_percent": warn_value, "hard_threshold_percent": hard_value},
        )

    for field, value in changes.items():
        setattr(budget, field, value.value if hasattr(value, "value") else value)
    budget.updated_by = principal.actor
    await _evaluate_budget(session, budget, request=request)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A budget named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="budget.updated",
        entity_type=ENTITY_BUDGET,
        entity_id=budget.id,
        entity_label=budget.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes), "status": budget.status},
        request=request,
    )
    await session.flush()
    await session.refresh(budget)
    return budget_payload(budget)


async def delete_budget(
    session: AsyncSession,
    principal: Principal,
    budget_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Close a budget. Alerts it already raised survive it."""
    principal.require(Role.ADMIN)
    budget = await get_budget(session, principal, budget_id)
    await audit.record(
        session,
        principal=principal,
        action="budget.deleted",
        entity_type=ENTITY_BUDGET,
        entity_id=budget.id,
        entity_label=budget.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Removed {telemetry.money(float(budget.amount_usd or 0.0))} budget "
            f"({telemetry.money(float(budget.spent_usd or 0.0))} spent)"
        ),
        request=request,
    )
    await session.delete(budget)
    await session.flush()


def _in_scope(budget: Budget, project: telemetry.AgentProject) -> bool:
    """Whether one agent's spend belongs to one budget."""
    scope = LimitScope(budget.scope)
    if scope is LimitScope.WORKSPACE:
        return True
    if scope is LimitScope.TEAM:
        return project.team == budget.scope_ref
    if scope is LimitScope.AGENT:
        return project.agent_id == budget.scope_ref
    return project.environment == budget.scope_ref


async def refresh_budgets(
    session: AsyncSession,
    principal: Principal,
    *,
    budget_id: str | None = None,
    request: Request | None = None,
) -> list[dict[str, Any]]:
    """Roll measured cost into budgets and evaluate their thresholds.

    Budgets store spend rather than joining it live, so this is the job that
    keeps the bars true — invoked from the screen and from the scheduled sweep.
    Budgets sharing a period are measured with one engine call, and a budget the
    engine reported no cost for keeps the spend it already had: an unanswered
    read is not evidence that spending stopped.
    """
    principal.require(Role.OPERATOR)
    client = get_engine_client()
    budgets = (
        [await get_budget(session, principal, budget_id)]
        if budget_id
        else await current_budgets(session, principal)
    )
    if not budgets:
        return []

    projects = await telemetry.workspace_projects(session, principal)
    at = _now()
    periods: dict[tuple[dt.datetime, dt.datetime], list[Budget]] = {}
    for budget in budgets:
        start = _as_utc(budget.period_start)
        end = min(_as_utc(budget.period_end), at)
        periods.setdefault((start, end), []).append(budget)

    for (start, end), group in periods.items():
        rollups = await telemetry.project_rollups(client, projects, start, end)
        for budget in group:
            matched = [rollup for rollup in rollups if _in_scope(budget, rollup.project)]
            spend = telemetry.sum_optional(rollup.cost_usd for rollup in matched)
            if spend is not None:
                budget.spent_usd = round(spend, 2)
            budget.updated_by = principal.actor
            await _evaluate_budget(session, budget, request=request)

    await audit.record(
        session,
        principal=principal,
        action="budget.refreshed",
        entity_type=ENTITY_BUDGET,
        entity_label=f"{len(budgets)} budget(s)",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Rolled measured cost into {len(budgets)} budget(s); "
            + ", ".join(f"{b.name} {_utilisation(b.spent_usd, b.amount_usd):.1f}%" for b in budgets)
        ),
        metadata={"budget_ids": [budget.id for budget in budgets]},
        request=request,
    )
    await session.flush()
    for budget in budgets:
        # ``updated_at`` is a server-side onupdate: the UPDATE expired it rather
        # than refetching it, so read it back before the caller serialises the row.
        await session.refresh(budget)
    return [budget_payload(budget, at=at) for budget in budgets]


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def _capacity_icon(resource_type: str) -> str:
    lowered = (resource_type or "").lower()
    for marker, icon in CAPACITY_ICONS:
        if marker in lowered:
            return icon
    return DEFAULT_CAPACITY_ICON


async def _capacity_readings(
    session: AsyncSession, principal: Principal, *, since: dt.datetime
) -> dict[str, list[CapacityRecord]]:
    """Every reading in the window, grouped by resource, oldest first."""
    stmt = (
        select(CapacityRecord)
        .where(
            CapacityRecord.workspace_id == principal.workspace_id,
            CapacityRecord.measured_at >= since,
        )
        .order_by(CapacityRecord.measured_at.asc())
        .limit(MAX_CAPACITY_READINGS)
    )
    grouped: dict[str, list[CapacityRecord]] = {}
    for record in (await session.execute(stmt)).scalars():
        grouped.setdefault(record.name, []).append(record)
    return grouped


async def capacity_rows(
    session: AsyncSession, principal: Principal, *, trend_days: int = TREND_DAYS
) -> list[dict[str, Any]]:
    """Latest reading per pool, with the trailing utilisation sparkline.

    A pool that has not reported inside the history window is not listed: the
    table shows measurements, and a stale reading is not one.
    """
    at = _now()
    grouped = await _capacity_readings(
        session, principal, since=at - dt.timedelta(days=CAPACITY_HISTORY_DAYS)
    )
    trend_floor = at - dt.timedelta(days=trend_days)

    rows: list[dict[str, Any]] = []
    for name, readings in grouped.items():
        latest = readings[-1]
        trend = [
            round(float(record.utilization_percent or 0.0), 1)
            for record in readings
            if _as_utc(record.measured_at) >= trend_floor
        ][-MAX_TREND_POINTS:]
        utilization = round(float(latest.utilization_percent or 0.0), 1)
        health = health_for(utilization)
        provisioned = float(latest.provisioned or 0.0)
        used = float(latest.used or 0.0)
        rows.append(
            {
                "name": name,
                "resource_type": latest.resource_type,
                "region": latest.region,
                "icon": _capacity_icon(latest.resource_type),
                "provisioned": provisioned,
                "used": used,
                "headroom": round(max(0.0, provisioned - used), 4),
                "unit": latest.unit,
                "utilization_percent": utilization,
                "headroom_percent": round(max(0.0, 100.0 - utilization), 1),
                "status": latest.status,
                "health": health,
                "health_color": health.color,
                "measured_at": latest.measured_at,
                "reading_count": len(readings),
                "trend": trend,
            }
        )
    rows.sort(key=lambda row: -row["utilization_percent"])
    return rows


def capacity_health(rows: Sequence[dict[str, Any]]) -> CapacityHealth:
    """Composite headroom score across every pool.

    A pool under the watch threshold scores 100; above it the score falls
    linearly to zero as utilisation approaches full. The label is taken from the
    statuses the source system reported, not from the score, so a single
    critical pool is never averaged away into a healthy headline.
    """
    if not rows:
        return CapacityHealth(score_percent=None, label="No readings")

    scores: list[float] = []
    counts = {CapacityStatus.HEALTHY: 0, CapacityStatus.WARNING: 0, CapacityStatus.CRITICAL: 0}
    for row in rows:
        utilization = float(row["utilization_percent"])
        if utilization <= WATCH_PERCENT:
            scores.append(100.0)
        else:
            headroom = max(0.0, 100.0 - utilization)
            scores.append(round(headroom / (100.0 - WATCH_PERCENT) * 100, 1))
        try:
            counts[CapacityStatus(row["status"])] += 1
        except ValueError:
            counts[CapacityStatus.HEALTHY] += 1

    if counts[CapacityStatus.CRITICAL]:
        label = CapacityStatus.CRITICAL.value
    elif counts[CapacityStatus.WARNING]:
        label = CapacityStatus.WARNING.value
    else:
        label = CapacityStatus.HEALTHY.value

    return CapacityHealth(
        score_percent=round(sum(scores) / len(scores), 1),
        label=label,
        resource_count=len(rows),
        healthy=counts[CapacityStatus.HEALTHY],
        warning=counts[CapacityStatus.WARNING],
        critical=counts[CapacityStatus.CRITICAL],
    )


async def capacity_series(
    session: AsyncSession,
    principal: Principal,
    *,
    resource: str,
    window: telemetry.MetricWindow,
    interval: MetricInterval | None = None,
) -> list[MetricPoint]:
    """Utilisation of one pool over time, from the readings we hold.

    Backs the live concurrency chart and any other per-resource trend: readings
    are averaged into the bucket they fall in, and a bucket nobody reported in
    stays empty rather than being carried forward.
    """
    span = telemetry.resolve_window(window)
    grid = interval or telemetry.default_interval(window)
    starts = telemetry.bucket_starts(span.start, span.end, grid)
    stmt = (
        select(CapacityRecord.measured_at, CapacityRecord.utilization_percent)
        .where(
            CapacityRecord.workspace_id == principal.workspace_id,
            CapacityRecord.name == resource,
            CapacityRecord.measured_at >= span.start,
            CapacityRecord.measured_at < span.end,
        )
        .order_by(CapacityRecord.measured_at.asc())
        .limit(MAX_CAPACITY_READINGS)
    )
    readings = [
        (_as_utc(measured_at), float(value or 0.0))
        for measured_at, value in (await session.execute(stmt)).all()
        if measured_at is not None
    ]
    if not readings:
        raise NotFound(f"No capacity readings for '{resource}' in this window.")
    values = telemetry.fold_mean(readings, starts, grid)
    return [
        MetricPoint(timestamp=start, label=telemetry.bucket_label(start, grid), value=value)
        for start, value in zip(starts, values, strict=True)
    ]


# ---------------------------------------------------------------------------
# Cost attribution
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CostContext:
    """Everything the cost panels need, measured once per request."""

    period: QuotaPeriod
    span: telemetry.Range
    projects: list[telemetry.AgentProject]
    current: list[telemetry.ProjectRollup]
    previous: list[telemetry.ProjectRollup]
    spend_usd: float | None
    previous_spend_usd: float | None


async def cost_context(
    session: AsyncSession, principal: Principal, period: QuotaPeriod
) -> CostContext:
    """Measure the period and the one before it, in parallel."""
    client = get_engine_client()
    span = resolve_period(period)
    projects = await telemetry.workspace_projects(session, principal)
    current, previous, spend, previous_spend = await asyncio.gather(
        telemetry.project_rollups(client, projects, span.start, span.end),
        telemetry.project_rollups(client, projects, span.previous_start, span.previous_end),
        telemetry.cost_total(client, projects, span.start, span.end),
        telemetry.cost_total(client, projects, span.previous_start, span.previous_end),
    )
    return CostContext(
        period=period,
        span=span,
        projects=projects,
        current=current,
        previous=previous,
        spend_usd=spend,
        previous_spend_usd=previous_spend,
    )


def _unit_cost(cost: float | None, tokens: float | None) -> float | None:
    """Cost per 1,000 tokens, or ``None`` when either side is unmeasured."""
    if cost is None or not tokens:
        return None
    return round(cost / tokens * 1000, 4)


def classify_service(model: str | None) -> tuple[str, str]:
    """Group a billed model into a service family for the Cost by Service donut.

    This regroups measured cost; it never redistributes it. A model that matches
    no rule is inference, and cost the engine reported without a model name
    lands in Other Services rather than being spread across the named ones.
    """
    if not model or not model.strip():
        return UNCLASSIFIED_SERVICE
    lowered = model.lower()
    for key, label, markers in SERVICE_RULES:
        if not markers:
            return key, label
        if any(marker in lowered for marker in markers):
            return key, label
    return UNCLASSIFIED_SERVICE


def model_cost_rows(context: CostContext) -> list[dict[str, Any]]:
    """Per-model cost, tokens, calls and unit-cost movement.

    Attribution is by the model an agent is configured with: the spend of every
    agent running a model is that model's spend. Unit cost is cost divided by
    the tokens measured over the same window, so a movement in it is a real
    change in what a thousand tokens cost, not a change in volume.
    """
    total_cost = telemetry.aggregate(context.current).cost_usd
    current = telemetry.group_rollups(
        context.current, lambda project: project.model, fallback="Unspecified"
    )
    previous = telemetry.group_rollups(
        context.previous, lambda project: project.model, fallback="Unspecified"
    )

    rows: list[dict[str, Any]] = []
    for model, group in current.items():
        head = telemetry.aggregate(group)
        tail = telemetry.aggregate(previous.get(model, []))
        unit = _unit_cost(head.cost_usd, head.tokens)
        previous_unit = _unit_cost(tail.cost_usd, tail.tokens)
        rows.append(
            {
                "model": model,
                "agent_count": len(group),
                "cost_usd": head.cost_usd,
                "cost_display": telemetry.money(head.cost_usd, decimals=2),
                "previous_cost_usd": tail.cost_usd,
                "cost_delta_percent": telemetry.relative_delta(head.cost_usd, tail.cost_usd),
                "tokens": head.tokens,
                "tokens_display": telemetry.compact(head.tokens),
                "api_calls": head.runs,
                "cost_per_1k_tokens": unit,
                "previous_cost_per_1k_tokens": previous_unit,
                "unit_cost_delta_percent": telemetry.relative_delta(unit, previous_unit),
                "share_percent": telemetry.share(head.cost_usd, total_cost),
                "color": telemetry.PALETTE[0],
            }
        )
    rows.sort(key=lambda row: (row["cost_usd"] is None, -(row["cost_usd"] or 0.0)))
    for position, row in enumerate(rows):
        row["color"] = telemetry.PALETTE[position % len(telemetry.PALETTE)]
    return rows


def service_cost_rows(context: CostContext) -> list[dict[str, Any]]:
    """Cost by service family, folded up from the per-model rows."""
    models = model_cost_rows(context)
    total = telemetry.sum_optional(row["cost_usd"] for row in models)

    folded: dict[str, dict[str, Any]] = {}
    for row in models:
        key, label = classify_service(row["model"])
        bucket = folded.setdefault(
            key,
            {"key": key, "label": label, "cost_usd": None, "model_count": 0},
        )
        bucket["model_count"] += 1
        if row["cost_usd"] is not None:
            bucket["cost_usd"] = (bucket["cost_usd"] or 0.0) + row["cost_usd"]

    rows = list(folded.values())
    for row in rows:
        row["cost_display"] = telemetry.money(row["cost_usd"], decimals=2)
        row["share_percent"] = telemetry.share(row["cost_usd"], total)
    rows.sort(key=lambda row: (row["cost_usd"] is None, -(row["cost_usd"] or 0.0)))
    for position, row in enumerate(rows):
        row["color"] = telemetry.PALETTE[position % len(telemetry.PALETTE)]
    return rows


def driver_rows(context: CostContext, *, limit: int = TOP_DRIVER_LIMIT) -> list[dict[str, Any]]:
    """The heaviest cost contributors, ranked.

    Same measurements as the per-model breakdown, cut to the short ranked list
    the Top Cost Drivers panel shows.
    """
    models = model_cost_rows(context)
    total = telemetry.sum_optional(row["cost_usd"] for row in models)
    rows: list[dict[str, Any]] = []
    for position, row in enumerate(models[:limit]):
        percent = telemetry.share(row["cost_usd"], total)
        rows.append(
            {
                "rank": position + 1,
                "key": row["model"],
                "label": row["model"],
                "cost_usd": row["cost_usd"],
                "cost_display": telemetry.money(row["cost_usd"], decimals=2),
                "share_percent": percent,
                "share_display": telemetry.percent(percent),
                "color": telemetry.PALETTE[position % len(telemetry.PALETTE)],
            }
        )
    return rows


def team_rows(context: CostContext) -> list[dict[str, Any]]:
    """Cost and usage by team, attributed through the agent registry."""
    total_cost = telemetry.aggregate(context.current).cost_usd
    grouped = telemetry.group_rollups(
        context.current, lambda project: project.team, fallback="Unassigned"
    )
    rows: list[dict[str, Any]] = []
    for team, group in grouped.items():
        head = telemetry.aggregate(group)
        percent = telemetry.share(head.cost_usd, total_cost)
        rows.append(
            {
                "team": team,
                "agent_count": len(group),
                "spend_usd": head.cost_usd,
                "spend_display": telemetry.money(head.cost_usd),
                "share_percent": percent,
                "share_display": telemetry.percent(percent),
                "tokens": head.tokens,
                "tokens_display": telemetry.compact(head.tokens),
                "api_calls": head.runs,
                "api_calls_display": telemetry.compact(float(head.runs)),
                "avg_cost_per_1k_tokens": _unit_cost(head.cost_usd, head.tokens),
                "trend": [],
            }
        )
    rows.sort(key=lambda row: (row["spend_usd"] is None, -(row["spend_usd"] or 0.0)))
    return rows


async def attach_team_trends(
    context: CostContext,
    rows: Sequence[dict[str, Any]],
    *,
    days: int = TREND_DAYS,
) -> None:
    """Fill in each team's daily spend sparkline, one engine call per team.

    Called on the page the table is about to render rather than on every team in
    the workspace, so the number of calls is bounded by the page size.
    """
    if not rows:
        return
    client = get_engine_client()
    end = _now()
    start = end - dt.timedelta(days=days)
    starts = telemetry.bucket_starts(start, end, MetricInterval.DAILY)

    by_team: dict[str, list[telemetry.AgentProject]] = {}
    for project in context.projects:
        by_team.setdefault(project.team or "Unassigned", []).append(project)

    wanted = [row for row in rows if by_team.get(row["team"])]
    if not wanted:
        return
    series = await asyncio.gather(
        *(
            telemetry.cost_points(client, by_team[row["team"]], start, end)
            for row in wanted
        )
    )
    for row, points in zip(wanted, series, strict=True):
        row["trend"] = telemetry.fold_sum(points, starts, MetricInterval.DAILY)


# ---------------------------------------------------------------------------
# Usage series and forecast
# ---------------------------------------------------------------------------


SERIES_FOR_METRIC: Final[dict[QuotaUsageMetric, SeriesMetric]] = {
    # Spend is billed cost; an API call is one recorded agent run, which is the
    # unit the engine counts and the unit a quota on requests is written in.
    QuotaUsageMetric.SPEND: SeriesMetric.COST,
    QuotaUsageMetric.TOKENS: SeriesMetric.TOKENS,
    QuotaUsageMetric.API_CALLS: SeriesMetric.RUNS,
}


async def usage_series(
    session: AsyncSession,
    principal: Principal,
    *,
    window: MetricWindow,
    interval: MetricInterval | None,
    metrics: Sequence[QuotaUsageMetric],
) -> MetricsSeriesResponse:
    """Spend, token and call trends on one shared bucket grid.

    The lines are the same measurements the Metrics screen charts, requested in
    this screen's vocabulary; sharing the grid is what lets the console overlay
    spend and volume on one axis.
    """
    return await telemetry.series(
        session,
        principal,
        window=window,
        interval=interval,
        metrics=[SERIES_FOR_METRIC[metric] for metric in metrics],
    )


async def spend_points(
    session: AsyncSession,
    principal: Principal,
    span: telemetry.Range,
    *,
    projects: Sequence[telemetry.AgentProject] | None = None,
) -> tuple[list[dt.datetime], list[float | None]]:
    """Daily spend over a window, on a daily grid.

    Pass ``projects`` when the caller already resolved them, so the spend chart
    and the KPI row do not each re-read the agent registry.
    """
    client = get_engine_client()
    resolved = (
        list(projects)
        if projects is not None
        else await telemetry.workspace_projects(session, principal)
    )
    starts = telemetry.bucket_starts(span.start, span.end, MetricInterval.DAILY)
    points = await telemetry.cost_points(client, resolved, span.start, span.end)
    return starts, telemetry.fold_sum(points, starts, MetricInterval.DAILY)


def _least_squares(values: Sequence[float]) -> tuple[float, float]:
    """Slope and intercept of the best-fit line through ``values``."""
    count = len(values)
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    variance = sum((index - mean_x) ** 2 for index in range(count))
    if variance == 0:
        return 0.0, mean_y
    covariance = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    slope = covariance / variance
    return slope, mean_y - slope * mean_x


async def forecast(
    session: AsyncSession,
    principal: Principal,
    *,
    period: QuotaPeriod = QuotaPeriod.MTD,
    context: CostContext | None = None,
) -> QuotaForecast:
    """Project period-end spend from the daily spend already measured.

    A straight least-squares fit over the observed days, extended to the end of
    the period. The interval is the 95% band implied by the residual spread, and
    anomalies are the days whose spend sits more than three residual standard
    deviations off the fitted line. With fewer than four measured days nothing
    is projected: a line through two points is not a forecast.

    ``context`` lets a caller that has already measured the period — the KPI
    summary does — hand its measurements over instead of paying for them twice.
    """
    measured = context if context is not None else await cost_context(session, principal, period)
    span = measured.span
    at = _now()
    period_end = (
        period_bounds(LimitPeriod.MONTHLY, at)[1] if period is QuotaPeriod.MTD else span.end
    )
    starts, values = await spend_points(
        session, principal, span, projects=measured.projects
    )
    observed = [(index, value) for index, value in enumerate(values) if value is not None]

    labels = [telemetry.bucket_label(start, MetricInterval.DAILY) for start in starts]
    points = [
        ForecastPoint(timestamp=start, label=label, actual_usd=value)
        for start, label, value in zip(starts, labels, values, strict=True)
    ]
    days_elapsed = len(starts)
    days_remaining = max(0, math.ceil((period_end - at).total_seconds() / DAY.total_seconds()))
    budgets = await current_budgets(session, principal)
    budget_total = (
        round(sum(float(budget.amount_usd or 0.0) for budget in budgets), 2) if budgets else None
    )
    observed_spend = round(sum(value for _, value in observed), 2) if observed else None

    if len(observed) < MIN_FORECAST_POINTS:
        return QuotaForecast(
            period_start=span.start,
            period_end=period_end,
            days_elapsed=days_elapsed,
            days_remaining=days_remaining,
            observed_spend_usd=observed_spend,
            budget_usd=budget_total,
            points=points,
        )

    series = [value for _, value in observed]
    slope, intercept = _least_squares(series)
    fitted = [slope * index + intercept for index in range(len(series))]
    residuals = [actual - predicted for actual, predicted in zip(series, fitted, strict=True)]
    sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0

    projected_daily: list[float] = []
    for offset in range(1, days_remaining + 1):
        projected_daily.append(max(0.0, slope * (len(series) - 1 + offset) + intercept))
    projected_total = round((observed_spend or 0.0) + sum(projected_daily), 2)

    for offset, value in enumerate(projected_daily, start=1):
        moment = starts[-1] + dt.timedelta(days=offset)
        points.append(
            ForecastPoint(
                timestamp=moment,
                label=telemetry.bucket_label(moment, MetricInterval.DAILY),
                forecast_usd=round(value, 2),
            )
        )

    anomalies = [
        labels[index]
        for (index, _), residual in zip(observed, residuals, strict=True)
        if sigma > 0 and abs(residual) > ANOMALY_SIGMA * sigma
    ]

    driver, driver_delta = _growth_driver(measured)

    return QuotaForecast(
        period_start=span.start,
        period_end=period_end,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        observed_spend_usd=observed_spend,
        projected_spend_usd=projected_total,
        budget_usd=budget_total,
        projected_utilization_percent=telemetry.share(projected_total, budget_total),
        confidence_interval_usd=(
            round(CONFIDENCE_Z * sigma * math.sqrt(days_remaining), 2) if days_remaining else 0.0
        ),
        daily_growth_usd=round(slope, 2),
        highest_growth_driver=driver,
        highest_growth_delta_usd=driver_delta,
        anomalies_detected=len(anomalies),
        anomaly_days=anomalies,
        points=points,
    )


def _growth_driver(context: CostContext) -> tuple[str | None, float | None]:
    """The model whose spend grew most against the comparison period."""
    best: tuple[str | None, float | None] = (None, None)
    for row in model_cost_rows(context):
        current = row["cost_usd"]
        previous = row["previous_cost_usd"]
        if current is None or previous is None:
            continue
        growth = current - previous
        if growth > 0 and (best[1] is None or growth > best[1]):
            best = (row["model"], round(growth, 2))
    return best


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def _insight(
    *,
    key: str,
    severity: InsightSeverity,
    icon: str,
    color: str,
    title: str,
    body: str,
    savings: float | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    recommendations: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "icon": icon,
        "color": color,
        "title": title,
        "body": body,
        "potential_savings_usd": savings,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "recommendations": list(recommendations),
    }


_SEVERITY_RANK: Final[dict[InsightSeverity, int]] = {
    InsightSeverity.CRITICAL: 0,
    InsightSeverity.WARNING: 1,
    InsightSeverity.INFO: 2,
}


async def insights(
    session: AsyncSession, principal: Principal, *, period: QuotaPeriod = QuotaPeriod.MTD
) -> list[dict[str, Any]]:
    """Observations computed from what was measured, in this request.

    Nothing here is stored or authored: each observation is a rule over budgets,
    quotas, capacity readings and per-model unit cost, and its body quotes the
    numbers that triggered it so an operator can check the reasoning.
    """
    at = _now()
    context = await cost_context(session, principal, period)
    budgets = await current_budgets(session, principal)
    quotas = await all_quota_rows(session, principal)
    capacity = await capacity_rows(session, principal)

    found: list[dict[str, Any]] = []

    for budget in budgets:
        projected = _projection(budget, at)
        amount = float(budget.amount_usd or 0.0)
        if projected is None or not amount:
            continue
        utilization = _utilisation(budget.spent_usd, amount)
        if projected > amount:
            overshoot = round(projected - amount, 2)
            found.append(
                _insight(
                    key=f"budget-pace:{budget.id}",
                    severity=(
                        InsightSeverity.CRITICAL
                        if utilization >= budget.hard_threshold_percent
                        else InsightSeverity.WARNING
                    ),
                    icon="alert",
                    color="amber",
                    title=f"{budget.name} is on pace to breach",
                    body=(
                        f"{telemetry.money(float(budget.spent_usd or 0.0))} of "
                        f"{telemetry.money(amount)} spent ({utilization:.1f}%); at this pace the "
                        f"period ends at {telemetry.money(projected)}, "
                        f"{telemetry.money(overshoot)} over."
                    ),
                    savings=overshoot,
                    entity_type=ENTITY_BUDGET,
                    entity_id=budget.id,
                    recommendations=[
                        "Move non-urgent batch work into the next period.",
                        "Route the highest-volume workloads to a cheaper model tier.",
                        f"Raise {budget.name} before the {budget.hard_threshold_percent:.0f}%"
                        " threshold blocks reporting.",
                    ],
                )
            )
        elif utilization < IDLE_UTILISATION_PERCENT:
            found.append(
                _insight(
                    key=f"budget-idle:{budget.id}",
                    severity=InsightSeverity.INFO,
                    icon="dollar",
                    color="green",
                    title=f"{budget.name} is under-consumed",
                    body=(
                        f"Only {utilization:.1f}% of {telemetry.money(amount)} is committed with "
                        f"{_days_until(budget.period_end, at=at)} day(s) left in the period."
                    ),
                    entity_type=ENTITY_BUDGET,
                    entity_id=budget.id,
                    recommendations=[
                        "Reallocate the headroom to a team that is close to its ceiling.",
                        "Right-size the budget at the next period boundary.",
                    ],
                )
            )

    for quota in quotas:
        utilization = quota["utilization_percent"]
        if utilization >= WATCH_PERCENT:
            found.append(
                _insight(
                    key=f"quota-pressure:{quota['id']}",
                    severity=(
                        InsightSeverity.CRITICAL
                        if utilization >= CRITICAL_PERCENT
                        else InsightSeverity.WARNING
                    ),
                    icon="gauge",
                    color="amber",
                    title=f"{quota['name']} is at {utilization:.0f}% of its limit",
                    body=(
                        f"{quota['used_display']} of {quota['limit_display']} {quota['unit']} "
                        f"used; {quota['resets_label'] or 'no reset scheduled'}."
                    ),
                    entity_type=ENTITY_QUOTA,
                    entity_id=quota["id"],
                    recommendations=[
                        "Request an increase so the ceiling is raised before it bites.",
                        "Shift lower-priority traffic to a scope with headroom.",
                    ],
                )
            )
        elif utilization and utilization < IDLE_UTILISATION_PERCENT:
            found.append(
                _insight(
                    key=f"quota-idle:{quota['id']}",
                    severity=InsightSeverity.INFO,
                    icon="layers",
                    color="purple",
                    title=f"{quota['name']} is over-provisioned",
                    body=(
                        f"Only {utilization:.1f}% of {quota['limit_display']} {quota['unit']} "
                        "has been consumed this period."
                    ),
                    entity_type=ENTITY_QUOTA,
                    entity_id=quota["id"],
                    recommendations=[
                        "Lower the ceiling to make an accidental runaway visible sooner.",
                    ],
                )
            )

    for row in model_cost_rows(context):
        movement = row["unit_cost_delta_percent"]
        if movement is None or abs(movement) < UNIT_COST_MOVE_PERCENT:
            continue
        rose = movement > 0
        exposure = None
        if row["tokens"] and row["cost_per_1k_tokens"] and row["previous_cost_per_1k_tokens"]:
            exposure = round(
                (row["cost_per_1k_tokens"] - row["previous_cost_per_1k_tokens"])
                * row["tokens"]
                / 1000,
                2,
            )
        found.append(
            _insight(
                key=f"unit-cost:{row['model']}",
                severity=InsightSeverity.WARNING if rose else InsightSeverity.INFO,
                icon="trendUp" if rose else "dollar",
                color="amber" if rose else "green",
                title=(
                    f"{row['model']} unit cost {'rose' if rose else 'fell'} "
                    f"{abs(movement):.1f}%"
                ),
                body=(
                    f"Cost per 1K tokens moved from "
                    f"{telemetry.money(row['previous_cost_per_1k_tokens'], decimals=4)} to "
                    f"{telemetry.money(row['cost_per_1k_tokens'], decimals=4)} on "
                    f"{telemetry.compact(row['tokens'])} tokens."
                ),
                savings=abs(exposure) if exposure else None,
                entity_type="model",
                entity_id=row["model"],
                recommendations=(
                    [
                        "Check whether traffic shifted to a more expensive deployment.",
                        "Re-test the workload against a smaller model at the same quality gate.",
                    ]
                    if rose
                    else ["Extend the change that produced the saving to comparable workloads."]
                ),
            )
        )

    for row in capacity:
        if row["utilization_percent"] < CRITICAL_PERCENT:
            continue
        found.append(
            _insight(
                key=f"capacity:{row['name']}",
                severity=InsightSeverity.CRITICAL,
                icon="activity",
                color="amber",
                title=f"{row['name']} has {row['headroom_percent']:.0f}% headroom left",
                body=(
                    f"{row['used']:,.1f} of {row['provisioned']:,.1f} {row['unit']} in use "
                    f"as of {row['measured_at']:%Y-%m-%d %H:%M}Z."
                ),
                entity_type="capacity",
                entity_id=row["name"],
                recommendations=[
                    "Provision additional capacity before the next peak.",
                    "Move batch workloads to an off-peak window.",
                ],
            )
        )

    # Loudest first, then by the money at stake: the panel is read top-down and
    # the first line should be the one worth acting on.
    found.sort(
        key=lambda item: (
            _SEVERITY_RANK[item["severity"]],
            -(item["potential_savings_usd"] or 0.0),
            item["title"],
        )
    )
    for position, item in enumerate(found):
        item["rank"] = position + 1
    return found


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


EVENT_ENTITY_TYPES: Final[tuple[str, ...]] = (ENTITY_QUOTA, ENTITY_BUDGET, "capacity")
MAX_EVENT_ROWS: Final[int] = 200


async def events(
    session: AsyncSession, principal: Principal, *, days: int = 30
) -> list[dict[str, Any]]:
    """The Quota Events feed: alerts this screen raised and changes it recorded.

    Two sources, one timeline. Alerts carry severity and their current state;
    audit rows carry the actor who made the change. Both are already
    workspace-scoped and neither can be edited from here.
    """
    since = _now() - dt.timedelta(days=days)
    alert_rows = (
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.workspace_id == principal.workspace_id,
                    Alert.source == SOURCE_SCREEN,
                    Alert.raised_at >= since,
                )
                .order_by(Alert.raised_at.desc())
                .limit(MAX_EVENT_ROWS)
            )
        )
        .scalars()
        .all()
    )
    change_rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.workspace_id == principal.workspace_id,
                    AuditEvent.entity_type.in_(EVENT_ENTITY_TYPES),
                    AuditEvent.occurred_at >= since,
                )
                .order_by(AuditEvent.occurred_at.desc())
                .limit(MAX_EVENT_ROWS)
            )
        )
        .scalars()
        .all()
    )

    merged: list[dict[str, Any]] = [
        {
            "id": alert.id,
            "occurred_at": alert.raised_at,
            "kind": "alert",
            "title": alert.title,
            "detail": alert.description,
            "actor": None,
            "severity": alert.severity,
            "status": alert.status,
            "entity_type": alert.source_entity_type,
            "entity_id": alert.source_entity_id,
        }
        for alert in alert_rows
    ]
    merged.extend(
        {
            "id": event.id,
            "occurred_at": event.occurred_at,
            "kind": "change",
            "title": event.entity_label or event.action,
            "detail": event.detail,
            "actor": event.actor,
            "severity": None,
            "status": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
        }
        for event in change_rows
    )
    merged.sort(key=lambda row: _as_utc(row["occurred_at"]), reverse=True)
    return merged


# ---------------------------------------------------------------------------
# KPI summary
# ---------------------------------------------------------------------------


async def summarise(
    session: AsyncSession, principal: Principal, *, period: QuotaPeriod = QuotaPeriod.MTD
) -> QuotaSummary:
    """The six KPI cards, each against the equivalent span of the prior period.

    Spend, tokens and calls are measured; budget is the sum of the ceilings we
    hold for the live period; unit cost is derived from the first two; capacity
    health is scored from the latest reading of every pool.
    """
    context = await cost_context(session, principal, period)
    head = telemetry.aggregate(context.current)
    tail = telemetry.aggregate(context.previous)

    budgets = await current_budgets(session, principal)
    budget_total = round(sum(float(budget.amount_usd or 0.0) for budget in budgets), 2)
    budget_spent = round(sum(float(budget.spent_usd or 0.0) for budget in budgets), 2)
    budget_utilization = _utilisation(budget_spent, budget_total) if budget_total else None

    capacity = capacity_health(await capacity_rows(session, principal))
    projection = await forecast(session, principal, period=period, context=context)

    unit_cost = _unit_cost(context.spend_usd, head.tokens)
    previous_unit_cost = _unit_cost(context.previous_spend_usd, tail.tokens)
    caption = "vs last month" if period is QuotaPeriod.MTD else f"vs prior {period.value}"
    suffix = "(MTD)" if period is QuotaPeriod.MTD else f"({period.value})"

    spend_delta = telemetry.relative_delta(context.spend_usd, context.previous_spend_usd)
    token_delta = telemetry.relative_delta(head.tokens, tail.tokens)
    call_delta = telemetry.relative_delta(float(head.runs), float(tail.runs))
    unit_delta = telemetry.relative_delta(unit_cost, previous_unit_cost)

    return QuotaSummary(
        period=period,
        period_start=context.span.start,
        period_end=context.span.end,
        previous_period_start=context.span.previous_start,
        previous_period_end=context.span.previous_end,
        spend=telemetry.build_kpi(
            key="spend",
            label=f"Total Spend {suffix}",
            value=context.spend_usd,
            previous=context.previous_spend_usd,
            unit="usd",
            icon="dollar",
            color="green",
            comparison=caption,
            higher_is_better=False,
            display=telemetry.money(context.spend_usd),
            delta=spend_delta,
            delta_display=None if spend_delta is None else f"{abs(spend_delta):.1f}%",
        ),
        budget=telemetry.build_kpi(
            key="budget",
            label=f"Total Budget {suffix}",
            value=budget_total or None,
            previous=None,
            unit="usd",
            icon="creditCard",
            color="blue",
            comparison=caption,
            higher_is_better=True,
            display=telemetry.money(budget_total or None),
            delta=None,
            delta_display=None,
            sub=(
                f"{budget_utilization:.1f}% utilized"
                if budget_utilization is not None
                else "No budget set"
            ),
        ),
        tokens=telemetry.build_kpi(
            key="tokens",
            label=f"Total Tokens {suffix}",
            value=head.tokens,
            previous=tail.tokens,
            unit="tokens",
            icon="layers",
            color="purple",
            comparison=caption,
            higher_is_better=False,
            display=telemetry.compact(head.tokens),
            delta=token_delta,
            delta_display=None if token_delta is None else f"{abs(token_delta):.1f}%",
        ),
        api_calls=telemetry.build_kpi(
            key="api_calls",
            label=f"API Calls {suffix}",
            value=float(head.runs),
            previous=float(tail.runs),
            unit="calls",
            icon="trendUp",
            color="cyan",
            comparison=caption,
            higher_is_better=False,
            display=telemetry.compact(float(head.runs)),
            delta=call_delta,
            delta_display=None if call_delta is None else f"{abs(call_delta):.1f}%",
        ),
        cost_per_1k_tokens=telemetry.build_kpi(
            key="cost_per_1k_tokens",
            label="Avg. Cost / 1K Tokens",
            value=unit_cost,
            previous=previous_unit_cost,
            unit="usd",
            icon="gauge",
            color="orange",
            comparison=caption,
            higher_is_better=False,
            display=telemetry.money(unit_cost, decimals=3),
            delta=unit_delta,
            delta_display=None if unit_delta is None else f"{abs(unit_delta):.1f}%",
        ),
        capacity=telemetry.build_kpi(
            key="capacity_health",
            label="Capacity Health",
            value=capacity.score_percent,
            previous=None,
            unit="percent",
            icon="activity",
            color="amber",
            comparison=caption,
            higher_is_better=True,
            display=telemetry.percent(capacity.score_percent, decimals=0),
            delta=None,
            delta_display=None,
            sub=capacity.label,
        ),
        budget_utilization_percent=budget_utilization,
        capacity_health=capacity,
        forecast_spend_usd=projection.projected_spend_usd,
    )


def kpi_cards(summary: QuotaSummary) -> list[MetricKpi]:
    """The six cards in display order, for callers that iterate them."""
    return [
        summary.spend,
        summary.budget,
        summary.tokens,
        summary.api_calls,
        summary.cost_per_1k_tokens,
        summary.capacity,
    ]
