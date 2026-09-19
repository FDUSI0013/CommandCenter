"""Wire contracts for Quota, Cost & Capacity.

The screen has three halves that meet in one place: money measured by the
telemetry engine (spend, tokens, calls), ceilings we own and enforce (quotas and
budgets), and infrastructure readings reported to us (capacity). The schemas
below keep that separation visible — anything the engine measured is nullable,
because "not reported" and "measured zero" are different facts, while anything
we own has a value we are responsible for.

Two vocabularies deserve a word:

* **status** is the durable state of a limit (``Active``/``Warning``/
  ``Exceeded``), written by the evaluator and never by a person;
* **health** is the three-way chip the table paints — Healthy under 70%, Watch
  under 85%, Critical at or above it — derived on read from utilisation.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.operations import (
    CapacityStatus,
    LimitPeriod,
    LimitScope,
    LimitStatus,
    QuotaEnforcement,
    QuotaResource,
)
from .metrics import MetricKpi, MetricPoint

__all__ = [
    "BudgetCreate",
    "BudgetRead",
    "BudgetUpdate",
    "CapacityReading",
    "CapacityReport",
    "CapacityRow",
    "CapacitySeries",
    "CapacityStatus",
    "CostDriverRow",
    "InsightRead",
    "LimitPeriod",
    "LimitScope",
    "LimitStatus",
    "ModelCostRow",
    "QuotaCheck",
    "QuotaCreate",
    "QuotaEnforcement",
    "QuotaEventRead",
    "QuotaExportDataset",
    "QuotaForecast",
    "QuotaIncreaseRequest",
    "QuotaOverview",
    "QuotaPeriod",
    "QuotaRead",
    "QuotaResource",
    "QuotaSummary",
    "QuotaUpdate",
    "QuotaUsageMetric",
    "ServiceCostRow",
    "TeamAllocationRow",
]

#: Utilisation at which a limit stops being comfortable, and at which it is in
#: trouble. One pair of thresholds drives the chip, the status and the alert.
WATCH_PERCENT: Final[float] = 70.0
CRITICAL_PERCENT: Final[float] = 85.0


class LimitHealth(enum.StrEnum):
    """The chip beside a quota, budget or capacity row."""

    HEALTHY = "Healthy"
    WATCH = "Watch"
    CRITICAL = "Critical"

    @property
    def color(self) -> str:
        return {"Healthy": "green", "Watch": "amber", "Critical": "red"}[self.value]


def health_for(utilization_percent: float | None) -> LimitHealth:
    """Map a utilisation onto the chip. Unknown utilisation reads as healthy."""
    if utilization_percent is None:
        return LimitHealth.HEALTHY
    if utilization_percent >= CRITICAL_PERCENT:
        return LimitHealth.CRITICAL
    if utilization_percent >= WATCH_PERCENT:
        return LimitHealth.WATCH
    return LimitHealth.HEALTHY


class QuotaPeriod(enum.StrEnum):
    """Measurement period the screen's cost and usage panels are read over."""

    MTD = "mtd"
    LAST_24H = "24h"
    LAST_7D = "7d"
    LAST_30D = "30d"
    LAST_90D = "90d"


class QuotaUsageMetric(enum.StrEnum):
    """The three lines the usage and spend charts draw."""

    SPEND = "spend"
    TOKENS = "tokens"
    API_CALLS = "api_calls"


class QuotaExportDataset(enum.StrEnum):
    """Which of the screen's tables the Export button is pointed at."""

    TEAMS = "teams"
    QUOTAS = "quotas"
    BUDGETS = "budgets"
    CAPACITY = "capacity"
    COST_BY_MODEL = "cost-by-model"
    COST_BY_SERVICE = "cost-by-service"
    TOP_DRIVERS = "top-drivers"


class InsightSeverity(enum.StrEnum):
    """How loudly an observation asks to be acted on."""

    INFO = "Info"
    WARNING = "Warning"
    CRITICAL = "Critical"


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


class QuotaRead(BaseModel):
    """One row of the Quota Utilization table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str = Field(description="Quota Type column, e.g. 'Tokens (MTD)'")
    resource: QuotaResource
    scope: LimitScope
    scope_ref: str | None = None

    limit_value: float
    used_value: float
    unit: str
    used_display: str = "0"
    limit_display: str = "0"
    utilization_percent: float = 0.0
    remaining_value: float = 0.0

    enforcement: QuotaEnforcement
    period: LimitPeriod
    status: LimitStatus
    health: LimitHealth = LimitHealth.HEALTHY
    health_color: str = "green"

    resets_at: dt.datetime | None = None
    resets_in_days: int | None = None
    resets_label: str | None = Field(None, description='e.g. "Resets in 18 days"')

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class QuotaCreate(BaseModel):
    """Create a quota. Usage and status are not accepted: the enforcer owns both."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    resource: QuotaResource = QuotaResource.TOKENS
    scope: LimitScope = LimitScope.WORKSPACE
    scope_ref: str | None = Field(
        None, max_length=120, description="Team, environment or agent the quota binds to"
    )
    limit_value: float = Field(gt=0, description="Ceiling in ``unit``")
    unit: str = Field("tokens", min_length=1, max_length=24)
    period: LimitPeriod = LimitPeriod.MONTHLY
    enforcement: QuotaEnforcement = QuotaEnforcement.WARN
    resets_at: dt.datetime | None = Field(
        None, description="Defaults to the end of the current period"
    )

    @field_validator("name", "unit")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _scope_needs_a_target(self) -> QuotaCreate:
        if self.scope is not LimitScope.WORKSPACE and not (self.scope_ref or "").strip():
            raise ValueError(f"scope_ref is required for a {self.scope.value}-scoped quota")
        return self


class QuotaUpdate(BaseModel):
    """Partial update. ``Warning`` and ``Exceeded`` cannot be set by hand."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    scope_ref: str | None = Field(None, max_length=120)
    limit_value: float | None = Field(None, gt=0)
    unit: str | None = Field(None, min_length=1, max_length=24)
    period: LimitPeriod | None = None
    enforcement: QuotaEnforcement | None = None
    status: LimitStatus | None = Field(
        None, description="Only Active and Disabled may be set by a caller"
    )
    resets_at: dt.datetime | None = None
    expected_updated_at: dt.datetime | None = Field(
        None, description="Optimistic concurrency guard; 409 if the row moved"
    )

    @field_validator("name", "unit")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class QuotaIncreaseRequest(BaseModel):
    """Ask for a bigger ceiling. Lands in the approvals queue, not in the row."""

    model_config = ConfigDict(extra="forbid")

    requested_limit: float = Field(gt=0, description="New ceiling being asked for")
    reason: str | None = Field(None, max_length=2000)
    justification_ticket: str | None = Field(None, max_length=64)


class QuotaCheck(BaseModel):
    """The enforcement answer the ingest path acts on."""

    allowed: bool = Field(description="False only when a Block-enforced quota is breached")
    resource: QuotaResource
    quota_id: str | None = Field(None, description="Tightest quota that applies; null if none")
    quota_name: str | None = None
    limit_value: float | None = None
    used_value: float | None = None
    projected_value: float | None = Field(
        None, description="Usage after the requested amount is counted"
    )
    remaining_value: float | None = None
    utilization_percent: float | None = None
    enforcement: QuotaEnforcement | None = None
    status: LimitStatus | None = None
    health: LimitHealth = LimitHealth.HEALTHY
    reason: str | None = Field(None, description="Why the call was refused, when it was")


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class BudgetRead(BaseModel):
    """One bar in Budget Status (MTD)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    scope: LimitScope
    scope_ref: str | None = None
    period: LimitPeriod
    amount_usd: float
    spent_usd: float
    currency: str = "USD"
    utilization_percent: float = 0.0
    remaining_usd: float = 0.0
    spent_display: str = "$0"
    amount_display: str = "$0"

    warn_threshold_percent: float
    hard_threshold_percent: float
    period_start: dt.datetime
    period_end: dt.datetime
    resets_in_days: int | None = None
    resets_label: str | None = Field(
        None, description='e.g. "Resets in 18 days"; "Period ended" once it has'
    )

    status: LimitStatus
    health: LimitHealth = LimitHealth.HEALTHY
    health_color: str = "green"
    projected_spend_usd: float | None = Field(
        None, description="Period-end spend at the pace observed so far"
    )
    on_pace_to_breach: bool = False
    owner_user_id: str | None = None

    created_at: dt.datetime
    updated_at: dt.datetime = Field(
        description=(
            "Moves on an edit and on a manual re-measure. The scheduled roll-up of "
            "spend is bookkeeping and does not move it, so it is safe to send back "
            "as `expected_updated_at`"
        )
    )


class BudgetCreate(BaseModel):
    """Create a budget. Spend is rolled up from measured cost, never supplied."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scope: LimitScope = LimitScope.WORKSPACE
    scope_ref: str | None = Field(None, max_length=120)
    period: LimitPeriod = LimitPeriod.MONTHLY
    amount_usd: float = Field(gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    warn_threshold_percent: float = Field(80.0, gt=0, le=200)
    hard_threshold_percent: float = Field(100.0, gt=0, le=200)
    period_start: dt.datetime | None = Field(
        None, description="Defaults to the start of the current period"
    )
    period_end: dt.datetime | None = None
    owner_user_id: str | None = Field(None, max_length=36)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _thresholds_ascend(self) -> BudgetCreate:
        if self.warn_threshold_percent >= self.hard_threshold_percent:
            raise ValueError("warn_threshold_percent must be below hard_threshold_percent")
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        if self.scope is not LimitScope.WORKSPACE and not (self.scope_ref or "").strip():
            raise ValueError(f"scope_ref is required for a {self.scope.value}-scoped budget")
        return self


class BudgetUpdate(BaseModel):
    """Partial update, including the threshold editor behind Edit Thresholds."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    amount_usd: float | None = Field(None, gt=0)
    warn_threshold_percent: float | None = Field(None, gt=0, le=200)
    hard_threshold_percent: float | None = Field(None, gt=0, le=200)
    owner_user_id: str | None = Field(None, max_length=36)
    scope_ref: str | None = Field(None, max_length=120)
    status: LimitStatus | None = Field(
        None, description="Only Active and Disabled may be set by a caller"
    )
    period_end: dt.datetime | None = None
    expected_updated_at: dt.datetime | None = None

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


class CapacitySeries(BaseModel):
    """Utilisation of one pool over time, from the readings we hold.

    Separate from the telemetry series contract on purpose: these points are
    infrastructure readings reported to us, not measurements the telemetry store
    made, and conflating the two would misattribute both.
    """

    resource: str
    unit: str = "percent"
    period_start: dt.datetime
    period_end: dt.datetime
    labels: list[str] = Field(default_factory=list)
    points: list[MetricPoint] = Field(default_factory=list)
    average_percent: float | None = None
    latest_percent: float | None = None


class CapacityReading(BaseModel):
    """One measurement of one pool, as the system that took it reports it.

    Utilisation and status are not accepted. Both are derived from ``used`` and
    ``provisioned`` with the thresholds every other chip on the screen uses, so
    the Capacity Health score and its label can never disagree with the row.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="e.g. 'GPU Capacity (A100)'")
    resource_type: str = Field(
        min_length=1, max_length=40, description="Family: GPU, CPU, Memory, Disk, Network…"
    )
    region: str | None = Field(None, max_length=60)
    provisioned: float = Field(gt=0, description="What the pool has, in ``unit``")
    used: float = Field(ge=0, description="What is in use, in ``unit``")
    unit: str = Field("units", min_length=1, max_length=24)
    measured_at: dt.datetime | None = Field(
        None, description="When the reading was taken; defaults to now"
    )

    @field_validator("name", "resource_type", "unit")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class CapacityReport(BaseModel):
    """A batch of readings from one reporter — a node exporter, a cron job."""

    model_config = ConfigDict(extra="forbid")

    readings: list[CapacityReading] = Field(min_length=1, max_length=100)


class CapacityRow(BaseModel):
    """One row of the Capacity Overview table, with its sparkline."""

    name: str
    resource_type: str
    region: str | None = None
    icon: str = Field(description="Icon key the row renders")
    provisioned: float
    used: float
    headroom: float = Field(description="provisioned − used, in ``unit``")
    unit: str
    utilization_percent: float
    headroom_percent: float
    status: CapacityStatus
    health: LimitHealth = LimitHealth.HEALTHY
    health_color: str = "green"
    measured_at: dt.datetime
    reading_count: int = Field(description="Readings behind the trend")
    trend: list[float] = Field(
        default_factory=list, description="Trailing utilisation readings, oldest first"
    )


# ---------------------------------------------------------------------------
# Cost analysis
# ---------------------------------------------------------------------------


class ModelCostRow(BaseModel):
    """One row of the per-model cost breakdown."""

    model: str
    agent_count: int = 0
    cost_usd: float | None = None
    cost_display: str = "—"
    previous_cost_usd: float | None = None
    cost_delta_percent: float | None = None
    tokens: float | None = None
    tokens_display: str = "—"
    api_calls: int = 0
    cost_per_1k_tokens: float | None = None
    previous_cost_per_1k_tokens: float | None = None
    unit_cost_delta_percent: float | None = Field(
        None, description="Movement in cost per 1K tokens against the prior period"
    )
    share_percent: float | None = None
    color: str = "purple"


class ServiceCostRow(BaseModel):
    """One slice of the Cost by Service donut."""

    key: str
    label: str
    cost_usd: float | None = None
    cost_display: str = "—"
    share_percent: float | None = None
    model_count: int = 0
    color: str = "purple"


class CostDriverRow(BaseModel):
    """One bar of Top Cost Drivers."""

    key: str
    label: str
    cost_usd: float | None = None
    cost_display: str = "—"
    share_percent: float | None = None
    share_display: str = "—"
    color: str = "purple"


class TeamAllocationRow(BaseModel):
    """One row of Cost & Usage by Team."""

    team: str
    agent_count: int = 0
    spend_usd: float | None = None
    spend_display: str = "—"
    share_percent: float | None = None
    share_display: str = "—"
    tokens: float | None = None
    tokens_display: str = "—"
    api_calls: int = 0
    api_calls_display: str = "0"
    avg_cost_per_1k_tokens: float | None = None
    trend: list[float | None] = Field(
        default_factory=list, description="Daily spend over the trailing week"
    )


class InsightRead(BaseModel):
    """One computed observation in Cost Optimization Insights.

    Every insight is derived from measured data at request time; there is no
    stored list and no editorial content. ``body`` states the numbers that
    triggered it so an operator can check the reasoning.
    """

    key: str
    severity: InsightSeverity
    icon: str
    color: str
    title: str
    body: str
    potential_savings_usd: float | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    recommendations: list[str] = Field(default_factory=list)


class QuotaEventRead(BaseModel):
    """One entry of Quota Events — an alert we raised or a change we recorded."""

    id: str
    occurred_at: dt.datetime
    kind: str = Field(description="'alert' or 'change'")
    title: str
    detail: str | None = None
    actor: str | None = None
    severity: str | None = None
    status: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None


class ForecastPoint(BaseModel):
    """One day of the forecast chart; exactly one of the two values is set."""

    timestamp: dt.datetime
    label: str
    actual_usd: float | None = None
    forecast_usd: float | None = None


class QuotaForecast(BaseModel):
    """Spend Forecast tab: a least-squares projection of the current period.

    ``method`` names the estimator so nobody has to guess how the numbers were
    produced, and every field is null when there is not enough measured history
    to fit a line — a forecast from two points is not a forecast.
    """

    method: str = "Ordinary least squares over daily spend"
    period_start: dt.datetime
    period_end: dt.datetime
    days_elapsed: int
    days_remaining: int
    observed_spend_usd: float | None = None
    projected_spend_usd: float | None = None
    budget_usd: float | None = None
    projected_utilization_percent: float | None = None
    confidence_interval_usd: float | None = Field(
        None, description="Half-width of the 95% interval on the projection"
    )
    confidence_level: float = 95.0
    daily_growth_usd: float | None = Field(None, description="Fitted slope, USD per day")
    highest_growth_driver: str | None = None
    highest_growth_delta_usd: float | None = None
    anomalies_detected: int = 0
    anomaly_days: list[str] = Field(default_factory=list)
    points: list[ForecastPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# KPI summary
# ---------------------------------------------------------------------------


class CapacityHealth(BaseModel):
    """The Capacity Health card, and the counts behind its label."""

    score_percent: float | None = Field(
        None, description="100 while every pool is under the watch threshold"
    )
    label: str = "Healthy"
    resource_count: int = 0
    healthy: int = 0
    warning: int = 0
    critical: int = 0


class QuotaSummary(BaseModel):
    """The six KPI cards on Quota, Cost & Capacity."""

    period: QuotaPeriod
    period_start: dt.datetime
    period_end: dt.datetime
    previous_period_start: dt.datetime
    previous_period_end: dt.datetime

    spend: MetricKpi
    budget: MetricKpi
    tokens: MetricKpi
    api_calls: MetricKpi
    cost_per_1k_tokens: MetricKpi
    capacity: MetricKpi

    budget_utilization_percent: float | None = None
    capacity_health: CapacityHealth
    forecast_spend_usd: float | None = Field(
        None, description="Period-end projection shown beside the spend chart"
    )


class QuotaOverview(BaseModel):
    """The KPI row and every cost panel, measured once.

    Each member is exactly what its own route answers -- ``summary`` is
    ``/quota/summary``, ``models`` is every row of ``/quota/cost-breakdown`` and
    so on -- in that route's default order. They are views of one measurement,
    so a screen that wants several asks here and the telemetry store is asked
    once rather than once per panel. The breakdowns arrive whole: they are a
    handful of rows, and the console sorts and pages them itself.
    """

    summary: QuotaSummary
    models: list[ModelCostRow] = Field(default_factory=list)
    services: list[ServiceCostRow] = Field(default_factory=list)
    drivers: list[CostDriverRow] = Field(default_factory=list)
    teams: list[TeamAllocationRow] = Field(
        default_factory=list, description="Sparklines are filled for the first ten"
    )
    insights: list[InsightRead] = Field(default_factory=list)


class QuotaSpendSeries(BaseModel):
    """Spend Over Time, with the projection the badge quotes."""

    period_start: dt.datetime
    period_end: dt.datetime
    labels: list[str] = Field(default_factory=list)
    actual: list[MetricPoint] = Field(default_factory=list)
    forecast: list[ForecastPoint] = Field(default_factory=list)
    forecast_spend_usd: float | None = None
