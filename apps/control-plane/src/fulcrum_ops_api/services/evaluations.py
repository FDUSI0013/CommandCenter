"""Evaluations business logic.

An evaluation is a judged scoring pass over a dataset. The verdicts come from an
SDK experiment: ``client.evaluate`` runs the agent over each case and scores it
onto a real trace. This service finds those traces, creates an experiment in the
telemetry engine, links each case to the trace that ran it, then supervises the
run — reading the judged scores back, reporting real progress, and caching the
metric averages on the ``EvaluationRun`` row so the table renders without a
fan-out call per row. The engine remains the source of truth for per-case
scores, which the detail view reads live.

Nothing here runs the agent or calls a judge model: ``judge_model`` is recorded
on the experiment as the model the SDK's scorers used, and a dataset no SDK
experiment has covered fails at once, saying so, rather than waiting for
verdicts nobody is going to write.

Three invariants hold in every function below:

* every statement filters on ``principal.workspace_id``, and every engine
  entity is addressed by a name namespaced to that workspace, so one tenant
  cannot read another's datasets or experiments;
* nothing is invented. A metric the judge did not produce is ``None``, a case
  the engine has not scored is ``Unscored``, and a run whose scores never
  arrived is Failed with the reason on the row;
* every state change writes an audit row, including the ones the background
  supervisor makes on its own.

The engine plumbing at the top of this module — namespacing, error
translation, dataset resolution, experiment registration — is shared with
``services.testing``, which runs a test suite as an experiment too.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, and_, func, nullslast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import (
    AppError,
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.base import new_id
from ..db.session import get_sessionmaker
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    EngineUnavailable,
    get_engine_client,
)
from ..models.identity import Role, User, Workspace
from ..models.quality import EvaluationRun, EvaluationStatus
from ..models.registry import Agent
from ..schemas.evaluations import (
    METRIC_KEYS,
    METRIC_LABELS,
    REGRESSION_THRESHOLD,
    DatasetCreate,
    DatasetItemRead,
    DatasetItemsCreate,
    DatasetRead,
    EvaluationComparison,
    EvaluationCreate,
    EvaluationDetail,
    EvaluationItemRead,
    EvaluationProgress,
    EvaluationRead,
    EvaluationRerun,
    EvaluationsSummary,
    EvaluationTrend,
    EvaluationTrendPoint,
    MetricScore,
    average_score,
    extra_scores,
    metric_values,
)
from . import audit

log = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "Evaluations"
ENTITY_TYPE: Final[str] = "evaluation"

#: Separator between the workspace namespace and the operator-facing name of an
#: engine entity. The engine is single-tenant behind this service, so tenancy is
#: enforced by namespacing every name we send and refusing every name we read
#: back that does not carry our own prefix.
NAMESPACE_SEPARATOR: Final[str] = "::"

#: Dataset cases pulled from the engine per round trip while registering a run.
ITEM_BATCH_SIZE: Final[int] = 250

#: Supervision loop: how often a running experiment is polled, and how long it
#: may run before the supervisor stops waiting for judged scores.
POLL_SECONDS: Final[float] = 3.0
EXECUTION_DEADLINE_SECONDS: Final[float] = 900.0

#: Once the experiment has read the same for this long, the run is concluded
#: with the verdicts it has. No verdict is produced after a run starts — the
#: scores are the ones the linked traces already carry — so a reading that has
#: stopped moving is not going to start again.
SCORE_SETTLE_SECONDS: Final[float] = 15.0

#: How often, and how far apart, the supervisor looks again for a row it was
#: handed but cannot see yet (the request that queued it may still be committing).
ROW_VISIBILITY_RETRIES: Final[int] = 5
ROW_VISIBILITY_WAIT_SECONDS: Final[float] = 0.2

#: The registration phase owns this share of the progress bar; scoring owns the
#: rest. Both halves report measured counts, never a timer.
REGISTER_PHASE_WEIGHT: Final[float] = 60.0

PHASE_PREPARING: Final[str] = "Preparing"
PHASE_REGISTERING: Final[str] = "Registering cases"
PHASE_SCORING: Final[str] = "Scoring"
PHASE_FINISHED: Final[str] = "Finished"

#: Judged scores run 0-1; at or above this a case reads as passing. It is the
#: only place that line is drawn for an evaluation case.
CASE_PASS_SCORE: Final[float] = 0.5

#: Ceilings on the ad-hoc scans the KPI cards and the baseline column need.
#: Both windows are bounded by time as well; these stop a pathological
#: workspace from turning one page load into an unbounded read.
SUMMARY_SCAN_LIMIT: Final[int] = 5000
BASELINE_SCAN_LIMIT: Final[int] = 500
EXPORT_LIMIT: Final[int] = 5000

#: The dataset listing reads the namespace's datasets from the engine in pages
#: of this size, and stops at the limit: search and tenancy are applied here, so
#: they have to be applied to the whole set before it is paged.
DATASET_SCAN_PAGE_SIZE: Final[int] = 100
DATASET_SCAN_LIMIT: Final[int] = 5000

#: Cases returned on one page of the detail view's per-case breakdown.
DEFAULT_ITEM_PAGE_SIZE: Final[int] = 25
MAX_ITEM_PAGE_SIZE: Final[int] = 100

#: How long the inspector waits on the telemetry store, all reads together. The
#: adapter's own bound is a 30 s timeout tried three times, and the console stops
#: listening at 30 s — so without this a slow store showed "could not load" for
#: a run whose every number was already in this database.
DETAIL_ENGINE_BUDGET_SECONDS: Final[float] = 10.0

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {EvaluationStatus.COMPLETED.value, EvaluationStatus.FAILED.value}
)

SORTABLE: Final[dict[str, Any]] = {
    "name": EvaluationRun.name,
    "agent_name": Agent.name,
    "dataset": EvaluationRun.dataset_ref,
    "judge_model": EvaluationRun.judge_model,
    "status": EvaluationRun.status,
    "cases": EvaluationRun.case_count,
    "started_at": EvaluationRun.started_at,
    "finished_at": EvaluationRun.finished_at,
    "occurred_at": EvaluationRun.created_at,
    "created_at": EvaluationRun.created_at,
}


# ---------------------------------------------------------------------------
# Engine plumbing (shared with services.testing)
# ---------------------------------------------------------------------------


def engine_namespace(principal: Principal) -> str:
    """The engine namespace this workspace owns."""
    return principal.engine_workspace or principal.workspace_slug or principal.workspace_id


async def workspace_namespace(session: AsyncSession, workspace_id: str) -> str:
    """The same namespace, resolved without a request.

    The background supervisor has no principal but must address exactly the
    engine entities the requester created, so it reads the namespace off the
    workspace row using the same precedence :func:`engine_namespace` applies.
    """
    row = (
        await session.execute(
            select(Workspace.engine_workspace, Workspace.slug).where(Workspace.id == workspace_id)
        )
    ).first()
    if row is None:
        return workspace_id
    return row[0] or row[1] or workspace_id


def namespaced(namespace: str, local: str) -> str:
    """Namespace an operator-facing name before it reaches the engine."""
    return f"{namespace}{NAMESPACE_SEPARATOR}{local}"


def engine_name(principal: Principal, local: str) -> str:
    """Namespace a name for the caller's workspace."""
    return namespaced(engine_namespace(principal), local)


def local_name(namespace: str, value: str | None) -> str | None:
    """Strip our namespace off an engine name, or ``None`` if it is not ours.

    Returning ``None`` for a foreign name is what makes a cross-tenant read
    impossible: a listing drops everything it cannot un-namespace.
    """
    prefix = f"{namespace}{NAMESPACE_SEPARATOR}"
    if not value or not value.startswith(prefix):
        return None
    return value[len(prefix) :]


def translate_engine_error(exc: EngineError) -> AppError:
    """Map an adapter failure onto the API's error envelope.

    The engine is never named to the caller: an outage is reported as the
    telemetry store being unavailable, and a refusal as a validation failure
    carrying the engine's own status.
    """
    if isinstance(exc, EngineNotFound):
        return NotFound("The telemetry store does not hold that entity.")
    if isinstance(exc, EngineUnavailable):
        return TelemetryBackendUnavailable()
    if isinstance(exc, EngineBadRequest):
        return ValidationFailed(
            "The telemetry store refused the request.",
            details={"status": exc.status},
        )
    return TelemetryBackendUnavailable()


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_instant(value: Any) -> dt.datetime | None:
    """Read one of the engine's ISO-8601 timestamps."""
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _as_utc(dt.datetime.fromisoformat(text))
    except ValueError:
        return None


def _text_of(value: Any) -> str | None:
    """Flatten a dataset item field, which may be a string or a JSON document."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("input", "question", "text", "content", "value", "output", "answer"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
        return ", ".join(f"{k}={v}" for k, v in value.items()) or None
    return str(value)


def _system_principal(workspace_id: str) -> Principal:
    """Actor the supervisor signs its own audit rows with.

    A run that finished while nobody was watching was not finished by the
    person who started it, and the audit trail has to say so.
    """
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="evaluation-supervisor",
        display_name="Evaluation Supervisor",
    )


async def resolve_dataset(
    client: EngineClient, namespace: str, dataset: str
) -> dict[str, Any]:
    """Load one of this workspace's datasets from the engine, or 404."""
    try:
        payload = await client.find_dataset_by_name(namespaced(namespace, dataset))
    except EngineError as exc:
        raise translate_engine_error(exc) from exc
    if not payload:
        raise NotFound(f"Dataset '{dataset}' does not exist in this workspace.")
    return payload


async def dataset_case_total(client: EngineClient, dataset_id: str) -> int:
    """How many cases the dataset holds, asked of the engine rather than cached."""
    try:
        page = await client.list_dataset_items(dataset_id, page=1, size=1)
    except EngineError as exc:
        raise translate_engine_error(exc) from exc
    return int(page.get("total") or 0)


def experiment_scores(payload: dict[str, Any]) -> dict[str, float]:
    """The judged averages carried by an experiment payload.

    Any judged score makes this non-empty, whatever its name — which is what
    the supervisor reads as "the experiment has verdicts".
    """
    for key in ("feedback_scores", "scores", "metrics"):
        if key in payload:
            values = metric_values(payload.get(key))
            if values:
                return values
    return {}


def experiment_scored_count(payload: dict[str, Any]) -> int:
    """Cases the experiment payload itself reports as judged.

    The store reports no such number. ``trace_count`` — which this used to
    return — is the number of cases *registered*: it equals the whole run the
    moment registration ends, so reading it as "judged" reported three scored
    cases out of fifty as fifty of fifty and pinned the progress bar at 99%. A
    payload that does carry an explicit judged count is believed; otherwise the
    answer is zero, and a caller that needs the number counts verdicts case by
    case (:func:`count_judged_cases`).
    """
    value = payload.get("scored_count")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return 0


def item_experiment_result(
    item: dict[str, Any], experiment_id: str | None
) -> dict[str, Any] | None:
    """The experiment result attached to a dataset item, when the engine sent one."""
    for key in ("experiment_items", "experiment_item", "experiments"):
        value = item.get(key)
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if experiment_id and row.get("experiment_id") not in (None, experiment_id):
                continue
            return row
    return None


#: How far back and how wide the search for SDK experiment traces reaches.
#: The search runs in two passes. The first asks the store for nothing but the
#: traces an SDK experiment stamped with this dataset's name, so it can afford
#: to follow the cursor a long way; the second reads everything and is only
#: run when the first found nothing, so it is capped much lower.
TRACE_LINK_WINDOW_DAYS = 30
TRACE_LINK_MAX_PROJECTS = 25
TRACE_LINK_PAGE_SIZE = 500
TRACE_LINK_MAX_TRACES = 10_000
TRACE_LINK_MAX_UNFILTERED_TRACES = 2_000
#: Projects searched side by side. Small on purpose: the store is shared with
#: every screen an operator has open.
TRACE_LINK_CONCURRENCY = 4


@dataclasses.dataclass
class TraceScan:
    """What a search for SDK experiment traces found, and how well it looked."""

    links: dict[str, str] = dataclasses.field(default_factory=dict)
    projects: int = 0
    #: Projects the store could not be read for. A search that failed everywhere
    #: has not shown that there is nothing to find.
    failed: int = 0

    @property
    def blind(self) -> bool:
        return self.projects > 0 and self.failed >= self.projects


async def _scan_project(
    client: EngineClient,
    project: str,
    *,
    since: dt.datetime,
    filters: list[dict[str, Any]] | None,
    cap: int,
    wanted: frozenset[str] | None,
) -> dict[str, str]:
    """One project's dataset-item traces, following the cursor up to ``cap`` rows."""
    found: dict[str, str] = {}
    last_id: str | None = None
    seen = 0
    while seen < cap:
        rows = await client.search_traces(
            project_name=project,
            filters=filters,
            from_time=since,
            limit=min(TRACE_LINK_PAGE_SIZE, cap - seen),
            last_retrieved_id=last_id,
            truncate=True,
        )
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            item_id = metadata.get("dataset_item_id")
            trace_id = row.get("id")
            if not isinstance(item_id, str) or not isinstance(trace_id, str):
                continue
            if wanted is not None and item_id not in wanted:
                continue
            # Newest run per case wins. Trace ids are UUIDv7, so the largest
            # id IS the newest — never trust the stream's order.
            current = found.get(item_id)
            if current is None or trace_id > current:
                found[item_id] = trace_id
        seen += len(rows)
        last_id = rows[-1].get("id") if rows else None
        # A short page is the store's end-of-stream signal.
        if len(rows) < TRACE_LINK_PAGE_SIZE or not last_id:
            break
    return found


async def scan_dataset_item_traces(
    client: EngineClient,
    session: AsyncSession,
    workspace_id: str,
    *,
    agent_id: str | None = None,
    dataset: str | None = None,
    wanted: frozenset[str] | None = None,
) -> TraceScan:
    """Search the workspace's projects for the traces an SDK experiment left.

    ``client.evaluate`` stamps every trace it makes with ``metadata.dataset``
    and ``metadata.dataset_item_id``. Reading a project's newest few hundred
    traces and hoping the experiment is among them stops working the day the
    agent has real traffic, so when the dataset is known the store is asked for
    that dataset's traces only and the cursor is followed. A store that refuses
    the filter, or traces that carry only the item id, are covered by a second,
    unfiltered pass — shallower, because it has to read everything.

    Projects are searched most recently active first, a few at a time, so the
    cap on how many are read drops the quietest agents rather than an arbitrary
    set of them.
    """
    stmt = select(Agent.engine_project_name).where(
        Agent.workspace_id == workspace_id,
        Agent.engine_project_name.is_not(None),
    )
    if agent_id:
        stmt = stmt.where(Agent.id == agent_id)
    stmt = stmt.order_by(nullslast(Agent.last_used_at.desc()), Agent.name.asc())
    names = [row for row in (await session.execute(stmt)).scalars() if row]
    projects = list(dict.fromkeys(names))[:TRACE_LINK_MAX_PROJECTS]
    if not projects:
        return TraceScan()

    since = _now() - dt.timedelta(days=TRACE_LINK_WINDOW_DAYS)
    gate = asyncio.Semaphore(TRACE_LINK_CONCURRENCY)

    async def _pass(filters: list[dict[str, Any]] | None, cap: int) -> TraceScan:
        async def _one(project: str) -> dict[str, str] | None:
            async with gate:
                try:
                    return await _scan_project(
                        client, project, since=since, filters=filters, cap=cap, wanted=wanted
                    )
                except EngineError:
                    return None

        result = TraceScan(projects=len(projects))
        for found in await asyncio.gather(*(_one(project) for project in projects)):
            if found is None:
                result.failed += 1
                continue
            for item_id, trace_id in found.items():
                current = result.links.get(item_id)
                if current is None or trace_id > current:
                    result.links[item_id] = trace_id
        return result

    if dataset:
        narrowed = await _pass(
            [{"field": "metadata", "key": "dataset", "operator": "=", "value": dataset}],
            TRACE_LINK_MAX_TRACES,
        )
        if narrowed.links:
            return narrowed
    return await _pass(None, TRACE_LINK_MAX_UNFILTERED_TRACES)


async def find_dataset_item_traces(
    client: EngineClient,
    session: AsyncSession,
    workspace_id: str,
    *,
    agent_id: str | None = None,
    dataset: str | None = None,
) -> dict[str, str]:
    """Traces an SDK experiment left behind, keyed by the dataset item they ran.

    Finding those traces and linking them into the platform's own experiment
    is what turns SDK scorer verdicts into the judged averages the Evaluations
    screen shows. Newest trace per case wins. A failure to look is reported as
    an empty map; a caller that needs to tell "found nothing" from "could not
    look" reads :func:`scan_dataset_item_traces` instead.
    """
    scan = await scan_dataset_item_traces(
        client, session, workspace_id, agent_id=agent_id, dataset=dataset
    )
    return scan.links


async def register_experiment_items(
    client: EngineClient,
    *,
    experiment_id: str | None,
    experiment_name: str,
    dataset_name: str,
    on_batch: Any = None,
    trace_links: Mapping[str, str] | None = None,
) -> int:
    """Bind every case of a dataset to an experiment, a page at a time.

    A case with a known trace (``trace_links``) is linked to it, so the
    experiment aggregates the scores that trace already carries; the rest are
    registered bare, which lists them against the experiment with no verdict —
    nothing judges a bare case afterwards. Returns the number of cases
    registered. ``on_batch`` is called with the running total after each page
    so the caller can publish real progress.
    """
    links = dict(trace_links or {})
    registered = 0
    last_id: str | None = None
    while True:
        try:
            rows = await client.stream_dataset_items(
                dataset_name, last_retrieved_id=last_id, limit=ITEM_BATCH_SIZE
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        if not rows:
            break

        ids = [row["id"] for row in rows if row.get("id")]
        # Linking needs the experiment's id; without one everything registers
        # bare, which is exactly the pre-linking behaviour.
        linkable = set(links) if experiment_id else set()
        linked = [
            {
                "id": new_id(),
                "experiment_id": experiment_id,
                "dataset_item_id": item_id,
                "trace_id": links[item_id],
            }
            for item_id in ids
            if item_id in linkable
        ]
        bare = [{"dataset_item_id": item_id} for item_id in ids if item_id not in linkable]
        try:
            if linked:
                await client.create_experiment_items(linked)
            if bare:
                await client.create_experiment_items_bulk(
                    bare,
                    experiment_name=experiment_name,
                    dataset_name=dataset_name,
                    experiment_id=experiment_id,
                )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        if ids:
            registered += len(ids)
            if on_batch is not None:
                on_batch(registered)

        last_id = rows[-1].get("id")
        if not last_id or len(rows) < ITEM_BATCH_SIZE:
            break
    return registered


async def dataset_item_ids(client: EngineClient, dataset_name: str) -> list[str]:
    """Every case id a dataset holds, read a page at a time."""
    ids: list[str] = []
    last_id: str | None = None
    while True:
        try:
            rows = await client.stream_dataset_items(
                dataset_name, last_retrieved_id=last_id, limit=ITEM_BATCH_SIZE
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        ids.extend(str(row["id"]) for row in rows if row.get("id"))
        last_id = rows[-1].get("id") if rows else None
        if not last_id or len(rows) < ITEM_BATCH_SIZE:
            break
    return ids


async def link_experiment_items(
    client: EngineClient,
    *,
    experiment_id: str,
    links: Mapping[str, str],
    on_batch: Any = None,
) -> int:
    """Bind cases to the traces that already ran them, a batch at a time.

    Only cases with a trace are bound. A case nothing ran has no verdict to
    aggregate and nothing downstream that will ever write one, so registering it
    blank buys a blank trace in the store and a wait for a judgement that is
    not coming.
    """
    pairs = list(links.items())
    linked = 0
    for start in range(0, len(pairs), ITEM_BATCH_SIZE):
        batch = [
            {
                "id": new_id(),
                "experiment_id": experiment_id,
                "dataset_item_id": item_id,
                "trace_id": trace_id,
            }
            for item_id, trace_id in pairs[start : start + ITEM_BATCH_SIZE]
        ]
        try:
            await client.create_experiment_items(batch)
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        linked += len(batch)
        if on_batch is not None:
            on_batch(linked)
    return linked


async def count_judged_cases(
    client: EngineClient, dataset_id: str, experiment_id: str, *, limit: int
) -> int:
    """Cases of an experiment that carry at least one judged score.

    The experiment payload cannot answer this: its ``trace_count`` is the number
    of cases *registered*, which is the whole run the moment registration ends.
    The comparison listing joins each case to its verdicts, so it is counted
    there — the same rows the inspector's breakdown is drawn from.
    """
    judged = 0
    seen = 0
    page = 1
    while seen < limit:
        try:
            payload = await client.list_dataset_items_with_experiments(
                dataset_id, [experiment_id], page=page, size=MAX_ITEM_PAGE_SIZE
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        rows = payload.get("content") or payload.get("items") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            result = item_experiment_result(row, experiment_id) or {}
            if metric_values(result.get("feedback_scores") or result.get("scores")):
                judged += 1
        seen += len(rows)
        if len(rows) < MAX_ITEM_PAGE_SIZE:
            break
        page += 1
    return judged


def _nothing_to_judge(dataset: str, *, agent_bound: bool) -> str:
    where = "this agent's telemetry" if agent_bound else "this workspace's telemetry"
    return (
        f"No SDK experiment traces for dataset '{dataset}' were found in {where} from the "
        f"last {TRACE_LINK_WINDOW_DAYS} days, so there is nothing to judge. Run "
        f"client.evaluate('{dataset}', task, scorers=[...]) with the SDK first: an "
        "evaluation reads the scores those traces carry."
    )


# ---------------------------------------------------------------------------
# Progress registry and the background supervisor
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RunProgress:
    """Live counters for one supervised run, held only in this process."""

    total: int = 0
    processed: int = 0
    scored: int = 0
    phase: str = PHASE_PREPARING
    detail: str | None = None

    def percent(self) -> float:
        if self.phase == PHASE_FINISHED:
            return 100.0
        if not self.total:
            return 0.0
        register = min(1.0, self.processed / self.total) * REGISTER_PHASE_WEIGHT
        score = min(1.0, self.scored / self.total) * (100.0 - REGISTER_PHASE_WEIGHT)
        return round(min(99.0, register + score), 1)


class EvaluationSupervisor:
    """Owns the asyncio task that drives one evaluation to a terminal state.

    One task per evaluation. Starting an evaluation that already has a live
    task is a no-op, so a retried request cannot double-register cases.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._progress: dict[str, RunProgress] = {}

    def start(self, evaluation_id: str, workspace_id: str) -> bool:
        existing = self._tasks.get(evaluation_id)
        if existing is not None and not existing.done():
            return False
        self._progress[evaluation_id] = RunProgress()
        task = asyncio.create_task(
            self._run(evaluation_id, workspace_id), name=f"evaluation:{evaluation_id}"
        )
        self._tasks[evaluation_id] = task
        task.add_done_callback(lambda t: self._forget(evaluation_id, t))
        return True

    def progress(self, evaluation_id: str) -> RunProgress | None:
        return self._progress.get(evaluation_id)

    def _forget(self, evaluation_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(evaluation_id) is task:
            self._tasks.pop(evaluation_id, None)
            # The counters go with the task. Every reader asks the row first and
            # only consults them for a run that is still live, so nothing reads
            # them again — but they were kept for the life of the worker, one
            # entry per evaluation ever supervised. (Guarded like the task: a
            # run restarted before this callback fired owns a newer entry.)
            self._progress.pop(evaluation_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # pragma: no cover - _run records its own failures
            log.error("evaluation %s ended in error: %s", evaluation_id, error)

    async def _run(self, evaluation_id: str, workspace_id: str) -> None:
        state = self._progress.setdefault(evaluation_id, RunProgress())
        try:
            await self._execute(evaluation_id, workspace_id, state)
        except asyncio.CancelledError:
            await self._fail(evaluation_id, workspace_id, "The run was cancelled.")
            raise
        except AppError as exc:
            await self._fail(evaluation_id, workspace_id, exc.message)
        except Exception:
            log.exception("evaluation %s aborted", evaluation_id)
            await self._fail(
                evaluation_id, workspace_id, "The run stopped on an unexpected error."
            )
        finally:
            state.phase = PHASE_FINISHED

    async def _execute(
        self, evaluation_id: str, workspace_id: str, state: RunProgress
    ) -> None:
        client = get_engine_client()
        principal = _system_principal(workspace_id)

        async with get_sessionmaker()() as session:
            run = await _load(session, workspace_id, evaluation_id)
            # The routes commit before they schedule this task, so the row is
            # normally there on the first look. A caller that scheduled first
            # would race its own COMMIT; a few short looks cover that, and a row
            # that never appears is an error worth a log line — returning in
            # silence is what used to leave a run Queued with nobody driving it.
            for _ in range(ROW_VISIBILITY_RETRIES):
                if run is not None:
                    break
                await session.rollback()
                await asyncio.sleep(ROW_VISIBILITY_WAIT_SECONDS)
                run = await _load(session, workspace_id, evaluation_id)
            if run is None:
                log.error(
                    "evaluation %s was handed to the supervisor but its row never became "
                    "visible; nothing is driving it",
                    evaluation_id,
                )
                return
            if run.status in TERMINAL_STATUSES:
                return
            dataset_ref = run.dataset_ref
            judge_model = run.judge_model
            agent_id = run.agent_id
            namespace = await workspace_namespace(session, workspace_id)
            run.status = EvaluationStatus.RUNNING.value
            run.started_at = _now()
            await session.commit()

        # Size the work before anything is written to the store, so the
        # progress bar is measured against a real total from the start.
        dataset = await resolve_dataset(client, namespace, dataset_ref)
        dataset_id = str(dataset.get("id") or "")
        engine_dataset = namespaced(namespace, dataset_ref)
        item_ids = await dataset_item_ids(client, engine_dataset) if dataset_id else []
        total = len(item_ids)
        if total <= 0:
            raise PreconditionFailed(
                f"Dataset '{dataset_ref}' holds no cases, so there is nothing to judge."
            )
        state.total = total

        # An evaluation is judged from the traces an SDK experiment left behind:
        # nothing here runs the agent over a case or calls the judge model, so a
        # case without such a trace can never receive a verdict. When no case
        # has one the run says so now. It used to register every case blank and
        # wait out the whole deadline for judgements nobody was going to write.
        async with get_sessionmaker()() as session:
            scan = await scan_dataset_item_traces(
                client,
                session,
                workspace_id,
                agent_id=agent_id,
                dataset=dataset_ref,
                wanted=frozenset(item_ids),
            )
        links = {item_id: scan.links[item_id] for item_id in item_ids if item_id in scan.links}
        if not links:
            if scan.blind:
                raise TelemetryBackendUnavailable()
            raise PreconditionFailed(_nothing_to_judge(dataset_ref, agent_bound=bool(agent_id)))
        state.phase = PHASE_REGISTERING

        async with get_sessionmaker()() as session:
            run = await _load(session, workspace_id, evaluation_id)
            if run is None:
                return
            run.case_count = total
            await session.commit()

        experiment_name = namespaced(namespace, f"eval-{evaluation_id}")
        try:
            experiment = await client.create_experiment(
                experiment_name,
                dataset_name=engine_dataset,
                metadata={
                    "judge_model": judge_model,
                    "agent_id": agent_id,
                    "evaluation_id": evaluation_id,
                    "source": SOURCE_SCREEN,
                },
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        experiment_id = str(experiment.get("id") or "") or None
        if experiment_id is None:
            raise TelemetryBackendUnavailable(
                "The telemetry store created the experiment without saying which one it is."
            )

        async with get_sessionmaker()() as session:
            run = await _load(session, workspace_id, evaluation_id)
            if run is not None:
                run.engine_experiment_id = experiment_id
                await session.commit()

        def _registered(done: int) -> None:
            # The bar's first half is the share of linkable cases bound so far.
            state.processed = round(total * done / len(links))

        linked = await link_experiment_items(
            client, experiment_id=experiment_id, links=links, on_batch=_registered
        )
        state.processed = total
        state.phase = PHASE_SCORING

        scores, judged = await self._await_scores(
            client, experiment_id, dataset_id, linked=linked, state=state
        )

        async with get_sessionmaker()() as session:
            run = await _load(session, workspace_id, evaluation_id)
            if run is None:
                return
            run.finished_at = _now()
            # Cases the run actually judged, not cases the dataset holds: the KPI
            # cards, the trend and the inspector all count from this number.
            run.case_count = judged
            if scores:
                run.scores = dict(scores)
                run.status = EvaluationStatus.COMPLETED.value
                average = average_score(dict(scores))
                detail = f"Judged {judged} of {total} case(s)" + (
                    f"; average {average:.2f}" if average is not None else ""
                )
                if judged < total:
                    run.notes = (
                        f"Judged {judged} of {total} cases; the other {total - judged} have no "
                        "scored SDK experiment trace, so the averages cover only the judged ones."
                    )
            else:
                run.status = EvaluationStatus.FAILED.value
                run.notes = (
                    f"Found SDK experiment traces for {linked} of {total} case(s) of "
                    f"'{dataset_ref}', but none of them carries a score. Pass scorers=[...] to "
                    "client.evaluate so each case is judged."
                )
                detail = run.notes
            await audit.record(
                session,
                principal=principal,
                action=(
                    "evaluation.completed"
                    if run.status == EvaluationStatus.COMPLETED.value
                    else "evaluation.failed"
                ),
                entity_type=ENTITY_TYPE,
                entity_id=run.id,
                entity_label=run.name,
                source_screen=SOURCE_SCREEN,
                detail=detail,
                metadata={"cases": run.case_count, "total_cases": total, "scores": run.scores},
            )
            await session.commit()

    async def _await_scores(
        self,
        client: EngineClient,
        experiment_id: str,
        dataset_id: str,
        *,
        linked: int,
        state: RunProgress,
    ) -> tuple[dict[str, float], int]:
        """Poll until every linked case shows its verdict, or the store goes quiet.

        Returns the judged averages and how many cases carry a score. The scores
        already exist on the linked traces; what is waited for is the store
        surfacing them on the experiment, which takes seconds. So the wait ends
        when every linked case is judged, or when the experiment has read the
        same for :data:`SCORE_SETTLE_SECONDS` -- a linked trace whose task raised
        has no score and never will, and holding the run open until the deadline
        for it helps nobody. The deadline remains as the outer bound.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + EXECUTION_DEADLINE_SECONDS
        scores: dict[str, float] = {}
        judged = 0
        reading: Any = None
        steady_since = loop.time()
        while True:
            try:
                payload = await client.get_experiment(experiment_id)
            except EngineNotFound:
                return scores, judged
            except EngineError as exc:
                raise translate_engine_error(exc) from exc

            scores = experiment_scores(payload) or scores
            seen = (payload.get("trace_count"), repr(payload.get("feedback_scores")))
            if seen != reading:
                reading = seen
                steady_since = loop.time()
                if scores:
                    # Counted only when the experiment's reading moves: the
                    # count pages the comparison listing, which is not free.
                    counted = await count_judged_cases(
                        client, dataset_id, experiment_id, limit=state.total or linked
                    )
                    judged = max(judged, min(linked, counted))
            state.scored = judged
            if scores and judged >= linked:
                return scores, judged
            if loop.time() - steady_since >= SCORE_SETTLE_SECONDS or loop.time() >= deadline:
                if scores and judged < linked:
                    # One last count on the way out. The averages and the
                    # per-case listing are two reads of a store that catches up
                    # with itself; the count taken when the averages last moved
                    # may be behind them, and it is about to be written down.
                    counted = await count_judged_cases(
                        client, dataset_id, experiment_id, limit=state.total or linked
                    )
                    judged = max(judged, min(linked, counted))
                    state.scored = judged
                return scores, judged
            await asyncio.sleep(POLL_SECONDS)

    async def _fail(self, evaluation_id: str, workspace_id: str, reason: str) -> None:
        async with get_sessionmaker()() as session:
            run = await _load(session, workspace_id, evaluation_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            run.status = EvaluationStatus.FAILED.value
            run.finished_at = _now()
            run.notes = reason
            # ``case_count`` held the dataset's size while the run was live, for
            # the progress bar. A run that failed judged nothing, and must not
            # add its dataset to the Test Cases Run card.
            run.case_count = 0
            await audit.record(
                session,
                principal=_system_principal(workspace_id),
                action="evaluation.failed",
                entity_type=ENTITY_TYPE,
                entity_id=run.id,
                entity_label=run.name,
                source_screen=SOURCE_SCREEN,
                detail=reason,
            )
            await session.commit()


supervisor = EvaluationSupervisor()


async def _load(
    session: AsyncSession, workspace_id: str, evaluation_id: str
) -> EvaluationRun | None:
    return (
        await session.execute(
            select(EvaluationRun).where(
                EvaluationRun.workspace_id == workspace_id,
                EvaluationRun.id == evaluation_id,
            )
        )
    ).scalar_one_or_none()


async def execute_evaluation(evaluation_id: str, workspace_id: str) -> None:
    """Background entry point used by the routes."""
    supervisor.start(evaluation_id, workspace_id)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(EvaluationRun).where(EvaluationRun.workspace_id == principal.workspace_id)


def _filtered_stmt(
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    dataset: str | None = None,
    agent_id: str | None = None,
    judge_model: str | None = None,
) -> Select:
    stmt = _scoped(principal).outerjoin(
        Agent,
        and_(
            Agent.id == EvaluationRun.agent_id,
            Agent.workspace_id == principal.workspace_id,
        ),
    )
    stmt = apply_search(
        stmt,
        params,
        [
            EvaluationRun.name,
            EvaluationRun.dataset_ref,
            EvaluationRun.judge_model,
            Agent.name,
            Agent.model,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            EvaluationRun.status: status,
            EvaluationRun.dataset_ref: dataset,
            EvaluationRun.agent_id: agent_id,
            EvaluationRun.judge_model: judge_model,
        },
    )
    return apply_sort(
        stmt, params, SORTABLE, default=EvaluationRun.created_at, default_desc=True
    )


async def _agents_for(
    session: AsyncSession, principal: Principal, rows: Sequence[EvaluationRun]
) -> dict[str, Agent]:
    ids = {row.agent_id for row in rows if row.agent_id}
    if not ids:
        return {}
    agents = (
        (
            await session.execute(
                select(Agent).where(
                    Agent.workspace_id == principal.workspace_id, Agent.id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return {agent.id: agent for agent in agents}


async def _baselines_for(
    session: AsyncSession, principal: Principal, rows: Sequence[EvaluationRun]
) -> dict[str, EvaluationRun]:
    """The previous completed run of the same agent and dataset, per row.

    One statement covers the whole page. The candidates are ranked *per pair*
    with a window function rather than by one global ``ORDER BY … LIMIT``: a
    single agent with thousands of runs would otherwise fill the window and
    leave every other row on the page without a baseline. Reading one more
    candidate than there are rows guarantees a baseline exists for each of
    them even when the whole page belongs to the same pair.
    """
    pairs = {(row.agent_id, row.dataset_ref) for row in rows}
    if not pairs:
        return {}

    clauses = [
        and_(
            EvaluationRun.agent_id.is_(None)
            if agent_id is None
            else EvaluationRun.agent_id == agent_id,
            EvaluationRun.dataset_ref == dataset,
        )
        for agent_id, dataset in pairs
    ]
    ranked = (
        _scoped(principal)
        .add_columns(
            func.row_number()
            .over(
                partition_by=(EvaluationRun.agent_id, EvaluationRun.dataset_ref),
                order_by=EvaluationRun.finished_at.desc(),
            )
            .label("rank")
        )
        .where(
            EvaluationRun.status == EvaluationStatus.COMPLETED.value,
            EvaluationRun.finished_at.is_not(None),
            or_(*clauses),
        )
        .subquery()
    )
    ranked_run = aliased(EvaluationRun, ranked)
    per_pair = min(len(rows) + 1, BASELINE_SCAN_LIMIT)
    candidates = (
        (
            await session.execute(
                select(ranked_run)
                .where(ranked.c.rank <= per_pair)
                .order_by(ranked.c.finished_at.desc())
            )
        )
        .scalars()
        .all()
    )

    by_pair: dict[tuple[str | None, str], list[EvaluationRun]] = {}
    for candidate in candidates:
        by_pair.setdefault((candidate.agent_id, candidate.dataset_ref), []).append(candidate)

    baselines: dict[str, EvaluationRun] = {}
    for row in rows:
        reference = row.finished_at or row.started_at or row.created_at
        for candidate in by_pair.get((row.agent_id, row.dataset_ref), ()):
            if candidate.id == row.id:
                continue
            if reference is not None and _as_utc(candidate.finished_at) >= _as_utc(reference):
                continue
            baselines[row.id] = candidate
            break
    return baselines


def _duration(run: EvaluationRun) -> float | None:
    if run.started_at is None or run.finished_at is None:
        return None
    return max(0.0, (_as_utc(run.finished_at) - _as_utc(run.started_at)).total_seconds())


def _read(
    run: EvaluationRun,
    *,
    agent: Agent | None = None,
    baseline: EvaluationRun | None = None,
) -> EvaluationRead:
    scores = metric_values(run.scores)
    avg = average_score(dict(scores))
    baseline_avg = average_score(metric_values(baseline.scores)) if baseline else None
    delta = None if avg is None or baseline_avg is None else round(avg - baseline_avg, 4)
    # A failed run judged nothing. The supervisor zeroes the column when it fails
    # a run itself, but a run the stale-run sweep failed for it (restart,
    # timeout) is left holding its dataset's size, which the table would show as
    # cases run.
    failed = run.status == EvaluationStatus.FAILED.value
    return EvaluationRead(
        id=run.id,
        name=run.name,
        agent_id=run.agent_id,
        agent_name=agent.name if agent else None,
        agent_model=agent.model if agent else None,
        dataset=run.dataset_ref,
        judge_model=run.judge_model,
        status=EvaluationStatus(run.status),
        cases=0 if failed else run.case_count,
        correctness=scores.get("correctness"),
        grounding=scores.get("grounding"),
        faithfulness=scores.get("faithfulness"),
        safety=scores.get("safety"),
        extra_scores=extra_scores(scores),
        avg_score=avg,
        baseline_run_id=baseline.id if baseline else None,
        baseline_avg_score=baseline_avg,
        baseline_delta=delta,
        is_regression=delta is not None and delta < -REGRESSION_THRESHOLD,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=_duration(run),
        occurred_at=run.finished_at or run.started_at or run.created_at,
        engine_experiment_id=run.engine_experiment_id,
        triggered_by_user_id=run.triggered_by_user_id,
        notes=run.notes,
        created_at=run.created_at,
        updated_at=run.updated_at,
        created_by=run.created_by,
    )


async def read_many(
    session: AsyncSession, principal: Principal, rows: Sequence[EvaluationRun]
) -> list[EvaluationRead]:
    """Turn rows into table records, resolving agents and baselines in two queries."""
    agents = await _agents_for(session, principal, rows)
    baselines = await _baselines_for(session, principal, rows)
    return [
        _read(
            row,
            agent=agents.get(row.agent_id) if row.agent_id else None,
            baseline=baselines.get(row.id),
        )
        for row in rows
    ]


async def list_evaluations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    dataset: str | None = None,
    agent_id: str | None = None,
    judge_model: str | None = None,
) -> tuple[Sequence[EvaluationRun], int]:
    """One page of the workspace's evaluations, newest first."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        dataset=dataset,
        agent_id=agent_id,
        judge_model=judge_model,
    )
    return await paginate(session, stmt, params)


async def export_evaluations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    dataset: str | None = None,
    agent_id: str | None = None,
    judge_model: str | None = None,
) -> Sequence[EvaluationRun]:
    """Every evaluation matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        dataset=dataset,
        agent_id=agent_id,
        judge_model=judge_model,
    ).limit(EXPORT_LIMIT)
    return (await session.execute(stmt)).scalars().all()


async def get_evaluation(
    session: AsyncSession, principal: Principal, evaluation_id: str
) -> EvaluationRun:
    """Load one evaluation, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as one that never existed.
    """
    run = await _load(session, principal.workspace_id, evaluation_id)
    if run is None:
        raise NotFound(f"Evaluation '{evaluation_id}' does not exist.")
    return run


async def datasets_in_use(session: AsyncSession, principal: Principal) -> list[str]:
    """Distinct datasets the workspace has evaluated — the Dataset filter's options."""
    rows = (
        await session.execute(
            select(EvaluationRun.dataset_ref)
            .where(EvaluationRun.workspace_id == principal.workspace_id)
            .group_by(EvaluationRun.dataset_ref)
            .order_by(EvaluationRun.dataset_ref.asc())
        )
    ).all()
    return [row[0] for row in rows]


async def get_detail(
    session: AsyncSession,
    principal: Principal,
    evaluation_id: str,
    *,
    item_page: int = 1,
    item_page_size: int = DEFAULT_ITEM_PAGE_SIZE,
) -> EvaluationDetail:
    """The evaluation with its per-metric and per-case breakdown.

    Metric averages are re-read from the engine when the run is still live, so
    an open detail view converges on the truth rather than on the cache.

    Everything about a finished run except its per-case breakdown lives in this
    database, and the inspector opens on every visit to the screen. So the store
    is asked only for what it alone holds, the reads it is asked for run side by
    side under one budget, and a store that is slow or down costs the view its
    case list (``items_unavailable``) — not the scores, baseline and trend that
    never needed it.
    """
    run = await get_evaluation(session, principal, evaluation_id)
    reads = await read_many(session, principal, [run])
    record = reads[0]
    baseline = (
        await get_evaluation(session, principal, record.baseline_run_id)
        if record.baseline_run_id
        else None
    )
    trend = await _trend_points(
        session,
        principal,
        window_days=90,
        agent_id=run.agent_id,
        dataset=run.dataset_ref,
    )

    scores = metric_values(run.scores)
    baseline_scores = metric_values(baseline.scores) if baseline else {}
    client = get_engine_client()

    # A finished run's numbers are on the row: ``case_count`` is the cases it
    # judged. Only a live run is worth a read of the experiment, and its judged
    # count comes from the supervisor, which counts verdicts rather than
    # registrations.
    terminal = run.status in TERMINAL_STATUSES
    state = None if terminal else supervisor.progress(evaluation_id)
    scored = record.cases if terminal else (state.scored if state is not None else 0)

    async def _live_scores() -> dict[str, float]:
        if terminal or not run.engine_experiment_id:
            return {}
        with contextlib.suppress(EngineError):
            return experiment_scores(await client.get_experiment(run.engine_experiment_id))
        return {}

    async def _cases() -> tuple[list[EvaluationItemRead], int, bool]:
        try:
            items, total = await _load_items(
                client,
                engine_namespace(principal),
                run,
                page=item_page,
                page_size=item_page_size,
            )
        except NotFound:
            # The dataset has been deleted since the run. The judged averages on
            # the row are still true, so the view keeps them and simply has no
            # cases to break down — better than 404-ing a historical evaluation.
            return [], 0, False
        except AppError as exc:
            log.warning("evaluation %s: case breakdown unavailable: %s", run.id, exc.message)
            return [], 0, True
        return items, total, False

    # No SQL inside the budget: expiry cancels what is awaited, and a statement
    # cancelled half way would poison the request's connection. The exchanges
    # themselves are shielded by the adapter and finish on their own timeouts.
    try:
        async with asyncio.timeout(DETAIL_ENGINE_BUDGET_SECONDS):
            live_scores, (items, item_total, items_unavailable) = await asyncio.gather(
                _live_scores(), _cases()
            )
    except TimeoutError:
        log.warning("evaluation %s: the telemetry store did not answer in time", run.id)
        live_scores, items, item_total, items_unavailable = {}, [], 0, True
    scores = live_scores or scores

    metrics = [
        MetricScore(
            key=key,
            label=METRIC_LABELS[key],
            value=scores.get(key),
            baseline=baseline_scores.get(key),
            delta=(
                round(scores[key] - baseline_scores[key], 4)
                if key in scores and key in baseline_scores
                else None
            ),
        )
        for key in METRIC_KEYS
    ]

    return EvaluationDetail(
        **record.model_dump(),
        metrics=metrics,
        items=items,
        item_page=item_page,
        item_page_size=item_page_size,
        item_total=item_total,
        items_unavailable=items_unavailable,
        scored_items=scored,
        trend=trend,
    )


async def _load_items(
    client: EngineClient,
    namespace: str,
    run: EvaluationRun,
    *,
    page: int,
    page_size: int,
) -> tuple[list[EvaluationItemRead], int]:
    """One page of the run's cases, read live from the engine."""
    size = max(1, min(page_size, MAX_ITEM_PAGE_SIZE))
    dataset = await resolve_dataset(client, namespace, run.dataset_ref)
    dataset_id = str(dataset.get("id") or "")
    if not dataset_id:
        return [], 0

    try:
        # The plain items listing carries no experiment results; the comparison
        # endpoint joins each case to this run's verdicts and outputs.
        if run.engine_experiment_id:
            payload = await client.list_dataset_items_with_experiments(
                dataset_id, [run.engine_experiment_id], page=max(1, page), size=size
            )
        else:
            payload = await client.list_dataset_items(dataset_id, page=max(1, page), size=size)
    except EngineError as exc:
        raise translate_engine_error(exc) from exc

    rows = payload.get("content") or payload.get("items") or []
    total = int(payload.get("total") or len(rows))
    offset = (max(1, page) - 1) * size

    items: list[EvaluationItemRead] = []
    for index, row in enumerate(rows, start=offset + 1):
        if not isinstance(row, dict):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        result = item_experiment_result(row, run.engine_experiment_id) or {}
        case_scores = metric_values(result.get("feedback_scores") or result.get("scores"))
        avg = average_score(dict(case_scores))
        items.append(
            EvaluationItemRead(
                id=str(row.get("id") or index),
                index=index,
                input=_text_of(data.get("input") or data.get("question")),
                expected_output=_text_of(
                    data.get("expected_output") or data.get("expected") or data.get("reference")
                ),
                actual_output=_text_of(result.get("output") or result.get("actual_output")),
                scores=case_scores,
                avg_score=avg,
                passed=None if avg is None else avg >= CASE_PASS_SCORE,
                trace_id=result.get("trace_id"),
            )
        )
    return items, total


async def get_progress(
    session: AsyncSession, principal: Principal, evaluation_id: str
) -> EvaluationProgress:
    """Real progress: measured counts from the supervisor, or what the row says.

    A run supervised by another worker has no in-process state here. Its
    progress is then read off the row — which phase it has reached, and the
    size of the work — and never from the store or from elapsed time: the modal
    polls this every second and a half, from every tab that has it open.
    """
    run = await get_evaluation(session, principal, evaluation_id)
    status = EvaluationStatus(run.status)
    terminal = run.status in TERMINAL_STATUSES

    state = supervisor.progress(evaluation_id)
    total = run.case_count
    processed = run.case_count if terminal else 0
    scored = run.case_count if status is EvaluationStatus.COMPLETED else 0
    phase = PHASE_FINISHED if terminal else PHASE_PREPARING
    detail = run.notes

    if state is not None and not terminal:
        total = state.total or total
        processed = state.processed
        scored = state.scored
        phase = state.phase
        detail = state.detail or detail
    elif not terminal and run.engine_experiment_id:
        # Supervised by another worker. The experiment exists, so registration
        # is under way or over; how many cases are judged is not something the
        # experiment payload can say (its trace count is registrations), so it
        # is not asked — a poll every second and a half from every open modal
        # was several hundred store reads per run for a number that was wrong.
        processed = total
        phase = PHASE_SCORING

    if terminal:
        percent = 100.0
    elif total:
        register = min(1.0, processed / total) * REGISTER_PHASE_WEIGHT
        percent = round(
            min(99.0, register + min(1.0, scored / total) * (100.0 - REGISTER_PHASE_WEIGHT)), 1
        )
    else:
        percent = 0.0

    started = run.started_at
    elapsed = (
        (_as_utc(run.finished_at or _now()) - _as_utc(started)).total_seconds()
        if started
        else None
    )
    return EvaluationProgress(
        evaluation_id=run.id,
        status=status,
        phase=phase,
        total_cases=total,
        processed_cases=processed,
        scored_cases=scored,
        percent=percent,
        is_terminal=terminal,
        started_at=run.started_at,
        finished_at=run.finished_at,
        elapsed_seconds=None if elapsed is None else round(elapsed, 1),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# KPI cards, trend and comparison
# ---------------------------------------------------------------------------


#: Everything the KPI cards read off a run. The console asks for the cards after
#: every table load, and each call scans two whole windows, so the scan loads
#: these five columns and nothing else: not the ORM row, with its notes, its
#: bookkeeping and an identity-map entry apiece for up to ten thousand rows.
_SUMMARY_COLUMNS: Final[tuple[Any, ...]] = (
    EvaluationRun.status,
    EvaluationRun.scores,
    EvaluationRun.case_count,
    EvaluationRun.agent_id,
    EvaluationRun.dataset_ref,
)


async def _window_runs(
    session: AsyncSession, principal: Principal, since: dt.datetime, until: dt.datetime
) -> Sequence[Any]:
    """The window's runs, oldest first, as rows of :data:`_SUMMARY_COLUMNS`."""
    stmt = (
        select(*_SUMMARY_COLUMNS)
        .where(
            EvaluationRun.workspace_id == principal.workspace_id,
            EvaluationRun.created_at >= since,
            EvaluationRun.created_at < until,
        )
        .order_by(EvaluationRun.created_at.asc())
        .limit(SUMMARY_SCAN_LIMIT)
    )
    return (await session.execute(stmt)).all()


def _count_regressions(runs: Sequence[Any]) -> int:
    """Runs that scored materially below the previous run of the same pair.

    The comparison walks each agent-and-dataset series in time order, which is
    exactly what the Regressions Caught card claims to count.
    """
    previous: dict[tuple[str | None, str], float] = {}
    regressions = 0
    for run in runs:
        if run.status != EvaluationStatus.COMPLETED.value:
            continue
        avg = average_score(metric_values(run.scores))
        if avg is None:
            continue
        key = (run.agent_id, run.dataset_ref)
        earlier = previous.get(key)
        if earlier is not None and avg < earlier - REGRESSION_THRESHOLD:
            regressions += 1
        previous[key] = avg
    return regressions


def _percent_delta(current: float, earlier: float) -> float | None:
    if earlier <= 0:
        return None
    return round((current - earlier) / earlier * 100, 1)


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = 30
) -> EvaluationsSummary:
    """The five KPI cards, each measured against the preceding window."""
    now = _now()
    window = dt.timedelta(days=window_days)
    current = await _window_runs(session, principal, now - window, now)
    previous = await _window_runs(session, principal, now - 2 * window, now - window)

    def _scores(runs: Sequence[Any]) -> list[float]:
        values = [
            avg
            for run in runs
            if run.status == EvaluationStatus.COMPLETED.value
            and (avg := average_score(metric_values(run.scores))) is not None
        ]
        return values

    current_scores = _scores(current)
    previous_scores = _scores(previous)
    avg_now = round(sum(current_scores) / len(current_scores), 4) if current_scores else None
    avg_before = (
        round(sum(previous_scores) / len(previous_scores), 4) if previous_scores else None
    )

    def _cases(runs: Sequence[Any]) -> int:
        # ``case_count`` is the cases a run judged only once it has completed.
        # While a run is live the column holds the dataset's size, for the
        # progress bar, and a run the reaper failed for it keeps that size —
        # neither has judged a case, so neither adds to Test Cases Run.
        return sum(
            run.case_count for run in runs if run.status == EvaluationStatus.COMPLETED.value
        )

    cases_now = _cases(current)
    cases_before = _cases(previous)

    judge = (
        await session.execute(
            select(EvaluationRun.judge_model, func.count(EvaluationRun.id).label("uses"))
            .where(
                EvaluationRun.workspace_id == principal.workspace_id,
                EvaluationRun.created_at >= now - window,
            )
            .group_by(EvaluationRun.judge_model)
            .order_by(func.count(EvaluationRun.id).desc())
            .limit(1)
        )
    ).first()

    regressions_now = _count_regressions(current)
    regressions_before = _count_regressions(previous)

    return EvaluationsSummary(
        window_days=window_days,
        evaluations=len(current),
        evaluations_delta=len(current) - len(previous),
        avg_score=avg_now,
        avg_score_delta=(
            None if avg_now is None or avg_before is None else round(avg_now - avg_before, 4)
        ),
        cases_run=cases_now,
        cases_run_delta_percent=_percent_delta(cases_now, cases_before),
        regressions_caught=regressions_now,
        regressions_caught_delta=regressions_now - regressions_before,
        judge_model=judge[0] if judge else None,
        running=sum(1 for run in current if run.status == EvaluationStatus.RUNNING.value),
        failed=sum(1 for run in current if run.status == EvaluationStatus.FAILED.value),
    )


async def _trend_points(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int,
    agent_id: str | None = None,
    dataset: str | None = None,
) -> list[EvaluationTrendPoint]:
    since = _now() - dt.timedelta(days=window_days)
    stmt = (
        _scoped(principal)
        .where(
            EvaluationRun.status == EvaluationStatus.COMPLETED.value,
            EvaluationRun.finished_at.is_not(None),
            EvaluationRun.finished_at >= since,
        )
        .order_by(EvaluationRun.finished_at.asc())
        .limit(SUMMARY_SCAN_LIMIT)
    )
    stmt = apply_filters(
        stmt, {EvaluationRun.agent_id: agent_id, EvaluationRun.dataset_ref: dataset}
    )
    runs = (await session.execute(stmt)).scalars().all()

    buckets: dict[dt.date, list[EvaluationRun]] = {}
    for run in runs:
        buckets.setdefault(_as_utc(run.finished_at).date(), []).append(run)

    points: list[EvaluationTrendPoint] = []
    for day in sorted(buckets):
        rows = buckets[day]
        per_metric: dict[str, list[float]] = {}
        for run in rows:
            for key, value in metric_values(run.scores).items():
                per_metric.setdefault(key, []).append(value)
        averages = {
            key: round(sum(values) / len(values), 4) for key, values in per_metric.items()
        }
        points.append(
            EvaluationTrendPoint(
                date=day,
                evaluations=len(rows),
                cases=sum(run.case_count for run in rows),
                avg_score=average_score(dict(averages)),
                correctness=averages.get("correctness"),
                grounding=averages.get("grounding"),
                faithfulness=averages.get("faithfulness"),
                safety=averages.get("safety"),
            )
        )
    return points


async def trend(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int = 30,
    agent_id: str | None = None,
    dataset: str | None = None,
) -> EvaluationTrend:
    """Daily judged averages over the window, oldest first."""
    return EvaluationTrend(
        window_days=window_days,
        points=await _trend_points(
            session, principal, window_days=window_days, agent_id=agent_id, dataset=dataset
        ),
        agent_id=agent_id,
        dataset=dataset,
    )


async def compare(
    session: AsyncSession, principal: Principal, baseline_id: str, candidate_id: str
) -> EvaluationComparison:
    """Two evaluations side by side, metric by metric."""
    if baseline_id == candidate_id:
        raise ValidationFailed("Choose two different evaluations to compare.")
    baseline = await get_evaluation(session, principal, baseline_id)
    candidate = await get_evaluation(session, principal, candidate_id)

    reads = await read_many(session, principal, [baseline, candidate])
    baseline_scores = metric_values(baseline.scores)
    candidate_scores = metric_values(candidate.scores)

    metrics: list[MetricScore] = []
    regressed: list[str] = []
    improved: list[str] = []
    for key in METRIC_KEYS:
        before = baseline_scores.get(key)
        after = candidate_scores.get(key)
        delta = None if before is None or after is None else round(after - before, 4)
        if delta is not None and delta < -REGRESSION_THRESHOLD:
            regressed.append(METRIC_LABELS[key])
        elif delta is not None and delta > REGRESSION_THRESHOLD:
            improved.append(METRIC_LABELS[key])
        metrics.append(
            MetricScore(
                key=key, label=METRIC_LABELS[key], value=after, baseline=before, delta=delta
            )
        )

    before_avg = average_score(dict(baseline_scores))
    after_avg = average_score(dict(candidate_scores))
    avg_delta = (
        None if before_avg is None or after_avg is None else round(after_avg - before_avg, 4)
    )
    if avg_delta is None:
        verdict = "Unchanged"
    elif avg_delta < -REGRESSION_THRESHOLD:
        verdict = "Regressed"
    elif avg_delta > REGRESSION_THRESHOLD:
        verdict = "Improved"
    else:
        verdict = "Unchanged"

    return EvaluationComparison(
        baseline=reads[0],
        candidate=reads[1],
        metrics=metrics,
        avg_score_delta=avg_delta,
        verdict=verdict,
        regressed_metrics=regressed,
        improved_metrics=improved,
    )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


async def list_datasets(
    principal: Principal, params: ListParams
) -> tuple[list[DatasetRead], int]:
    """The workspace's datasets, read from the engine and un-namespaced.

    Anything whose name does not carry this workspace's prefix is dropped, so
    the list can only ever contain datasets this tenant owns.

    The engine pages what *it* matched — every name containing the namespace —
    while the search and the tenancy filter are applied here. Filtering one
    engine page therefore returned short or empty pages under a total that
    counted rows the caller would never see: a search whose matches sat on the
    engine's second page came back empty, and a client paging until an empty
    page stopped before reaching them. So the namespace's datasets are read in
    full (bounded by :data:`DATASET_SCAN_LIMIT`), filtered, and only then paged,
    and the total is the total of what was kept.
    """
    client = get_engine_client()
    namespace = engine_namespace(principal)
    rows: list[Any] = []
    engine_page = 1
    while len(rows) < DATASET_SCAN_LIMIT:
        try:
            payload = await client.list_datasets(
                page=engine_page, size=DATASET_SCAN_PAGE_SIZE, name=namespace
            )
        except EngineError as exc:
            raise translate_engine_error(exc) from exc
        batch = payload.get("content") or payload.get("datasets") or []
        rows.extend(batch)
        if len(batch) < DATASET_SCAN_PAGE_SIZE:
            break
        engine_page += 1

    needle = (params.q or "").strip().lower()

    items: list[DatasetRead] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = local_name(namespace, row.get("name"))
        if name is None:
            continue
        if needle and needle not in name.lower():
            continue
        items.append(
            DatasetRead(
                name=name,
                engine_id=row.get("id"),
                description=row.get("description"),
                case_count=int(row.get("dataset_items_count") or row.get("size") or 0),
                experiment_count=int(row.get("experiment_count") or 0),
                tags=[tag for tag in (row.get("tags") or []) if isinstance(tag, str)],
                created_at=_parse_instant(row.get("created_at")),
                last_updated_at=_parse_instant(row.get("last_updated_at")),
                created_by=row.get("created_by"),
            )
        )
    return items[params.offset : params.offset + params.page_size], len(items)


async def create_dataset(
    session: AsyncSession,
    principal: Principal,
    payload: DatasetCreate,
    *,
    request: Request | None = None,
) -> DatasetRead:
    """Create a dataset in the engine under this workspace's namespace."""
    principal.require(Role.MEMBER)
    client = get_engine_client()
    name = engine_name(principal, payload.name)

    try:
        existing = await client.find_dataset_by_name(name)
    except EngineError as exc:
        raise translate_engine_error(exc) from exc
    if existing:
        raise Conflict(f"A dataset named '{payload.name}' already exists.")

    try:
        created = await client.create_dataset(
            name, description=payload.description, tags=payload.tags or None
        )
    except EngineError as exc:
        raise translate_engine_error(exc) from exc

    await audit.record(
        session,
        principal=principal,
        action="evaluation.dataset_created",
        entity_type="dataset",
        entity_id=str(created.get("id") or payload.name),
        entity_label=payload.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Created dataset {payload.name}",
        request=request,
    )
    await session.flush()

    return DatasetRead(
        name=payload.name,
        engine_id=created.get("id"),
        description=payload.description,
        case_count=int(created.get("dataset_items_count") or 0),
        tags=payload.tags,
        created_at=_parse_instant(created.get("created_at")),
    )


async def add_dataset_items(
    session: AsyncSession,
    principal: Principal,
    dataset: str,
    payload: DatasetItemsCreate,
    *,
    request: Request | None = None,
) -> int:
    """Append cases to a dataset. Returns how many were sent."""
    principal.require(Role.MEMBER)
    client = get_engine_client()
    resolved = await resolve_dataset(client, engine_namespace(principal), dataset)

    # The engine requires a provenance on every case; ours arrive through the
    # API, by hand from the console or programmatically from the SDK.
    source = "sdk" if principal.kind == "api_key" else "manual"
    items = [
        {
            "source": source,
            "data": {
                "input": item.input,
                **({"expected_output": item.expected_output} if item.expected_output else {}),
                **item.metadata,
            },
        }
        for item in payload.items
    ]
    try:
        await client.create_dataset_items_batch(items, dataset_id=str(resolved.get("id") or ""))
    except EngineError as exc:
        raise translate_engine_error(exc) from exc

    await audit.record(
        session,
        principal=principal,
        action="evaluation.dataset_items_added",
        entity_type="dataset",
        entity_id=str(resolved.get("id") or dataset),
        entity_label=dataset,
        source_screen=SOURCE_SCREEN,
        detail=f"Appended {len(items)} case(s) to {dataset}",
        metadata={"cases": len(items)},
        request=request,
    )
    await session.flush()
    return len(items)


async def list_dataset_items(
    principal: Principal, dataset: str, *, page: int = 1, page_size: int = DEFAULT_ITEM_PAGE_SIZE
) -> tuple[list[DatasetItemRead], int]:
    """One page of a dataset's cases, read live from the engine."""
    client = get_engine_client()
    resolved = await resolve_dataset(client, engine_namespace(principal), dataset)
    size = max(1, min(page_size, MAX_ITEM_PAGE_SIZE))
    try:
        payload = await client.list_dataset_items(
            str(resolved.get("id") or ""), page=max(1, page), size=size
        )
    except EngineError as exc:
        raise translate_engine_error(exc) from exc

    rows = payload.get("content") or payload.get("items") or []
    items: list[DatasetItemRead] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        known = {"input", "question", "expected_output", "expected", "reference"}
        items.append(
            DatasetItemRead(
                id=str(row.get("id") or ""),
                input=_text_of(data.get("input") or data.get("question")),
                expected_output=_text_of(
                    data.get("expected_output") or data.get("expected") or data.get("reference")
                ),
                metadata={k: v for k, v in data.items() if k not in known},
                created_at=_parse_instant(row.get("created_at")),
            )
        )
    return items, int(payload.get("total") or len(items))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _resolve_agent(
    session: AsyncSession, principal: Principal, agent_id: str | None
) -> Agent | None:
    if agent_id is None:
        return None
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.workspace_id == principal.workspace_id, Agent.id == agent_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound(f"Agent '{agent_id}' does not exist.")
    return agent


async def create_evaluation(
    session: AsyncSession,
    principal: Principal,
    payload: EvaluationCreate,
    *,
    request: Request | None = None,
) -> EvaluationRun:
    """Queue an evaluation. The supervisor is started by the route afterwards.

    The dataset is resolved against the engine before the row is written: a run
    against a dataset that does not exist would only fail later, in a place the
    operator cannot see.
    """
    principal.require(Role.MEMBER)
    agent = await _resolve_agent(session, principal, payload.agent_id)
    client = get_engine_client()
    await resolve_dataset(client, engine_namespace(principal), payload.dataset)

    label = agent.name if agent else "Workspace"
    run = EvaluationRun(
        workspace_id=principal.workspace_id,
        name=payload.name or f"{label} x {payload.dataset}",
        agent_id=payload.agent_id,
        dataset_ref=payload.dataset,
        judge_model=payload.judge_model,
        status=EvaluationStatus.QUEUED.value,
        case_count=0,
        scores={},
        triggered_by_user_id=principal.user_id,
        notes=payload.notes,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(run)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="evaluation.started",
        entity_type=ENTITY_TYPE,
        entity_id=run.id,
        entity_label=run.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Queued {payload.dataset} against judge {payload.judge_model}",
        metadata={"dataset": payload.dataset, "agent_id": payload.agent_id},
        request=request,
    )
    await session.flush()
    await session.refresh(run)
    return run


async def rerun_evaluation(
    session: AsyncSession,
    principal: Principal,
    evaluation_id: str,
    payload: EvaluationRerun | None = None,
    *,
    request: Request | None = None,
) -> EvaluationRun:
    """Queue a fresh run with the source run's agent, dataset and judge.

    A re-run is a new row on purpose: overwriting the original would destroy
    the baseline the next comparison needs.
    """
    principal.require(Role.MEMBER)
    source = await get_evaluation(session, principal, evaluation_id)
    if source.status == EvaluationStatus.RUNNING.value:
        raise PreconditionFailed(f"'{source.name}' is still running.")

    payload = payload or EvaluationRerun()
    run = EvaluationRun(
        workspace_id=principal.workspace_id,
        name=source.name,
        agent_id=source.agent_id,
        dataset_ref=source.dataset_ref,
        judge_model=payload.judge_model or source.judge_model,
        status=EvaluationStatus.QUEUED.value,
        case_count=0,
        scores={},
        triggered_by_user_id=principal.user_id,
        notes=payload.notes or f"Re-run of {source.id}",
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(run)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="evaluation.rerun",
        entity_type=ENTITY_TYPE,
        entity_id=run.id,
        entity_label=run.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Re-run of evaluation {source.id}",
        metadata={"source_evaluation_id": source.id},
        request=request,
    )
    await session.flush()
    await session.refresh(run)
    return run


async def owner_names(
    session: AsyncSession, principal: Principal, user_ids: Sequence[str | None]
) -> dict[str, str]:
    """Display names for a set of user ids. Shared with the testing screen."""
    ids = {user_id for user_id in user_ids if user_id}
    if not ids:
        return {}
    rows = (await session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all()
    return {user_id: full_name for user_id, full_name in rows}
