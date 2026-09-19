"""Testing & Regression routes.

Twenty-one endpoints back one screen and its seven tabs: the six KPI cards, the
suite table with its four filters and CSV export, the Create Test Suite modal,
Run Suite with live progress, Promote Baseline, the baseline comparison, the
Test Runs, Baselines, Schedules and Environments tabs. The Datasets and
Evaluations tabs are served by the Evaluations router, which owns them.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
audit writes and every call to the telemetry engine live in
``services.testing``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.quality import RunTrigger, SuiteStatus, SuiteType, TestRunStatus
from ...schemas.testing import (
    EXPORT_COLUMNS,
    RUN_EXPORT_COLUMNS,
    BaselinePromote,
    BaselineRead,
    EnvironmentBinding,
    RunComparison,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
    TestingSummary,
    TestRunDetail,
    TestRunProgress,
    TestRunRead,
    TestRunRequest,
    TestSuiteCreate,
    TestSuiteRead,
    TestSuiteUpdate,
    describe_cron,
    next_fire,
)
from ...services import testing as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/testing", tags=["Testing & Regression"])


def _run_columns() -> tuple[tuple[str, str], ...]:
    """The runs CSV's columns: the schema's, with Status put beside Result.

    Two different facts. ``status`` is how the run ended -- Failed when any case
    failed -- and is what the Test Runs tab sorts and filters on; ``result`` is
    the verdict its pass rate earned. The tab shows both, and a file exported
    under Status = Failed that carried only the verdict read "Passed" on a 95%
    run with nothing to say why it was there. Left alone once the schema's own
    list names the column, so it can move there without appearing twice.
    """
    if any(key == "status" for key, _ in RUN_EXPORT_COLUMNS):
        return RUN_EXPORT_COLUMNS
    columns: list[tuple[str, str]] = []
    for key, header in RUN_EXPORT_COLUMNS:
        if key == "result":
            columns.append(("status", "Status"))
        columns.append((key, header))
    return tuple(columns)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


def _schedule_result(suite: Any, message: str) -> ActionResult:
    """Shape a suite whose cron just changed as the Schedules tab reads it."""
    cron = suite.schedule_cron
    next_run = next_fire(cron, dt.datetime.now(dt.UTC)) if cron else None
    return ActionResult(
        message=message,
        entity_id=suite.id,
        data={
            "suite_id": suite.id,
            "cron": cron,
            "cadence": describe_cron(cron) if cron else None,
            "next_run_at": next_run.isoformat() if next_run else None,
        },
    )


# ---------------------------------------------------------------------------
# Screen-level reads
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=TestingSummary, summary="Testing KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> TestingSummary:
    """The six KPI cards: suites, tests executed, pass rate, regressions
    detected, average duration and flaky tests.

    Everything is aggregated from the runs that finished inside the window and
    compared against the window before it. Flaky tests are counted by looking
    for cases that changed verdict across that history, not read from a field.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/compare", response_model=RunComparison, summary="Compare two runs")
async def compare_runs(
    principal: CurrentPrincipal,
    session: Db,
    baseline: Annotated[str, Query(description="Run id to compare against")],
    candidate: Annotated[str, Query(description="Run id under review")],
) -> RunComparison:
    """Two runs case by case, regressions first.

    A regression is a case that passed on the baseline and fails on the
    candidate; a fix is the reverse. Cases only one run holds are reported as
    Added or Removed rather than being silently counted as either.
    """
    return await service.compare_runs(session, principal, baseline, candidate)


@router.get("/baselines", response_model=Page[BaselineRead], summary="List baselines")
async def list_baselines(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
) -> Page[BaselineRead]:
    """The Baselines tab: each suite's baseline against its newest finished run,
    with the pass-rate delta promotion would lock in."""
    rows, total = await service.list_baselines(session, principal, params)
    return Page.build(rows, total, params.page, params.page_size)


@router.get(
    "/environments", response_model=list[EnvironmentBinding], summary="List environments"
)
async def list_environments(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[EnvironmentBinding]:
    """The Environments tab: how many suites each environment carries, how they
    ran in the window, and when it last came through with nothing failing."""
    return await service.list_environments(session, principal, window_days=window_days)


@router.get("/export", summary="Export test suites as CSV")
async def export_suites(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    suite_type: Annotated[SuiteType | None, Query(alias="type")] = None,
    suite_status: Annotated[SuiteStatus | None, Query(alias="status")] = None,
    environment: Annotated[str | None, Query()] = None,
    owner_user_id: Annotated[str | None, Query(alias="owner")] = None,
    agent_id: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The current view as CSV, honouring the table's search and four filters."""
    rows = await service.export_suites(
        session,
        principal,
        params,
        suite_type=suite_type.value if suite_type else None,
        status=suite_status.value if suite_status else None,
        environment=environment,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
    )
    records = await service.read_suites(session, principal, rows)
    payload: list[dict[str, Any]] = [
        {
            "name": record.name,
            "suite_type": record.suite_type.value,
            "environment": record.environment,
            "status": record.status.value,
            "result": record.result.value,
            "pass_rate": record.pass_rate,
            "case_count": record.case_count,
            "last_run_at": record.last_run_at.isoformat() if record.last_run_at else None,
            "next_run_at": record.next_run_at.isoformat() if record.next_run_at else None,
            "baseline": record.baseline,
            "owner_name": record.owner_name,
            "team": record.team,
            "flaky_count": record.flaky_count,
            "regressions": record.regressions,
            "dataset": record.dataset,
        }
        for record in records
    ]
    return StreamingResponse(
        iter([to_csv(payload, EXPORT_COLUMNS)]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="test-suites-{_stamp()}.csv"'},
    )


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@router.get("/schedules", response_model=Page[ScheduleRead], summary="List schedules")
async def list_schedules(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    environment: Annotated[str | None, Query()] = None,
    suite_type: Annotated[SuiteType | None, Query(alias="type")] = None,
) -> Page[ScheduleRead]:
    """The Schedules tab.

    A schedule is a suite's cron binding, so its id is the suite's id. Next-run
    times are computed from the expression rather than stored, which keeps them
    correct after a cadence change without a migration.
    """
    rows, total = await service.list_schedules(
        session,
        principal,
        params,
        environment=environment,
        suite_type=suite_type.value if suite_type else None,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "/schedules",
    response_model=ActionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a suite",
)
async def create_schedule(
    principal: CurrentPrincipal,
    session: Db,
    payload: ScheduleCreate,
    request: Request,
) -> ActionResult:
    """Bind a cron expression to a suite that does not already have one.

    The expression is validated before it is stored, so an unparseable cadence
    is refused at the edge instead of silently never firing. Requires the
    operator role.
    """
    suite = await service.create_schedule(session, principal, payload, request=request)
    return _schedule_result(suite, f"{suite.name} runs {describe_cron(payload.cron)}.")


@router.patch(
    "/schedules/{schedule_id}", response_model=ActionResult, summary="Update a schedule"
)
async def update_schedule(
    principal: CurrentPrincipal,
    session: Db,
    schedule_id: str,
    payload: ScheduleUpdate,
    request: Request,
) -> ActionResult:
    """Change a schedule's cadence. Requires the operator role."""
    suite = await service.update_schedule(
        session, principal, schedule_id, payload, request=request
    )
    return _schedule_result(suite, f"{suite.name} now runs {describe_cron(payload.cron)}.")


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unschedule a suite",
)
async def delete_schedule(
    principal: CurrentPrincipal, session: Db, schedule_id: str, request: Request
) -> None:
    """Remove the schedule. The suite itself is untouched and can still be run
    by hand. Requires the operator role."""
    await service.delete_schedule(session, principal, schedule_id, request=request)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=Page[TestRunRead], summary="List test runs")
async def list_runs(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    suite_id: Annotated[str | None, Query()] = None,
    run_status: Annotated[TestRunStatus | None, Query(alias="status")] = None,
    trigger: Annotated[RunTrigger | None, Query()] = None,
) -> Page[TestRunRead]:
    """The Test Runs tab: run reference, suite, trigger, result, pass rate,
    duration and time, newest first."""
    rows, total = await service.list_runs(
        session,
        principal,
        params,
        suite_id=suite_id,
        status=run_status.value if run_status else None,
        trigger=trigger.value if trigger else None,
    )
    records = await service.read_runs(session, principal, rows)
    return Page.build(records, total, params.page, params.page_size)


@router.get("/runs/export", summary="Export test runs as CSV")
async def export_runs(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    suite_id: Annotated[str | None, Query()] = None,
    run_status: Annotated[TestRunStatus | None, Query(alias="status")] = None,
    trigger: Annotated[RunTrigger | None, Query()] = None,
) -> StreamingResponse:
    """The Test Runs tab as CSV, honouring the same filters.

    Every matching run up to ``EXPORT_LIMIT``, the cap the suites export beside
    it already applies.
    """
    # This used to take one 200-row page -- the most a *table* may ask for -- and
    # call it the export. Nothing in the file said it had stopped, so a workspace
    # with a few weeks of scheduled runs downloaded its newest 200 and read them
    # as its history. ``model_copy`` does not re-validate, which is what lets the
    # export's cap through a field the query string is held to 200 on; the same
    # ``list_runs`` still does the scoping, the filtering and the ordering, so the
    # file cannot drift from the table it was exported from.
    export_params = params.model_copy(
        update={"page": 1, "page_size": service.EXPORT_LIMIT}
    )
    rows, _ = await service.list_runs(
        session,
        principal,
        export_params,
        suite_id=suite_id,
        status=run_status.value if run_status else None,
        trigger=trigger.value if trigger else None,
    )
    records = await service.read_runs(session, principal, rows)
    payload: list[dict[str, Any]] = [
        {
            "run_ref": record.run_ref,
            "suite_name": record.suite_name,
            "trigger": record.trigger.value,
            "status": record.status.value,
            "result": record.result.value,
            "pass_rate": record.pass_rate,
            "total_cases": record.total_cases,
            "passed": record.passed,
            "failed": record.failed,
            "regression_count": record.regression_count,
            "duration_label": record.duration_label,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }
        for record in records
    ]
    return StreamingResponse(
        iter([to_csv(payload, _run_columns())]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="test-runs-{_stamp()}.csv"'},
    )


@router.get("/runs/{run_id}", response_model=TestRunDetail, summary="Get a test run")
async def get_run(principal: CurrentPrincipal, session: Db, run_id: str) -> TestRunDetail:
    """One run with its per-case verdicts and its diff against the baseline.

    Cases the engine never judged are reported as Unscored and are excluded
    from the pass rate: missing evidence is not a failure.
    """
    return await service.get_run_detail(session, principal, run_id)


# ---------------------------------------------------------------------------
# Suites
# ---------------------------------------------------------------------------


@router.get("/suites", response_model=Page[TestSuiteRead], summary="List test suites")
async def list_suites(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    suite_type: Annotated[
        SuiteType | None, Query(alias="type", description="Regression, Evaluation, Load, ...")
    ] = None,
    suite_status: Annotated[
        SuiteStatus | None, Query(alias="status", description="Active, Draft or Disabled")
    ] = None,
    environment: Annotated[
        str | None, Query(description="Production, Staging, UAT, ...")
    ] = None,
    owner_user_id: Annotated[str | None, Query(alias="owner")] = None,
    agent_id: Annotated[str | None, Query()] = None,
    scheduled: Annotated[
        bool | None,
        Query(description="true: only suites with a cadence; false: only ones without"),
    ] = None,
) -> Page[TestSuiteRead]:
    """One page of the workspace's suites, ordered by name by default.

    ``status`` is the suite's lifecycle; ``result`` is the verdict of its last
    run, which is what the table's Status column shows. Pass rate, trend,
    regressions and average duration all come from the suite's own run history.
    """
    rows, total = await service.list_suites(
        session,
        principal,
        params,
        suite_type=suite_type.value if suite_type else None,
        status=suite_status.value if suite_status else None,
        environment=environment,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        scheduled=scheduled,
    )
    records = await service.read_suites(session, principal, rows)
    return Page.build(records, total, params.page, params.page_size)


@router.post(
    "/suites",
    response_model=TestSuiteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a test suite",
)
async def create_suite(
    principal: CurrentPrincipal,
    session: Db,
    payload: TestSuiteCreate,
    request: Request,
) -> TestSuiteRead:
    """Create a suite over an existing dataset.

    The dataset is resolved in the telemetry engine and its case count is read
    from there, so the suite knows how much work a run is before one is
    started. Requires the member role.
    """
    suite = await service.create_suite(session, principal, payload, request=request)
    records = await service.read_suites(session, principal, [suite])
    return records[0]


@router.get("/suites/{suite_id}", response_model=TestSuiteRead, summary="Get a test suite")
async def get_suite(
    principal: CurrentPrincipal, session: Db, suite_id: str
) -> TestSuiteRead:
    """One suite. A suite in another workspace answers 404, not 403."""
    suite = await service.get_suite(session, principal, suite_id)
    records = await service.read_suites(session, principal, [suite])
    return records[0]


@router.patch("/suites/{suite_id}", response_model=TestSuiteRead, summary="Update a test suite")
async def update_suite(
    principal: CurrentPrincipal,
    session: Db,
    suite_id: str,
    payload: TestSuiteUpdate,
    request: Request,
) -> TestSuiteRead:
    """Partially update a suite.

    Changing the dataset re-resolves it in the engine and re-reads the case
    count. Send `expected_updated_at` to make the write conditional. Requires
    the member role.
    """
    suite = await service.update_suite(
        session, principal, suite_id, payload, request=request
    )
    records = await service.read_suites(session, principal, [suite])
    return records[0]


@router.delete(
    "/suites/{suite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a test suite",
)
async def delete_suite(
    principal: CurrentPrincipal, session: Db, suite_id: str, request: Request
) -> None:
    """Remove a suite and its run history.

    Refused with 412 while a run is queued or in flight. The audit trail
    survives the deletion. Requires the admin role.
    """
    await service.delete_suite(session, principal, suite_id, request=request)


# ---------------------------------------------------------------------------
# Suite verbs
# ---------------------------------------------------------------------------


@router.post(
    "/suites/{suite_id}/run",
    response_model=TestRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a test suite",
)
async def run_suite(
    principal: CurrentPrincipal,
    session: Db,
    suite_id: str,
    background: BackgroundTasks,
    request: Request,
    payload: TestRunRequest | None = None,
) -> TestRunRead:
    """Queue a run and hand it to the background runner.

    One run per suite at a time: a second request while one is in flight is
    refused with 409 rather than racing it. Poll
    `GET /testing/suites/{id}/runs/{run_id}/progress` for the bar. Requires the
    member role.
    """
    run = await service.start_run(session, principal, suite_id, payload, request=request)
    # The runner opens its own session, so the row has to be durable before the
    # task may look for it. The request-scoped session commits only after the
    # response has been sent — and sending the response *includes* running the
    # background tasks — so without this the runner's SELECT raced the COMMIT,
    # and a run that lost sat Queued with nobody driving it: every later Run
    # answered 409 and Delete 412 until the stale sweep reaped it half an hour
    # on. Same order the exports route uses, for the same reason. The response
    # is built first: what is committed stays committed, so nothing that can
    # still fail may come between the commit and handing the run to its runner.
    records = await service.read_runs(session, principal, [run])
    await session.commit()
    background.add_task(service.execute_run, run.id, principal.workspace_id)
    return records[0]


@router.get(
    "/suites/{suite_id}/runs",
    response_model=Page[TestRunRead],
    summary="List a suite's runs",
)
async def list_suite_runs(
    principal: CurrentPrincipal,
    session: Db,
    suite_id: str,
    params: Annotated[ListParams, Depends(list_params)],
    run_status: Annotated[TestRunStatus | None, Query(alias="status")] = None,
    trigger: Annotated[RunTrigger | None, Query()] = None,
) -> Page[TestRunRead]:
    """One suite's run history, newest first."""
    rows, total = await service.list_runs(
        session,
        principal,
        params,
        suite_id=suite_id,
        status=run_status.value if run_status else None,
        trigger=trigger.value if trigger else None,
    )
    records = await service.read_runs(session, principal, rows)
    return Page.build(records, total, params.page, params.page_size)


@router.get(
    "/suites/{suite_id}/runs/{run_id}/progress",
    response_model=TestRunProgress,
    summary="Test run progress",
)
async def get_run_progress(
    principal: CurrentPrincipal, session: Db, suite_id: str, run_id: str
) -> TestRunProgress:
    """Measured progress of a run.

    Counts come from the runner driving it, or — when the run belongs to
    another worker — from what that runner last wrote onto the run. The
    percentage is derived from cases registered and cases judged; nothing here
    is a timer.
    """
    return await service.get_progress(session, principal, suite_id, run_id)


@router.post(
    "/suites/{suite_id}/runs/{run_id}/cancel",
    response_model=TestRunRead,
    summary="Cancel a test run",
)
async def cancel_run(
    principal: CurrentPrincipal,
    session: Db,
    suite_id: str,
    run_id: str,
    request: Request,
) -> TestRunRead:
    """Stop a queued or running run.

    The run ends as Cancelled with no verdict, and the suite can be run again
    or deleted at once. A run that has already ended answers 409. Requires the
    member role.
    """
    run = await service.cancel_run(session, principal, suite_id, run_id, request=request)
    # Durable first, then wake the supervisor: it re-reads the row to learn why
    # it was woken, and must find the cancellation there.
    await session.commit()
    service.runner.nudge(run.id)
    records = await service.read_runs(session, principal, [run])
    return records[0]


@router.post(
    "/suites/{suite_id}/promote-baseline",
    response_model=ActionResult,
    summary="Promote a baseline",
)
async def promote_baseline(
    principal: CurrentPrincipal,
    session: Db,
    suite_id: str,
    background: BackgroundTasks,
    request: Request,
    payload: BaselinePromote | None = None,
) -> ActionResult:
    """Make a finished run the suite's baseline.

    Defaults to the newest finished run. Only a finished run may be promoted:
    a queued or errored one has no verdicts for later runs to be diffed
    against. Requires the operator role.
    """
    suite, run = await service.promote_baseline(
        session, principal, suite_id, payload, request=request
    )
    return ActionResult(
        message=f"{suite.name} baseline is now {run.run_ref}.",
        entity_id=suite.id,
        data={
            "suite_id": suite.id,
            "baseline_run_id": run.id,
            "baseline": run.run_ref,
            "pass_rate": run.pass_rate,
        },
    )
