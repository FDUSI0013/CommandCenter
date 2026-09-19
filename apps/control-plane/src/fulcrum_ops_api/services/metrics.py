"""Platform-wide telemetry rollups.

Everything the Metrics screen shows is measured by the telemetry engine and
reshaped here. Three rules hold throughout:

* **Tenancy comes from our database, not from the engine.** The engine is a
  single-tenant service behind this control plane, so the set of telemetry
  namespaces a request may read is derived from the ``agents`` rows of the
  caller's workspace. A project the workspace does not own is never asked for,
  which makes a cross-tenant read impossible rather than merely forbidden.
* **Nothing is invented.** A measure the engine did not report comes back as
  ``None``; it is never zero-filled, interpolated or estimated. If the engine
  cannot be reached the request fails with :class:`TelemetryBackendUnavailable`.
* **Aggregation is explicit.** Counts and money sum; percentiles are combined as
  a runs-weighted mean of the per-project percentile, because the engine exposes
  no cross-project percentile and a weighted mean is the closest honest
  aggregate. Every such rule is stated on the function that applies it.

The window helpers, the project rollups and the row-paginator are also used by
``services.quota``, which needs the same measurements to attribute spend.

**What one page costs the store.** Every number here is an aggregation over the
telemetry store, and the screen asks for the same ones several times over: the
KPI row, the model table and the platform donut all want the current window's
per-project rollup, and four of the six charts want the same per-project run
counts. Asked naively that was 4N+6 simultaneous aggregations for N agents on
every page load, every window flip and every column sort. Three things keep it
in hand, all in the "Engine reads" section below:

* windows that end "now" are snapped to a shared boundary, so requests made a
  moment apart ask the *same* question;
* each engine measurement is remembered for ``metrics_cache_seconds`` and
  computed once however many callers want it (``core.ttlcache``) -- keyed per
  project, so a failed page retries only what failed and the "all agents" and
  single-agent views share what they can;
* at most ``metrics_engine_concurrency`` of them run against the store at once.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Final, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import TelemetryBackendUnavailable, ValidationFailed
from ..core.ttlcache import SingleFlightCache
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineTimeout,
    EngineUnavailable,
    get_engine_client,
)
from ..engine.client import deadline as engine_deadline
from ..models.governance import ApprovalRequest, ApprovalStatus, PolicyViolation
from ..models.registry import Agent
from ..schemas.metrics import (
    DEFAULT_INTERVAL,
    SERIES_PRESENTATION,
    MetricInterval,
    MetricKpi,
    MetricPoint,
    MetricSeries,
    MetricsOverview,
    MetricsSeriesResponse,
    MetricsSummary,
    MetricWindow,
    ModelUsageRow,
    PlatformUsageRow,
    SeriesMetric,
    TrendDirection,
)

SOURCE_SCREEN: Final[str] = "Metrics"

#: Metric vocabulary of the telemetry engine. Kept in one place so the rest of
#: the module speaks in domain terms.
ENGINE_TRACE_COUNT: Final[str] = "TRACE_COUNT"
ENGINE_TOKEN_USAGE: Final[str] = "TOKEN_USAGE"  # noqa: S105 - a metric name
ENGINE_COST: Final[str] = "COST"
ENGINE_DURATION: Final[str] = "DURATION"
#: The engine has no failed-run *count* metric; it reports the failure share of
#: each bucket as a percentage (0–100). Success rate is derived from this plus
#: the run count, never from a count that does not exist.
ENGINE_TRACE_ERROR_RATE: Final[str] = "TRACE_ERROR_RATE"

ENGINE_INTERVAL: Final[dict[MetricInterval, str]] = {
    MetricInterval.HOURLY: "HOURLY",
    MetricInterval.DAILY: "DAILY",
    MetricInterval.WEEKLY: "WEEKLY",
}

#: The engine reports durations in milliseconds; every latency the console
#: renders is in seconds.
MILLISECONDS_PER_SECOND: Final[float] = 1000.0

#: Project-stat pages to walk before giving up. 100 rows a page is the engine's
#: maximum, so this covers 5,000 provisioned agents in one workspace.
MAX_STAT_PAGES: Final[int] = 50
STAT_PAGE_SIZE: Final[int] = 100

#: Governance counts are bucketed in Python from raw timestamps; this caps how
#: many rows one chart may pull so a noisy workspace cannot exhaust memory.
MAX_GOVERNANCE_ROWS: Final[int] = 50_000

#: Palette the donuts and bar charts cycle through, in order.
PALETTE: Final[tuple[str, ...]] = (
    "purple",
    "blue",
    "cyan",
    "amber",
    "green",
    "orange",
    "red",
    "gray",
)

#: Column order of the Metrics CSV, matching the on-screen chart table.
DAILY_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("day", "Day"),
    ("runs", "Runs"),
    ("success_rate", "SuccessRate"),
    ("latency_p50", "LatencyP50"),
    ("latency_p90", "LatencyP90"),
    ("tokens", "Tokens"),
    ("cost_usd", "CostUSD"),
]

MODEL_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("model", "Model"),
    ("agent_count", "Agents"),
    ("runs", "Runs"),
    ("tokens", "Tokens"),
    ("cost_usd", "CostUSD"),
    ("avg_latency_seconds", "AvgLatencySeconds"),
    ("success_rate_percent", "SuccessRatePercent"),
    ("tokens_share_percent", "TokensSharePercent"),
]

PLATFORM_EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("platform", "Platform"),
    ("agent_count", "Agents"),
    ("runs", "Runs"),
    ("runs_share_percent", "RunsSharePercent"),
    ("tokens", "Tokens"),
    ("cost_usd", "CostUSD"),
]


# ---------------------------------------------------------------------------
# Engine failures
# ---------------------------------------------------------------------------


def telemetry_unavailable(exc: EngineError) -> TelemetryBackendUnavailable:
    """Translate an adapter failure into the API's typed 503.

    The adapter's exception classes are internal; the client sees one stable
    error code and never a stack of transport detail. No caller substitutes a
    number for a failed read.
    """
    if isinstance(exc, EngineTimeout):
        message = "The telemetry store did not answer in time. Retry in a moment."
    elif isinstance(exc, EngineUnavailable):
        message = "The telemetry store is unreachable."
    elif isinstance(exc, EngineBadRequest):
        message = "The telemetry store rejected the query for this window."
    else:
        message = "The telemetry store is temporarily unavailable."
    return TelemetryBackendUnavailable(message)


# ---------------------------------------------------------------------------
# Windows and buckets
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Range:
    """A half-open measurement window, and the window immediately before it."""

    start: dt.datetime
    end: dt.datetime
    previous_start: dt.datetime
    previous_end: dt.datetime

    @property
    def span(self) -> dt.timedelta:
        return self.end - self.start


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


#: A window that ends "now" ends on the next of these boundaries instead. A
#: minute is invisible on a day-long window and five are invisible on a week.
WINDOW_SNAP: Final[dt.timedelta] = dt.timedelta(minutes=1)
LONG_WINDOW_SNAP: Final[dt.timedelta] = dt.timedelta(minutes=5)
LONG_WINDOW: Final[dt.timedelta] = dt.timedelta(days=7)


def snap_window_end(moment: dt.datetime, span: dt.timedelta) -> dt.datetime:
    """Round the end of a "to now" window up onto a shared boundary.

    ``now()`` has microsecond precision, so the KPI row, the charts and the
    breakdown tables of one page load each measured a *different* window, a few
    milliseconds apart -- identical numbers, but nothing could be shared between
    them and the store ran every aggregation once per panel. Snapped, they ask
    the same question and one answer serves them all.

    Up, not down: a run that landed a second ago has to be inside the window it
    was asked about. The few minutes of future this admits hold no telemetry.
    """
    step = (LONG_WINDOW_SNAP if span >= LONG_WINDOW else WINDOW_SNAP).total_seconds()
    return dt.datetime.fromtimestamp(math.ceil(moment.timestamp() / step) * step, dt.UTC)


def resolve_window(window: MetricWindow, *, at: dt.datetime | None = None) -> Range:
    """Turn ``30d`` into the two instants it means, plus the comparison window.

    The comparison window is the same span ending where this one starts, so a
    "vs prior 30d" delta compares equal amounts of time. An explicit ``at`` is
    honoured to the microsecond; only "now" is snapped (``snap_window_end``).
    """
    span = dt.timedelta(days=window.days)
    end = at or snap_window_end(now(), span)
    start = end - span
    return Range(start=start, end=end, previous_start=start - span, previous_end=start)


def resolve_window_days(days: int, *, at: dt.datetime | None = None) -> Range:
    """The same window arithmetic for a caller that has a day count, not an enum.

    ``MetricWindow`` covers the four spans the Metrics screen offers; screens
    with their own window control need the arithmetic without the vocabulary.
    """
    span = dt.timedelta(days=max(1, int(days)))
    end = at or snap_window_end(now(), span)
    start = end - span
    return Range(start=start, end=end, previous_start=start - span, previous_end=start)


def month_to_date(*, at: dt.datetime | None = None) -> Range:
    """The current calendar month so far, against the same span last month."""
    moment = at or now()
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # The month is taken from the real instant and only then is the end
    # snapped: in the last minutes of a month the snapped end is the first of
    # the next one, which is where this month stops, not where a new one starts.
    end = at or snap_window_end(moment, moment - start)
    previous_start = (start - dt.timedelta(days=1)).replace(day=1)
    # Same elapsed distance into the previous month, so a mid-month comparison
    # is like-for-like rather than a full month against a partial one.
    previous_end = min(previous_start + (end - start), start)
    return Range(
        start=start, end=end, previous_start=previous_start, previous_end=previous_end
    )


def default_interval(window: MetricWindow) -> MetricInterval:
    return DEFAULT_INTERVAL[window]


def floor_to_bucket(
    moment: dt.datetime, interval: MetricInterval, origin: dt.datetime
) -> dt.datetime:
    """Snap an instant down onto its bucket start.

    Every width is a *calendar* unit in UTC -- the hour, the day, the week from
    Monday 00:00 -- because that is how the engine buckets, and a grid that
    disagrees with the engine's cannot place the engine's points. Weeks used to
    be anchored on the window start instead (``origin``, kept for callers that
    pass it): the engine's first weekly bucket, stamped the Monday *before* a
    window that opens mid-week, then fell off the front of the grid, and every
    later week was credited to whichever window-anchored bucket held its Monday.
    """
    del origin
    moment = moment.replace(tzinfo=dt.UTC) if moment.tzinfo is None else moment.astimezone(dt.UTC)
    if interval is MetricInterval.HOURLY:
        return moment.replace(minute=0, second=0, microsecond=0)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval is MetricInterval.DAILY:
        return midnight
    return midnight - dt.timedelta(days=midnight.weekday())


def bucket_starts(
    start: dt.datetime, end: dt.datetime, interval: MetricInterval
) -> list[dt.datetime]:
    """Every bucket start from ``start`` to ``end``, inclusive of the last partial.

    The first bucket is the calendar bucket ``start`` falls in, so it usually
    opens before the window does and holds only the part of it inside the
    window -- exactly as the engine's first bucket does.
    """
    step = {
        MetricInterval.HOURLY: dt.timedelta(hours=1),
        MetricInterval.DAILY: dt.timedelta(days=1),
        MetricInterval.WEEKLY: dt.timedelta(days=7),
    }[interval]
    cursor = floor_to_bucket(start, interval, start)
    out: list[dt.datetime] = []
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out or [floor_to_bucket(start, interval, start)]


def bucket_label(moment: dt.datetime, interval: MetricInterval) -> str:
    """X-axis label: an hour of the day, or a "Mon D" date."""
    if interval is MetricInterval.HOURLY:
        return moment.strftime("%H:%M")
    return f"{moment.strftime('%b')} {moment.day}"


# ---------------------------------------------------------------------------
# Engine payload normalisation
# ---------------------------------------------------------------------------


def _records(payload: Any) -> list[dict[str, Any]]:
    """Rows out of an engine envelope, whichever collection key it used."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("content", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _number(value: Any) -> float | None:
    """Coerce a reported measure to a float, or ``None`` if it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _dig(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _measure(row: dict[str, Any], *paths: str) -> float | None:
    """First of several spellings of one measure, searched through the row.

    Stat rows arrive either flat or wrapped in a ``metrics``/``stats``/``usage``
    envelope. Aliases are only ever synonyms of the *same* quantity — a p99 is
    never accepted in place of a p90.
    """
    envelopes = [row]
    for key in ("metrics", "stats", "usage"):
        nested = row.get(key)
        if isinstance(nested, dict):
            envelopes.append(nested)
    for path in paths:
        for envelope in envelopes:
            found = _number(_dig(envelope, path))
            if found is not None:
                return found
    return None


def _instant(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def named_series(payload: Any) -> dict[str, list[tuple[dt.datetime, float]]]:
    """Engine series payload as ``{series name: [(instant, value), …]}``.

    The workspace metric endpoints answer with one entry per series name — a
    model, a token-usage key or a percentile — each carrying its own points.
    Rows without a usable instant are dropped rather than guessed at.
    """
    out: dict[str, list[tuple[dt.datetime, float]]] = {}
    for row in _records(payload):
        name = row.get("name") or row.get("key") or row.get("label")
        if not isinstance(name, str):
            continue
        raw = row.get("data") or row.get("points") or []
        points: list[tuple[dt.datetime, float]] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            moment = _instant(
                entry.get("time") or entry.get("timestamp") or entry.get("bucket")
            )
            value = _number(entry.get("value") if "value" in entry else entry.get("count"))
            if moment is not None and value is not None:
                points.append((moment, value))
        if points:
            out.setdefault(name, []).extend(points)
    return out


def _is_total_key(name: str) -> bool:
    return "total" in name.lower()


def collapse_series(
    series: dict[str, list[tuple[dt.datetime, float]]],
) -> list[tuple[dt.datetime, float]]:
    """Reduce a multi-name payload to one line without double counting.

    When the engine reports an explicit total series alongside its components
    (prompt/completion/total tokens, for instance) the total wins; otherwise the
    components are summed.
    """
    if not series:
        return []
    totals = {name: points for name, points in series.items() if _is_total_key(name)}
    chosen = totals or series
    merged: list[tuple[dt.datetime, float]] = []
    for points in chosen.values():
        merged.extend(points)
    return merged


def _match_percentile(
    series: dict[str, list[tuple[dt.datetime, float]]], percentile: str
) -> list[tuple[dt.datetime, float]]:
    """Pick the p50 or p90 line out of a duration payload.

    Nothing is substituted: if the store reports no series for the requested
    percentile the chart is empty, because a neighbouring percentile is a
    different number and presenting it as this one would be a lie.
    """
    for name, points in series.items():
        normalised = name.lower().replace("_", "").replace(".", "").replace("-", "")
        if normalised.endswith(percentile) or normalised == percentile:
            return points
    return []


# ---------------------------------------------------------------------------
# Engine reads: bounded, and shared between the callers that want them
# ---------------------------------------------------------------------------

T = TypeVar("T")

#: One entry per engine measurement -- a project's token total, a project's
#: metric series, a window's stats walk or cost. Values are what the engine
#: reported, normalised; they are shared between requests, so every reader
#: treats them as read-only. Registry attributes (an agent's model, platform,
#: team) are deliberately *not* in here: rollups are rebuilt around the fresh
#: ``AgentProject`` on every request, so an edit shows at once.
_memory: SingleFlightCache[Any] = SingleFlightCache(
    ttl=lambda: settings.metrics_cache_seconds, max_entries=4096
)

_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def forget() -> None:
    """Forget every remembered measurement. For tests and for operators."""
    _memory.invalidate()


def _engine_gate() -> asyncio.Semaphore:
    """This worker's bound on concurrent analytics reads.

    Built on first use, per event loop, rather than at import: a semaphore
    belongs to the loop it first waits on, the application has exactly one, and
    the test suite has one per test.
    """
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Semaphore(max(1, settings.metrics_engine_concurrency)))
    return _gate[1]


async def _bounded(call: Callable[[], Awaitable[T]]) -> T:
    """One engine read, queued behind the worker-wide bound."""
    async with _engine_gate():
        return await call()


async def _shared(
    key: tuple[Any, ...], compute: Callable[[], Awaitable[T]], *, fresh: bool = False
) -> T:
    """``compute()``, or the answer somebody else got for the same key.

    ``fresh`` neither reads nor writes the memory: it is for a caller that
    *persists* what it measures and so must not be handed a remembered number.
    """
    if fresh:
        return await compute()
    return await _memory.get(key, compute)


async def _answered(work: Awaitable[T], *, what: str) -> T:
    """Await one request's engine fan-out under the one-request deadline.

    Bounding concurrency means calls queue, and a queue behind a slow store can
    outlast the console's patience; the deadline makes this API the one that
    answers, with the typed 503. Work already shared through ``_shared`` is not
    lost to it -- it finishes on its own and is there for the retry.
    """
    try:
        async with engine_deadline(what=what):
            return await work
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# Workspace projects and per-project rollups
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgentProject:
    """One agent of the caller's workspace and the namespace it reports into."""

    agent_id: str
    agent_name: str
    platform: str
    model: str | None
    team: str | None
    environment: str
    project_id: str
    project_name: str | None


@dataclasses.dataclass(frozen=True)
class ProjectRollup:
    """What the engine measured for one agent's namespace over one window."""

    project: AgentProject
    runs: int
    errors: float | None
    tokens: float | None
    cost_usd: float | None
    latency_p50: float | None
    latency_p90: float | None


@dataclasses.dataclass(frozen=True)
class Totals:
    """A set of rollups reduced to the numbers the KPI row shows."""

    runs: int
    errors: float | None
    tokens: float | None
    cost_usd: float | None
    latency_p50: float | None
    latency_p90: float | None
    success_rate: float | None


async def workspace_projects(
    session: AsyncSession,
    principal: Principal,
    agent_ids: Sequence[str] | None = None,
) -> list[AgentProject]:
    """The telemetry namespaces this workspace owns, read from our own tables.

    An agent that has never been provisioned has no namespace and is therefore
    invisible to every telemetry read — there is nothing to measure yet.
    """
    rows = (
        (
            await session.execute(
                select(
                    Agent.id,
                    Agent.name,
                    Agent.platform,
                    Agent.model,
                    Agent.team,
                    Agent.environment,
                    Agent.engine_project_id,
                    Agent.engine_project_name,
                )
                .where(
                    Agent.workspace_id == principal.workspace_id,
                    Agent.engine_project_id.is_not(None),
                    # An empty sequence is "no agents", not "every agent": the
                    # caller asked for a set and the set happened to be empty.
                    *(
                        [Agent.id.in_(list(agent_ids))]
                        if agent_ids is not None
                        else []
                    ),
                )
                .order_by(Agent.name.asc())
            )
        )
        .all()
    )
    return [
        AgentProject(
            agent_id=row[0],
            agent_name=row[1],
            platform=row[2],
            model=row[3],
            team=row[4],
            environment=row[5],
            project_id=row[6],
            project_name=row[7],
        )
        for row in rows
    ]


async def _stats_by_project(
    client: EngineClient,
    wanted: set[str],
    start: dt.datetime,
    end: dt.datetime,
    *,
    fresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Walk the engine's project statistics and keep only our namespaces.

    The walk is remembered whole, under the set of namespaces it was for: the
    KPI row, the model table and the platform donut all want exactly this, for
    exactly the same set, within the same second.
    """

    async def walk() -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        page = 1
        while page <= MAX_STAT_PAGES and len(found) < len(wanted):
            payload = await _bounded(
                lambda page=page: client.get_project_stats(
                    page=page, size=STAT_PAGE_SIZE, from_time=start, to_time=end
                )
            )
            rows = _records(payload)
            for row in rows:
                identifier = row.get("id") or row.get("project_id")
                if isinstance(identifier, str) and identifier in wanted:
                    found[identifier] = row
            if len(rows) < STAT_PAGE_SIZE:
                break
            page += 1
        return found

    return await _shared(("project-stats", frozenset(wanted), start, end), walk, fresh=fresh)


def _stat_map(payload: Any) -> dict[str, float]:
    """Flatten a ``{"stats": [{"name": …, "value": …}]}`` reply to numbers.

    Nested values become dotted names — ``duration`` → ``duration.p50`` and
    ``error_count`` → ``error_count.count`` — matching the flat dotted names
    the endpoint already uses for ``usage_sum.total_tokens`` and friends.
    """
    values: dict[str, float] = {}

    def absorb(name: str, value: Any) -> None:
        number = _number(value)
        if number is not None:
            values.setdefault(name, number)
        elif isinstance(value, dict):
            for key, nested in value.items():
                absorb(f"{name}.{str(key).strip().lower()}", nested)

    rows = payload.get("stats") if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            absorb(row["name"].strip().lower(), row.get("value"))
    return values


async def _project_token_totals(
    client: EngineClient,
    projects: Sequence[AgentProject],
    start: dt.datetime,
    end: dt.datetime,
    *,
    fresh: bool = False,
) -> dict[str, float | None]:
    """Total tokens per namespace over the window.

    The project-stats endpoint reports usage only as a per-trace *average*
    (avgMap), which cannot honestly be turned back into a total. The
    trace-stats endpoint reports the actual sums under ``usage_sum.*``, so
    tokens are read there — one call per namespace, a bounded number at a
    time, each remembered under its own namespace.
    """

    async def read(project: AgentProject) -> float | None:
        payload = await _bounded(
            lambda: client.get_trace_stats(
                project_id=project.project_id, from_time=start, to_time=end
            )
        )
        values = _stat_map(payload)
        total = values.get("usage_sum.total_tokens")
        if total is None:
            prompt = values.get("usage_sum.prompt_tokens")
            completion = values.get("usage_sum.completion_tokens")
            if prompt is None and completion is None:
                return None
            total = (prompt or 0.0) + (completion or 0.0)
        return total

    async def one(project: AgentProject) -> tuple[str, float | None]:
        key = ("trace-tokens", project.project_id, start, end)
        return project.project_id, await _shared(key, lambda: read(project), fresh=fresh)

    results = await asyncio.gather(*(one(project) for project in projects))
    return dict(results)


def _stat_runs(row: dict[str, Any]) -> int:
    return int(_measure(row, "trace_count", "traces", "total_traces", "run_count") or 0)


#: The envelope reader under its public name. Other screens page engine rows of
#: their own (spans, for the Connection Center's traffic view) and were reaching
#: for the private spelling, which nothing promised to keep.
records = _records


async def project_run_counts(
    client: EngineClient,
    project_ids: set[str],
    start: dt.datetime,
    end: dt.datetime,
    *,
    fresh: bool = False,
) -> dict[str, int]:
    """Runs per namespace over one window: the statistics walk and nothing else.

    For a caller that wants traffic volume only. ``project_rollups`` would also
    sum tokens, one aggregation per busy namespace, for a number such a caller
    never reads. The walk is the same one the Metrics screen shares, so asking
    here costs nothing when that screen has just asked. A namespace the engine
    has no statistics for counts zero runs: an agent nobody used in the window.
    """
    if not project_ids:
        return {}
    try:
        stats = await _stats_by_project(client, set(project_ids), start, end, fresh=fresh)
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc
    return {
        project_id: _stat_runs(stats.get(project_id, {})) for project_id in project_ids
    }


async def project_rollups(
    client: EngineClient,
    projects: Sequence[AgentProject],
    start: dt.datetime,
    end: dt.datetime,
    *,
    fresh: bool = False,
    tokens: bool = True,
) -> list[ProjectRollup]:
    """Measure every namespace of the workspace over one window.

    A namespace the engine has no statistics for yields a rollup of zero runs
    and unreported measures — that is an agent which has not been used in the
    window, not a failure.

    The engine measurements underneath are shared for ``metrics_cache_seconds``
    with every other caller asking about the same window. Pass ``fresh=True``
    when the result is going to be *stored* -- a budget's rolled-up spend, say
    -- rather than shown: a remembered number is fine on a screen that redraws
    in a minute and wrong in a row that is compared against a threshold.

    ``tokens=False`` is for a caller that reads only what the statistics walk
    reports -- runs, errors, cost, latency. Token totals are the expensive half
    (one aggregation per busy namespace), and the budget roll-up made them on
    every pass for a number it never looked at. Skipped, ``tokens`` is ``None``
    on every rollup: not measured, which is not the same as none.
    """
    if not projects:
        return []
    try:
        stats = await _stats_by_project(
            client, {project.project_id for project in projects}, start, end, fresh=fresh
        )
        # Tokens cost one aggregation per namespace, and a namespace with no
        # runs in the window has no tokens to sum -- so only the ones the stats
        # walk found busy are asked. On a fleet where most agents are idle on
        # any given day that is most of the calls this function used to make.
        tokens_by_project: dict[str, float | None] = {}
        if tokens:
            tokens_by_project = await _project_token_totals(
                client,
                [p for p in projects if _stat_runs(stats.get(p.project_id, {})) > 0],
                start,
                end,
                fresh=fresh,
            )
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc

    rollups: list[ProjectRollup] = []
    for project in projects:
        row = stats.get(project.project_id, {})
        p50 = _measure(row, "duration.p50", "duration_p50", "latency_p50", "p50")
        p90 = _measure(row, "duration.p90", "duration_p90", "latency_p90", "p90")
        rollups.append(
            ProjectRollup(
                project=project,
                runs=_stat_runs(row),
                # The engine reports the failure tally as an object with a
                # deviation attached: {"count": N, "deviation": M}.
                errors=_measure(row, "error_count.count", "error_count", "errors", "failed_count"),
                tokens=tokens_by_project.get(project.project_id),
                # `total_estimated_cost` alone is the per-trace AVERAGE; only
                # the `_sum` spelling is the window's actual spend.
                cost_usd=_measure(
                    row, "total_estimated_cost_sum", "total_cost", "estimated_cost"
                ),
                latency_p50=None if p50 is None else p50 / MILLISECONDS_PER_SECOND,
                latency_p90=None if p90 is None else p90 / MILLISECONDS_PER_SECOND,
            )
        )
    return rollups


def sum_optional(values: Iterable[float | None]) -> float | None:
    """Sum the reported values; ``None`` when nothing reported at all."""
    total: float | None = None
    for value in values:
        if value is None:
            continue
        total = value if total is None else total + value
    return total


def _weighted_percentile(
    rollups: Sequence[ProjectRollup], pick: Callable[[ProjectRollup], float | None]
) -> float | None:
    """Runs-weighted mean of a per-project percentile.

    The engine exposes no percentile across projects, and averaging percentiles
    is an approximation: it is stated as such on the schema field the console
    renders, and it is weighted by run count so a busy agent dominates a quiet
    one exactly as it would in a true cross-project percentile.
    """
    weight = 0.0
    accumulated = 0.0
    for rollup in rollups:
        value = pick(rollup)
        if value is None or rollup.runs <= 0:
            continue
        accumulated += value * rollup.runs
        weight += rollup.runs
    if weight <= 0:
        return None
    return round(accumulated / weight, 3)


def aggregate(rollups: Sequence[ProjectRollup]) -> Totals:
    """Reduce per-project measurements to workspace totals."""
    runs = sum(rollup.runs for rollup in rollups)
    errors = sum_optional(rollup.errors for rollup in rollups)
    success = None
    if runs > 0 and errors is not None:
        success = round(max(0.0, (runs - errors)) / runs * 100, 2)
    return Totals(
        runs=runs,
        errors=errors,
        tokens=sum_optional(rollup.tokens for rollup in rollups),
        cost_usd=sum_optional(rollup.cost_usd for rollup in rollups),
        latency_p50=_weighted_percentile(rollups, lambda r: r.latency_p50),
        latency_p90=_weighted_percentile(rollups, lambda r: r.latency_p90),
        success_rate=success,
    )


async def cost_total(
    client: EngineClient,
    projects: Sequence[AgentProject],
    start: dt.datetime,
    end: dt.datetime,
) -> float | None:
    """Billed spend over a window, from the engine's cost accounting."""
    if not projects:
        return None
    project_ids = [project.project_id for project in projects]
    try:
        payload = await _shared(
            ("cost-summary", frozenset(project_ids), start, end),
            lambda: _bounded(
                lambda: client.get_cost_summary(
                    interval_start=start, interval_end=end, project_ids=project_ids
                )
            ),
        )
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc
    if not isinstance(payload, dict):
        return None
    # The engine answers {"name": "cost", "current": X, "previous": Y}; the
    # other spellings are kept for older payload shapes.
    return _measure(
        payload, "current", "total", "total_cost", "cost", "total_estimated_cost", "value"
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def compact(value: float | None, *, suffix: str = "") -> str:
    """1_420_000_000 → "1.42B". Used for every count the cards render."""
    if value is None:
        return "—"
    magnitude = abs(value)
    for cut, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= cut:
            scaled = value / cut
            text = f"{scaled:.2f}" if magnitude < cut * 10 else f"{scaled:.1f}"
            return f"{text.rstrip('0').rstrip('.')}{unit}{suffix}"
    if float(value).is_integer():
        return f"{int(value):,}{suffix}"
    return f"{value:,.2f}{suffix}"


def money(value: float | None, *, decimals: int = 0) -> str:
    """Money, at a precision that cannot hide the amount.

    Per-run LLM spend is routinely a fraction of a cent. Rendering $0.0000101
    as "$0" reads as "this cost nothing", which is a different claim from
    "this cost very little" — so when the requested precision would round a
    non-zero amount away, it widens until two significant digits survive. A
    measured zero still prints as zero.
    """
    if value is None:
        return "—"
    if value != 0 and abs(value) < 10**-decimals:
        decimals = min(10, math.ceil(-math.log10(abs(value))) + 1)
    return f"${value:,.{decimals}f}"


def percent(value: float | None, *, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}%"


def seconds(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}s"


def share(part: float | None, whole: float | None) -> float | None:
    """Percentage of a total, or ``None`` when there is no total to share."""
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _direction(delta: float | None) -> TrendDirection:
    if delta is None or abs(delta) < 1e-9:
        return TrendDirection.FLAT
    return TrendDirection.UP if delta > 0 else TrendDirection.DOWN


def _verdict(direction: TrendDirection, higher_is_better: bool) -> bool | None:
    if direction is TrendDirection.FLAT:
        return None
    return higher_is_better if direction is TrendDirection.UP else not higher_is_better


def relative_delta(current: float | None, previous: float | None) -> float | None:
    """Percentage change; ``None`` when there is no baseline to compare against."""
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def absolute_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 3)


def build_kpi(
    *,
    key: str,
    label: str,
    value: float | None,
    previous: float | None,
    unit: str,
    icon: str,
    color: str,
    comparison: str,
    higher_is_better: bool,
    display: str,
    delta: float | None,
    delta_display: str | None,
    sub: str | None = None,
) -> MetricKpi:
    """Assemble one card. Direction and verdict are derived, never passed in."""
    direction = _direction(delta)
    return MetricKpi(
        key=key,
        label=label,
        value=value,
        display=display,
        unit=unit,
        sub=sub,
        previous=previous,
        delta=delta,
        delta_display=None if delta is None else delta_display,
        direction=direction,
        good=_verdict(direction, higher_is_better),
        comparison=comparison,
        icon=icon,
        color=color,
    )


# ---------------------------------------------------------------------------
# In-memory paging for rows computed outside SQL
# ---------------------------------------------------------------------------


def filter_rows(
    rows: Sequence[dict[str, Any]],
    params: ListParams,
    *,
    sortable: Sequence[str],
    default_key: str,
    search_keys: Sequence[str],
    default_desc: bool = True,
) -> list[dict[str, Any]]:
    """Search and sort rows that were computed rather than queried.

    Telemetry rollups are assembled in this process, so the standard SQL helpers
    cannot be used; the query contract the console relies on — ``q`` and
    ``sort`` — is honoured identically, including the 422 raised for an
    unsortable column. Exports call this directly, which is what makes a CSV
    cover every filtered row rather than the page the table happens to show.
    """
    working = list(rows)

    if params.q:
        needle = params.q.strip().lower()
        working = [
            row
            for row in working
            if any(needle in str(row.get(key) or "").lower() for key in search_keys)
        ]

    key = params.sort_key or default_key
    if key not in sortable:
        raise ValidationFailed(
            f"Cannot sort by '{key}'.", details={"sortable": sorted(sortable)}
        )
    descending = params.descending if params.sort_key else default_desc

    def _order(row: dict[str, Any]) -> tuple[int, float, str]:
        value = row.get(key)
        if value is None:
            # Unreported values sort last in both directions: absence is not a
            # small number, and it should never head the table.
            return (1 if not descending else 0, 0.0, "")
        if isinstance(value, bool):
            return (0 if not descending else 1, float(value), "")
        if isinstance(value, (int, float)):
            return (0 if not descending else 1, float(value), "")
        return (0 if not descending else 1, 0.0, str(value).lower())

    working.sort(key=_order, reverse=descending)
    return working


def paginate_rows(
    rows: Sequence[dict[str, Any]],
    params: ListParams,
    *,
    sortable: Sequence[str],
    default_key: str,
    search_keys: Sequence[str],
    default_desc: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """One page of computed rows, plus the total the filter matched."""
    working = filter_rows(
        rows,
        params,
        sortable=sortable,
        default_key=default_key,
        search_keys=search_keys,
        default_desc=default_desc,
    )
    start = params.offset
    return working[start : start + params.page_size], len(working)


# ---------------------------------------------------------------------------
# KPI summary
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Measured:
    """Both windows of one request, measured: what every KPI is derived from."""

    span: Range
    projects: list[AgentProject]
    current: list[ProjectRollup]
    previous: list[ProjectRollup]
    cost_now: float | None
    cost_before: float | None


async def _measure_windows(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None,
) -> _Measured:
    client = get_engine_client()
    span = resolve_window(window)
    projects = await workspace_projects(session, principal, agent_ids)
    current, previous, cost_now, cost_before = await _answered(
        asyncio.gather(
            project_rollups(client, projects, span.start, span.end),
            project_rollups(client, projects, span.previous_start, span.previous_end),
            cost_total(client, projects, span.start, span.end),
            cost_total(client, projects, span.previous_start, span.previous_end),
        ),
        what="the metrics summary",
    )
    return _Measured(
        span=span,
        projects=projects,
        current=current,
        previous=previous,
        cost_now=cost_now,
        cost_before=cost_before,
    )


async def summarise(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None = None,
) -> MetricsSummary:
    """The six KPI cards, each measured twice so the delta is real.

    The current and comparison windows are measured with the same query against
    the same namespaces, so a delta reflects a change in behaviour rather than a
    change in what was counted.
    """
    return _summary(window, await _measure_windows(session, principal, window, agent_ids))


async def overview(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None = None,
) -> MetricsOverview:
    """The KPI row, the model table and the platform donut from one measurement.

    All three are views of the current window's per-agent rollup. Served apart,
    each route measures it again -- and the shared memory only helps when the
    three requests happen to reach the same worker. Served together it is
    measured once by construction.
    """
    measured = await _measure_windows(session, principal, window, agent_ids)
    return MetricsOverview(
        summary=_summary(window, measured),
        models=to_model_rows(_model_rows(measured.current)),
        platforms=to_platform_rows(_platform_rows(measured.current)),
    )


def _summary(window: MetricWindow, measured: _Measured) -> MetricsSummary:
    """Shape two measured windows into the six cards."""
    span, projects = measured.span, measured.projects
    current, previous = measured.current, measured.previous
    cost_now, cost_before = measured.cost_now, measured.cost_before
    head = aggregate(current)
    tail = aggregate(previous)
    caption = window.comparison_label

    runs_delta = relative_delta(float(head.runs), float(tail.runs))
    success_delta = absolute_delta(head.success_rate, tail.success_rate)
    p50_delta = absolute_delta(head.latency_p50, tail.latency_p50)
    p90_delta = absolute_delta(head.latency_p90, tail.latency_p90)
    token_delta = relative_delta(head.tokens, tail.tokens)
    cost_delta = relative_delta(cost_now, cost_before)

    return MetricsSummary(
        window=window,
        window_label=window.label,
        period_start=span.start,
        period_end=span.end,
        previous_period_start=span.previous_start,
        previous_period_end=span.previous_end,
        agent_count=len(projects),
        total_runs=build_kpi(
            key="total_runs",
            label=f"Total Runs ({window.value})",
            value=float(head.runs),
            previous=float(tail.runs),
            unit="runs",
            icon="activity",
            color="purple",
            comparison=caption,
            higher_is_better=True,
            display=compact(float(head.runs)),
            delta=runs_delta,
            delta_display=None if runs_delta is None else f"{abs(runs_delta):.1f}%",
        ),
        success_rate=build_kpi(
            key="success_rate",
            label="Success Rate",
            value=head.success_rate,
            previous=tail.success_rate,
            unit="percent",
            icon="target",
            color="green",
            comparison=caption,
            higher_is_better=True,
            display=percent(head.success_rate),
            delta=success_delta,
            delta_display=None if success_delta is None else f"{abs(success_delta):.1f} pp",
        ),
        latency_p50=build_kpi(
            key="latency_p50",
            label="Avg Latency (p50)",
            value=head.latency_p50,
            previous=tail.latency_p50,
            unit="seconds",
            icon="clock",
            color="amber",
            comparison=caption,
            higher_is_better=False,
            display=seconds(head.latency_p50),
            delta=p50_delta,
            delta_display=None if p50_delta is None else f"{abs(p50_delta):.2f}s",
        ),
        latency_p90=build_kpi(
            key="latency_p90",
            label="Latency (p90)",
            value=head.latency_p90,
            previous=tail.latency_p90,
            unit="seconds",
            icon="clock",
            color="orange",
            comparison=caption,
            higher_is_better=False,
            display=seconds(head.latency_p90),
            delta=p90_delta,
            delta_display=None if p90_delta is None else f"{abs(p90_delta):.2f}s",
        ),
        tokens=build_kpi(
            key="tokens",
            label=f"Tokens ({window.value})",
            value=head.tokens,
            previous=tail.tokens,
            unit="tokens",
            icon="layers",
            color="blue",
            comparison=caption,
            higher_is_better=False,
            display=compact(head.tokens),
            delta=token_delta,
            delta_display=None if token_delta is None else f"{abs(token_delta):.1f}%",
        ),
        cost=build_kpi(
            key="cost",
            label=f"Cost ({window.value})",
            value=cost_now,
            previous=cost_before,
            unit="usd",
            icon="dollar",
            color="cyan",
            comparison=caption,
            higher_is_better=False,
            # Cents matter here: rounding measured spend to "$0" reads as
            # "nothing was spent". `money` widens precision on its own for
            # anything smaller than the requested rounding.
            display=money(cost_now, decimals=2),
            delta=cost_delta,
            delta_display=None if cost_delta is None else f"{abs(cost_delta):.1f}%",
        ),
    )


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------


def fold_sum(
    points: Iterable[tuple[dt.datetime, float]],
    starts: Sequence[dt.datetime],
    interval: MetricInterval,
) -> list[float | None]:
    """Sum measured points into the shared bucket grid."""
    slots: list[float | None] = [None] * len(starts)
    for position, value in _placed(points, starts, interval):
        slots[position] = value if slots[position] is None else (slots[position] or 0.0) + value
    return slots


def _placed(
    points: Iterable[tuple[dt.datetime, float]],
    starts: Sequence[dt.datetime],
    interval: MetricInterval,
) -> Iterable[tuple[int, float]]:
    """Each measured point with the position of the bucket it belongs to.

    A point stamped before the grid opens is credited to the first bucket, not
    discarded. Every read is already limited to the window, so such a point is
    in-window data carrying a coarser stamp than the grid, and dropping it made
    a chart's total fall short of the KPI card above it without a word. Nothing
    is credited backwards from beyond the last bucket: the engine pads the far
    end of a series with empty buckets, and those are not measurements.
    """
    if not starts:
        return
    index = {start: position for position, start in enumerate(starts)}
    for moment, value in points:
        floored = floor_to_bucket(moment, interval, starts[0])
        position = 0 if floored < starts[0] else index.get(floored)
        if position is not None:
            yield position, value


def fold_mean(
    points: Iterable[tuple[dt.datetime, float]],
    starts: Sequence[dt.datetime],
    interval: MetricInterval,
) -> list[float | None]:
    """Average points into buckets — the right reduction for a latency line."""
    sums: list[float] = [0.0] * len(starts)
    counts: list[int] = [0] * len(starts)
    for position, value in _placed(points, starts, interval):
        sums[position] += value
        counts[position] += 1
    return [
        round(sums[i] / counts[i], 3) if counts[i] else None for i in range(len(starts))
    ]


#: The workspace-wide metrics endpoint answers exactly one metric: token usage
#: aggregated over spans. Every other series has to be read per project.
#: Asking it for anything else is a 400, not an empty result.
WORKSPACE_METRIC: Final[str] = "SPAN_TOKEN_USAGE"  # noqa: S105 - a metric name


async def engine_series(
    client: EngineClient,
    projects: Sequence[AgentProject],
    metric_type: str,
    interval: MetricInterval,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, list[tuple[dt.datetime, float]]]:
    """A named series per metric, read from whichever endpoint can answer it.

    Token usage is the only metric the workspace-wide endpoint serves, and it
    serves it for every project in one call. Trace counts, cost, duration and
    error counts are project-scoped, so they are fanned out across the
    workspace's projects and summed per interval — which is what "across the
    workspace" means for those metrics anyway.
    """
    if not projects:
        return {}

    if metric_type == ENGINE_TOKEN_USAGE:
        project_ids = [project.project_id for project in projects]

        async def usage() -> dict[str, list[tuple[dt.datetime, float]]]:
            payload = await _bounded(
                lambda: client.get_workspace_usage(
                    metric_type=WORKSPACE_METRIC,
                    interval=ENGINE_INTERVAL[interval],
                    interval_start=start,
                    interval_end=end,
                    project_ids=project_ids,
                )
            )
            return named_series(payload)

        key = ("workspace-usage", frozenset(project_ids), ENGINE_INTERVAL[interval], start, end)
        try:
            return await _shared(key, usage)
        except EngineError as exc:
            raise telemetry_unavailable(exc) from exc

    per_project = await _per_project_series(client, projects, metric_type, interval, start, end)

    merged: dict[str, dict[dt.datetime, float]] = {}
    for series in per_project:
        for name, points in series.items():
            bucket = merged.setdefault(name, {})
            for at, value in points:
                bucket[at] = bucket.get(at, 0.0) + value
    return {name: sorted(points.items()) for name, points in merged.items()}


async def _per_project_series(
    client: EngineClient,
    projects: Sequence[AgentProject],
    metric_type: str,
    interval: MetricInterval,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, list[tuple[dt.datetime, float]]]]:
    """One named-series payload per project, kept apart.

    ``engine_series`` sums the projects together, which is the right reduction
    for counts and the wrong one for rates and percentiles — those have to be
    weighted by each project's traffic before they may be combined.

    Each project's payload is remembered on its own, so the runs chart, the
    success-rate chart and both latency lines -- which all need the run counts
    -- read them once between them, whichever request gets there first.
    """

    async def read(project: AgentProject) -> dict[str, list[tuple[dt.datetime, float]]]:
        payload = await _bounded(
            lambda: client.get_project_metrics(
                project.project_id,
                metric_type=metric_type,
                interval=ENGINE_INTERVAL[interval],
                interval_start=start,
                interval_end=end,
            )
        )
        return named_series(payload)

    async def one(project: AgentProject) -> dict[str, list[tuple[dt.datetime, float]]]:
        key = (
            "project-series",
            project.project_id,
            metric_type,
            ENGINE_INTERVAL[interval],
            start,
            end,
        )
        return await _shared(key, lambda: read(project))

    try:
        return list(await asyncio.gather(*(one(project) for project in projects)))
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc


PerProject = list[dict[str, list[tuple[dt.datetime, float]]]]


class _SeriesReads:
    """One request's per-project reads, each made once however many lines use it.

    Four of the six engine lines are derived from the per-project run counts --
    runs sums them, success rate and both latency lines weight by them -- and
    the two latency lines come out of the *same* duration payload. Resolved
    line by line, the six-line export read the run counts four times and the
    durations twice: 7N+2 aggregations for N agents, 5N of them repeats.

    Reads start on first use, so a request for cost or violations alone reads
    nothing per project.
    """

    def __init__(
        self,
        client: EngineClient,
        projects: Sequence[AgentProject],
        interval: MetricInterval,
        span: Range,
    ) -> None:
        self.client = client
        self.projects = projects
        self.interval = interval
        self.span = span
        self._reads: dict[str, asyncio.Task[PerProject]] = {}

    def per_project(self, metric_type: str) -> asyncio.Task[PerProject]:
        read = self._reads.get(metric_type)
        if read is None:
            read = asyncio.ensure_future(
                _per_project_series(
                    self.client,
                    self.projects,
                    metric_type,
                    self.interval,
                    self.span.start,
                    self.span.end,
                )
            )
            # Several lines await this one task; whichever fails first ends the
            # request, and the outcome must not then be reported as unobserved.
            read.add_done_callback(lambda done: done.cancelled() or done.exception())
            self._reads[metric_type] = read
        return read


def _sum_lines(lines: Sequence[Sequence[float | None]], size: int) -> list[float | None]:
    """Bucket-wise sum of several folded lines; a bucket nobody reported stays empty."""
    return [sum_optional(line[position] for line in lines) for position in range(size)]


async def _run_count_values(
    reads: _SeriesReads, starts: Sequence[dt.datetime]
) -> list[float | None]:
    """Runs per bucket: each project's count line, folded, then summed."""
    counts = await reads.per_project(ENGINE_TRACE_COUNT)
    return _sum_lines(
        [fold_sum(collapse_series(series), starts, reads.interval) for series in counts],
        len(starts),
    )


async def _success_rate_values(
    reads: _SeriesReads, starts: Sequence[dt.datetime]
) -> list[float | None]:
    """Per-bucket success rate across the workspace.

    The engine reports failures as a share of each bucket (``TRACE_ERROR_RATE``,
    0–100) rather than a count, so each project's failed runs are reconstructed
    — rate × runs — and only then summed. A bucket where a project reports runs
    but no rate is unknown, not perfect.
    """
    interval = reads.interval
    if not reads.projects:
        return [None] * len(starts)
    counts, rates = await asyncio.gather(
        reads.per_project(ENGINE_TRACE_COUNT), reads.per_project(ENGINE_TRACE_ERROR_RATE)
    )
    totals = [0.0] * len(starts)
    failed = [0.0] * len(starts)
    unknown = [False] * len(starts)
    for count_series, rate_series in zip(counts, rates, strict=True):
        bucket_counts = fold_sum(collapse_series(count_series), starts, interval)
        bucket_rates = fold_mean(collapse_series(rate_series), starts, interval)
        for position, count in enumerate(bucket_counts):
            if not count:
                continue
            totals[position] += count
            rate = bucket_rates[position]
            if rate is None:
                unknown[position] = True
            else:
                failed[position] += count * rate / 100.0
    return [
        None
        if not totals[position] or unknown[position]
        else round(max(0.0, totals[position] - failed[position]) / totals[position] * 100, 2)
        for position in range(len(starts))
    ]


async def _latency_percentile_values(
    reads: _SeriesReads, wanted: str, starts: Sequence[dt.datetime]
) -> list[float | None]:
    """Runs-weighted per-bucket percentile across the workspace, in seconds.

    Summing percentiles across projects is meaningless; as with the KPI row's
    ``_weighted_percentile``, each project's line is weighted by its run count,
    which is exact for one project and a stated approximation for several.

    The duration payload carries every percentile the engine computes, so the
    p50 and the p90 line are two picks from one read.
    """
    interval = reads.interval
    if not reads.projects:
        return [None] * len(starts)
    counts, durations = await asyncio.gather(
        reads.per_project(ENGINE_TRACE_COUNT), reads.per_project(ENGINE_DURATION)
    )
    weights = [0.0] * len(starts)
    accumulated = [0.0] * len(starts)
    for count_series, duration_series in zip(counts, durations, strict=True):
        bucket_counts = fold_sum(collapse_series(count_series), starts, interval)
        bucket_values = fold_mean(_match_percentile(duration_series, wanted), starts, interval)
        for position, count in enumerate(bucket_counts):
            value = bucket_values[position]
            if not count or value is None:
                continue
            accumulated[position] += value * count
            weights[position] += count
    return [
        None
        if weights[position] <= 0
        else round(accumulated[position] / weights[position] / MILLISECONDS_PER_SECOND, 3)
        for position in range(len(starts))
    ]


async def cost_points(
    client: EngineClient,
    projects: Sequence[AgentProject],
    start: dt.datetime,
    end: dt.datetime,
) -> list[tuple[dt.datetime, float]]:
    if not projects:
        return []
    project_ids = [project.project_id for project in projects]
    try:
        payload = await _shared(
            ("cost-series", frozenset(project_ids), start, end),
            lambda: _bounded(
                lambda: client.get_cost_series(
                    interval_start=start, interval_end=end, project_ids=project_ids
                )
            ),
        )
    except EngineError as exc:
        raise telemetry_unavailable(exc) from exc
    return collapse_series(named_series(payload))


async def _governance_points(
    session: AsyncSession,
    principal: Principal,
    metric: SeriesMetric,
    start: dt.datetime,
    end: dt.datetime,
) -> list[tuple[dt.datetime, float]]:
    """Violation and escalation timestamps out of our own governance tables.

    Escalations are bucketed by the instant the request was raised: the row
    records no separate escalation timestamp, and bucketing on ``updated_at``
    would move a request every time a comment landed on it.
    """
    if metric is SeriesMetric.VIOLATIONS:
        stmt = (
            select(PolicyViolation.occurred_at)
            .where(
                PolicyViolation.workspace_id == principal.workspace_id,
                PolicyViolation.occurred_at >= start,
                PolicyViolation.occurred_at < end,
            )
            .limit(MAX_GOVERNANCE_ROWS)
        )
    else:
        stmt = (
            select(ApprovalRequest.requested_at)
            .where(
                ApprovalRequest.workspace_id == principal.workspace_id,
                ApprovalRequest.status == ApprovalStatus.ESCALATED.value,
                ApprovalRequest.requested_at >= start,
                ApprovalRequest.requested_at < end,
            )
            .limit(MAX_GOVERNANCE_ROWS)
        )
    stamps = (await session.execute(stmt)).scalars().all()
    return [(stamp, 1.0) for stamp in stamps if stamp is not None]


GOVERNANCE_METRICS: Final[tuple[SeriesMetric, ...]] = (
    SeriesMetric.VIOLATIONS,
    SeriesMetric.ESCALATIONS,
)


async def _engine_line(
    reads: _SeriesReads, metric: SeriesMetric, starts: Sequence[dt.datetime]
) -> list[float | None]:
    """Resolve one engine-measured metric onto the shared bucket grid."""
    client, projects, interval, span = reads.client, reads.projects, reads.interval, reads.span

    if metric is SeriesMetric.COST:
        if interval is MetricInterval.HOURLY:
            # The workspace cost endpoint takes no interval and answers in
            # *days*. Folded onto an hourly grid that was one spike at 00:00
            # carrying the whole of today, and yesterday evening's share of the
            # window gone altogether. The per-project COST series does honour
            # HOURLY, and it is the same money: a trace's cost is the sum of
            # its spans', which is what the workspace endpoint adds up.
            costs = await reads.per_project(ENGINE_COST)
            return _sum_lines(
                [fold_sum(collapse_series(series), starts, interval) for series in costs],
                len(starts),
            )
        return fold_sum(await cost_points(client, projects, span.start, span.end), starts, interval)

    if metric is SeriesMetric.TOKENS:
        series = await engine_series(
            client, projects, ENGINE_TOKEN_USAGE, interval, span.start, span.end
        )
        return fold_sum(collapse_series(series), starts, interval)

    if metric is SeriesMetric.RUNS:
        return await _run_count_values(reads, starts)

    if metric in (SeriesMetric.LATENCY_P50, SeriesMetric.LATENCY_P90):
        wanted = "p50" if metric is SeriesMetric.LATENCY_P50 else "p90"
        return await _latency_percentile_values(reads, wanted, starts)

    return await _success_rate_values(reads, starts)


async def series(
    session: AsyncSession,
    principal: Principal,
    *,
    window: MetricWindow,
    interval: MetricInterval | None = None,
    metrics: Sequence[SeriesMetric],
    agent_ids: Sequence[str] | None = None,
) -> MetricsSeriesResponse:
    """Every requested line, on one shared bucket grid.

    The grid is generated here rather than taken from the engine so lines from
    different sources — engine counts, engine costs, our governance tables —
    land on identical x positions and can share one chart axis.
    """
    client = get_engine_client()
    span = resolve_window(window)
    grid_interval = interval or default_interval(window)
    starts = bucket_starts(span.start, span.end, grid_interval)
    projects = await workspace_projects(session, principal, agent_ids)

    # Our own tables first, one statement at a time and outside the engine
    # deadline: a session runs one statement at once, and a statement cancelled
    # half way costs the request its connection.
    governance: dict[SeriesMetric, list[float | None]] = {}
    for metric in metrics:
        if metric in GOVERNANCE_METRICS and metric not in governance:
            points = await _governance_points(session, principal, metric, span.start, span.end)
            # A governance chart counts events, so an empty bucket is a
            # measured zero rather than an absent measurement.
            folded = fold_sum(points, starts, grid_interval)
            governance[metric] = [0.0 if value is None else value for value in folded]

    reads = _SeriesReads(client, projects, grid_interval, span)
    measured = list(
        dict.fromkeys(metric for metric in metrics if metric not in GOVERNANCE_METRICS)
    )
    engine_lines = dict(
        zip(
            measured,
            await _answered(
                asyncio.gather(*(_engine_line(reads, metric, starts) for metric in measured)),
                what="the metrics charts",
            ),
            strict=True,
        )
    )
    resolved = [
        governance[metric] if metric in GOVERNANCE_METRICS else engine_lines[metric]
        for metric in metrics
    ]

    lines: list[MetricSeries] = []
    for metric, values in zip(metrics, resolved, strict=True):
        label, unit, color = SERIES_PRESENTATION[metric]
        reported = [value for value in values if value is not None]
        cumulative = metric not in (
            SeriesMetric.SUCCESS_RATE,
            SeriesMetric.LATENCY_P50,
            SeriesMetric.LATENCY_P90,
        )
        lines.append(
            MetricSeries(
                metric=metric,
                label=label,
                unit=unit,
                color=color,
                points=[
                    MetricPoint(
                        timestamp=start,
                        label=bucket_label(start, grid_interval),
                        value=value,
                    )
                    for start, value in zip(starts, values, strict=True)
                ],
                total=round(sum(reported), 3) if (reported and cumulative) else None,
                average=round(sum(reported) / len(reported), 3) if reported else None,
            )
        )

    return MetricsSeriesResponse(
        window=window,
        interval=grid_interval,
        period_start=span.start,
        period_end=span.end,
        labels=[bucket_label(start, grid_interval) for start in starts],
        series=lines,
    )


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------


def group_rollups(
    rollups: Sequence[ProjectRollup], key: Callable[[AgentProject], str | None], *, fallback: str
) -> dict[str, list[ProjectRollup]]:
    """Bucket per-project measurements by an attribute of the agent."""
    grouped: dict[str, list[ProjectRollup]] = {}
    for rollup in rollups:
        name = key(rollup.project) or fallback
        grouped.setdefault(name, []).append(rollup)
    return grouped


async def model_rows(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-model rollup for the Top Models table and the usage donut.

    A "model" here is the model an agent is *configured* with, so the row is the
    sum of the telemetry of every agent running it. That is the only attribution
    available without asking the engine to group by a span attribute, and it is
    exact for the overwhelmingly common case of one model per agent.
    """
    return _model_rows(await _current_rollups(session, principal, window, agent_ids))


async def _current_rollups(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None,
) -> list[ProjectRollup]:
    """The current window's per-agent rollup, which both breakdowns group."""
    client = get_engine_client()
    span = resolve_window(window)
    projects = await workspace_projects(session, principal, agent_ids)
    return await _answered(
        project_rollups(client, projects, span.start, span.end),
        what="the metrics breakdown",
    )


def _model_rows(rollups: Sequence[ProjectRollup]) -> list[dict[str, Any]]:
    overall = aggregate(rollups)

    rows: list[dict[str, Any]] = []
    grouped = group_rollups(rollups, lambda project: project.model, fallback="Unspecified")
    for model, group in grouped.items():
        totals = aggregate(group)
        rows.append(
            {
                "model": model,
                "agent_count": len(group),
                "runs": totals.runs,
                "tokens": totals.tokens,
                "tokens_display": compact(totals.tokens),
                "cost_usd": totals.cost_usd,
                "cost_display": money(totals.cost_usd, decimals=2),
                "avg_latency_seconds": totals.latency_p50,
                "success_rate_percent": totals.success_rate,
                "tokens_share_percent": share(totals.tokens, overall.tokens),
                "cost_share_percent": share(totals.cost_usd, overall.cost_usd),
                "runs_share_percent": share(float(totals.runs), float(overall.runs)),
                "color": PALETTE[0],
            }
        )
    # Heaviest consumer first: the donut and the bar chart are both read
    # top-down, and the colour follows that order so the two agree.
    rows.sort(key=lambda row: (row["tokens"] is None, -(row["tokens"] or 0.0)))
    for position, row in enumerate(rows):
        row["color"] = PALETTE[position % len(PALETTE)]
    return rows


async def platform_rows(
    session: AsyncSession,
    principal: Principal,
    window: MetricWindow,
    agent_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Runs by platform, for the donut beside the model table."""
    return _platform_rows(await _current_rollups(session, principal, window, agent_ids))


def _platform_rows(rollups: Sequence[ProjectRollup]) -> list[dict[str, Any]]:
    overall = aggregate(rollups)

    rows: list[dict[str, Any]] = []
    grouped = group_rollups(rollups, lambda project: project.platform, fallback="Unassigned")
    for platform, group in grouped.items():
        totals = aggregate(group)
        rows.append(
            {
                "platform": platform,
                "agent_count": len(group),
                "runs": totals.runs,
                "runs_display": compact(float(totals.runs)),
                "runs_share_percent": share(float(totals.runs), float(overall.runs)),
                "tokens": totals.tokens,
                "cost_usd": totals.cost_usd,
                "color": PALETTE[0],
            }
        )
    rows.sort(key=lambda row: -row["runs"])
    for position, row in enumerate(rows):
        row["color"] = PALETTE[position % len(PALETTE)]
    return rows


def to_model_rows(rows: Sequence[dict[str, Any]]) -> list[ModelUsageRow]:
    return [ModelUsageRow.model_validate(row) for row in rows]


def to_platform_rows(rows: Sequence[dict[str, Any]]) -> list[PlatformUsageRow]:
    return [PlatformUsageRow.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def daily_rows(
    session: AsyncSession,
    principal: Principal,
    *,
    window: MetricWindow,
    interval: MetricInterval | None = None,
    agent_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """The charted series transposed into one row per bucket, for the CSV."""
    payload = await series(
        session,
        principal,
        window=window,
        interval=interval,
        agent_ids=agent_ids,
        metrics=[
            SeriesMetric.RUNS,
            SeriesMetric.SUCCESS_RATE,
            SeriesMetric.LATENCY_P50,
            SeriesMetric.LATENCY_P90,
            SeriesMetric.TOKENS,
            SeriesMetric.COST,
        ],
    )
    columns = {line.metric: line.points for line in payload.series}
    rows: list[dict[str, Any]] = []
    for position, label in enumerate(payload.labels):
        rows.append(
            {
                "day": label,
                "runs": columns[SeriesMetric.RUNS][position].value,
                "success_rate": columns[SeriesMetric.SUCCESS_RATE][position].value,
                "latency_p50": columns[SeriesMetric.LATENCY_P50][position].value,
                "latency_p90": columns[SeriesMetric.LATENCY_P90][position].value,
                "tokens": columns[SeriesMetric.TOKENS][position].value,
                "cost_usd": columns[SeriesMetric.COST][position].value,
            }
        )
    return rows
