"""Wire contracts for the Metrics screen.

The Metrics screen is a read-only rollup of the telemetry engine: six KPI cards
with a period-over-period delta, six trend charts, a per-model table with a
usage donut, and a runs-by-platform donut. Nothing on it is editable, so this
module carries no write schemas at all.

Two conventions run through everything here:

* a numeric field is ``None`` when the telemetry store did not report it for the
  requested window. It is never zero-filled — a zero means "measured zero", and
  the console renders the two states differently;
* every value carries the pre-formatted ``display`` string the card or table
  cell renders, so the console never re-implements the compact-number rules.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class MetricWindow(enum.StrEnum):
    """The four ranges the screen's window picker offers."""

    LAST_24H = "24h"
    LAST_7D = "7d"
    LAST_30D = "30d"
    LAST_90D = "90d"

    @property
    def days(self) -> int:
        return _WINDOW_DAYS[self]

    @property
    def label(self) -> str:
        return _WINDOW_LABELS[self]

    @property
    def comparison_label(self) -> str:
        """Caption under the delta, e.g. "vs prior 30d"."""
        return f"vs prior {self.value}"


_WINDOW_DAYS: Final[dict[MetricWindow, int]] = {
    MetricWindow.LAST_24H: 1,
    MetricWindow.LAST_7D: 7,
    MetricWindow.LAST_30D: 30,
    MetricWindow.LAST_90D: 90,
}

_WINDOW_LABELS: Final[dict[MetricWindow, str]] = {
    MetricWindow.LAST_24H: "Last 24 hours",
    MetricWindow.LAST_7D: "Last 7 days",
    MetricWindow.LAST_30D: "Last 30 days",
    MetricWindow.LAST_90D: "Last 90 days",
}


class MetricInterval(enum.StrEnum):
    """Bucket width of a series. Each window has a sensible default."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


DEFAULT_INTERVAL: Final[dict[MetricWindow, MetricInterval]] = {
    MetricWindow.LAST_24H: MetricInterval.HOURLY,
    MetricWindow.LAST_7D: MetricInterval.DAILY,
    MetricWindow.LAST_30D: MetricInterval.DAILY,
    MetricWindow.LAST_90D: MetricInterval.WEEKLY,
}


class SeriesMetric(enum.StrEnum):
    """One line on one of the six trend charts.

    ``violations`` and ``escalations`` are governance counts held in our own
    database; the other six are measured by the telemetry engine.
    """

    RUNS = "runs"
    SUCCESS_RATE = "success_rate"
    LATENCY_P50 = "latency_p50"
    # The engine computes p50/p90/p99 for durations — p95 does not exist there,
    # and presenting a neighbouring percentile under that name would be a lie.
    LATENCY_P90 = "latency_p90"
    TOKENS = "tokens"
    COST = "cost"
    VIOLATIONS = "violations"
    ESCALATIONS = "escalations"


class TrendDirection(enum.StrEnum):
    """Arrow the KPI card draws next to the delta."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


#: Presentation of each series, matching the colours the charts already use.
SERIES_PRESENTATION: Final[dict[SeriesMetric, tuple[str, str, str]]] = {
    # metric: (label, unit, colour)
    SeriesMetric.RUNS: ("Runs", "runs", "purple"),
    SeriesMetric.SUCCESS_RATE: ("Success Rate", "percent", "green"),
    SeriesMetric.LATENCY_P50: ("Latency p50", "seconds", "purple"),
    SeriesMetric.LATENCY_P90: ("Latency p90", "seconds", "orange"),
    SeriesMetric.TOKENS: ("Tokens", "tokens", "blue"),
    SeriesMetric.COST: ("Cost", "usd", "orange"),
    SeriesMetric.VIOLATIONS: ("Policy Violations", "count", "red"),
    SeriesMetric.ESCALATIONS: ("Escalations", "count", "amber"),
}


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class MetricKpi(BaseModel):
    """One of the six cards in the KPI row.

    ``good`` is the *judgement* on the movement, not on the value: falling cost
    is good, falling success rate is not, and the card colours the delta from
    this flag rather than from the sign of ``delta``.
    """

    key: str = Field(description="Stable identifier, e.g. 'total_runs'")
    label: str = Field(description="Card title exactly as rendered")
    value: float | None = Field(None, description="Measured value; null when unreported")
    display: str = Field(description="Pre-formatted value, e.g. '356K' or '$128,450'")
    unit: str = Field(description="runs | percent | seconds | tokens | usd")
    sub: str | None = Field(
        None, description="Caption a card shows instead of a delta, e.g. '64.2% utilized'"
    )

    previous: float | None = Field(None, description="Same measure over the prior window")
    delta: float | None = Field(
        None, description="Signed change: percent for counts, absolute for rates and latency"
    )
    delta_display: str | None = Field(None, description="e.g. '14.2%' or '0.21s'")
    direction: TrendDirection = TrendDirection.FLAT
    good: bool | None = Field(
        None, description="Whether the movement is favourable; null when there is no delta"
    )
    comparison: str = Field(description="Caption under the delta, e.g. 'vs prior 30d'")

    icon: str = Field(description="Icon key the card renders")
    color: str = Field(description="Accent colour key the card renders")


class MetricsSummary(BaseModel):
    """The KPI row, plus the exact windows every number was measured over."""

    window: MetricWindow
    window_label: str
    period_start: dt.datetime
    period_end: dt.datetime
    previous_period_start: dt.datetime
    previous_period_end: dt.datetime
    agent_count: int = Field(description="Provisioned agents the rollup covers")

    total_runs: MetricKpi
    success_rate: MetricKpi
    latency_p50: MetricKpi
    latency_p90: MetricKpi
    tokens: MetricKpi
    cost: MetricKpi


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------


class MetricPoint(BaseModel):
    """One bucket of one series."""

    timestamp: dt.datetime = Field(description="Start of the bucket, UTC")
    label: str = Field(description="X-axis label, e.g. 'Aug 18' or '14:00'")
    value: float | None = Field(None, description="Null when the bucket has no measurement")


class MetricSeries(BaseModel):
    """One line on a chart."""

    metric: SeriesMetric
    label: str
    unit: str
    color: str
    points: list[MetricPoint] = Field(default_factory=list)
    total: float | None = Field(None, description="Sum over the window; null for rates")
    average: float | None = Field(None, description="Mean of the reported buckets")


class MetricsSeriesResponse(BaseModel):
    """Every requested line over one shared bucket grid."""

    window: MetricWindow
    interval: MetricInterval
    period_start: dt.datetime
    period_end: dt.datetime
    labels: list[str] = Field(
        default_factory=list, description="Shared x-axis labels, one per bucket"
    )
    series: list[MetricSeries] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------


class ModelUsageRow(BaseModel):
    """One row of the Top Models table and one slice of the usage donut.

    The rollup is by *configured* model: the telemetry of every agent whose
    configuration names this model, summed.
    """

    model: str
    agent_count: int = Field(description="Agents configured with this model")
    runs: int = 0
    tokens: float | None = None
    tokens_display: str = "—"
    cost_usd: float | None = None
    cost_display: str = "—"
    avg_latency_seconds: float | None = Field(
        None, description="Runs-weighted mean of the per-agent p50"
    )
    success_rate_percent: float | None = None
    tokens_share_percent: float | None = Field(
        None, description="Share of workspace tokens, 0-100"
    )
    cost_share_percent: float | None = None
    runs_share_percent: float | None = None
    color: str = Field(description="Donut/bar colour key")


class PlatformUsageRow(BaseModel):
    """One slice of the Runs by Platform donut."""

    platform: str
    agent_count: int
    runs: int = 0
    runs_display: str = "0"
    runs_share_percent: float | None = None
    tokens: float | None = None
    cost_usd: float | None = None
    color: str


class MetricsOverview(BaseModel):
    """The KPI row and both breakdowns, measured once.

    The three are different views of one measurement -- the current window's
    per-agent rollup -- so a screen that wants all of them asks here and the
    store is asked once, rather than once per panel. The breakdowns arrive
    whole (they are a handful of rows), in the order the donuts draw them.
    """

    summary: MetricsSummary
    models: list[ModelUsageRow] = Field(default_factory=list)
    platforms: list[PlatformUsageRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class MetricsExportDataset(enum.StrEnum):
    """Which table the Export button is pointed at."""

    SERIES = "series"
    MODELS = "models"
    PLATFORMS = "platforms"


class MetricsDailyRow(BaseModel):
    """One row of the exported daily table: the four columns the CSV carries."""

    model_config = ConfigDict(from_attributes=True)

    day: str
    runs: float | None = None
    success_rate: float | None = None
    latency_p50: float | None = None
    latency_p90: float | None = None
    tokens: float | None = None
    cost_usd: float | None = None
