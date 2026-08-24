"""Testing & Regression business logic.

A suite is a dataset of cases plus the workflow state we own — environment,
owner, schedule, baseline. A run of that suite is an experiment in the
telemetry engine: this service creates it, registers the cases, supervises the
scoring, then writes the per-case verdicts onto the ``TestRun`` row. Those
stored verdicts are what makes everything downstream real — the baseline diff,
the regression count and the flaky detector all read run history rather than a
number somebody typed.

Every number on the screen is derived:

* ``pass_rate`` is passed cases over scored cases for that run;
* ``regression_count`` is cases that passed on the baseline and fail now;
* ``flaky_count`` is cases that flipped verdict across the recent run history,
  recomputed after every run;
* the KPI cards aggregate runs inside the window and compare against the one
  before it.

Cases the engine has not judged are ``Unscored`` and are excluded from the pass
rate rather than counted as failures — an unjudged case is missing evidence,
not a defect.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import (
    AppError,
    Conflict,
    NotFound,
    PreconditionFailed,
    ValidationFailed,
)
from ..db.session import get_sessionmaker
from ..engine import EngineClient, EngineError, EngineNotFound, get_engine_client
from ..models.identity import Role, User
from ..models.quality import (
    RunTrigger,
    SuiteStatus,
    SuiteType,
    TestRun,
    TestRunStatus,
    TestSuite,
)
from ..models.registry import Agent
from ..schemas.evaluations import average_score, metric_values
from ..schemas.testing import (
    CASE_PASS_SCORE,
    FLAKY_WINDOW_RUNS,
    TREND_POINTS,
    BaselinePromote,
    BaselineRead,
    CaseDelta,
    CaseStatus,
    EnvironmentBinding,
    RunComparison,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
    SuiteResult,
    TestCaseResult,
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
    verdict_for,
)
from . import audit
from .evaluations import (
    PHASE_FINISHED,
    PHASE_PREPARING,
    PHASE_REGISTERING,
    PHASE_SCORING,
    POLL_SECONDS,
    REGISTER_PHASE_WEIGHT,
    dataset_case_total,
    experiment_scored_count,
    item_experiment_result,
    namespaced,
    owner_names,
    register_experiment_items,
    resolve_dataset,
    translate_engine_error,
    workspace_namespace,
)
from .evaluations import engine_namespace as _engine_namespace

log = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "Testing & Regression Suite"
SUITE_ENTITY: Final[str] = "test_suite"
RUN_ENTITY: Final[str] = "test_run"

#: Window the KPI cards measure over.
WINDOW_DAYS: Final[int] = 30

#: A run may take this long before the supervisor stops waiting for verdicts.
EXECUTION_DEADLINE_SECONDS: Final[float] = 1800.0

#: Cases read back per engine round trip, and the ceiling on one run's cases.
CASE_PAGE_SIZE: Final[int] = 100
CASE_FETCH_LIMIT: Final[int] = 2000

#: Ceilings on the history scans behind the KPI cards and the flaky detector.
SUMMARY_RUN_LIMIT: Final[int] = 2000
FLAKY_SCAN_RUNS: Final[int] = 500
EXPORT_LIMIT: Final[int] = 5000

FINISHED_STATUSES: Final[tuple[str, ...]] = (
    TestRunStatus.PASSED.value,
    TestRunStatus.FAILED.value,
)
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        TestRunStatus.PASSED.value,
        TestRunStatus.FAILED.value,
        TestRunStatus.ERROR.value,
        TestRunStatus.CANCELLED.value,
    }
)

SORTABLE: Final[dict[str, Any]] = {
    "name": TestSuite.name,
    "suite_type": TestSuite.suite_type,
    "environment": TestSuite.environment,
    "status": TestSuite.status,
    "pass_rate": TestSuite.pass_rate,
    "case_count": TestSuite.case_count,
    "last_run_at": TestSuite.last_run_at,
    "flaky_count": TestSuite.flaky_count,
    "dataset": TestSuite.dataset_ref,
    "created_at": TestSuite.created_at,
    "updated_at": TestSuite.updated_at,
}

RUN_SORTABLE: Final[dict[str, Any]] = {
    "run_ref": TestRun.run_ref,
    "status": TestRun.status,
    "trigger": TestRun.trigger,
    "pass_rate": TestRun.pass_rate,
    "total_cases": TestRun.total_cases,
    "duration_seconds": TestRun.duration_seconds,
    "started_at": TestRun.started_at,
    "finished_at": TestRun.finished_at,
    "created_at": TestRun.created_at,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def format_duration(seconds: float | None) -> str | None:
    """Render a duration the way the KPI tile and the run table show it."""
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _system_principal(workspace_id: str) -> Principal:
    """Actor the run supervisor signs its own audit rows with."""
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="test-runner",
        display_name="Test Runner",
    )


# ---------------------------------------------------------------------------
# Per-case bookkeeping
# ---------------------------------------------------------------------------


def _cases_of(run: TestRun) -> list[dict[str, Any]]:
    """The per-case verdicts stored on a run, or an empty list."""
    cases = (run.summary or {}).get("cases")
    return [case for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []


def _case_results(run: TestRun) -> list[TestCaseResult]:
    results: list[TestCaseResult] = []
    for case in _cases_of(run):
        raw = case.get("status")
        try:
            status = CaseStatus(raw)
        except ValueError:
            status = CaseStatus.UNSCORED
        results.append(
            TestCaseResult(
                id=str(case.get("id") or ""),
                name=case.get("name"),
                status=status,
                score=case.get("score"),
                trace_id=case.get("trace_id"),
            )
        )
    return results


def _tally(cases: Sequence[dict[str, Any]]) -> tuple[int, int, int, int, float | None]:
    """Passed, failed, skipped, unscored and the pass rate over scored cases."""
    passed = sum(1 for case in cases if case.get("status") == CaseStatus.PASSED.value)
    failed = sum(1 for case in cases if case.get("status") == CaseStatus.FAILED.value)
    skipped = sum(1 for case in cases if case.get("status") == CaseStatus.SKIPPED.value)
    unscored = sum(1 for case in cases if case.get("status") == CaseStatus.UNSCORED.value)
    scored = passed + failed
    rate = round(passed / scored * 100, 1) if scored else None
    return passed, failed, skipped, unscored, rate


def _diff_cases(
    baseline: Sequence[dict[str, Any]], candidate: Sequence[dict[str, Any]]
) -> list[CaseDelta]:
    """Case-by-case movement between two runs, regressions first."""
    before = {str(case.get("id")): case for case in baseline if case.get("id")}
    after = {str(case.get("id")): case for case in candidate if case.get("id")}

    def _status(case: dict[str, Any] | None) -> CaseStatus | None:
        if case is None or case.get("status") not in _CASE_VALUES:
            return None
        return CaseStatus(case["status"])

    deltas: list[CaseDelta] = []
    for case_id in sorted(set(before) | set(after)):
        old, new = before.get(case_id), after.get(case_id)
        old_status, new_status = _status(old), _status(new)

        if old is None:
            change = "Added"
        elif new is None:
            change = "Removed"
        elif old_status is CaseStatus.PASSED and new_status is CaseStatus.FAILED:
            change = "Regression"
        elif old_status is CaseStatus.FAILED and new_status is CaseStatus.PASSED:
            change = "Fix"
        else:
            change = "Unchanged"

        deltas.append(
            CaseDelta(
                id=case_id,
                name=(new or old or {}).get("name"),
                baseline_status=old_status,
                candidate_status=new_status,
                baseline_score=(old or {}).get("score"),
                candidate_score=(new or {}).get("score"),
                change=change,
                is_regression=change == "Regression",
            )
        )
    deltas.sort(key=lambda d: (not d.is_regression, d.change != "Fix", d.id))
    return deltas


_CASE_VALUES: Final[frozenset[str]] = frozenset(status.value for status in CaseStatus)


def _compare_payload(
    baseline: TestRun | None, candidate_cases: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """What a finished run stores about its diff against the baseline."""
    if baseline is None:
        return {"baseline_run_id": None, "regressions": [], "fixes": []}
    deltas = _diff_cases(_cases_of(baseline), candidate_cases)
    regressions = [delta.id for delta in deltas if delta.is_regression]
    fixes = [delta.id for delta in deltas if delta.change == "Fix"]
    _, _, _, _, candidate_rate = _tally(candidate_cases)
    return {
        "baseline_run_id": baseline.id,
        "baseline_run_ref": baseline.run_ref,
        "baseline_pass_rate": baseline.pass_rate,
        "pass_rate_delta": (
            None
            if candidate_rate is None or baseline.pass_rate is None
            else round(candidate_rate - baseline.pass_rate, 1)
        ),
        "regressions": regressions,
        "fixes": fixes,
        "added": [delta.id for delta in deltas if delta.change == "Added"],
        "removed": [delta.id for delta in deltas if delta.change == "Removed"],
    }


def _flaky_cases(runs: Sequence[TestRun]) -> set[str]:
    """Cases that changed verdict more than once across the given runs.

    ``runs`` must be ordered oldest first. A case that failed once and stayed
    failed is broken, not flaky; a case that alternates is flaky. Requiring two
    transitions is what keeps a single genuine regression out of this count.
    """
    sequences: dict[str, list[str]] = {}
    for run in runs:
        for case in _cases_of(run):
            status = case.get("status")
            if status in (CaseStatus.PASSED.value, CaseStatus.FAILED.value):
                sequences.setdefault(str(case.get("id")), []).append(status)

    flaky: set[str] = set()
    for case_id, verdicts in sequences.items():
        if len(set(verdicts)) < 2:
            continue
        transitions = sum(
            1 for before, after in zip(verdicts, verdicts[1:], strict=False) if before != after
        )
        if transitions >= 2:
            flaky.add(case_id)
    return flaky


async def _recent_runs(
    session: AsyncSession, workspace_id: str, suite_id: str, limit: int
) -> list[TestRun]:
    """The suite's most recent finished runs, oldest first."""
    rows = (
        (
            await session.execute(
                select(TestRun)
                .where(
                    TestRun.workspace_id == workspace_id,
                    TestRun.suite_id == suite_id,
                    TestRun.status.in_(FINISHED_STATUSES),
                )
                .order_by(TestRun.finished_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


async def refresh_flaky_count(
    session: AsyncSession, workspace_id: str, suite: TestSuite
) -> int:
    """Recompute the suite's flaky count from its run history."""
    runs = await _recent_runs(session, workspace_id, suite.id, FLAKY_WINDOW_RUNS)
    suite.flaky_count = len(_flaky_cases(runs))
    return suite.flaky_count


# ---------------------------------------------------------------------------
# The run supervisor
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RunState:
    """Live counters for one supervised run, held only in this process."""

    total: int = 0
    processed: int = 0
    scored: int = 0
    passed: int = 0
    failed: int = 0
    phase: str = PHASE_PREPARING
    detail: str | None = None


class SuiteRunner:
    """Owns the asyncio task that drives one suite run to a terminal state."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._state: dict[str, RunState] = {}

    def start(self, run_id: str, workspace_id: str) -> bool:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return False
        self._state[run_id] = RunState()
        task = asyncio.create_task(self._run(run_id, workspace_id), name=f"test-run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._forget(run_id, t))
        return True

    def state(self, run_id: str) -> RunState | None:
        return self._state.get(run_id)

    def _forget(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # pragma: no cover - _run records its own failures
            log.error("test run %s ended in error: %s", run_id, error)

    async def _run(self, run_id: str, workspace_id: str) -> None:
        state = self._state.setdefault(run_id, RunState())
        try:
            await self._execute(run_id, workspace_id, state)
        except asyncio.CancelledError:
            await self._abandon(run_id, workspace_id, "The run was cancelled.")
            raise
        except AppError as exc:
            await self._abandon(run_id, workspace_id, exc.message)
        except Exception:
            log.exception("test run %s aborted", run_id)
            await self._abandon(
                run_id, workspace_id, "The run stopped on an unexpected error."
            )
        finally:
            state.phase = PHASE_FINISHED

    async def _execute(self, run_id: str, workspace_id: str, state: RunState) -> None:
        client = get_engine_client()

        async with get_sessionmaker()() as session:
            run = await _load_run(session, workspace_id, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            suite = await session.get(TestSuite, run.suite_id)
            if suite is None:
                raise NotFound("The suite this run belongs to no longer exists.")
            dataset_ref = suite.dataset_ref
            suite_id = suite.id
            suite_name = suite.name
            run_ref = run.run_ref
            namespace = await workspace_namespace(session, workspace_id)
            run.status = TestRunStatus.RUNNING.value
            run.started_at = _now()
            await session.commit()

        dataset = await resolve_dataset(client, namespace, dataset_ref)
        dataset_id = str(dataset.get("id") or "")
        total = await dataset_case_total(client, dataset_id) if dataset_id else 0
        if total <= 0:
            raise PreconditionFailed(
                f"Dataset '{dataset_ref}' holds no cases, so there is nothing to run."
            )
        state.total = total
        state.phase = PHASE_REGISTERING

        experiment_name = namespaced(namespace, f"suite-{suite_id}-{run_ref}")
        engine_dataset = namespaced(namespace, dataset_ref)
        try:
            experiment = await client.create_experiment(
                experiment_name,
                dataset_name=engine_dataset,
                metadata={
                    "suite_id": suite_id,
                    "suite_name": suite_name,
                    "run_ref": run_ref,
                    "source": SOURCE_SCREEN,
                },
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        experiment_id = str(experiment.get("id") or "") or None

        async with get_sessionmaker()() as session:
            run = await _load_run(session, workspace_id, run_id)
            if run is not None:
                run.engine_experiment_id = experiment_id
                run.total_cases = total
                await session.commit()

        def _registered(done: int) -> None:
            state.processed = done

        await register_experiment_items(
            client,
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            dataset_name=engine_dataset,
            on_batch=_registered,
        )
        state.processed = total
        state.phase = PHASE_SCORING

        cases = await self._await_cases(client, experiment_id, dataset_id, total, state)
        await self._finish(run_id, workspace_id, cases, total)

    async def _await_cases(
        self,
        client: EngineClient,
        experiment_id: str | None,
        dataset_id: str,
        total: int,
        state: RunState,
    ) -> list[dict[str, Any]]:
        """Poll until every case has a verdict, or the deadline passes."""
        deadline = asyncio.get_running_loop().time() + EXECUTION_DEADLINE_SECONDS
        cases: list[dict[str, Any]] = []
        while True:
            if experiment_id is not None:
                with contextlib.suppress(EngineNotFound):
                    payload = await client.get_experiment(experiment_id)
                    state.scored = min(total, experiment_scored_count(payload))

            cases = await _read_cases(client, dataset_id, experiment_id, total)
            scored = sum(
                1
                for case in cases
                if case.get("status") in (CaseStatus.PASSED.value, CaseStatus.FAILED.value)
            )
            state.scored = max(state.scored, scored)
            state.passed = sum(
                1 for case in cases if case.get("status") == CaseStatus.PASSED.value
            )
            state.failed = sum(
                1 for case in cases if case.get("status") == CaseStatus.FAILED.value
            )
            if scored >= total:
                return cases
            if asyncio.get_running_loop().time() >= deadline:
                return cases
            await asyncio.sleep(POLL_SECONDS)

    async def _finish(
        self,
        run_id: str,
        workspace_id: str,
        cases: Sequence[dict[str, Any]],
        total: int,
    ) -> None:
        async with get_sessionmaker()() as session:
            run = await _load_run(session, workspace_id, run_id)
            if run is None:
                return
            suite = await session.get(TestSuite, run.suite_id)

            passed, failed, skipped, unscored, rate = _tally(cases)
            baseline = (
                await _load_run(session, workspace_id, suite.baseline_run_id)
                if suite and suite.baseline_run_id
                else None
            )
            comparison = _compare_payload(baseline, cases)

            finished = _now()
            run.finished_at = finished
            run.duration_seconds = (
                None
                if run.started_at is None
                else round((finished - _as_utc(run.started_at)).total_seconds(), 1)
            )
            run.total_cases = total
            run.passed = passed
            run.failed = failed
            run.skipped = skipped
            run.pass_rate = rate
            run.regression_count = len(comparison.get("regressions") or [])
            run.baseline_comparison = comparison
            run.summary = {
                "cases": list(cases),
                "scored": passed + failed,
                "unscored": unscored,
                "engine_experiment_id": run.engine_experiment_id,
            }
            if passed + failed == 0:
                run.status = TestRunStatus.ERROR.value
                detail = (
                    "The telemetry store returned no verdicts within "
                    f"{int(EXECUTION_DEADLINE_SECONDS)}s."
                )
            else:
                run.status = (
                    TestRunStatus.PASSED.value if failed == 0 else TestRunStatus.FAILED.value
                )
                detail = (
                    f"{passed} passed, {failed} failed, {unscored} unscored"
                    + (f"; pass rate {rate:.1f}%" if rate is not None else "")
                )

            if suite is not None:
                suite.last_run_at = finished
                suite.case_count = total
                if rate is not None:
                    suite.pass_rate = rate
                await session.flush()
                await refresh_flaky_count(session, workspace_id, suite)

            await audit.record(
                session,
                principal=_system_principal(workspace_id),
                action=(
                    "test_run.completed"
                    if run.status != TestRunStatus.ERROR.value
                    else "test_run.errored"
                ),
                entity_type=RUN_ENTITY,
                entity_id=run.id,
                entity_label=run.run_ref,
                source_screen=SOURCE_SCREEN,
                detail=detail,
                metadata={
                    "pass_rate": rate,
                    "regressions": run.regression_count,
                    "suite_id": run.suite_id,
                },
            )
            await session.commit()

    async def _abandon(self, run_id: str, workspace_id: str, reason: str) -> None:
        async with get_sessionmaker()() as session:
            run = await _load_run(session, workspace_id, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            run.status = TestRunStatus.ERROR.value
            run.finished_at = _now()
            run.summary = {**(run.summary or {}), "error": reason}
            await audit.record(
                session,
                principal=_system_principal(workspace_id),
                action="test_run.errored",
                entity_type=RUN_ENTITY,
                entity_id=run.id,
                entity_label=run.run_ref,
                source_screen=SOURCE_SCREEN,
                detail=reason,
            )
            await session.commit()


runner = SuiteRunner()


async def _read_cases(
    client: EngineClient, dataset_id: str, experiment_id: str | None, total: int
) -> list[dict[str, Any]]:
    """Per-case verdicts for this experiment, read from the engine.

    A case the engine has judged carries a score; one it has not is recorded as
    ``Unscored`` so the run reports missing evidence instead of a false failure.
    """
    if not dataset_id:
        return []
    cases: list[dict[str, Any]] = []
    page = 1
    while len(cases) < min(total, CASE_FETCH_LIMIT):
        try:
            payload = await client.list_dataset_items(
                dataset_id, page=page, size=CASE_PAGE_SIZE
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        rows = payload.get("content") or payload.get("items") or []
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            result = item_experiment_result(row, experiment_id) or {}
            scores = metric_values(result.get("feedback_scores") or result.get("scores"))
            score = average_score(dict(scores))
            if score is None:
                status = CaseStatus.UNSCORED.value
            elif score >= CASE_PASS_SCORE:
                status = CaseStatus.PASSED.value
            else:
                status = CaseStatus.FAILED.value
            name = data.get("name") or data.get("case") or data.get("input")
            cases.append(
                {
                    "id": str(row.get("id") or ""),
                    "name": str(name)[:120] if isinstance(name, str) else None,
                    "status": status,
                    "score": score,
                    "trace_id": result.get("trace_id"),
                }
            )
        if len(rows) < CASE_PAGE_SIZE:
            break
        page += 1
    return cases


async def _load_run(
    session: AsyncSession, workspace_id: str, run_id: str | None
) -> TestRun | None:
    if not run_id:
        return None
    return (
        await session.execute(
            select(TestRun).where(
                TestRun.workspace_id == workspace_id, TestRun.id == run_id
            )
        )
    ).scalar_one_or_none()


async def execute_run(run_id: str, workspace_id: str) -> None:
    """Background entry point used by the routes."""
    runner.start(run_id, workspace_id)


# ---------------------------------------------------------------------------
# Suite reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(TestSuite).where(TestSuite.workspace_id == principal.workspace_id)


def _filtered_stmt(
    principal: Principal,
    params: ListParams,
    *,
    suite_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    owner_user_id: str | None = None,
    agent_id: str | None = None,
    scheduled: bool | None = None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt, params, [TestSuite.name, TestSuite.suite_type, TestSuite.dataset_ref]
    )
    stmt = apply_filters(
        stmt,
        {
            TestSuite.suite_type: suite_type,
            TestSuite.status: status,
            TestSuite.environment: environment,
            TestSuite.owner_user_id: owner_user_id,
            TestSuite.agent_id: agent_id,
        },
    )
    if scheduled is True:
        stmt = stmt.where(TestSuite.schedule_cron.is_not(None))
    elif scheduled is False:
        stmt = stmt.where(TestSuite.schedule_cron.is_(None))
    return apply_sort(stmt, params, SORTABLE, default=TestSuite.name, default_desc=False)


async def _latest_runs(
    session: AsyncSession, principal: Principal, suite_ids: Sequence[str]
) -> dict[str, list[TestRun]]:
    """Recent runs per suite, newest first, for the result, trend and duration.

    One statement covers the page. The runs are ranked *per suite* with a
    window function rather than by one global ``ORDER BY … LIMIT``: a single
    busy suite would otherwise fill the window and leave the quieter suites on
    the page reading as though they had never run.
    """
    if not suite_ids:
        return {}
    ranked = (
        select(
            TestRun,
            func.row_number()
            .over(partition_by=TestRun.suite_id, order_by=TestRun.created_at.desc())
            .label("rank"),
        )
        .where(
            TestRun.workspace_id == principal.workspace_id,
            TestRun.suite_id.in_(list(suite_ids)),
        )
        .subquery()
    )
    ranked_run = aliased(TestRun, ranked)
    rows = (
        (
            await session.execute(
                select(ranked_run)
                .where(ranked.c.rank <= TREND_POINTS)
                .order_by(ranked.c.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    buckets: dict[str, list[TestRun]] = {}
    for row in rows:
        buckets.setdefault(row.suite_id, []).append(row)
    return buckets


def _suite_read(
    suite: TestSuite,
    *,
    runs: Sequence[TestRun] = (),
    baseline: TestRun | None = None,
    owner: str | None = None,
    team: str | None = None,
    agent_name: str | None = None,
) -> TestSuiteRead:
    latest = runs[0] if runs else None
    finished = [run for run in runs if run.status in FINISHED_STATUSES]
    last_finished = finished[0] if finished else None

    if latest is not None and latest.status in (
        TestRunStatus.RUNNING.value,
        TestRunStatus.QUEUED.value,
    ):
        result = verdict_for(latest.status, latest.pass_rate)
    elif last_finished is not None:
        result = verdict_for(last_finished.status, last_finished.pass_rate)
    elif latest is not None:
        result = verdict_for(latest.status, latest.pass_rate)
    else:
        result = SuiteResult.NEVER_RUN

    durations = [run.duration_seconds for run in finished if run.duration_seconds is not None]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    trend = [
        run.pass_rate
        for run in reversed(finished[:TREND_POINTS])
        if run.pass_rate is not None
    ]

    return TestSuiteRead(
        id=suite.id,
        name=suite.name,
        suite_type=SuiteType(suite.suite_type),
        environment=suite.environment,
        status=SuiteStatus(suite.status),
        result=result,
        agent_id=suite.agent_id,
        agent_name=agent_name,
        dataset=suite.dataset_ref,
        case_count=suite.case_count,
        pass_rate=(last_finished.pass_rate if last_finished else suite.pass_rate),
        last_run_at=suite.last_run_at,
        last_run_id=latest.id if latest else None,
        next_run_at=(
            next_fire(suite.schedule_cron, _now()) if suite.schedule_cron else None
        ),
        schedule_cron=suite.schedule_cron,
        baseline_run_id=suite.baseline_run_id,
        baseline=baseline.run_ref if baseline else None,
        owner_user_id=suite.owner_user_id,
        owner_name=owner,
        team=team,
        flaky_count=suite.flaky_count,
        regressions=last_finished.regression_count if last_finished else 0,
        avg_duration_seconds=avg_duration,
        avg_duration_label=format_duration(avg_duration),
        trend=trend,
        tags=[tag for tag in (suite.tags or []) if isinstance(tag, str)],
        created_at=suite.created_at,
        updated_at=suite.updated_at,
        created_by=suite.created_by,
        updated_by=suite.updated_by,
    )


async def read_suites(
    session: AsyncSession, principal: Principal, rows: Sequence[TestSuite]
) -> list[TestSuiteRead]:
    """Turn suites into table records, resolving runs, owners and agents."""
    runs = await _latest_runs(session, principal, [row.id for row in rows])

    baseline_ids = [row.baseline_run_id for row in rows if row.baseline_run_id]
    baselines: dict[str, TestRun] = {}
    if baseline_ids:
        found = (
            (
                await session.execute(
                    select(TestRun).where(
                        TestRun.workspace_id == principal.workspace_id,
                        TestRun.id.in_(baseline_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        baselines = {run.id: run for run in found}

    owner_ids = {row.owner_user_id for row in rows if row.owner_user_id}
    teams: dict[str, str | None] = {}
    if owner_ids:
        teams = {
            user_id: team
            for user_id, team in (
                await session.execute(
                    select(User.id, User.team).where(User.id.in_(owner_ids))
                )
            ).all()
        }
    owners = await owner_names(session, principal, list(owner_ids))

    agent_ids = {row.agent_id for row in rows if row.agent_id}
    agents: dict[str, str] = {}
    if agent_ids:
        agents = {
            agent_id: name
            for agent_id, name in (
                await session.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.workspace_id == principal.workspace_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).all()
        }

    return [
        _suite_read(
            row,
            runs=runs.get(row.id, ()),
            baseline=baselines.get(row.baseline_run_id or ""),
            owner=owners.get(row.owner_user_id or ""),
            team=teams.get(row.owner_user_id or ""),
            agent_name=agents.get(row.agent_id or ""),
        )
        for row in rows
    ]


async def list_suites(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    suite_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    owner_user_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[Sequence[TestSuite], int]:
    """One page of the workspace's suites, ordered by name by default."""
    stmt = _filtered_stmt(
        principal,
        params,
        suite_type=suite_type,
        status=status,
        environment=environment,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
    )
    return await paginate(session, stmt, params)


async def export_suites(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    suite_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    owner_user_id: str | None = None,
    agent_id: str | None = None,
) -> Sequence[TestSuite]:
    """Every suite matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    stmt = _filtered_stmt(
        principal,
        params,
        suite_type=suite_type,
        status=status,
        environment=environment,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
    ).limit(EXPORT_LIMIT)
    return (await session.execute(stmt)).scalars().all()


async def get_suite(session: AsyncSession, principal: Principal, suite_id: str) -> TestSuite:
    """Load one suite, or raise :class:`NotFound`."""
    suite = (
        await session.execute(_scoped(principal).where(TestSuite.id == suite_id))
    ).scalar_one_or_none()
    if suite is None:
        raise NotFound(f"Test suite '{suite_id}' does not exist.")
    return suite


# ---------------------------------------------------------------------------
# Run reads
# ---------------------------------------------------------------------------


def _run_read(run: TestRun, *, suite: TestSuite | None = None) -> TestRunRead:
    return TestRunRead(
        id=run.id,
        run_ref=run.run_ref,
        suite_id=run.suite_id,
        suite_name=suite.name if suite else None,
        environment=suite.environment if suite else None,
        status=TestRunStatus(run.status),
        result=verdict_for(run.status, run.pass_rate),
        trigger=RunTrigger(run.trigger),
        total_cases=run.total_cases,
        passed=run.passed,
        failed=run.failed,
        skipped=run.skipped,
        pass_rate=run.pass_rate,
        regression_count=run.regression_count,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        duration_label=format_duration(run.duration_seconds),
        triggered_by_user_id=run.triggered_by_user_id,
        engine_experiment_id=run.engine_experiment_id,
        baseline_run_id=(run.baseline_comparison or {}).get("baseline_run_id"),
        created_at=run.created_at,
    )


async def read_runs(
    session: AsyncSession, principal: Principal, rows: Sequence[TestRun]
) -> list[TestRunRead]:
    """Turn runs into table records, resolving suite names in one query."""
    suite_ids = {row.suite_id for row in rows}
    suites: dict[str, TestSuite] = {}
    if suite_ids:
        found = (
            (
                await session.execute(
                    _scoped(principal).where(TestSuite.id.in_(suite_ids))
                )
            )
            .scalars()
            .all()
        )
        suites = {suite.id: suite for suite in found}
    return [_run_read(row, suite=suites.get(row.suite_id)) for row in rows]


async def list_runs(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    suite_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
) -> tuple[Sequence[TestRun], int]:
    """One page of runs, newest first, optionally for one suite."""
    if suite_id is not None:
        await get_suite(session, principal, suite_id)

    stmt = select(TestRun).where(TestRun.workspace_id == principal.workspace_id)
    stmt = apply_search(stmt, params, [TestRun.run_ref])
    stmt = apply_filters(
        stmt,
        {TestRun.suite_id: suite_id, TestRun.status: status, TestRun.trigger: trigger},
    )
    stmt = apply_sort(stmt, params, RUN_SORTABLE, default=TestRun.created_at, default_desc=True)
    return await paginate(session, stmt, params)


async def get_run(session: AsyncSession, principal: Principal, run_id: str) -> TestRun:
    """Load one run, or raise :class:`NotFound`."""
    run = await _load_run(session, principal.workspace_id, run_id)
    if run is None:
        raise NotFound(f"Test run '{run_id}' does not exist.")
    return run


async def get_run_detail(
    session: AsyncSession, principal: Principal, run_id: str
) -> TestRunDetail:
    """One run with its per-case results."""
    run = await get_run(session, principal, run_id)
    suite = (
        await session.execute(_scoped(principal).where(TestSuite.id == run.suite_id))
    ).scalar_one_or_none()
    record = _run_read(run, suite=suite)
    return TestRunDetail(
        **record.model_dump(),
        cases=_case_results(run),
        unscored=int((run.summary or {}).get("unscored") or 0),
        baseline_comparison=dict(run.baseline_comparison or {}),
    )


async def get_progress(
    session: AsyncSession, principal: Principal, suite_id: str, run_id: str
) -> TestRunProgress:
    """Real progress: measured counts from the runner, or from the engine."""
    await get_suite(session, principal, suite_id)
    run = await get_run(session, principal, run_id)
    if run.suite_id != suite_id:
        raise NotFound(f"Test run '{run_id}' does not belong to suite '{suite_id}'.")

    terminal = run.status in TERMINAL_STATUSES
    state = runner.state(run_id)
    total = run.total_cases
    processed = run.total_cases if terminal else 0
    scored = run.passed + run.failed if terminal else 0
    passed, failed = run.passed, run.failed
    phase = PHASE_FINISHED if terminal else PHASE_PREPARING
    detail = (run.summary or {}).get("error")

    if state is not None and not terminal:
        total = state.total or total
        processed = state.processed
        scored = state.scored
        passed, failed = state.passed, state.failed
        phase = state.phase
        detail = state.detail or detail
    elif not terminal and run.engine_experiment_id:
        client = get_engine_client()
        with contextlib.suppress(EngineError):
            payload = await client.get_experiment(run.engine_experiment_id)
            scored = min(total, experiment_scored_count(payload))
            processed = total
            phase = PHASE_SCORING

    if terminal:
        percent = 100.0
    elif total:
        register = min(1.0, processed / total) * REGISTER_PHASE_WEIGHT
        percent = round(
            min(99.0, register + min(1.0, scored / total) * (100.0 - REGISTER_PHASE_WEIGHT)),
            1,
        )
    else:
        percent = 0.0

    elapsed = (
        (_as_utc(run.finished_at or _now()) - _as_utc(run.started_at)).total_seconds()
        if run.started_at
        else None
    )
    return TestRunProgress(
        run_id=run.id,
        run_ref=run.run_ref,
        suite_id=run.suite_id,
        status=TestRunStatus(run.status),
        phase=phase,
        total_cases=total,
        processed_cases=processed,
        scored_cases=scored,
        passed=passed,
        failed=failed,
        percent=percent,
        is_terminal=terminal,
        started_at=run.started_at,
        finished_at=run.finished_at,
        elapsed_seconds=None if elapsed is None else round(elapsed, 1),
        detail=detail if isinstance(detail, str) else None,
    )


# ---------------------------------------------------------------------------
# Suite writes
# ---------------------------------------------------------------------------


async def _validate_agent(
    session: AsyncSession, principal: Principal, agent_id: str | None
) -> None:
    if agent_id is None:
        return
    agent = (
        await session.execute(
            select(Agent.id).where(
                Agent.workspace_id == principal.workspace_id, Agent.id == agent_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound(f"Agent '{agent_id}' does not exist.")


async def create_suite(
    session: AsyncSession,
    principal: Principal,
    payload: TestSuiteCreate,
    *,
    request: Request | None = None,
) -> TestSuite:
    """Create a suite once its dataset is known to exist in the engine."""
    principal.require(Role.MEMBER)
    await _validate_agent(session, principal, payload.agent_id)

    clash = (
        await session.execute(_scoped(principal).where(TestSuite.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A test suite named '{payload.name}' already exists.")

    client = get_engine_client()
    dataset = await resolve_dataset(client, _engine_namespace(principal), payload.dataset)
    case_count = await dataset_case_total(client, str(dataset.get("id") or ""))

    suite = TestSuite(
        workspace_id=principal.workspace_id,
        name=payload.name,
        suite_type=payload.suite_type.value,
        environment=payload.environment,
        status=payload.status.value,
        agent_id=payload.agent_id,
        dataset_ref=payload.dataset,
        case_count=case_count,
        schedule_cron=payload.schedule_cron,
        owner_user_id=payload.owner_user_id or principal.user_id,
        tags=list(payload.tags),
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(suite)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A test suite named '{payload.name}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="test_suite.created",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{suite.suite_type} suite on {suite.environment} "
            f"over {suite.dataset_ref} ({case_count} case(s))"
        ),
        metadata={"dataset": suite.dataset_ref, "cases": case_count},
        request=request,
    )
    await session.flush()
    await session.refresh(suite)
    return suite


async def update_suite(
    session: AsyncSession,
    principal: Principal,
    suite_id: str,
    payload: TestSuiteUpdate,
    *,
    request: Request | None = None,
) -> TestSuite:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.MEMBER)
    suite = await get_suite(session, principal, suite_id)

    if payload.expected_updated_at is not None and suite.updated_at is not None:
        expected = _as_utc(payload.expected_updated_at)
        if abs((_as_utc(suite.updated_at) - expected).total_seconds()) > 1:
            raise Conflict(f"'{suite.name}' was changed by someone else. Reload and try again.")

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return suite

    if "name" in changes and changes["name"] != suite.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    TestSuite.name == changes["name"], TestSuite.id != suite.id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A test suite named '{changes['name']}' already exists.")

    if "agent_id" in changes:
        await _validate_agent(session, principal, changes["agent_id"])

    if "dataset" in changes and changes["dataset"] != suite.dataset_ref:
        client = get_engine_client()
        dataset = await resolve_dataset(
            client, _engine_namespace(principal), changes["dataset"]
        )
        suite.dataset_ref = changes.pop("dataset")
        suite.case_count = await dataset_case_total(client, str(dataset.get("id") or ""))
    else:
        changes.pop("dataset", None)

    for field, value in changes.items():
        setattr(suite, field, value.value if isinstance(value, (SuiteType, SuiteStatus)) else value)
    suite.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A test suite named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="test_suite.updated",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    await session.refresh(suite)
    return suite


async def delete_suite(
    session: AsyncSession,
    principal: Principal,
    suite_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a suite and its runs. The audit trail survives the deletion."""
    principal.require(Role.ADMIN)
    suite = await get_suite(session, principal, suite_id)

    live = (
        await session.execute(
            select(func.count(TestRun.id)).where(
                TestRun.workspace_id == principal.workspace_id,
                TestRun.suite_id == suite.id,
                TestRun.status.in_((TestRunStatus.QUEUED.value, TestRunStatus.RUNNING.value)),
            )
        )
    ).scalar_one()
    if live:
        raise PreconditionFailed(
            f"'{suite.name}' has {live} run(s) in flight. Wait for them to finish."
        )

    await audit.record(
        session,
        principal=principal,
        action="test_suite.deleted",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed {suite.suite_type} suite",
        request=request,
    )
    await session.delete(suite)
    await session.flush()


async def _next_run_ref(session: AsyncSession, workspace_id: str) -> str:
    """A short, workspace-unique reference for the run.

    Sequential so operators can read it, but checked before it is used: two
    runs started in the same second would otherwise collide on the unique
    index, and a random suffix is a better answer than a failed request.
    """
    used = (
        await session.execute(
            select(func.count(TestRun.id)).where(TestRun.workspace_id == workspace_id)
        )
    ).scalar_one()
    candidate = f"tr-{int(used) + 1:05d}"
    taken = (
        await session.execute(
            select(TestRun.id).where(
                TestRun.workspace_id == workspace_id, TestRun.run_ref == candidate
            )
        )
    ).scalar_one_or_none()
    return candidate if taken is None else f"tr-{uuid.uuid4().hex[:8]}"


async def start_run(
    session: AsyncSession,
    principal: Principal,
    suite_id: str,
    payload: TestRunRequest | None = None,
    *,
    request: Request | None = None,
) -> TestRun:
    """Queue a run of the suite. The runner is started by the route afterwards."""
    principal.require(Role.MEMBER)
    suite = await get_suite(session, principal, suite_id)
    payload = payload or TestRunRequest()

    if suite.status == SuiteStatus.DISABLED.value:
        raise PreconditionFailed(f"'{suite.name}' is disabled. Enable it before running it.")

    live = (
        await session.execute(
            select(TestRun).where(
                TestRun.workspace_id == principal.workspace_id,
                TestRun.suite_id == suite.id,
                TestRun.status.in_((TestRunStatus.QUEUED.value, TestRunStatus.RUNNING.value)),
            )
        )
    ).scalars().first()
    if live is not None:
        raise Conflict(f"'{suite.name}' is already running as {live.run_ref}.")

    run = TestRun(
        workspace_id=principal.workspace_id,
        suite_id=suite.id,
        run_ref=await _next_run_ref(session, principal.workspace_id),
        status=TestRunStatus.QUEUED.value,
        total_cases=suite.case_count,
        trigger=payload.trigger.value,
        triggered_by_user_id=principal.user_id,
        summary={"notes": payload.notes} if payload.notes else {},
        baseline_comparison={},
    )
    session.add(run)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="test_run.started",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        entity_label=run.run_ref,
        source_screen=SOURCE_SCREEN,
        detail=f"Queued {suite.name} ({payload.trigger.value})",
        metadata={"suite_id": suite.id, "trigger": payload.trigger.value},
        request=request,
    )
    await session.flush()
    await session.refresh(run)
    return run


async def promote_baseline(
    session: AsyncSession,
    principal: Principal,
    suite_id: str,
    payload: BaselinePromote | None = None,
    *,
    request: Request | None = None,
) -> tuple[TestSuite, TestRun]:
    """Make a finished run the suite's baseline.

    Only a finished run may become a baseline: a queued or errored run has no
    verdicts to diff future runs against.
    """
    principal.require(Role.OPERATOR)
    suite = await get_suite(session, principal, suite_id)
    payload = payload or BaselinePromote()

    if payload.run_id:
        run = await get_run(session, principal, payload.run_id)
        if run.suite_id != suite.id:
            raise ValidationFailed(f"Run '{run.run_ref}' does not belong to '{suite.name}'.")
    else:
        run = (
            (
                await session.execute(
                    select(TestRun)
                    .where(
                        TestRun.workspace_id == principal.workspace_id,
                        TestRun.suite_id == suite.id,
                        TestRun.status.in_(FINISHED_STATUSES),
                    )
                    .order_by(TestRun.finished_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if run is None:
            raise PreconditionFailed(f"'{suite.name}' has no finished run to promote.")

    if run.status not in FINISHED_STATUSES:
        raise PreconditionFailed(
            f"Run '{run.run_ref}' is {run.status}; only a finished run can be a baseline."
        )
    if suite.baseline_run_id == run.id:
        raise Conflict(f"Run '{run.run_ref}' is already the baseline for '{suite.name}'.")

    previous = suite.baseline_run_id
    suite.baseline_run_id = run.id
    suite.updated_by = principal.actor
    await session.flush()

    detail = f"Baseline set to {run.run_ref}"
    if run.pass_rate is not None:
        detail += f" (pass rate {run.pass_rate:.1f}%)"
    if payload.reason:
        detail += f" - {payload.reason}"

    await audit.record(
        session,
        principal=principal,
        action="test_suite.baseline_promoted",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"previous_baseline_run_id": previous, "baseline_run_id": run.id},
        request=request,
    )
    await session.flush()
    await session.refresh(suite)
    return suite, run


# ---------------------------------------------------------------------------
# Comparison, baselines, schedules and environments
# ---------------------------------------------------------------------------


async def compare_runs(
    session: AsyncSession, principal: Principal, baseline_id: str, candidate_id: str
) -> RunComparison:
    """Two runs case by case, regressions first."""
    if baseline_id == candidate_id:
        raise ValidationFailed("Choose two different runs to compare.")
    baseline = await get_run(session, principal, baseline_id)
    candidate = await get_run(session, principal, candidate_id)

    deltas = _diff_cases(_cases_of(baseline), _cases_of(candidate))
    regressions = sum(1 for delta in deltas if delta.is_regression)
    fixes = sum(1 for delta in deltas if delta.change == "Fix")
    added = sum(1 for delta in deltas if delta.change == "Added")
    removed = sum(1 for delta in deltas if delta.change == "Removed")
    unchanged = sum(1 for delta in deltas if delta.change == "Unchanged")

    pass_rate_delta = (
        None
        if baseline.pass_rate is None or candidate.pass_rate is None
        else round(candidate.pass_rate - baseline.pass_rate, 1)
    )
    if regressions and regressions > fixes:
        verdict = "Regressed"
    elif fixes > regressions:
        verdict = "Improved"
    else:
        verdict = "Unchanged"

    reads = await read_runs(session, principal, [baseline, candidate])
    return RunComparison(
        baseline=reads[0],
        candidate=reads[1],
        pass_rate_delta=pass_rate_delta,
        regressions=regressions,
        fixes=fixes,
        added=added,
        removed=removed,
        unchanged=unchanged,
        verdict=verdict,
        cases=deltas,
    )


async def list_baselines(
    session: AsyncSession, principal: Principal, params: ListParams
) -> tuple[list[BaselineRead], int]:
    """The Baselines tab: each suite's baseline against its newest finished run."""
    rows, total = await paginate(session, _filtered_stmt(principal, params), params)
    if not rows:
        return [], total

    runs = await _latest_runs(session, principal, [row.id for row in rows])
    baseline_ids = [row.baseline_run_id for row in rows if row.baseline_run_id]
    baselines: dict[str, TestRun] = {}
    if baseline_ids:
        found = (
            (
                await session.execute(
                    select(TestRun).where(
                        TestRun.workspace_id == principal.workspace_id,
                        TestRun.id.in_(baseline_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        baselines = {run.id: run for run in found}

    payload: list[BaselineRead] = []
    for suite in rows:
        baseline = baselines.get(suite.baseline_run_id or "")
        finished = [
            run
            for run in runs.get(suite.id, ())
            if run.status in FINISHED_STATUSES and run.id != suite.baseline_run_id
        ]
        candidate = finished[0] if finished else None
        delta = (
            None
            if candidate is None or baseline is None
            or candidate.pass_rate is None
            or baseline.pass_rate is None
            else round(candidate.pass_rate - baseline.pass_rate, 1)
        )
        payload.append(
            BaselineRead(
                suite_id=suite.id,
                suite_name=suite.name,
                baseline_run_id=baseline.id if baseline else None,
                baseline=baseline.run_ref if baseline else None,
                baseline_pass_rate=baseline.pass_rate if baseline else None,
                candidate_run_id=candidate.id if candidate else None,
                candidate=candidate.run_ref if candidate else None,
                candidate_pass_rate=candidate.pass_rate if candidate else None,
                pass_rate_delta=delta,
                regressions=candidate.regression_count if candidate else 0,
                promotable=candidate is not None,
            )
        )
    return payload, total


async def list_schedules(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    environment: str | None = None,
    suite_type: str | None = None,
) -> tuple[list[ScheduleRead], int]:
    """The Schedules tab. A schedule is a suite's cron binding."""
    stmt = _filtered_stmt(
        principal, params, environment=environment, suite_type=suite_type, scheduled=True
    )
    rows, total = await paginate(session, stmt, params)
    owners = await owner_names(session, principal, [row.owner_user_id for row in rows])
    now = _now()
    return [
        ScheduleRead(
            id=row.id,
            suite_id=row.id,
            suite_name=row.name,
            suite_type=SuiteType(row.suite_type),
            environment=row.environment,
            cron=row.schedule_cron or "",
            cadence=describe_cron(row.schedule_cron or ""),
            next_run_at=next_fire(row.schedule_cron or "", now),
            last_run_at=row.last_run_at,
            status=(
                "Active" if row.status == SuiteStatus.ACTIVE.value else "Paused"
            ),
            case_count=row.case_count,
            owner_name=owners.get(row.owner_user_id or ""),
        )
        for row in rows
    ], total


async def create_schedule(
    session: AsyncSession,
    principal: Principal,
    payload: ScheduleCreate,
    *,
    request: Request | None = None,
) -> TestSuite:
    """Bind a cron expression to a suite that does not have one."""
    principal.require(Role.OPERATOR)
    suite = await get_suite(session, principal, payload.suite_id)
    if suite.schedule_cron:
        raise Conflict(
            f"'{suite.name}' is already scheduled ({suite.schedule_cron}). Update it instead."
        )

    suite.schedule_cron = payload.cron
    suite.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="test_suite.scheduled",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Scheduled {describe_cron(payload.cron)} ({payload.cron})",
        metadata={"cron": payload.cron},
        request=request,
    )
    await session.flush()
    await session.refresh(suite)
    return suite


async def update_schedule(
    session: AsyncSession,
    principal: Principal,
    schedule_id: str,
    payload: ScheduleUpdate,
    *,
    request: Request | None = None,
) -> TestSuite:
    """Change a schedule's cadence."""
    principal.require(Role.OPERATOR)
    suite = await get_suite(session, principal, schedule_id)
    if not suite.schedule_cron:
        raise NotFound(f"'{suite.name}' has no schedule to update.")

    previous = suite.schedule_cron
    suite.schedule_cron = payload.cron
    suite.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="test_suite.schedule_updated",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Cadence {previous} to {payload.cron}",
        metadata={"previous_cron": previous, "cron": payload.cron},
        request=request,
    )
    await session.flush()
    await session.refresh(suite)
    return suite


async def delete_schedule(
    session: AsyncSession,
    principal: Principal,
    schedule_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Unschedule a suite. The suite itself is left alone."""
    principal.require(Role.OPERATOR)
    suite = await get_suite(session, principal, schedule_id)
    if not suite.schedule_cron:
        raise NotFound(f"'{suite.name}' has no schedule to remove.")

    previous = suite.schedule_cron
    suite.schedule_cron = None
    suite.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="test_suite.unscheduled",
        entity_type=SUITE_ENTITY,
        entity_id=suite.id,
        entity_label=suite.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed schedule {previous}",
        metadata={"previous_cron": previous},
        request=request,
    )
    await session.flush()


async def list_environments(
    session: AsyncSession, principal: Principal, *, window_days: int = WINDOW_DAYS
) -> list[EnvironmentBinding]:
    """The Environments tab: how much of the suite estate each one carries."""
    since = _now() - dt.timedelta(days=window_days)

    suite_rows = (
        await session.execute(
            select(TestSuite.environment, func.count(TestSuite.id))
            .where(TestSuite.workspace_id == principal.workspace_id)
            .group_by(TestSuite.environment)
        )
    ).all()

    active_rows = {
        environment: int(count)
        for environment, count in (
            await session.execute(
                select(TestSuite.environment, func.count(TestSuite.id))
                .where(
                    TestSuite.workspace_id == principal.workspace_id,
                    TestSuite.status == SuiteStatus.ACTIVE.value,
                )
                .group_by(TestSuite.environment)
            )
        ).all()
    }

    runs = (
        (
            await session.execute(
                select(TestRun, TestSuite.environment)
                .join(TestSuite, TestSuite.id == TestRun.suite_id)
                .where(
                    TestRun.workspace_id == principal.workspace_id,
                    TestRun.status.in_(FINISHED_STATUSES),
                    TestRun.finished_at >= since,
                )
                .order_by(TestRun.finished_at.desc())
                .limit(SUMMARY_RUN_LIMIT)
            )
        )
        .all()
    )

    per_env: dict[str, list[TestRun]] = {}
    for run, environment in runs:
        per_env.setdefault(environment, []).append(run)

    bindings: list[EnvironmentBinding] = []
    for environment, suites in suite_rows:
        window_runs = per_env.get(environment, [])
        passed = sum(run.passed for run in window_runs)
        failed = sum(run.failed for run in window_runs)
        full_pass = [
            run for run in window_runs if run.failed == 0 and run.passed > 0
        ]
        bindings.append(
            EnvironmentBinding(
                environment=environment,
                suites=int(suites),
                active_suites=active_rows.get(environment, 0),
                runs_30d=len(window_runs),
                pass_rate_30d=(
                    round(passed / (passed + failed) * 100, 1) if passed + failed else None
                ),
                failing_suites=len({run.suite_id for run in window_runs if run.failed}),
                last_full_pass_at=full_pass[0].finished_at if full_pass else None,
                last_run_at=window_runs[0].finished_at if window_runs else None,
            )
        )
    bindings.sort(key=lambda binding: binding.environment)
    return bindings


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


async def _runs_between(
    session: AsyncSession, principal: Principal, since: dt.datetime, until: dt.datetime
) -> Sequence[TestRun]:
    stmt = (
        select(TestRun)
        .where(
            TestRun.workspace_id == principal.workspace_id,
            TestRun.finished_at.is_not(None),
            TestRun.finished_at >= since,
            TestRun.finished_at < until,
        )
        .order_by(TestRun.finished_at.asc())
        .limit(SUMMARY_RUN_LIMIT)
    )
    return (await session.execute(stmt)).scalars().all()


def _flaky_in(runs: Sequence[TestRun]) -> int:
    """Flaky cases across a window, counted per suite over its own history."""
    by_suite: dict[str, list[TestRun]] = {}
    for run in runs[:FLAKY_SCAN_RUNS]:
        by_suite.setdefault(run.suite_id, []).append(run)
    return sum(len(_flaky_cases(suite_runs)) for suite_runs in by_suite.values())


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = WINDOW_DAYS
) -> TestingSummary:
    """The six KPI cards, each measured against the preceding window."""
    now = _now()
    window = dt.timedelta(days=window_days)
    current = await _runs_between(session, principal, now - window, now)
    previous = await _runs_between(session, principal, now - 2 * window, now - window)

    suites_now = (
        await session.execute(
            select(func.count(TestSuite.id)).where(
                TestSuite.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one()
    suites_before = (
        await session.execute(
            select(func.count(TestSuite.id)).where(
                TestSuite.workspace_id == principal.workspace_id,
                TestSuite.created_at < now - window,
            )
        )
    ).scalar_one()

    def _executed(runs: Sequence[TestRun]) -> tuple[int, int, int, float | None, float | None]:
        passed = sum(run.passed for run in runs)
        failed = sum(run.failed for run in runs)
        executed = passed + failed + sum(run.skipped for run in runs)
        rate = round(passed / (passed + failed) * 100, 1) if passed + failed else None
        durations = [run.duration_seconds for run in runs if run.duration_seconds is not None]
        duration = round(sum(durations) / len(durations), 1) if durations else None
        return executed, passed, failed, rate, duration

    executed_now, _, _, rate_now, duration_now = _executed(current)
    executed_before, _, _, rate_before, duration_before = _executed(previous)

    regressions_now = sum(run.regression_count for run in current)
    regressions_before = sum(run.regression_count for run in previous)
    flaky_now = _flaky_in(current)
    flaky_before = _flaky_in(previous)

    def _percent(current_value: float | None, earlier: float | None) -> float | None:
        if current_value is None or earlier is None or earlier <= 0:
            return None
        return round((current_value - earlier) / earlier * 100, 1)

    running = (
        await session.execute(
            select(func.count(TestRun.id)).where(
                TestRun.workspace_id == principal.workspace_id,
                TestRun.status.in_((TestRunStatus.QUEUED.value, TestRunStatus.RUNNING.value)),
            )
        )
    ).scalar_one()

    return TestingSummary(
        window_days=window_days,
        suites=int(suites_now or 0),
        suites_delta=int(suites_now or 0) - int(suites_before or 0),
        tests_executed=executed_now,
        tests_executed_delta_percent=_percent(executed_now, executed_before),
        pass_rate=rate_now,
        pass_rate_delta=(
            None if rate_now is None or rate_before is None else round(rate_now - rate_before, 1)
        ),
        regressions_detected=regressions_now,
        regressions_delta_percent=_percent(regressions_now, regressions_before),
        avg_duration_seconds=duration_now,
        avg_duration_label=format_duration(duration_now),
        avg_duration_delta_percent=_percent(duration_now, duration_before),
        flaky_tests=flaky_now,
        flaky_tests_delta=flaky_now - flaky_before,
        running=int(running or 0),
    )
