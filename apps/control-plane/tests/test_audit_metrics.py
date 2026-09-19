"""Regressions for the 2026-09-18 audit of the Metrics rollups.

Two kinds of defect are pinned here.

**What one page costs the store.** A Metrics page load put 4N+6 simultaneous
aggregations on the telemetry store for N agents, computed the same per-agent
rollup three times, and did it all again for every column sort. The tests count
the engine's traffic -- the double records every call -- rather than timing
anything, so they say exactly which read came back.

**Charts that lost data.** The 90-day (weekly) charts dropped the engine's first
calendar week, and the 24-hour cost chart folded the engine's *daily* cost
points onto an hourly grid. Those tests compare a chart's total with the KPI
card that sits above it, which is the reconciliation an operator does by eye.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from conftest import error_code, principal_for
from engine_double import EngineDouble
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.services import metrics as service

#: The reads that are aggregations over the store, by path fragment.
STATS = "/projects/stats"
TOKENS = "/traces/stats"
COSTS = "/workspaces/costs"


class SlowEngine(EngineDouble):
    """The double, able to take its time and to say how busy it was.

    ``delay`` holds every request open for that long, which is what makes
    concurrency observable: ``peak`` is the most requests that were ever inside
    the engine at once.
    """

    def __init__(self) -> None:
        super().__init__()
        self.delay = 0.0
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.delay:
            await super().__call__(scope, receive, send)
            return
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            await super().__call__(scope, receive, send)
        finally:
            self.in_flight -= 1


@pytest.fixture
def engine() -> SlowEngine:
    return SlowEngine()


@pytest.fixture
def remembered(monkeypatch):
    """Switch the shared measurements back on, as production has them."""
    monkeypatch.setattr(settings, "metrics_cache_seconds", 60.0)
    service.forget()
    yield
    service.forget()


def run(engine, agent, *, at=None, seconds=1.0, tokens=100, cost=0.01, failed=False):
    """One finished run in an agent's namespace."""
    started = at or dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)
    return engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=started,
        end_time=started + dt.timedelta(seconds=seconds),
        usage={"total_tokens": tokens},
        total_estimated_cost=cost,
        **({"error_info": {"message": "boom"}} if failed else {}),
    )


async def fleet(factory, workspace, engine, count, **fields):
    return [
        await factory.provisioned_agent(workspace, engine, name=f"Agent {n}", **fields)
        for n in range(count)
    ]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_a_window_that_ends_now_ends_on_a_boundary_every_panel_shares():
    """Microsecond-precision ends meant no two panels measured the same window."""
    moment = dt.datetime(2026, 9, 17, 14, 37, 12, 345678, tzinfo=dt.UTC)

    day = service.snap_window_end(moment, dt.timedelta(days=1))
    month = service.snap_window_end(moment, dt.timedelta(days=30))

    assert day == dt.datetime(2026, 9, 17, 14, 38, tzinfo=dt.UTC)
    assert month == dt.datetime(2026, 9, 17, 14, 40, tzinfo=dt.UTC)
    # Already on a boundary: left alone, not pushed a whole step on.
    assert service.snap_window_end(day, dt.timedelta(days=1)) == day

    first, second = (service.resolve_window(service.MetricWindow.LAST_30D) for _ in range(2))
    assert first.end.second == 0 and first.end.microsecond == 0 and first.end.minute % 5 == 0
    assert first.end >= dt.datetime.now(dt.UTC), "snapped up, so 'now' is inside the window"
    assert (second.end - first.end) in (dt.timedelta(0), service.LONG_WINDOW_SNAP)


def test_an_explicit_instant_is_honoured_to_the_microsecond():
    moment = dt.datetime(2026, 9, 17, 14, 37, 12, 345678, tzinfo=dt.UTC)

    assert service.resolve_window(service.MetricWindow.LAST_7D, at=moment).end == moment
    assert service.resolve_window_days(3, at=moment).end == moment
    assert service.month_to_date(at=moment).end == moment


def test_month_to_date_in_the_last_minute_of_a_month_is_still_that_month(monkeypatch):
    last_minute = dt.datetime(2026, 9, 30, 23, 59, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(service, "now", lambda: last_minute)

    span = service.month_to_date()

    assert span.start == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    assert span.end == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


async def test_a_run_that_landed_a_moment_ago_is_inside_the_window(
    admin_client, factory, workspace, engine
):
    (agent,) = await fleet(factory, workspace, engine, 1)
    run(engine, agent, at=dt.datetime.now(dt.UTC))

    summary = (await admin_client.get("/api/v1/metrics/summary")).json()

    assert summary["total_runs"]["value"] == 1


# ---------------------------------------------------------------------------
# What one page costs the store
# ---------------------------------------------------------------------------


async def test_the_panels_of_one_page_share_one_measurement(
    admin_client, factory, workspace, engine, remembered
):
    """KPI row, model table and platform donut: one rollup, not three."""
    for agent in await fleet(factory, workspace, engine, 3):
        run(engine, agent)

    for path in ("summary", "models", "platforms"):
        response = await admin_client.get(f"/api/v1/metrics/{path}")
        assert response.status_code == 200, response.text

    # Two windows (current and comparison), each walked once and each busy
    # namespace asked for its tokens once. The comparison window is empty, so
    # nobody is asked for tokens there at all.
    assert len(engine.calls_to(STATS)) == 2
    assert len(engine.calls_to(TOKENS)) == 3
    assert len(engine.calls_to(COSTS)) == 2


async def test_sorting_the_model_table_costs_the_store_nothing(
    admin_client, factory, workspace, engine, remembered
):
    for agent in await fleet(factory, workspace, engine, 2):
        run(engine, agent)
    await admin_client.get("/api/v1/metrics/models")
    engine.reset_calls()

    for sort in ("runs", "-cost_usd", "model"):
        response = await admin_client.get("/api/v1/metrics/models", params={"sort": sort})
        assert response.status_code == 200, response.text

    assert engine.calls == []


async def test_an_agent_edit_shows_at_once_even_while_measurements_are_remembered(
    admin_client, db, factory, workspace, engine, remembered
):
    """Engine numbers are remembered; registry attributes never are."""
    from sqlalchemy import update

    from fulcrum_ops_api.models.registry import Agent

    (agent,) = await fleet(factory, workspace, engine, 1, model="gpt-4o")
    run(engine, agent)
    before = (await admin_client.get("/api/v1/metrics/models")).json()["items"]

    await db.execute(update(Agent).where(Agent.id == agent.id).values(model="claude-sonnet"))
    after = (await admin_client.get("/api/v1/metrics/models")).json()["items"]

    assert [row["model"] for row in before] == ["gpt-4o"]
    assert [row["model"] for row in after] == ["claude-sonnet"]
    assert after[0]["runs"] == 1


async def test_a_failed_measurement_is_not_remembered(
    admin_client, factory, workspace, engine, remembered
):
    (agent,) = await fleet(factory, workspace, engine, 1)
    run(engine, agent)

    engine.fail(503)
    down = await admin_client.get("/api/v1/metrics/summary")
    engine.recover()
    back = await admin_client.get("/api/v1/metrics/summary")

    assert down.status_code == 503 and error_code(down) == "telemetry_unavailable"
    assert back.status_code == 200, back.text
    assert back.json()["total_runs"]["value"] == 1


async def test_an_idle_agent_is_not_asked_for_tokens_it_cannot_have(
    admin_client, factory, workspace, engine
):
    """One aggregation per namespace, for the namespaces that had runs."""
    busy, *_idle = await fleet(factory, workspace, engine, 4)
    run(engine, busy, tokens=250)

    rows = (await admin_client.get("/api/v1/metrics/platforms")).json()["items"]

    assert len(engine.calls_to(TOKENS)) == 1
    assert rows[0]["agent_count"] == 4 and rows[0]["runs"] == 1 and rows[0]["tokens"] == 250


async def test_a_worker_runs_a_bounded_number_of_aggregations_at_once(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Eight busy agents used to mean eight simultaneous token aggregations."""
    monkeypatch.setattr(settings, "metrics_engine_concurrency", 2)
    for agent in await fleet(factory, workspace, engine, 8):
        run(engine, agent)
    engine.delay = 0.02

    response = await admin_client.get("/api/v1/metrics/summary")

    assert response.status_code == 200, response.text
    assert response.json()["total_runs"]["value"] == 8
    assert engine.peak <= 2, f"{engine.peak} aggregations were on the store at once"


async def test_a_queue_behind_a_slow_store_is_answered_not_abandoned(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Bounded reads queue; the request still answers, typed, before the console gives up."""
    monkeypatch.setattr(settings, "engine_fanout_deadline_seconds", 0.05)
    for agent in await fleet(factory, workspace, engine, 2):
        run(engine, agent)
    engine.delay = 0.5

    response = await admin_client.get("/api/v1/metrics/summary")

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"


async def test_a_rollup_that_will_be_stored_is_measured_afresh(
    db, factory, workspace, admin, engine, engine_client, remembered
):
    """``fresh=True`` is for a caller that persists the number (budget spend)."""
    (agent,) = await fleet(factory, workspace, engine, 1)
    run(engine, agent, cost=1.0)
    span = service.resolve_window(service.MetricWindow.LAST_7D)
    async with db.session() as session:
        projects = await service.workspace_projects(
            session, principal_for(workspace, admin, Role.ADMIN)
        )

    first = await service.project_rollups(engine_client, projects, span.start, span.end)
    run(engine, agent, cost=2.0)
    shown = await service.project_rollups(engine_client, projects, span.start, span.end)
    stored = await service.project_rollups(
        engine_client, projects, span.start, span.end, fresh=True
    )

    assert [r.runs for r in first] == [1]
    assert [r.runs for r in shown] == [1], "a screen is served the remembered measurement"
    assert [r.runs for r in stored] == [2]
    assert stored[0].cost_usd == pytest.approx(3.0)


async def test_the_overview_answers_cards_and_breakdowns_from_one_measurement(
    admin_client, factory, workspace, engine
):
    fast, slow = await fleet(factory, workspace, engine, 2)
    await factory.provisioned_agent(workspace, engine, name="Other", model="claude-sonnet")
    run(engine, fast, tokens=100)
    run(engine, slow, tokens=300, failed=True)

    summary = (await admin_client.get("/api/v1/metrics/summary")).json()
    models = (await admin_client.get("/api/v1/metrics/models")).json()["items"]
    platforms = (await admin_client.get("/api/v1/metrics/platforms")).json()["items"]
    engine.reset_calls()
    response = await admin_client.get("/api/v1/metrics/overview")

    assert response.status_code == 200, response.text
    overview = response.json()
    # Remembering is off here: this is one measurement by construction.
    assert len(engine.calls_to(STATS)) == 2
    assert len(engine.calls_to(TOKENS)) == 2
    assert overview["summary"]["total_runs"] == summary["total_runs"]
    assert overview["summary"]["success_rate"]["value"] == 50.0
    assert overview["models"] == models
    assert overview["platforms"] == platforms
    assert [row["model"] for row in overview["models"]] == ["gpt-4o", "claude-sonnet"]


async def test_the_overview_is_scoped_to_the_callers_workspace(
    admin_client, factory, workspace, other_workspace, engine
):
    (mine,) = await fleet(factory, workspace, engine, 1)
    theirs = await factory.provisioned_agent(other_workspace, engine, name="Theirs")
    run(engine, mine)
    run(engine, theirs)
    run(engine, theirs)

    overview = (await admin_client.get("/api/v1/metrics/overview")).json()

    assert overview["summary"]["total_runs"]["value"] == 1
    assert overview["summary"]["agent_count"] == 1


# ---------------------------------------------------------------------------
# Charts and the export: each per-project series read once
# ---------------------------------------------------------------------------


def series_reads(engine) -> dict[str, int]:
    """How many per-project series reads the engine served, by metric type."""
    tally: dict[str, int] = {}
    for call in engine.calls:
        if "/projects/" in call.path and call.path.endswith("/metrics"):
            kind = call.body["metric_type"]
            tally[kind] = tally.get(kind, 0) + 1
    return tally


def line(payload, metric) -> dict:
    return next(row for row in payload["series"] if row["metric"] == metric)


async def test_the_export_reads_each_per_project_series_once(
    admin_client, factory, workspace, engine
):
    """Run counts were read four times per agent and durations twice: 7N+2 calls."""
    for agent in await fleet(factory, workspace, engine, 3):
        run(engine, agent)

    response = await admin_client.get("/api/v1/metrics/export")

    assert response.status_code == 200, response.text
    assert series_reads(engine) == {"TRACE_COUNT": 3, "TRACE_ERROR_RATE": 3, "DURATION": 3}


async def test_every_line_asked_for_at_once_still_carries_the_right_numbers(
    admin_client, factory, workspace, engine
):
    steady, flaky = await fleet(factory, workspace, engine, 2)
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    run(engine, steady, at=moment, seconds=2.0)
    run(engine, steady, at=moment, seconds=2.0)
    run(engine, flaky, at=moment, seconds=4.0)
    run(engine, flaky, at=moment, seconds=4.0, failed=True)

    payload = (await admin_client.get("/api/v1/metrics/series", params={"window": "24h"})).json()

    assert line(payload, "runs")["total"] == 4
    assert [p["value"] for p in line(payload, "success_rate")["points"] if p["value"]] == [75.0]
    # Runs-weighted across the two agents: (2 x 2.0s + 2 x 4.0s) / 4.
    assert [p["value"] for p in line(payload, "latency_p50")["points"] if p["value"]] == [3.0]
    assert [p["value"] for p in line(payload, "latency_p90")["points"] if p["value"]] == [3.0]
    # One read per agent per series -- COST among them, because a 24-hour grid
    # is hourly and only the per-project cost series can be asked for hours.
    assert series_reads(engine) == {
        "TRACE_COUNT": 2,
        "TRACE_ERROR_RATE": 2,
        "DURATION": 2,
        "COST": 2,
    }


async def test_a_chart_with_no_per_project_line_reads_nothing_per_project(
    admin_client, factory, workspace, engine
):
    for agent in await fleet(factory, workspace, engine, 2):
        run(engine, agent)

    response = await admin_client.get(
        "/api/v1/metrics/series",
        params=[("metric", "violations"), ("metric", "escalations"), ("metric", "tokens")],
    )

    assert response.status_code == 200, response.text
    assert series_reads(engine) == {}
    assert [row["metric"] for row in response.json()["series"]] == [
        "violations",
        "escalations",
        "tokens",
    ]


# ---------------------------------------------------------------------------
# Weekly charts: the engine's calendar weeks, not weeks from the window start
# ---------------------------------------------------------------------------


def test_the_engines_weekly_buckets_land_one_to_one_on_the_grid():
    """The auditor's case: 90 days back from a Thursday afternoon, 14 engine weeks."""
    weekly = service.MetricInterval.WEEKLY
    span = service.resolve_window(
        service.MetricWindow.LAST_90D, at=dt.datetime(2026, 9, 17, 14, 37, tzinfo=dt.UTC)
    )
    starts = service.bucket_starts(span.start, span.end, weekly)
    # What the engine answers: one point per calendar week, the first stamped
    # the Monday BEFORE the window opens.
    first_monday = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)
    engine_points = [(first_monday + dt.timedelta(days=7 * week), 100.0) for week in range(14)]

    folded = service.fold_sum(engine_points, starts, weekly)

    assert starts[0] == first_monday
    assert all(start.weekday() == 0 and start.hour == 0 for start in starts)
    assert folded == [100.0] * 14, "every engine week has a bucket of its own"


def test_a_point_stamped_before_the_grid_is_credited_not_dropped():
    hourly = service.MetricInterval.HOURLY
    start = dt.datetime(2026, 9, 16, 14, 37, tzinfo=dt.UTC)
    starts = service.bucket_starts(start, start + dt.timedelta(days=1), hourly)
    midnight = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)
    beyond = starts[-1] + dt.timedelta(hours=1)

    folded = service.fold_sum([(midnight, 3.0), (starts[2], 5.0), (beyond, 9.0)], starts, hourly)

    assert folded[0] == 3.0 and folded[2] == 5.0
    assert sum(value for value in folded if value is not None) == 8.0, "padding is not data"


async def test_the_ninety_day_chart_adds_up_to_the_card_above_it(
    admin_client, factory, workspace, engine
):
    """A run in the window's first, partial week used to vanish from the chart."""
    (agent,) = await fleet(factory, workspace, engine, 1)
    span = service.resolve_window(service.MetricWindow.LAST_90D)
    run(engine, agent, at=span.start + dt.timedelta(hours=1))
    run(engine, agent)

    params = {"window": "90d"}
    card = (await admin_client.get("/api/v1/metrics/summary", params=params)).json()
    chart = (await admin_client.get("/api/v1/metrics/series", params=params)).json()

    runs = line(chart, "runs")
    assert chart["interval"] == "weekly"
    assert card["total_runs"]["value"] == 2
    assert runs["total"] == 2, "the chart reconciles with the Total Runs card"
    assert runs["points"][0]["value"] == 1, "the first calendar week is on the chart"
    stamps = [dt.datetime.fromisoformat(p["timestamp"]) for p in runs["points"]]
    assert all(stamp.weekday() == 0 and stamp.hour == 0 for stamp in stamps)
    assert {b - a for a, b in zip(stamps, stamps[1:], strict=False)} == {dt.timedelta(days=7)}


# ---------------------------------------------------------------------------
# The 24-hour cost chart: hourly money on an hourly grid
# ---------------------------------------------------------------------------


def spend(engine, agent, *, at, usd):
    """A run whose cost is carried by its span, as the engine accounts for it."""
    trace = run(engine, agent, at=at, cost=0.0)
    engine._store_span(
        {
            "trace_id": trace["id"],
            "project_name": agent.engine_project_name,
            "type": "llm",
            "start_time": trace["start_time"],
            "end_time": trace["end_time"],
            "total_estimated_cost": usd,
        }
    )
    engine._refresh_trace(trace["id"])


async def test_the_last_24_hours_cost_chart_is_hourly_and_adds_up_to_the_card(
    admin_client, factory, workspace, engine
):
    """Daily cost points on an hourly grid: one spike at 00:00, the evening before lost."""
    (agent,) = await fleet(factory, workspace, engine, 1)
    now = dt.datetime.now(dt.UTC)
    # Twenty hours ago and one hour ago. Whatever the time of day, the two are
    # different hours, and unless it is after 20:00 they are different days --
    # which is when the daily stamp of the first fell off the front of the grid.
    spend(engine, agent, at=now - dt.timedelta(hours=20), usd=3.0)
    spend(engine, agent, at=now - dt.timedelta(hours=1), usd=5.0)

    params = {"window": "24h"}
    card = (await admin_client.get("/api/v1/metrics/summary", params=params)).json()
    chart = (
        await admin_client.get("/api/v1/metrics/series", params={**params, "metric": "cost"})
    ).json()

    cost = line(chart, "cost")
    spent = [(p["timestamp"], p["value"]) for p in cost["points"] if p["value"]]
    assert chart["interval"] == "hourly"
    assert card["cost"]["value"] == pytest.approx(8.0)
    assert cost["total"] == pytest.approx(8.0), "the chart reconciles with the Cost card"
    assert [value for _stamp, value in spent] == [3.0, 5.0], "each spend sits in its own hour"
    hours = [dt.datetime.fromisoformat(stamp) for stamp, _value in spent]
    assert hours[0] == (now - dt.timedelta(hours=20)).replace(minute=0, second=0, microsecond=0)
    assert hours[1] == (now - dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


async def test_longer_windows_still_read_cost_in_one_workspace_call(
    admin_client, factory, workspace, engine
):
    for agent in await fleet(factory, workspace, engine, 3):
        spend(engine, agent, at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2), usd=1.5)

    chart = (
        await admin_client.get("/api/v1/metrics/series", params={"window": "7d", "metric": "cost"})
    ).json()

    assert line(chart, "cost")["total"] == pytest.approx(4.5)
    assert series_reads(engine) == {}
    assert len([c for c in engine.calls if c.path.endswith("/workspaces/costs")]) == 1
