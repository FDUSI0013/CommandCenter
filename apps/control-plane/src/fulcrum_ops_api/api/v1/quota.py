"""Quota, Cost & Capacity routes.

One screen, seven tabs, twenty-six endpoints: the KPI row, the spend and usage
charts, the cost breakdowns, the team allocation table, the computed insights,
the quota and budget registries with their verbs, the capacity readings, the
event feed and the CSV export.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
threshold evaluation, alert raising and every engine call live in
``services.quota``; the telemetry rollups it attributes spend from live in
``services.metrics``.

Route order matters in this module: every fixed path is declared before
``/{quota_id}``, which would otherwise swallow it.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.operations import (
    LimitPeriod,
    LimitScope,
    LimitStatus,
    QuotaEnforcement,
    QuotaResource,
)
from ...schemas.metrics import MetricInterval, MetricsSeriesResponse, MetricWindow
from ...schemas.quota import (
    BudgetCreate,
    BudgetRead,
    BudgetUpdate,
    CapacityReport,
    CapacityRow,
    CapacitySeries,
    CostDriverRow,
    InsightRead,
    ModelCostRow,
    QuotaCreate,
    QuotaEventRead,
    QuotaExportDataset,
    QuotaForecast,
    QuotaIncreaseRequest,
    QuotaOverview,
    QuotaPeriod,
    QuotaRead,
    QuotaSummary,
    QuotaUpdate,
    QuotaUsageMetric,
    ServiceCostRow,
    TeamAllocationRow,
)
from ...services import metrics as telemetry
from ...services import quota as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/quota", tags=["Quota, Cost & Capacity"])

ListQuery = Annotated[ListParams, Depends(list_params)]
PeriodQuery = Annotated[
    QuotaPeriod, Query(description="Measurement period: mtd, 24h, 7d, 30d or 90d")
]
WindowQuery = Annotated[MetricWindow, Query(description="Chart window: 24h, 7d, 30d or 90d")]

MODEL_COST_SORTABLE = (
    "model",
    "agent_count",
    "cost_usd",
    "previous_cost_usd",
    "cost_delta_percent",
    "tokens",
    "api_calls",
    "cost_per_1k_tokens",
    "unit_cost_delta_percent",
    "share_percent",
)
SERVICE_SORTABLE = ("label", "cost_usd", "share_percent", "model_count")
DRIVER_SORTABLE = ("rank", "label", "cost_usd", "share_percent")
TEAM_SORTABLE = (
    "team",
    "agent_count",
    "spend_usd",
    "share_percent",
    "tokens",
    "api_calls",
    "avg_cost_per_1k_tokens",
)
CAPACITY_SORTABLE = (
    "name",
    "resource_type",
    "region",
    "provisioned",
    "used",
    "headroom",
    "utilization_percent",
    "status",
    "measured_at",
)
INSIGHT_SORTABLE = ("rank", "title", "severity", "potential_savings_usd")
EVENT_SORTABLE = ("occurred_at", "title", "kind", "severity", "status")


def _attachment(name: str) -> dict[str, str]:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return {"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'}


def _csv(
    rows: list[dict[str, Any]], columns: list[tuple[str, str]], name: str
) -> StreamingResponse:
    return StreamingResponse(
        iter([to_csv(rows, columns)]),
        media_type="text/csv; charset=utf-8",
        headers=_attachment(name),
    )


# ---------------------------------------------------------------------------
# KPI row, charts and analysis
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=QuotaSummary, summary="Quota and cost KPI summary")
async def get_summary(
    principal: CurrentPrincipal, session: Db, period: PeriodQuery = QuotaPeriod.MTD
) -> QuotaSummary:
    """The six KPI cards: spend, budget, tokens, calls, unit cost, capacity.

    Spend, tokens and calls are measured over the period and over the equivalent
    span of the one before it, so each delta compares like with like. Budget is
    the sum of the ceilings in force; capacity health is scored from the latest
    reading of every pool that has reported.
    """
    return await service.summarise(session, principal, period=period)


@router.get(
    "/overview",
    response_model=QuotaOverview,
    summary="KPI summary and every cost panel, measured once",
)
async def get_overview(
    principal: CurrentPrincipal, session: Db, period: PeriodQuery = QuotaPeriod.MTD
) -> QuotaOverview:
    """Everything on the Overview and Costs tabs that is not a chart.

    `summary` is exactly what `/quota/summary` answers; `models`, `services`,
    `drivers`, `teams` and `insights` are every row of `/quota/cost-breakdown`,
    `/quota/cost-by-service`, `/quota/top-drivers`, `/quota/team-allocation`
    and `/quota/insights` in their default order. All six are views of one
    measurement of the period, so asking here costs the telemetry store one
    measurement where asking the six routes costs six. The first ten teams
    carry their sparkline.
    """
    return QuotaOverview.model_validate(
        await service.overview(session, principal, period=period)
    )


@router.get(
    "/cost-breakdown",
    response_model=Page[ModelCostRow],
    summary="Cost and usage by model",
)
async def cost_breakdown(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    period: PeriodQuery = QuotaPeriod.MTD,
) -> Page[ModelCostRow]:
    """Spend, tokens, calls and unit-cost movement for every model in use.

    Cost is attributed through the agent registry: the spend of every agent
    configured with a model is that model's spend. `cost_per_1k_tokens` is
    measured cost over measured tokens for the same window, so a move in it is a
    real change in unit price rather than a change in volume.
    """
    context = await service.cost_context(session, principal, period)
    rows, total = telemetry.paginate_rows(
        service.model_cost_rows(context),
        params,
        sortable=MODEL_COST_SORTABLE,
        default_key="cost_usd",
        search_keys=("model",),
    )
    return Page.build(
        [ModelCostRow.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.get(
    "/cost-by-service",
    response_model=Page[ServiceCostRow],
    summary="Cost by service family",
)
async def cost_by_service(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    period: PeriodQuery = QuotaPeriod.MTD,
) -> Page[ServiceCostRow]:
    """The Cost by Service donut: measured model spend folded into families.

    The classifier only regroups measured cost — embedding, speech, image,
    retrieval and inference — and cost the store reported without a model name
    lands in Other Services rather than being spread across the named families.
    """
    context = await service.cost_context(session, principal, period)
    rows, total = telemetry.paginate_rows(
        service.service_cost_rows(context),
        params,
        sortable=SERVICE_SORTABLE,
        default_key="cost_usd",
        search_keys=("label",),
    )
    return Page.build(
        [ServiceCostRow.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.get(
    "/top-drivers",
    response_model=Page[CostDriverRow],
    summary="Top cost drivers",
)
async def top_drivers(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    period: PeriodQuery = QuotaPeriod.MTD,
    limit: Annotated[int, Query(ge=1, le=50, description="How many drivers to rank")] = 10,
) -> Page[CostDriverRow]:
    """The ranked short list behind the Top Cost Drivers bars.

    The same measurements as `/cost-breakdown`, cut to the heaviest
    contributors and carrying each one's share of total spend.
    """
    context = await service.cost_context(session, principal, period)
    rows, total = telemetry.paginate_rows(
        service.driver_rows(context, limit=limit),
        params,
        sortable=DRIVER_SORTABLE,
        default_key="rank",
        search_keys=("label",),
        default_desc=False,
    )
    return Page.build(
        [CostDriverRow.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.get(
    "/team-allocation",
    response_model=Page[TeamAllocationRow],
    summary="Cost and usage by team",
)
async def team_allocation(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    period: PeriodQuery = QuotaPeriod.MTD,
    with_trend: Annotated[
        bool, Query(description="Fetch each row's daily spend sparkline")
    ] = True,
) -> Page[TeamAllocationRow]:
    """Spend, tokens, calls and unit cost per team, attributed via the registry.

    Sparklines cost one telemetry read per team, so they are fetched for the
    page being rendered and can be turned off entirely with `with_trend=false`.
    """
    context = await service.cost_context(session, principal, period)
    rows, total = telemetry.paginate_rows(
        service.team_rows(context),
        params,
        sortable=TEAM_SORTABLE,
        default_key="spend_usd",
        search_keys=("team",),
    )
    if with_trend:
        await service.attach_team_trends(context, rows)
    return Page.build(
        [TeamAllocationRow.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.get(
    "/insights",
    response_model=Page[InsightRead],
    summary="Cost optimisation insights",
)
async def list_insights(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    period: PeriodQuery = QuotaPeriod.MTD,
) -> Page[InsightRead]:
    """Observations computed from what was measured, ranked by severity.

    Budgets on pace to breach, quotas near their ceiling, models whose unit cost
    moved, pools running out of headroom, and ceilings nobody is using. Every
    body quotes the numbers that triggered it; nothing here is stored or
    authored, so an insight disappears the moment the data stops supporting it.
    """
    rows, total = telemetry.paginate_rows(
        await service.insights(session, principal, period=period),
        params,
        sortable=INSIGHT_SORTABLE,
        default_key="rank",
        search_keys=("title", "body"),
        default_desc=False,
    )
    return Page.build(
        [InsightRead.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.get(
    "/usage-series",
    response_model=MetricsSeriesResponse,
    summary="Spend, token and call trends",
)
async def usage_series(
    principal: CurrentPrincipal,
    session: Db,
    window: WindowQuery = MetricWindow.LAST_30D,
    interval: Annotated[
        MetricInterval | None, Query(description="Bucket width; defaults to the window's own")
    ] = None,
    metric: Annotated[
        list[QuotaUsageMetric] | None, Query(description="Repeat to overlay lines")
    ] = None,
) -> MetricsSeriesResponse:
    """One or more of spend, tokens and API calls on a shared bucket grid.

    Omit `metric` and all three come back. An API call is one recorded agent
    run, which is the unit the telemetry store counts and the unit a request
    quota is written in.
    """
    wanted = list(metric) if metric else list(QuotaUsageMetric)
    return await service.usage_series(
        session, principal, window=window, interval=interval, metrics=wanted
    )


@router.get("/forecast", response_model=QuotaForecast, summary="Spend forecast")
async def get_forecast(
    principal: CurrentPrincipal, session: Db, period: PeriodQuery = QuotaPeriod.MTD
) -> QuotaForecast:
    """Project period-end spend from the daily spend already measured.

    A least-squares fit over the observed days extended to the period end, with
    the 95% band implied by the residual spread, the model whose spend grew
    most, and the days sitting more than three residual standard deviations off
    the line. With fewer than four measured days nothing is projected and the
    projection fields come back null.
    """
    return await service.forecast(session, principal, period=period)


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


@router.get("/capacity", response_model=Page[CapacityRow], summary="Capacity overview")
async def list_capacity(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    resource_type: Annotated[
        str | None, Query(description="Family filter, e.g. 'GPU' or 'Memory'")
    ] = None,
    region: Annotated[str | None, Query(description="Region filter")] = None,
) -> Page[CapacityRow]:
    """Latest reading per provisioned pool, with utilisation, headroom and trend.

    Busiest pool first. A pool that has not reported inside the history window
    is not listed: this table shows measurements, and a stale reading is not one.
    """
    rows = await service.capacity_rows(session, principal)
    if resource_type:
        rows = [row for row in rows if row["resource_type"] == resource_type]
    if region:
        rows = [row for row in rows if row["region"] == region]
    page, total = telemetry.paginate_rows(
        rows,
        params,
        sortable=CAPACITY_SORTABLE,
        default_key="utilization_percent",
        search_keys=("name", "resource_type", "region"),
    )
    return Page.build(
        [CapacityRow.model_validate(row) for row in page], total, params.page, params.page_size
    )


@router.post(
    "/capacity",
    response_model=ActionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Report capacity readings",
)
async def report_capacity(
    principal: CurrentPrincipal, session: Db, payload: CapacityReport
) -> ActionResult:
    """Record what a reporter measured: up to a hundred readings per call.

    This is how a pool the platform cannot see for itself -- a GPU fleet, a
    vector store, a network link -- gets onto the Capacity tab; the platform
    records its own memory, disk and CPU on its own clock. Send `provisioned`
    and `used`; utilisation and the Healthy/Warning/Critical status are derived
    here with the thresholds the rest of the screen uses, so they are not
    accepted. A reading dated in the future, or older than the thirty days of
    history the screen shows, is refused with 422; so is one filed under a pool
    the platform reports about itself (`Platform Memory`, `Host Memory`,
    `Platform Disk`, `Host CPU`). Requires the operator role (an API key needs
    the `admin` scope).
    """
    recorded = await service.record_capacity(session, principal, payload)
    return ActionResult(
        message=f"{recorded} capacity reading(s) recorded.", data={"recorded": recorded}
    )


@router.get(
    "/capacity-series",
    response_model=CapacitySeries,
    summary="Utilisation history for one pool",
)
async def capacity_series(
    principal: CurrentPrincipal,
    session: Db,
    resource: Annotated[str, Query(description="Resource name, e.g. 'GPU Capacity (A100)'")],
    window: WindowQuery = MetricWindow.LAST_24H,
    interval: Annotated[MetricInterval | None, Query()] = None,
) -> CapacitySeries:
    """Utilisation of one pool over time, from the readings we hold.

    Backs the live concurrency chart and any other per-resource trend. Readings
    are averaged into the bucket they fall in; a bucket nobody reported in stays
    empty rather than being carried forward. A resource with no readings in the
    window answers 404.
    """
    points = await service.capacity_series(
        session, principal, resource=resource, window=window, interval=interval
    )
    span = telemetry.resolve_window(window)
    reported = [point.value for point in points if point.value is not None]
    return CapacitySeries(
        resource=resource,
        period_start=span.start,
        period_end=span.end,
        labels=[point.label for point in points],
        points=points,
        average_percent=round(sum(reported) / len(reported), 2) if reported else None,
        latest_percent=reported[-1] if reported else None,
    )


# ---------------------------------------------------------------------------
# Events and export
# ---------------------------------------------------------------------------


@router.get("/events", response_model=Page[QuotaEventRead], summary="Quota events")
async def list_events(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    days: Annotated[int, Query(ge=1, le=365, description="How far back to read")] = 30,
    kind: Annotated[str | None, Query(description="'alert' or 'change'")] = None,
) -> Page[QuotaEventRead]:
    """One timeline of the alerts this screen raised and the changes it recorded.

    Alerts carry severity and their current state; audit rows carry the actor
    who made the change. Neither can be written from here.
    """
    rows = await service.events(session, principal, days=days)
    if kind:
        rows = [row for row in rows if row["kind"] == kind]
    page, total = telemetry.paginate_rows(
        rows,
        params,
        sortable=EVENT_SORTABLE,
        default_key="occurred_at",
        search_keys=("title", "detail", "actor"),
    )
    return Page.build(
        [QuotaEventRead.model_validate(row) for row in page], total, params.page, params.page_size
    )


@router.get("/export", summary="Export a quota table as CSV", response_class=StreamingResponse)
async def export_quota(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    dataset: Annotated[
        QuotaExportDataset, Query(description="Which table to export")
    ] = QuotaExportDataset.TEAMS,
    period: PeriodQuery = QuotaPeriod.MTD,
) -> StreamingResponse:
    """Stream any of the screen's seven tables as CSV.

    The export runs the same search and sort the on-screen table is using and
    covers every filtered row, not just the page in view. `dataset=teams` is the
    default because that is what the screen's Export button asks for.
    """
    if dataset is QuotaExportDataset.QUOTAS:
        rows = telemetry.filter_rows(
            await service.all_quota_rows(session, principal),
            params,
            sortable=("name", "resource", "scope", "used_value", "limit_value", "status"),
            default_key="name",
            search_keys=("name", "unit", "scope_ref"),
            default_desc=False,
        )
        return _csv(rows, service.QUOTA_EXPORT_COLUMNS, "quotas")

    if dataset is QuotaExportDataset.BUDGETS:
        budgets, _ = await service.list_budgets(
            session, principal, params.model_copy(update={"page": 1, "page_size": 200})
        )
        return _csv(budgets, service.BUDGET_EXPORT_COLUMNS, "budgets")

    if dataset is QuotaExportDataset.CAPACITY:
        rows = telemetry.filter_rows(
            await service.capacity_rows(session, principal),
            params,
            sortable=CAPACITY_SORTABLE,
            default_key="utilization_percent",
            search_keys=("name", "resource_type", "region"),
        )
        return _csv(rows, service.CAPACITY_EXPORT_COLUMNS, "capacity")

    context = await service.cost_context(session, principal, period)
    if dataset is QuotaExportDataset.COST_BY_MODEL:
        rows = telemetry.filter_rows(
            service.model_cost_rows(context),
            params,
            sortable=MODEL_COST_SORTABLE,
            default_key="cost_usd",
            search_keys=("model",),
        )
        return _csv(rows, service.MODEL_COST_EXPORT_COLUMNS, "cost-by-model")

    if dataset is QuotaExportDataset.COST_BY_SERVICE:
        rows = telemetry.filter_rows(
            service.service_cost_rows(context),
            params,
            sortable=SERVICE_SORTABLE,
            default_key="cost_usd",
            search_keys=("label",),
        )
        return _csv(rows, service.SERVICE_EXPORT_COLUMNS, "cost-by-service")

    if dataset is QuotaExportDataset.TOP_DRIVERS:
        rows = telemetry.filter_rows(
            service.driver_rows(context),
            params,
            sortable=DRIVER_SORTABLE,
            default_key="rank",
            search_keys=("label",),
            default_desc=False,
        )
        return _csv(rows, service.DRIVER_EXPORT_COLUMNS, "top-cost-drivers")

    rows = telemetry.filter_rows(
        service.team_rows(context),
        params,
        sortable=TEAM_SORTABLE,
        default_key="spend_usd",
        search_keys=("team",),
    )
    return _csv(rows, service.TEAM_EXPORT_COLUMNS, "cost-usage-by-team")


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@router.get("/budgets", response_model=Page[BudgetRead], summary="List budgets")
async def list_budgets(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    scope: Annotated[LimitScope | None, Query(description="Workspace, Team, Agent…")] = None,
    budget_status: Annotated[
        LimitStatus | None, Query(alias="status", description="Active, Warning, Exceeded…")
    ] = None,
    period: Annotated[LimitPeriod | None, Query(description="Monthly, Quarterly, Annual")] = None,
    active_only: Annotated[
        bool, Query(description="Only budgets whose period contains now")
    ] = False,
) -> Page[BudgetRead]:
    """The budget bars, each with its utilisation and pace projection.

    `projected_spend_usd` is the period-end figure at the pace observed so far
    and is null until enough of the period has elapsed for a pace to mean
    anything.
    """
    rows, total = await service.list_budgets(
        session,
        principal,
        params,
        scope=scope,
        status=budget_status,
        period=period,
        active_only=active_only,
    )
    return Page.build(
        [BudgetRead.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.post(
    "/budgets",
    response_model=BudgetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a budget",
)
async def create_budget(
    principal: CurrentPrincipal, session: Db, payload: BudgetCreate, request: Request
) -> BudgetRead:
    """Open a spend ceiling for a period.

    Spend is never supplied by a caller. It is measured as the budget is opened
    -- a budget created mid-period answers with what the period has already
    cost, and raises its alert at once if that is past a threshold -- and kept
    true afterwards by the scheduled roll-up. If the telemetry store cannot
    answer, the budget still opens, at zero. Requires the admin role.
    """
    return BudgetRead.model_validate(
        await service.create_budget(session, principal, payload, request=request)
    )


@router.post(
    "/budgets/refresh",
    response_model=ActionResult,
    summary="Roll measured cost into budgets",
)
async def refresh_budgets(
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    budget_id: Annotated[
        str | None, Query(description="Refresh one budget instead of all live ones")
    ] = None,
) -> ActionResult:
    """Re-measure spend for every live budget and evaluate its thresholds.

    The platform runs this pass on its own clock; this route is the same pass
    on demand. It is where a threshold crossing raises its alert. Without
    `budget_id` it also closes the books on budgets whose period has ended
    (they come back `Expired`, with their final spend) and opens the current
    period for recurring ones. Budgets sharing a period are measured with one
    telemetry read. Requires the operator role.
    """
    refreshed = await service.refresh_budgets(
        session, principal, budget_id=budget_id, request=request
    )
    if not refreshed:
        return ActionResult(message="No live budgets to refresh.", data={"budgets": []})
    breached = [
        row
        for row in refreshed
        if row["status"] in (LimitStatus.WARNING.value, LimitStatus.EXCEEDED.value)
    ]
    payload = [BudgetRead.model_validate(row).model_dump(mode="json") for row in refreshed]
    return ActionResult(
        message=(
            f"{len(refreshed)} budget(s) refreshed; {len(breached)} past a threshold."
        ),
        data={"budgets": payload},
    )


@router.get("/budgets/{budget_id}", response_model=BudgetRead, summary="Get a budget")
async def get_budget(principal: CurrentPrincipal, session: Db, budget_id: str) -> BudgetRead:
    """One budget. A budget in another workspace answers 404, not 403."""
    budget = await service.get_budget(session, principal, budget_id)
    return BudgetRead.model_validate(service.budget_payload(budget))


@router.patch("/budgets/{budget_id}", response_model=BudgetRead, summary="Update a budget")
async def update_budget(
    principal: CurrentPrincipal,
    session: Db,
    budget_id: str,
    payload: BudgetUpdate,
    request: Request,
) -> BudgetRead:
    """Amend a budget, including the threshold editor.

    Thresholds are re-evaluated against the spend already recorded, so lowering
    one under current spend raises its alert in the same transaction as the
    edit. Send `expected_updated_at` to make the write conditional. Requires the
    admin role.
    """
    return BudgetRead.model_validate(
        await service.update_budget(session, principal, budget_id, payload, request=request)
    )


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a budget",
)
async def delete_budget(
    principal: CurrentPrincipal, session: Db, budget_id: str, request: Request
) -> None:
    """Close a budget. Alerts it already raised survive it. Requires admin."""
    await service.delete_budget(session, principal, budget_id, request=request)


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[QuotaRead], summary="List quotas")
async def list_quotas(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    resource: Annotated[
        QuotaResource | None, Query(description="Tokens, Requests, Cost, Concurrency, Storage")
    ] = None,
    scope: Annotated[LimitScope | None, Query(description="Workspace, Team, Agent…")] = None,
    quota_status: Annotated[
        LimitStatus | None, Query(alias="status", description="Active, Warning, Exceeded…")
    ] = None,
    enforcement: Annotated[
        QuotaEnforcement | None, Query(description="Block, Warn or Log")
    ] = None,
) -> Page[QuotaRead]:
    """The Quota Utilization table.

    Every row carries its utilisation, the Healthy/Watch/Critical chip derived
    from it, and the countdown to its next reset. Free-text search covers the
    name, the unit and the scope target.
    """
    rows, total = await service.list_quotas(
        session,
        principal,
        params,
        resource=resource,
        scope=scope,
        status=quota_status,
        enforcement=enforcement,
    )
    return Page.build(
        [QuotaRead.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.post(
    "",
    response_model=QuotaRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a quota",
)
async def create_quota(
    principal: CurrentPrincipal, session: Db, payload: QuotaCreate, request: Request
) -> QuotaRead:
    """Register an enforceable ceiling.

    It starts empty and Active; usage and status are written by the enforcement
    path, never by a caller. A `Block` quota refuses traffic on breach, while
    `Warn` and `Log` admit it and record the breach. Requires the admin role.
    """
    return QuotaRead.model_validate(
        await service.create_quota(session, principal, payload, request=request)
    )


@router.get("/{quota_id}", response_model=QuotaRead, summary="Get a quota")
async def get_quota(principal: CurrentPrincipal, session: Db, quota_id: str) -> QuotaRead:
    """One quota. A quota in another workspace answers 404, not 403."""
    quota = await service.get_quota(session, principal, quota_id)
    return QuotaRead.model_validate(service.quota_payload(quota))


@router.patch("/{quota_id}", response_model=QuotaRead, summary="Update a quota")
async def update_quota(
    principal: CurrentPrincipal,
    session: Db,
    quota_id: str,
    payload: QuotaUpdate,
    request: Request,
) -> QuotaRead:
    """Amend a ceiling and re-evaluate it against the usage already counted.

    Raising a limit can clear a breach and lowering one can create it, so the
    status is always recomputed. `Warning` and `Exceeded` are refused with 422:
    they belong to the evaluator. Requires the admin role.
    """
    return QuotaRead.model_validate(
        await service.update_quota(session, principal, quota_id, payload, request=request)
    )


@router.delete(
    "/{quota_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a quota"
)
async def delete_quota(
    principal: CurrentPrincipal, session: Db, quota_id: str, request: Request
) -> None:
    """Remove a ceiling. Enforcement stops at once; the audit row remains.

    Requires the admin role.
    """
    await service.delete_quota(session, principal, quota_id, request=request)


@router.post(
    "/{quota_id}/request-increase",
    response_model=ActionResult,
    summary="Request a quota increase",
)
async def request_increase(
    principal: CurrentPrincipal,
    session: Db,
    quota_id: str,
    payload: QuotaIncreaseRequest,
    request: Request,
) -> ActionResult:
    """Route a bigger ceiling into the approvals queue.

    The quota is not touched by asking: an increase is a decision, and it is
    recorded where every other governed decision lives. Approving the request
    raises the ceiling to the number asked for here, in the same transaction as
    the approval. The risk rung — and therefore the SLA — is derived from how
    much bigger the ask is. Any member may request.
    """
    outcome = await service.request_increase(
        session, principal, quota_id, payload, request=request
    )
    return ActionResult(
        message=(
            f"Increase to {outcome['requested_limit']:,.0f} {outcome['unit']} requested; "
            f"{outcome['request_ref']} is with the approvers and is applied on approval."
        ),
        entity_id=quota_id,
        data=outcome,
    )
