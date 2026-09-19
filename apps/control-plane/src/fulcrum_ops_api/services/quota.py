"""Quota, cost and capacity.

This module owns three things the rest of the platform depends on:

* **Metering and enforcement.** A quota fills up in one of two places, by what
  it limits. *Requests* and *Tokens* are counted by ``services.ingest`` as
  telemetry is accepted: it refuses a batch a ``Block`` quota cannot take,
  increments ``used_value`` in SQL so concurrent batches cannot lose a count,
  and then hands the row to :func:`evaluate_quota` here, which owns the status,
  the alert and its wording. *Cost* is not known when a batch arrives -- the
  engine prices a run after the fact -- so it is measured on the platform clock:
  :func:`meter_cost_quotas`, run with the budget sweep, sets ``used_value`` from
  the spend the engine reports for the quota's scope and period.
  ``check_quota``, ``enforce_quota`` and ``record_usage`` are the same rules as
  a library; nothing on the request path calls them, and ``record_usage`` is a
  read-modify-write that must not be put on a concurrent one.
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
import calendar
import dataclasses
import datetime as dt
import logging
import math
import os
import shutil
import statistics
import time
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import (
    Conflict,
    NotFound,
    QuotaExceeded,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.base import stamp
from ..db.session import get_sessionmaker
from ..engine import EngineError, get_engine_client
from ..engine import deadline as engine_deadline
from ..models.governance import AuditEvent
from ..models.identity import Role, Workspace
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
    CapacityReport,
    ForecastPoint,
    InsightSeverity,
    LimitHealth,
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

log = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "Quota, Cost & Capacity"
ENTITY_QUOTA: Final[str] = "quota"
ENTITY_BUDGET: Final[str] = "budget"

#: The approvals queue knows an increase by this action. ``request_increase``
#: files under it and ``apply_approved_increase`` answers to it.
INCREASE_ACTION: Final[str] = "Quota Increase"
INCREASE_REQUESTED: Final[str] = "quota.increase_requested"
#: How many of a quota's past increase requests are searched for the one being
#: decided. They are read newest first, and a quota does not collect fifty open
#: requests.
MAX_INCREASE_LOOKBACK: Final[int] = 50

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


async def evaluate_quota(
    session: AsyncSession, quota: Quota, *, request: Request | None = None
) -> LimitStatus:
    """Move a quota's status to match its usage and alert on an escalation.

    An alert is raised only when the state actually changes, and it is
    deduplicated on the quota and the state it entered, so a ceiling that stays
    breached produces one alert with a rising occurrence count.

    Public because the ingest path calls it: that is where Requests and Tokens
    quotas fill up, and the status and the alert are this module's to decide.
    """
    previous = quota.status
    status = _status_for(_utilisation(quota.used_value, quota.limit_value), previous)
    quota.status = status.value
    if status.value != previous:
        await _alert_quota(session, quota, status, request=request)
    return status


#: The name this had while it was private. ``services.ingest`` still looks it up
#: by that spelling (``getattr``, so a rename would not fail -- it would silently
#: stop raising threshold alerts from ingest). Kept until ingest asks for
#: :func:`evaluate_quota` by name.
_evaluate_quota = evaluate_quota


async def _alert_quota(
    session: AsyncSession,
    quota: Quota,
    status: LimitStatus,
    *,
    request: Request | None = None,
) -> None:
    """Raise the alert for a quota that has just *entered* ``status``.

    Only Warning and Exceeded are news; dropping back to Active moves the chip
    and nothing else. Split from the evaluator so the clock's meter, which
    writes the status as bookkeeping rather than as an edit, raises exactly the
    alert an edit or an ingest batch would.
    """
    if status not in (LimitStatus.WARNING, LimitStatus.EXCEEDED):
        return
    utilization = _utilisation(quota.used_value, quota.limit_value)
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
        await evaluate_quota(session, quota, request=request)
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
        await evaluate_quota(session, quota, request=request)

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

    The quota is not touched here: an increase is a decision, and the decision
    is recorded where every other governed decision lives. When it is approved,
    ``apply_approved_increase`` raises the ceiling in the same transaction --
    to the number filed here, which is why it is written to the audit row below
    as well as to the payload an approver reads.
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
            action=INCREASE_ACTION,
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
        action=INCREASE_REQUESTED,
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


async def apply_approved_increase(
    *,
    session: AsyncSession,
    principal: Principal,
    approval_request: Any,
    request: Request | None = None,
) -> str | None:
    """Carry an approved Quota Increase through to the quota it asked about.

    Approving one used to change the request and nothing else: the queue said
    Approved, the quota kept its old ceiling, and a ``Block`` quota went on
    refusing traffic until an admin found it and typed the new number in by
    hand. ``services.approvals`` calls this in the transaction that records the
    approval, so the decision and its effect land together or not at all.

    Returns the sentence to add to the decision's message, or ``None`` when the
    request is not a quota increase. It never raises over the state of the
    quota: a quota deleted while its request waited, or already raised past the
    ask, leaves the approval standing and says so.

    **What is applied is what was filed, not what the payload says now.** A
    request's payload can be edited by any member while it is open, and anybody
    can raise a request by hand with this action and a payload of their
    choosing. The risk rung, the SLA and the line the approver read were all
    derived from the number ``request_increase`` was given, and that number is
    in the audit trail, which no API can edit, under the request's reference. A
    request with no such row was not filed from this screen, and one whose
    payload has drifted from it is not the request that was reviewed; neither
    moves a ceiling.
    """
    row = approval_request
    if row.action != INCREASE_ACTION:
        return None
    payload = row.payload if isinstance(row.payload, dict) else {}
    quota_id = payload.get("quota_id")
    asked = payload.get("requested_limit")
    if (
        not isinstance(quota_id, str)
        or isinstance(asked, bool)
        or not isinstance(asked, (int, float))
    ):
        return "It names no quota and limit, so no ceiling was changed."

    filed_rows = (
        (
            await session.execute(
                select(AuditEvent.event_metadata)
                .where(
                    AuditEvent.workspace_id == row.workspace_id,
                    AuditEvent.action == INCREASE_REQUESTED,
                    AuditEvent.entity_type == ENTITY_QUOTA,
                    AuditEvent.entity_id == quota_id,
                )
                .order_by(AuditEvent.occurred_at.desc())
                .limit(MAX_INCREASE_LOOKBACK)
            )
        )
        .scalars()
        .all()
    )
    filed = next(
        (
            metadata
            for metadata in filed_rows
            if isinstance(metadata, dict) and metadata.get("request_ref") == row.request_ref
        ),
        None,
    )
    if filed is None:
        return f"It was not filed from {SOURCE_SCREEN}, so no ceiling was changed."
    requested = float(asked)
    if float(filed.get("requested_limit") or 0.0) != requested:
        return (
            "Its payload no longer matches the increase that was filed, so no ceiling "
            "was changed. Ask for the increase again."
        )

    quota = (
        await session.execute(
            select(Quota)
            .where(Quota.id == quota_id, Quota.workspace_id == row.workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if quota is None:
        return "The quota no longer exists, so there was nothing to raise."

    current = float(quota.limit_value or 0.0)
    if requested <= current:
        return (
            f"'{quota.name}' already allows {telemetry.compact(current)} {quota.unit}; "
            "nothing was changed."
        )

    quota.limit_value = requested
    quota.updated_by = principal.actor
    # A raised ceiling can clear a Warning or a breach; a switched-off quota
    # stays switched off at its new limit.
    if quota.status not in INERT_STATUSES:
        await evaluate_quota(session, quota, request=request)
    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="quota.increase_applied",
        entity_type=ENTITY_QUOTA,
        entity_id=quota.id,
        entity_label=quota.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Raised from {telemetry.compact(current)} to {telemetry.compact(requested)} "
            f"{quota.unit} under {row.request_ref}"
        ),
        metadata={
            "request_ref": row.request_ref,
            "from": current,
            "to": requested,
            "status": quota.status,
        },
        request=request,
    )
    return (
        f"'{quota.name}' now allows {telemetry.compact(requested)} {quota.unit}; "
        "the new ceiling is in force."
    )


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
        # A period that is over does not "reset today", however many days ago
        # it ended; its successor is a row of its own.
        "resets_label": (
            "Period ended" if moment >= _as_utc(budget.period_end) else _resets_label(days)
        ),
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


def _budget_status(budget: Budget, spent: float, at: dt.datetime) -> LimitStatus:
    """The state a budget is in at ``spent`` dollars. Decides; writes nothing."""
    if budget.status == LimitStatus.DISABLED.value:
        return LimitStatus.DISABLED
    utilization = _utilisation(spent, float(budget.amount_usd or 0.0))
    if at >= _as_utc(budget.period_end):
        return LimitStatus.EXPIRED
    if utilization >= budget.hard_threshold_percent:
        return LimitStatus.EXCEEDED
    if utilization >= budget.warn_threshold_percent:
        return LimitStatus.WARNING
    return LimitStatus.ACTIVE


async def _evaluate_budget(
    session: AsyncSession, budget: Budget, *, request: Request | None = None
) -> LimitStatus:
    """Move a budget's status to match its spend and alert on an escalation."""
    previous = budget.status
    if previous == LimitStatus.DISABLED.value:
        return LimitStatus.DISABLED

    status = _budget_status(budget, float(budget.spent_usd or 0.0), _now())
    budget.status = status.value
    if status.value != previous:
        await _alert_budget(session, budget, status, request=request)
    return status


async def _alert_budget(
    session: AsyncSession,
    budget: Budget,
    status: LimitStatus,
    *,
    request: Request | None = None,
) -> None:
    """Raise the alert for a budget that has just *entered* ``status``.

    Only an escalation is news: dropping back to Active, or reaching the end of
    the period, moves the chip and nothing else. Deduplicated on the budget and
    the state it entered, so a threshold that stays crossed is one alert.
    """
    if status not in (LimitStatus.WARNING, LimitStatus.EXCEEDED):
        return
    amount = float(budget.amount_usd or 0.0)
    spent = float(budget.spent_usd or 0.0)
    utilization = _utilisation(spent, amount)
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


async def create_budget(
    session: AsyncSession,
    principal: Principal,
    payload: BudgetCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Open a budget for a period and measure what has already been spent in it.

    A budget opened on the twentieth is twenty days into its period. It used to
    start at $0 and stay there until somebody pressed Re-measure Spend -- "0.0%
    utilized" on the KPI row and "under-consumed" in the insights, over spend
    that might already be past the ceiling. It is measured here, so its first
    reading is true and a threshold it is already over raises its alert at
    once. A store that cannot answer does not stop the budget being created: it
    opens at zero and the scheduled roll-up fills it in.
    """
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
    try:
        await _roll_up_spend(session, principal, [budget], at=at, request=request)
    except (TelemetryBackendUnavailable, EngineError) as exc:
        # An unanswered read is not a reason to refuse the budget, and it is
        # not evidence of zero spend either: the row keeps its opening zero and
        # the next roll-up measures it.
        log.warning("budget %s opened unmeasured: %s", budget.id, exc)
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


def _in_scope(budget: Budget | Quota, project: telemetry.AgentProject) -> bool:
    """Whether one agent's spend belongs to one budget -- or one quota: both
    carry the same ``scope`` and ``scope_ref``."""
    scope = LimitScope(budget.scope)
    if scope is LimitScope.WORKSPACE:
        return True
    if scope is LimitScope.TEAM:
        return project.team == budget.scope_ref
    if scope is LimitScope.AGENT:
        return project.agent_id == budget.scope_ref
    return project.environment == budget.scope_ref


async def _roll_up_spend(
    session: AsyncSession,
    principal: Principal,
    budgets: Sequence[Budget],
    *,
    at: dt.datetime,
    bookkeeping: bool = False,
    request: Request | None = None,
) -> None:
    """Measure what each budget's scope has cost over its period, and evaluate it.

    Budgets sharing a period share one measurement. The measurement is always a
    fresh one: this number is stored and compared against a threshold, so it is
    never the one a dashboard remembered a minute ago. A budget the engine
    reported no cost for keeps the spend it already had: an unanswered read is
    not evidence that spending stopped.

    ``bookkeeping`` is how the clock writes. A person pressing Re-measure Spend
    edits the row and is named on it. The scheduled roll-up is an observation
    about the row, not a change to it, so it must not move ``updated_at`` -- the
    optimistic-concurrency token -- under whoever has the budget open, and it
    writes nothing at all when the spend and the status are what they were.
    """
    if not budgets:
        return
    client = get_engine_client()
    projects = await telemetry.workspace_projects(session, principal)
    periods: dict[tuple[dt.datetime, dt.datetime], list[Budget]] = {}
    for budget in budgets:
        start = _as_utc(budget.period_start)
        end = min(_as_utc(budget.period_end), at)
        if end > start:  # a budget for a period that has not opened has no spend
            periods.setdefault((start, end), []).append(budget)

    for (start, end), group in periods.items():
        try:
            async with engine_deadline(what="the budget roll-up"):
                # Only ``cost_usd`` is read below, so the per-agent token
                # aggregations are not asked for: they were the larger half of
                # what every pass cost the store.
                rollups = await telemetry.project_rollups(
                    client, projects, start, end, fresh=True, tokens=False
                )
        except EngineError as exc:
            raise telemetry.telemetry_unavailable(exc) from exc
        for budget in group:
            matched = [rollup for rollup in rollups if _in_scope(budget, rollup.project)]
            spend = telemetry.sum_optional(rollup.cost_usd for rollup in matched)
            if spend is not None and at >= _as_utc(budget.period_end):
                # What a period cost does not fall once the period is over. A
                # lower answer for one -- the first scheduled pass closes books
                # that may be months old -- means the store has let the traces
                # go, and the figure we recorded while it had them is the truer.
                spend = max(spend, float(budget.spent_usd or 0.0))
            if bookkeeping:
                await _book_spend(session, budget, spend, at, request=request)
                continue
            if spend is not None:
                budget.spent_usd = round(spend, 2)
            budget.updated_by = principal.actor
            await _evaluate_budget(session, budget, request=request)


async def _book_spend(
    session: AsyncSession,
    budget: Budget,
    spend: float | None,
    at: dt.datetime,
    *,
    request: Request | None = None,
) -> None:
    """The scheduled roll-up's write: spend and status, without it being an edit."""
    previous = budget.status
    spent = float(budget.spent_usd or 0.0) if spend is None else round(spend, 2)
    status = _budget_status(budget, spent, at)
    changes: dict[str, Any] = {}
    if spent != float(budget.spent_usd or 0.0):
        changes["spent_usd"] = spent
    if status.value != previous:
        changes["status"] = status.value
    if not changes:
        return
    await stamp(session, [budget], **changes)
    if status.value != previous:
        await _alert_budget(session, budget, status, request=request)


async def _lapsed_budgets(
    session: AsyncSession, principal: Principal, at: dt.datetime
) -> list[Budget]:
    """Budgets whose period has ended and whose books have not been closed.

    Nothing used to look at these: the roll-up read only the live period, so a
    budget past its end stayed "Active", "Resets today", for ever. Measuring one
    a last time records the period's final spend and moves it to Expired.
    """
    stmt = (
        _scoped_budgets(principal)
        .where(Budget.period_end <= at, Budget.status.not_in(INERT_STATUSES))
        .order_by(Budget.period_end.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _open_next_periods(
    session: AsyncSession,
    principal: Principal,
    at: dt.datetime,
    *,
    request: Request | None = None,
) -> list[Budget]:
    """Carry every recurring budget whose period has ended into the current one.

    A budget is a row per period, and nothing opened the next row: on the first
    of the month "Production monthly" dropped out of the live set, the KPI card
    fell back to "No budget set" and the forecast lost its ceiling. The newest
    row of each name is the budget as its owner last left it, so when that row
    has lapsed its amount, thresholds, scope and owner are carried into the
    period that contains now.

    Three kinds of budget are left alone. One that was switched off stays off --
    Disabled is how an admin ends a recurring budget. One created with
    hand-picked dates was never "Monthly" in the calendar sense, so there is no
    next period to infer for it. And only the period that has *just* ended
    carries forward: a budget nobody renewed for a whole period of its own is
    one its owner let go, and this pass did not exist until now -- without the
    rule its first run would reopen every budget the workspace ever abandoned
    and add them all to the Total Budget card.
    """
    covered = set(
        (
            await session.execute(
                select(Budget.name).where(
                    Budget.workspace_id == principal.workspace_id, Budget.period_end > at
                )
            )
        )
        .scalars()
        .all()
    )
    lapsed = (
        (
            await session.execute(
                _scoped_budgets(principal)
                .where(Budget.period_end <= at)
                .order_by(Budget.period_start.desc())
            )
        )
        .scalars()
        .all()
    )

    opened: list[Budget] = []
    for budget in lapsed:
        if budget.name in covered:
            continue
        covered.add(budget.name)  # only the newest row of a name speaks for it
        if budget.status == LimitStatus.DISABLED.value:
            continue
        period = LimitPeriod(budget.period)
        span = (_as_utc(budget.period_start), _as_utc(budget.period_end))
        if period_bounds(period, span[0]) != span:
            continue
        period_start, period_end = period_bounds(period, at)
        if span[1] != period_start:
            continue  # it lapsed a period or more ago and was never renewed
        successor = Budget(
            workspace_id=budget.workspace_id,
            name=budget.name,
            scope=budget.scope,
            scope_ref=budget.scope_ref,
            period=budget.period,
            amount_usd=budget.amount_usd,
            spent_usd=0.0,
            currency=budget.currency,
            warn_threshold_percent=budget.warn_threshold_percent,
            hard_threshold_percent=budget.hard_threshold_percent,
            period_start=period_start,
            period_end=period_end,
            owner_user_id=budget.owner_user_id,
            status=LimitStatus.ACTIVE.value,
            created_by=principal.actor,
            updated_by=principal.actor,
        )
        try:
            async with session.begin_nested():
                session.add(successor)
                await session.flush()
        except IntegrityError:
            continue  # another worker opened it between the read and the insert
        await audit.record(
            session,
            principal=principal,
            action="budget.rolled_over",
            entity_type=ENTITY_BUDGET,
            entity_id=successor.id,
            entity_label=successor.name,
            source_screen=SOURCE_SCREEN,
            detail=(
                f"Opened the {budget.period.lower()} period starting "
                f"{period_start.date().isoformat()} at "
                f"{telemetry.money(float(budget.amount_usd or 0.0))}, carried forward from "
                f"the period that ended {span[1].date().isoformat()}"
            ),
            metadata={
                "previous_budget_id": budget.id,
                "amount_usd": float(budget.amount_usd or 0.0),
                "period_start": period_start.isoformat(),
            },
            request=request,
        )
        opened.append(successor)
    return opened


async def refresh_budgets(
    session: AsyncSession,
    principal: Principal,
    *,
    budget_id: str | None = None,
    scheduled: bool = False,
    request: Request | None = None,
) -> list[dict[str, Any]]:
    """Roll measured cost into budgets and evaluate their thresholds.

    Budgets store spend rather than joining it live, so this is the job that
    keeps the bars true -- invoked from the screen, and by ``run_budget_sweep``
    on the platform clock. Without a ``budget_id`` it is the whole pass: it
    closes the books on every budget whose period has ended, opens the current
    period for the recurring ones, and measures everything that is live.

    ``scheduled`` is the clock's pass. It writes as bookkeeping (see
    ``_roll_up_spend``) and leaves no ``budget.refreshed`` audit row -- at one
    pass every ten minutes that would *be* the workspace's audit trail. What
    the clock changes is still on the record: a threshold crossed raises its
    alert, and a period opened writes ``budget.rolled_over``.
    """
    principal.require(Role.OPERATOR)
    at = _now()
    if budget_id:
        budgets = [await get_budget(session, principal, budget_id)]
    else:
        await _open_next_periods(session, principal, at, request=request)
        budgets = [
            *await _lapsed_budgets(session, principal, at),
            *await current_budgets(session, principal),
        ]
    if not budgets:
        return []

    await _roll_up_spend(
        session, principal, budgets, at=at, bookkeeping=scheduled, request=request
    )

    if not scheduled:
        await audit.record(
            session,
            principal=principal,
            action="budget.refreshed",
            entity_type=ENTITY_BUDGET,
            entity_label=f"{len(budgets)} budget(s)",
            source_screen=SOURCE_SCREEN,
            detail=(
                f"Rolled measured cost into {len(budgets)} budget(s); "
                + ", ".join(
                    f"{b.name} {_utilisation(b.spent_usd, b.amount_usd):.1f}%" for b in budgets
                )
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
# Cost quotas -- metered on the clock
#
# Ingest counts requests and tokens as they arrive. It cannot count dollars:
# the engine prices a run after it has been stored. So a quota on Cost had no
# meter at all -- it could be created, it read "$0 of $500" for ever, it never
# warned and it never breached. Its usage is measured here instead, the way a
# budget's spend is, by the same pass.
# ---------------------------------------------------------------------------

#: Months in one period of each cadence.
PERIOD_MONTHS: Final[dict[LimitPeriod, int]] = {
    LimitPeriod.MONTHLY: 1,
    LimitPeriod.QUARTERLY: 3,
    LimitPeriod.ANNUAL: 12,
}
#: How many periods a long-neglected quota is walked forward through before the
#: arithmetic gives up and the calendar period is used instead.
MAX_PERIOD_ROLLS: Final[int] = 64


def _shift_months(moment: dt.datetime, months: int) -> dt.datetime:
    """Move an instant by whole months, keeping its day where the month has one.

    Unlike ``_add_months`` this is for a reset instant somebody chose -- the
    fifteenth, say -- which has to stay the fifteenth as its period rolls.
    """
    year, month = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def _cost_period(quota: Quota, at: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The period of ``quota`` that contains ``at``: its start and its reset.

    A period ends at the quota's reset instant and is one cadence long, so a
    reset that has passed is walked forward until it has not. The start is
    always derived from the reset, never stored, so every pass over an unedited
    quota measures the same window.
    """
    period = LimitPeriod(quota.period)
    if quota.resets_at is None:
        return period_bounds(period, at)
    step = PERIOD_MONTHS[period]
    resets_at = _as_utc(quota.resets_at)
    for _ in range(MAX_PERIOD_ROLLS):
        if resets_at > at:
            return _shift_months(resets_at, -step), resets_at
        resets_at = _shift_months(resets_at, step)
    return period_bounds(period, at)


def _quota_covers(quota: Quota, project: telemetry.AgentProject) -> bool:
    """Whether one agent's spend counts against one quota.

    The rule ingest applies to requests and tokens: a narrower quota that names
    no target covers nothing, rather than every agent that has no team.
    """
    if quota.scope != LimitScope.WORKSPACE.value and not quota.scope_ref:
        return False
    return _in_scope(quota, project)


async def _book_cost(
    session: AsyncSession,
    quota: Quota,
    spend: float | None,
    resets_at: dt.datetime,
    *,
    request: Request | None = None,
) -> None:
    """The meter's write: usage, period and status, without it being an edit.

    As with a budget's scheduled roll-up, this is an observation about the row:
    it must not move ``updated_at`` under an admin who has the quota open, and
    it writes nothing when nothing moved. A period the engine reports no cost
    for keeps the usage it had -- an unanswered read is not evidence of no
    spend -- unless the period has just rolled, when the counter starts again
    as every quota's does.
    """
    previous = quota.status
    rolled = quota.resets_at is None or _as_utc(quota.resets_at) != resets_at
    if spend is not None:
        used = round(spend, 4)
    elif rolled:
        used = 0.0
    else:
        used = float(quota.used_value or 0.0)
    status = _status_for(_utilisation(used, quota.limit_value), previous)

    changes: dict[str, Any] = {}
    if used != float(quota.used_value or 0.0):
        changes["used_value"] = used
    if rolled:
        changes["resets_at"] = resets_at
    if status.value != previous:
        changes["status"] = status.value
    if not changes:
        return
    await stamp(session, [quota], **changes)
    if status.value != previous:
        await _alert_quota(session, quota, status, request=request)


async def meter_cost_quotas(
    session: AsyncSession,
    principal: Principal,
    *,
    at: dt.datetime | None = None,
    request: Request | None = None,
) -> int:
    """Set every live Cost quota's usage from the spend the engine measured.

    Usage is *set*, not added to: the engine is asked what the quota's scope has
    cost since its period opened, and that is the number. Quotas whose periods
    open together share one measurement, always a fresh one, because it is
    stored and compared against a ceiling. Returns how many quotas were metered;
    a workspace with none costs one query and no engine read.

    The store is asked before any row is locked, and the quotas are then read
    again under the lock: one an admin switched off, deleted or re-dated while
    the store was answering is left to the next pass rather than overruled.

    This does not make ``Block`` refuse anything. Cost is known only after the
    run it belongs to, so a Cost quota warns and alerts; the chip and the alert
    say so at the moment the ceiling is crossed, whatever its enforcement.
    """
    moment = at or _now()
    live = _scoped_quotas(principal).where(
        Quota.resource == QuotaResource.COST.value, Quota.status.not_in(INERT_STATUSES)
    )
    quotas = (await session.execute(live)).scalars().all()
    if not quotas:
        return 0

    client = get_engine_client()
    projects = await telemetry.workspace_projects(session, principal)
    measured: dict[dt.datetime, list[telemetry.ProjectRollup]] = {}
    for start in sorted({_cost_period(quota, moment)[0] for quota in quotas}):
        if start >= moment:
            continue  # a first period that has not opened yet has no spend
        try:
            async with engine_deadline(what="the cost quota meter"):
                measured[start] = await telemetry.project_rollups(
                    client, projects, start, moment, fresh=True, tokens=False
                )
        except EngineError as exc:
            raise telemetry.telemetry_unavailable(exc) from exc

    locked = (
        (
            await session.execute(
                live.where(Quota.id.in_([quota.id for quota in quotas]))
                # Id order, as ingest takes its quota rows, so two writers
                # holding several of the same rows cannot deadlock.
                .order_by(Quota.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    metered = 0
    for quota in locked:
        start, resets_at = _cost_period(quota, moment)
        rollups = measured.get(start)
        if rollups is None:
            continue
        spend = telemetry.sum_optional(
            rollup.cost_usd for rollup in rollups if _quota_covers(quota, rollup.project)
        )
        await _book_cost(session, quota, spend, resets_at, request=request)
        metered += 1
    return metered


#: When this process last ran the budget pass, on the monotonic clock.
_last_budget_sweep: float | None = None


def _sweep_principal(workspace_id: str) -> Principal:
    """How the clock signs what it does, as the scheduler's other sweeps do."""
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="platform-scheduler",
        display_name="Platform Scheduler",
    )


async def run_budget_sweep(*, force: bool = False) -> dict[str, int]:
    """The scheduled pass over every workspace's budgets. Never raises.

    This is the sweep ``refresh_budgets`` always said it had and the model's
    "rolled up from usage on a schedule" always assumed. Without it a budget's
    spend moved only when an operator pressed a button: the 80% and 100% alerts
    never fired on their own, and nothing closed or reopened a period.

    Called from the platform scheduler on every tick, it runs at most once per
    ``budget_sweep_interval_seconds`` in this process; it measures, so it has no
    business running every thirty seconds. The gate is per process on purpose:
    with several workers taking turns at the scheduler's lock the worst case is
    one pass per worker per interval, which costs a few store reads, and every
    write here is idempotent. It waits on the telemetry store, so the scheduler
    runs it *after* giving its lock back, never under it.

    Each workspace is measured on its own session and committed on its own: a
    store that cannot answer for one, or one bad row, costs that workspace this
    pass and nobody else anything.

    Cost quotas are metered by the same pass (``meter_cost_quotas``): they are
    the same kind of number -- measured spend held against a ceiling -- wanted
    at the same cadence, and this is the one measuring job the clock runs. Their
    counts are reported only for a pass that had a Cost quota to meter.
    """
    global _last_budget_sweep
    moment = time.monotonic()
    if (
        not force
        and _last_budget_sweep is not None
        and moment - _last_budget_sweep < settings.budget_sweep_interval_seconds
    ):
        return {}
    _last_budget_sweep = moment

    counts = {"budgets_measured": 0, "budget_sweeps_failed": 0}
    try:
        async with get_sessionmaker()() as session:
            workspace_ids = (
                (
                    await session.execute(
                        select(Budget.workspace_id)
                        .where(Budget.status != LimitStatus.DISABLED.value)
                        # A workspace may cap its spend with a quota and keep
                        # no budget at all; UNION also removes the duplicates.
                        .union(
                            select(Quota.workspace_id).where(
                                Quota.resource == QuotaResource.COST.value,
                                Quota.status.not_in(INERT_STATUSES),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
    except Exception:  # noqa: BLE001 - the scheduler runs this beside other deferred work
        # "Never raises" includes the first read: the scheduler runs this after
        # its lock with the export jobs it claimed under it, and an exception
        # here would leave those claimed and never generated.
        counts["budget_sweeps_failed"] += 1
        log.exception("budget sweep could not list the workspaces to measure")
        return counts

    for workspace_id in workspace_ids:
        principal = _sweep_principal(workspace_id)
        try:
            async with get_sessionmaker()() as session:
                # Opening a period is our own SQL and is committed before
                # anything is measured: a store that cannot answer on the first
                # of the month must not also cost the workspace its budget.
                await _open_next_periods(session, principal, _now())
                await session.commit()
                rows = await refresh_budgets(session, principal, scheduled=True)
                await session.commit()
            counts["budgets_measured"] += len(rows)
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            counts["budget_sweeps_failed"] += 1
            log.exception("budget sweep failed for workspace %s", workspace_id)
        # On a session of its own, so a budget row that failed above does not
        # cost the workspace its quota meter as well, nor the other way round.
        try:
            async with get_sessionmaker()() as session:
                metered = await meter_cost_quotas(session, principal)
                await session.commit()
            if metered:
                counts["cost_quotas_metered"] = counts.get("cost_quotas_metered", 0) + metered
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            counts["cost_quota_sweeps_failed"] = counts.get("cost_quota_sweeps_failed", 0) + 1
            log.exception("cost quota meter failed for workspace %s", workspace_id)
    return counts


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
) -> dict[str, list[Any]]:
    """The newest readings of every pool in the window, newest first per pool.

    The table needs one row per pool -- its latest reading -- and a sparkline
    of at most ``MAX_TREND_POINTS``. This used to load every reading of the
    last thirty days as ORM objects (up to 20,000 of them) to find those few,
    three times per Overview page; and because it read oldest-first under a row
    cap, a workspace past the cap was shown readings that were weeks old as
    "latest". The database now ranks the readings and hands back only the ones
    the table draws, with the count of readings behind each pool beside them.
    """
    position = (
        func.row_number()
        .over(
            partition_by=CapacityRecord.name,
            order_by=(CapacityRecord.measured_at.desc(), CapacityRecord.id.desc()),
        )
        .label("position")
    )
    ranked = (
        select(
            CapacityRecord.name,
            CapacityRecord.resource_type,
            CapacityRecord.region,
            CapacityRecord.provisioned,
            CapacityRecord.used,
            CapacityRecord.unit,
            CapacityRecord.utilization_percent,
            CapacityRecord.status,
            CapacityRecord.measured_at,
            position,
            func.count().over(partition_by=CapacityRecord.name).label("reading_count"),
        )
        .where(
            CapacityRecord.workspace_id == principal.workspace_id,
            CapacityRecord.measured_at >= since,
        )
        .subquery()
    )
    stmt = (
        select(ranked)
        .where(ranked.c.position <= MAX_TREND_POINTS)
        .order_by(ranked.c.name.asc(), ranked.c.position.asc())
    )
    grouped: dict[str, list[Any]] = {}
    for record in (await session.execute(stmt)).all():
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
        latest = readings[0]
        trend = [
            round(float(record.utilization_percent or 0.0), 1)
            for record in reversed(readings)  # the sparkline reads oldest first
            if _as_utc(record.measured_at) >= trend_floor
        ]
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
                "reading_count": int(latest.reading_count),
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
# Capacity — writes
#
# The table above had no writer. Nothing in the product, its deployment or its
# tests ever inserted a capacity reading, so the Capacity tab, the Overview
# card and the sixth KPI were empty on every installation. There are two
# writers now: a reporter outside the platform posts what it measured, and the
# platform records what it can see of itself on its own clock.
# ---------------------------------------------------------------------------

#: How far ahead of our clock a reporter's may run before a reading is refused.
CAPACITY_CLOCK_SKEW: Final[dt.timedelta] = dt.timedelta(minutes=5)


def _capacity_status(utilization: float) -> CapacityStatus:
    """The stored verdict, from the same thresholds the chip is drawn with."""
    health = health_for(utilization)
    if health is LimitHealth.CRITICAL:
        return CapacityStatus.CRITICAL
    if health is LimitHealth.WATCH:
        return CapacityStatus.WARNING
    return CapacityStatus.HEALTHY


def _capacity_record(
    workspace_id: str,
    *,
    name: str,
    resource_type: str,
    region: str | None,
    provisioned: float,
    used: float,
    unit: str,
    measured_at: dt.datetime,
) -> CapacityRecord:
    utilization = round(used / provisioned * 100, 1)
    return CapacityRecord(
        workspace_id=workspace_id,
        name=name,
        resource_type=resource_type,
        region=region,
        provisioned=provisioned,
        used=used,
        unit=unit,
        utilization_percent=utilization,
        status=_capacity_status(utilization).value,
        measured_at=measured_at,
    )


async def record_capacity(
    session: AsyncSession, principal: Principal, payload: CapacityReport
) -> int:
    """Store a reporter's readings. Returns how many were recorded.

    Readings are measurements, not governed changes: like ingested telemetry
    they are not audited one by one, or a reporter on a five-minute cadence
    would be the workspace's audit trail.
    """
    principal.require(Role.OPERATOR)
    at = _now()
    oldest = at - dt.timedelta(days=CAPACITY_HISTORY_DAYS)
    # The platform's own pools are not a reporter's to write. ``run_capacity_sweep``
    # reads whether a pass is due from the newest reading under those names, in
    # any workspace -- so one tenant's cron job posting "Platform Disk" every few
    # minutes would stop the platform recording its readings for every tenant.
    reserved = {name.casefold() for name in PLATFORM_POOLS}
    records: list[CapacityRecord] = []
    for position, reading in enumerate(payload.readings):
        if reading.name.casefold() in reserved:
            raise ValidationFailed(
                f"Reading {position + 1}: '{reading.name}' is a pool the platform reports "
                "about itself. Report yours under a name of its own.",
                details={"field": f"readings[{position}].name"},
            )
        measured_at = _as_utc(reading.measured_at) if reading.measured_at else at
        if measured_at > at + CAPACITY_CLOCK_SKEW:
            raise ValidationFailed(
                f"Reading {position + 1} ('{reading.name}') is dated in the future.",
                details={"field": f"readings[{position}].measured_at"},
            )
        if measured_at < oldest:
            raise ValidationFailed(
                f"Reading {position + 1} ('{reading.name}') is older than the "
                f"{CAPACITY_HISTORY_DAYS} days of history the screen shows.",
                details={"field": f"readings[{position}].measured_at"},
            )
        records.append(
            _capacity_record(
                principal.workspace_id,
                name=reading.name,
                resource_type=reading.resource_type,
                region=reading.region,
                provisioned=reading.provisioned,
                used=reading.used,
                unit=reading.unit,
                measured_at=measured_at,
            )
        )
    session.add_all(records)
    await session.flush()
    return len(records)


@dataclasses.dataclass(frozen=True)
class PlatformReading:
    """One thing the control plane measured about the machine it runs on."""

    name: str
    resource_type: str
    provisioned: float
    used: float
    unit: str


#: The pools the platform reports about itself. The names are fixed: they are
#: how the sweep recognises its own readings when it asks whether one is due.
PLATFORM_MEMORY: Final[str] = "Control Plane Memory"
PLATFORM_HOST_MEMORY: Final[str] = "Host Memory"
PLATFORM_DISK: Final[str] = "Platform Disk"
PLATFORM_CPU: Final[str] = "Host CPU"
PLATFORM_POOLS: Final[tuple[str, ...]] = (
    PLATFORM_MEMORY,
    PLATFORM_HOST_MEMORY,
    PLATFORM_DISK,
    PLATFORM_CPU,
)

_GIB: Final[float] = float(1024**3)
#: A cgroup with no memory limit reports a number this large, or the word "max".
_NO_LIMIT_BYTES: Final[int] = 1 << 60

#: ``(busy, total)`` jiffies at this process's previous CPU sample.
_cpu_sample: tuple[float, float] | None = None


def _read_number(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _memory_reading() -> PlatformReading | None:
    """This container's memory against its limit, else the host's.

    Inside a limited container ``/proc/meminfo`` still describes the host, so
    the cgroup files are asked first (v2, then v1): the limit the container will
    be killed at is the capacity that matters to it.
    """
    for limit_path, used_path in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        (
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ),
    ):
        limit, used = _read_number(limit_path), _read_number(used_path)
        if limit and used is not None and limit < _NO_LIMIT_BYTES:
            return PlatformReading(
                PLATFORM_MEMORY, "Memory", round(limit / _GIB, 3), round(used / _GIB, 3), "GiB"
            )
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            fields = {
                line.split(":", 1)[0]: line.split(":", 1)[1].split()[0]
                for line in handle
                if ":" in line
            }
        total = int(fields["MemTotal"]) * 1024
        available = int(fields["MemAvailable"]) * 1024
    except (OSError, KeyError, ValueError, IndexError):
        return None
    if total <= 0:
        return None
    return PlatformReading(
        PLATFORM_HOST_MEMORY,
        "Memory",
        round(total / _GIB, 3),
        round((total - available) / _GIB, 3),
        "GiB",
    )


def _disk_reading() -> PlatformReading | None:
    """The filesystem this process writes to."""
    try:
        usage = shutil.disk_usage(os.path.abspath(os.sep))
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return PlatformReading(
        PLATFORM_DISK, "Disk", round(usage.total / _GIB, 2), round(usage.used / _GIB, 2), "GiB"
    )


def _cpu_reading() -> PlatformReading | None:
    """Cores busy, averaged over the time since this process last looked.

    ``/proc/stat`` counts jiffies since boot, so utilisation is a difference
    between two samples. The first look has nothing to subtract from and
    reports nothing, rather than reporting the average since boot as "now".
    """
    global _cpu_sample
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            fields = handle.readline().split()
    except OSError:
        return None
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    try:
        jiffies = [float(value) for value in fields[1:]]
    except ValueError:
        return None
    idle = jiffies[3] + (jiffies[4] if len(jiffies) > 4 else 0.0)  # idle + iowait
    sample = (sum(jiffies) - idle, sum(jiffies))
    previous, _cpu_sample = _cpu_sample, sample
    if previous is None or sample[1] <= previous[1]:
        return None
    busy = max(0.0, sample[0] - previous[0]) / (sample[1] - previous[1])
    cores = float(os.cpu_count() or 1)
    return PlatformReading(PLATFORM_CPU, "CPU", cores, round(busy * cores, 3), "cores")


def platform_readings() -> list[PlatformReading]:
    """What this process can honestly say about the machine under it.

    Only what is measured: a reading the operating system will not give --
    no ``/proc`` on this platform, no cgroup limit, a first CPU sample -- is
    left out, never estimated.
    """
    found = (_memory_reading(), _disk_reading(), _cpu_reading())
    return [reading for reading in found if reading is not None]


async def run_capacity_sweep(*, force: bool = False) -> dict[str, int]:
    """Record the platform's own capacity readings and drop the expired ones.

    Called from the platform scheduler, *under* its lock: it is short SQL and a
    few local file reads, and the lock is what makes one worker the writer.
    Whether a pass is due is decided from the table rather than from a timer in
    this process -- the newest platform reading says when the last pass ran,
    whichever worker ran it -- so four workers record one reading per interval,
    not four. At the default fifteen minutes a month of readings is a few
    thousand rows per workspace.

    Capacity rows are workspace-scoped and the machine is shared, so the same
    reading is written once for every active workspace.
    """
    # Looked at on every call, due or not: CPU is a difference between two looks,
    # and the closer together they are the more "now" the reading is.
    readings = [reading for reading in platform_readings() if reading.provisioned > 0]
    at = _now()
    counts = {"capacity_recorded": 0, "capacity_expired": 0}
    async with get_sessionmaker()() as session:
        if not force:
            due_after = at - dt.timedelta(seconds=settings.capacity_sweep_interval_seconds)
            recent = await session.execute(
                select(CapacityRecord.id)
                .where(
                    CapacityRecord.name.in_(PLATFORM_POOLS),
                    CapacityRecord.measured_at > due_after,
                )
                .limit(1)
            )
            if recent.first() is not None:
                return {}

        workspace_ids = (
            (await session.execute(select(Workspace.id).where(Workspace.status == "active")))
            .scalars()
            .all()
        )
        records = [
            _capacity_record(
                workspace_id,
                name=reading.name,
                resource_type=reading.resource_type,
                region=None,
                provisioned=reading.provisioned,
                used=reading.used,
                unit=reading.unit,
                measured_at=at,
            )
            for workspace_id in workspace_ids
            for reading in readings
        ]
        session.add_all(records)
        counts["capacity_recorded"] = len(records)

        # Readings past the history window are never shown again; whoever
        # reported them, keeping them only makes every read of the table slower.
        expired = await session.execute(
            sa_delete(CapacityRecord).where(
                CapacityRecord.measured_at < at - dt.timedelta(days=CAPACITY_HISTORY_DAYS)
            )
        )
        counts["capacity_expired"] = expired.rowcount or 0
        await session.commit()
    return counts


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
    #: Daily spend over ``span`` as ``(bucket starts, values)``, when the caller
    #: asked for it to be measured alongside everything else; ``None`` when it
    #: did not, and the forecast then reads it for itself.
    daily_spend: tuple[list[dt.datetime], list[float | None]] | None = None


async def cost_context(
    session: AsyncSession,
    principal: Principal,
    period: QuotaPeriod,
    *,
    with_daily_spend: bool = False,
) -> CostContext:
    """Measure the period and the one before it, in parallel.

    ``with_daily_spend`` adds the daily cost series to the same gather. The KPI
    row needs it for its forecast, and used to await it only *after* everything
    else had answered -- one more store round trip added end to end to the
    slowest card on the screen.

    The whole fan-out answers inside the one-request engine deadline: a slow
    store costs the page a typed 503 it can retry, not a worker and a database
    connection held for as long as every queued read takes to time out.
    """
    client = get_engine_client()
    span = resolve_period(period)
    projects = await telemetry.workspace_projects(session, principal)
    reads: list[Any] = [
        telemetry.project_rollups(client, projects, span.start, span.end),
        telemetry.project_rollups(client, projects, span.previous_start, span.previous_end),
        telemetry.cost_total(client, projects, span.start, span.end),
        telemetry.cost_total(client, projects, span.previous_start, span.previous_end),
    ]
    if with_daily_spend:
        reads.append(telemetry.cost_points(client, projects, span.start, span.end))
    try:
        async with engine_deadline(what="the cost and usage measurement"):
            measured = await asyncio.gather(*reads)
    except EngineError as exc:
        raise telemetry.telemetry_unavailable(exc) from exc
    current, previous, spend, previous_spend = measured[:4]
    return CostContext(
        period=period,
        span=span,
        projects=projects,
        current=current,
        previous=previous,
        spend_usd=spend,
        previous_spend_usd=previous_spend,
        daily_spend=_daily_spend(span, measured[4]) if with_daily_spend else None,
    )


def _daily_spend(
    span: telemetry.Range, points: Sequence[tuple[dt.datetime, float]]
) -> tuple[list[dt.datetime], list[float | None]]:
    """Cost points folded onto the daily grid the spend chart and forecast share."""
    starts = telemetry.bucket_starts(span.start, span.end, MetricInterval.DAILY)
    return starts, telemetry.fold_sum(points, starts, MetricInterval.DAILY)


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
    # The shared window, not ``_now()``: an end stamped to the microsecond is a
    # question nobody else has ever asked, so every page view paid for every
    # team's series again however recently it had been measured.
    window = telemetry.resolve_window_days(days)
    start, end = window.start, window.end
    starts = telemetry.bucket_starts(start, end, MetricInterval.DAILY)

    by_team: dict[str, list[telemetry.AgentProject]] = {}
    for project in context.projects:
        by_team.setdefault(project.team or "Unassigned", []).append(project)

    wanted = [row for row in rows if by_team.get(row["team"])]
    if not wanted:
        return
    # One series per team is a fan-out like the one in ``cost_context``, and it
    # answers inside the same one-request deadline: the reads queue behind the
    # worker-wide bound, and a queue behind a slow store must cost the page a
    # typed 503, not the worker for as long as ten reads take to time out.
    try:
        async with engine_deadline(what="the team spend trends"):
            series = await asyncio.gather(
                *(
                    telemetry.cost_points(client, by_team[row["team"]], start, end)
                    for row in wanted
                )
            )
    except EngineError as exc:
        raise telemetry.telemetry_unavailable(exc) from exc
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
    return _daily_spend(span, await telemetry.cost_points(client, resolved, span.start, span.end))


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
    budgets: Sequence[Budget] | None = None,
) -> QuotaForecast:
    """Project period-end spend from the daily spend already measured.

    A straight least-squares fit over the observed days, extended to the end of
    the period. The interval is the 95% band implied by the residual spread, and
    anomalies are the days whose spend sits more than three residual standard
    deviations off the fitted line. With fewer than four measured days nothing
    is projected: a line through two points is not a forecast.

    ``context`` and ``budgets`` let a caller that has already measured the
    period and read the live budgets — the KPI summary does both — hand them
    over instead of paying for them twice.
    """
    measured = (
        context
        if context is not None
        else await cost_context(session, principal, period, with_daily_spend=True)
    )
    span = measured.span
    at = _now()
    period_end = (
        period_bounds(LimitPeriod.MONTHLY, at)[1] if period is QuotaPeriod.MTD else span.end
    )
    if measured.daily_spend is not None:
        starts, values = measured.daily_spend
    else:
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
    if budgets is None:
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
    session: AsyncSession,
    principal: Principal,
    *,
    period: QuotaPeriod = QuotaPeriod.MTD,
    context: CostContext | None = None,
    budgets: Sequence[Budget] | None = None,
    capacity: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Observations computed from what was measured, in this request.

    Nothing here is stored or authored: each observation is a rule over budgets,
    quotas, capacity readings and per-model unit cost, and its body quotes the
    numbers that triggered it so an operator can check the reasoning.

    ``context``, ``budgets`` and ``capacity`` are for a caller that already
    holds them (``overview`` does): the panel is then a view of the measurement
    the rest of the page was drawn from, not a second one.
    """
    at = _now()
    if context is None:
        context = await cost_context(session, principal, period)
    if budgets is None:
        budgets = await current_budgets(session, principal)
    quotas = await all_quota_rows(session, principal)
    if capacity is None:
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
    session: AsyncSession,
    principal: Principal,
    *,
    period: QuotaPeriod = QuotaPeriod.MTD,
    context: CostContext | None = None,
    budgets: Sequence[Budget] | None = None,
    capacity_readings: Sequence[dict[str, Any]] | None = None,
) -> QuotaSummary:
    """The six KPI cards, each against the equivalent span of the prior period.

    Spend, tokens and calls are measured; budget is the sum of the ceilings we
    hold for the live period; unit cost is derived from the first two; capacity
    health is scored from the latest reading of every pool.

    Everything the cards need from the store is measured in one gather -- the
    forecast's daily series included -- and the budgets are read once and handed
    to the forecast rather than read again inside it.
    """
    if context is None:
        context = await cost_context(session, principal, period, with_daily_spend=True)
    head = telemetry.aggregate(context.current)
    tail = telemetry.aggregate(context.previous)

    if budgets is None:
        budgets = await current_budgets(session, principal)
    budget_total = round(sum(float(budget.amount_usd or 0.0) for budget in budgets), 2)
    budget_spent = round(sum(float(budget.spent_usd or 0.0) for budget in budgets), 2)
    budget_utilization = _utilisation(budget_spent, budget_total) if budget_total else None

    if capacity_readings is None:
        capacity_readings = await capacity_rows(session, principal)
    capacity = capacity_health(capacity_readings)
    projection = await forecast(
        session, principal, period=period, context=context, budgets=budgets
    )

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


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

#: Teams whose sparkline the overview fetches: the page the Overview card shows.
OVERVIEW_TEAM_TRENDS: Final[int] = 10


async def overview(
    session: AsyncSession, principal: Principal, *, period: QuotaPeriod = QuotaPeriod.MTD
) -> dict[str, Any]:
    """The KPI row and every cost panel of the screen, from one measurement.

    The cards, the per-model table, the service donut, the driver bars, the team
    table and the insights are all views of one thing: the period's per-agent
    rollup and the one before it. Served apart, the Overview tab measured that
    four times over and the Costs tab three more -- each a walk of the store's
    project statistics plus a token aggregation per busy agent, twice. The
    telemetry layer's shared memory only helps when those requests happen to
    reach the same worker; served together it is measured once by construction.

    The breakdowns arrive whole, heaviest first. They are a handful of rows, so
    the console sorts, searches and pages them without asking again.
    """
    context = await cost_context(session, principal, period, with_daily_spend=True)
    budgets = await current_budgets(session, principal)
    capacity = await capacity_rows(session, principal)

    teams = team_rows(context)
    await attach_team_trends(context, teams[:OVERVIEW_TEAM_TRENDS])
    return {
        "summary": await summarise(
            session,
            principal,
            period=period,
            context=context,
            budgets=budgets,
            capacity_readings=capacity,
        ),
        "models": model_cost_rows(context),
        "services": service_cost_rows(context),
        "drivers": driver_rows(context),
        "teams": teams,
        "insights": await insights(
            session,
            principal,
            period=period,
            context=context,
            budgets=budgets,
            capacity=capacity,
        ),
    }
