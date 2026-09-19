"""Evaluations routes.

Thirteen endpoints back one screen: the KPI cards, the scored table and its two
filters, the CSV export, the Run Evaluation modal, the inspector's score
breakdown and trend, the progress the modal watches while a run is live, the
Re-run button, the baseline comparison, and the dataset picker behind all of it.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
audit writes and every call to the telemetry engine live in
``services.evaluations``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.quality import EvaluationStatus
from ...schemas.evaluations import (
    EXPORT_COLUMNS,
    DatasetCreate,
    DatasetItemRead,
    DatasetItemsCreate,
    DatasetRead,
    EvaluationComparison,
    EvaluationCreate,
    EvaluationDetail,
    EvaluationProgress,
    EvaluationRead,
    EvaluationRerun,
    EvaluationsSummary,
    EvaluationTrend,
)
from ...services import evaluations as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db, Principal

router = APIRouter(prefix="/evaluations", tags=["Evaluations"])


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


async def _start(
    session: Db, principal: Principal, run: Any, background: BackgroundTasks
) -> EvaluationRead:
    """Make a queued run durable, then hand it to the supervisor.

    The supervisor reads the row on a connection of its own, and background
    tasks run *before* the request-scoped session commits. Scheduled first, the
    supervisor's opening SELECT races the COMMIT; when it wins it finds no row,
    gives up without a word, and the run sits Queued behind a frozen progress
    bar until the reaper fails it a quarter of an hour later. Committing here
    closes that window — the same order the export queue uses — and means a
    caller holding a 202 is holding a promise the database has already accepted.
    """
    await session.commit()
    background.add_task(service.execute_evaluation, run.id, principal.workspace_id)
    records = await service.read_many(session, principal, [run])
    return records[0]


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{evaluation_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=EvaluationsSummary, summary="Evaluation KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> EvaluationsSummary:
    """The five KPI cards: evaluations, average score, cases run, regressions
    caught and the judge in use.

    Every card carries its movement against the preceding window of the same
    length, which is what the console prints under the number.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/trend", response_model=EvaluationTrend, summary="Score trend")
async def get_trend(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
    agent_id: Annotated[str | None, Query(description="Restrict to one agent")] = None,
    dataset: Annotated[str | None, Query(description="Restrict to one dataset")] = None,
) -> EvaluationTrend:
    """Daily judged averages, oldest first.

    Only completed runs contribute: a run still being scored would drag the
    line down for no reason other than that it has not finished.
    """
    return await service.trend(
        session, principal, window_days=window_days, agent_id=agent_id, dataset=dataset
    )


@router.get("/compare", response_model=EvaluationComparison, summary="Compare to baseline")
async def compare_evaluations(
    principal: CurrentPrincipal,
    session: Db,
    baseline: Annotated[str, Query(description="Evaluation id to compare against")],
    candidate: Annotated[str, Query(description="Evaluation id under review")],
) -> EvaluationComparison:
    """Two evaluations metric by metric, with the verdict the row action shows."""
    return await service.compare(session, principal, baseline, candidate)


@router.get("/datasets", response_model=Page[DatasetRead], summary="List datasets")
async def list_datasets(
    principal: CurrentPrincipal,
    params: Annotated[ListParams, Depends(list_params)],
) -> Page[DatasetRead]:
    """The datasets this workspace can evaluate against.

    They live in the telemetry engine; only those namespaced to this workspace
    are returned, so a dataset belonging to another tenant is invisible rather
    than merely unreachable.
    """
    items, total = await service.list_datasets(principal, params)
    return Page.build(items, total, params.page, params.page_size)


@router.get(
    "/datasets/in-use",
    response_model=list[str],
    summary="Datasets this workspace has evaluated",
)
async def list_datasets_in_use(principal: CurrentPrincipal, session: Db) -> list[str]:
    """The distinct datasets on the workspace's evaluations — the table's Dataset filter.

    Read from the evaluations themselves rather than from the engine's dataset
    listing: the filter has to offer every dataset a row in the table names,
    including one that has since been deleted, and none that no row does.
    """
    return await service.datasets_in_use(session, principal)


@router.post(
    "/datasets",
    response_model=DatasetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a dataset",
)
async def create_dataset(
    principal: CurrentPrincipal,
    session: Db,
    payload: DatasetCreate,
    request: Request,
) -> DatasetRead:
    """Create an empty dataset, then fill it with `POST /datasets/{name}/items`.

    Requires the member role.
    """
    return await service.create_dataset(session, principal, payload, request=request)


@router.get(
    "/datasets/{dataset}/items",
    response_model=Page[DatasetItemRead],
    summary="List dataset cases",
)
async def list_dataset_items(
    principal: CurrentPrincipal,
    dataset: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[DatasetItemRead]:
    """One page of a dataset's cases, read live from the telemetry engine."""
    items, total = await service.list_dataset_items(
        principal, dataset, page=page, page_size=page_size
    )
    return Page.build(items, total, page, page_size)


@router.post(
    "/datasets/{dataset}/items",
    response_model=ActionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add dataset cases",
)
async def add_dataset_items(
    principal: CurrentPrincipal,
    session: Db,
    dataset: str,
    payload: DatasetItemsCreate,
    request: Request,
) -> ActionResult:
    """Append up to 1000 cases. The engine upserts, so a replay is harmless.

    Requires the member role.
    """
    added = await service.add_dataset_items(session, principal, dataset, payload, request=request)
    return ActionResult(
        message=f"{added} case(s) added to {dataset}.",
        entity_id=dataset,
        data={"added": added},
    )


@router.get("/export", summary="Export evaluations as CSV")
async def export_evaluations(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    evaluation_status: Annotated[
        EvaluationStatus | None, Query(alias="status", description="Completed, Running or Failed")
    ] = None,
    dataset: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    judge_model: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The current view as CSV.

    Honours the same search and filters as the list endpoint, so the file
    matches exactly what the operator is looking at.
    """
    rows = await service.export_evaluations(
        session,
        principal,
        params,
        status=evaluation_status.value if evaluation_status else None,
        dataset=dataset,
        agent_id=agent_id,
        judge_model=judge_model,
    )
    records = await service.read_many(session, principal, rows)
    payload: list[dict[str, Any]] = [
        {
            "agent_name": record.agent_name or "-",
            "agent_model": record.agent_model,
            "dataset": record.dataset,
            "cases": record.cases,
            "correctness": record.correctness,
            "grounding": record.grounding,
            "faithfulness": record.faithfulness,
            "safety": record.safety,
            "avg_score": record.avg_score,
            "baseline_delta": record.baseline_delta,
            "judge_model": record.judge_model,
            "status": record.status.value,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }
        for record in records
    ]
    return StreamingResponse(
        iter([to_csv(payload, EXPORT_COLUMNS)]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="evaluations-{_stamp()}.csv"'
        },
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[EvaluationRead], summary="List evaluations")
async def list_evaluations(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    evaluation_status: Annotated[
        EvaluationStatus | None, Query(alias="status", description="Completed, Running or Failed")
    ] = None,
    dataset: Annotated[str | None, Query(description="Exact dataset name")] = None,
    agent_id: Annotated[str | None, Query()] = None,
    judge_model: Annotated[str | None, Query()] = None,
) -> Page[EvaluationRead]:
    """One page of the workspace's evaluations, newest first.

    Each row carries the four judged metrics and the delta against the previous
    completed run of the same agent and dataset, so the table can flag a
    regression without a second request. Free-text search covers the run name,
    the dataset, the judge and the agent; sorting is available on every column
    the database holds, which excludes the four score columns because they live
    in the engine's judgement rather than in a sortable field.
    """
    rows, total = await service.list_evaluations(
        session,
        principal,
        params,
        status=evaluation_status.value if evaluation_status else None,
        dataset=dataset,
        agent_id=agent_id,
        judge_model=judge_model,
    )
    records = await service.read_many(session, principal, rows)
    return Page.build(records, total, params.page, params.page_size)


@router.post(
    "",
    response_model=EvaluationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an evaluation",
)
async def create_evaluation(
    principal: CurrentPrincipal,
    session: Db,
    payload: EvaluationCreate,
    background: BackgroundTasks,
    request: Request,
) -> EvaluationRead:
    """Start an evaluation — the Run Evaluation modal.

    The row is created Queued and the experiment is driven by a background
    supervisor, so the request returns immediately; poll
    `GET /evaluations/{id}/progress` for the bar the modal shows. The dataset is
    resolved against the engine first, so a typo fails here rather than three
    seconds later in a task nobody is watching. Requires the member role.
    """
    run = await service.create_evaluation(session, principal, payload, request=request)
    return await _start(session, principal, run, background)


# ---------------------------------------------------------------------------
# Single evaluation
# ---------------------------------------------------------------------------


@router.get(
    "/{evaluation_id}", response_model=EvaluationDetail, summary="Get an evaluation"
)
async def get_evaluation(
    principal: CurrentPrincipal,
    session: Db,
    evaluation_id: str,
    item_page: Annotated[int, Query(ge=1, description="Page of the case breakdown")] = 1,
    item_page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> EvaluationDetail:
    """One evaluation with its per-metric scores and per-case breakdown.

    The metric values are re-read from the engine while a run is still live, so
    an open inspector converges on the truth instead of the cached average. An
    evaluation in another workspace answers 404, not 403.
    """
    return await service.get_detail(
        session, principal, evaluation_id, item_page=item_page, item_page_size=item_page_size
    )


@router.get(
    "/{evaluation_id}/progress",
    response_model=EvaluationProgress,
    summary="Evaluation progress",
)
async def get_progress(
    principal: CurrentPrincipal, session: Db, evaluation_id: str
) -> EvaluationProgress:
    """Measured progress of a running evaluation.

    Counts come from the supervisor driving the run, or — when the run belongs
    to another worker — from the experiment itself. The percentage is derived
    from cases registered and cases judged; nothing here is a timer.
    """
    return await service.get_progress(session, principal, evaluation_id)


@router.post(
    "/{evaluation_id}/rerun",
    response_model=EvaluationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run an evaluation",
)
async def rerun_evaluation(
    principal: CurrentPrincipal,
    session: Db,
    evaluation_id: str,
    background: BackgroundTasks,
    request: Request,
    payload: EvaluationRerun | None = None,
) -> EvaluationRead:
    """Queue a fresh run with the same agent, dataset and judge.

    The original row is left untouched: it is the baseline the new run will be
    compared against. Requires the member role.
    """
    run = await service.rerun_evaluation(
        session, principal, evaluation_id, payload, request=request
    )
    return await _start(session, principal, run, background)
