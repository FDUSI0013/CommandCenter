"""Prompt Manager routes.

Fourteen operations back one screen: the KPI cards, the filtered table and its
CSV download, the New Prompt dialog, the inspector's prompt text, details and
version pipe, the Test Prompt button, and the draft to review to approved or
blocked lifecycle.

Handlers here only parse, delegate and shape. The prompt bodies themselves live
in the telemetry engine's registry and are reached through the adapter;
namespacing, the governance state machine, RBAC and the audit trail live in
``services.prompts``. The inspector's audit history is served by the audit
router with ``entity_type=prompt``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.registry import EnvironmentType
from ...schemas.prompts import (
    PromptActionResponse,
    PromptCreate,
    PromptDiff,
    PromptExecuteRequest,
    PromptExecuteResult,
    PromptLifecycleRequest,
    PromptRead,
    PromptsSummary,
    PromptStatus,
    PromptTestRequest,
    PromptTestResult,
    PromptVersionCreate,
    PromptVersionDetail,
    PromptVersionRead,
)
from ...services import prompts as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/prompts", tags=["Prompts"])


@dataclasses.dataclass(frozen=True)
class PromptFilters:
    """The Prompt Manager's dropdowns, plus the agent filter the SDK may add."""

    status: PromptStatus | None = None
    environment: str | None = None
    agent: str | None = None


def prompt_filters(
    status_filter: Annotated[
        PromptStatus | None,
        Query(alias="status", description="Draft, In Review, Approved or Blocked."),
    ] = None,
    environment: Annotated[
        EnvironmentType | None, Query(alias="env", description="Environment dropdown.")
    ] = None,
    agent: Annotated[str | None, Query(description="Owning agent's name.")] = None,
) -> PromptFilters:
    """Read the table's dropdown filters off the query string."""
    return PromptFilters(
        status=status_filter,
        environment=environment.value if environment else None,
        agent=agent,
    )


Filters = Annotated[PromptFilters, Depends(prompt_filters)]
Params = Annotated[ListParams, Depends(list_params)]
Window = Annotated[
    int, Query(ge=1, le=365, description="Rolling window for the run statistics.")
]


def _csv_row(prompt: PromptRead) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, _header in service.EXPORT_COLUMNS:
        value = getattr(prompt, key, None)
        if isinstance(value, enum.Enum):
            value = value.value
        elif isinstance(value, dt.datetime):
            value = value.isoformat()
        row[key] = value
    return row


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{prompt_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=PromptsSummary, summary="Prompt KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> PromptsSummary:
    """Totals by governance state plus the average success rate.

    The counts come from this workspace's namespace in the prompt registry
    joined with our audit trail; the success rate is averaged over the prompts
    whose agent actually reported runs in the window, and is null when nothing
    did rather than being reported as zero.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/export", summary="Export prompts as CSV")
async def export_prompts(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> StreamingResponse:
    """Download the filtered prompt list as CSV.

    Honours exactly the search, sort and dropdowns the table is showing.
    """
    rows = await service.export_prompts(
        session,
        principal,
        params,
        status=filters.status,
        environment=filters.environment,
        agent=filters.agent,
        window_days=window_days,
    )
    body = to_csv([_csv_row(row) for row in rows], service.EXPORT_COLUMNS)
    filename = f"prompts-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[PromptRead], summary="List prompts")
async def list_prompts(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> Page[PromptRead]:
    """One page of the workspace's prompts, most recently changed first.

    Rows carry the head version's template and token estimate, the governance
    state, and the run count and success rate of the agent the prompt belongs
    to. Free-text search covers the name, the agent, the owner, the description
    and the environment.
    """
    rows, total = await service.list_prompts(
        session,
        principal,
        params,
        status=filters.status,
        environment=filters.environment,
        agent=filters.agent,
        window_days=window_days,
    )
    return Page[PromptRead].build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=PromptRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a prompt",
)
async def create_prompt(
    principal: CurrentPrincipal,
    session: Db,
    payload: PromptCreate,
    request: Request,
) -> PromptRead:
    """Author a prompt and its first commit.

    It lands Draft: nothing is approved without a review, and the name is
    namespaced to this workspace before it reaches the registry. Requires the
    member role.
    """
    return await service.create_prompt(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Single prompt
# ---------------------------------------------------------------------------


@router.get("/{prompt_id}", response_model=PromptRead, summary="Get a prompt")
async def get_prompt(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> PromptRead:
    """One prompt with its head template. Another workspace's answers 404."""
    return await service.get_prompt(session, principal, prompt_id, window_days=window_days)


@router.get(
    "/{prompt_id}/versions",
    response_model=Page[PromptVersionRead],
    summary="List prompt versions",
)
async def list_versions(
    principal: CurrentPrincipal, session: Db, prompt_id: str, params: Params
) -> Page[PromptVersionRead]:
    """Commit history for one prompt, newest first.

    The head commit reports the prompt's live governance state; earlier commits
    report the state recorded when they were cut.
    """
    rows, total = await service.list_versions(session, principal, prompt_id, params)
    return Page[PromptVersionRead].build(rows, total, params.page, params.page_size)


@router.post(
    "/{prompt_id}/versions",
    response_model=PromptActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Commit a new version",
)
async def create_version(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptVersionCreate,
    request: Request,
) -> PromptActionResponse:
    """Commit a new body.

    A body change sends the prompt back to Draft: approval covers the text that
    was approved, not the name. Requires the member role.
    """
    prompt, version = await service.create_version(
        session, principal, prompt_id, payload, request=request
    )
    return PromptActionResponse(
        prompt=prompt,
        message=(
            f"{prompt.name} {version.version or ''} drafted; submit it for review "
            "when ready."
        ).replace(" ,", ","),
        version=version,
    )


@router.get("/{prompt_id}/diff", response_model=PromptDiff, summary="Diff two versions")
async def diff_versions(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    from_commit: Annotated[
        str, Query(alias="from", description="Baseline commit, id or version label.")
    ],
    to_commit: Annotated[
        str, Query(alias="to", description="Commit to compare, id or version label.")
    ],
) -> PromptDiff:
    """Unified diff of two commits' templates, line by line."""
    return await service.diff(
        session, principal, prompt_id, from_commit=from_commit, to_commit=to_commit
    )


@router.get(
    "/{prompt_id}/versions/{commit}",
    response_model=PromptVersionDetail,
    summary="Get one prompt version",
)
async def get_version(
    principal: CurrentPrincipal, session: Db, prompt_id: str, commit: str
) -> PromptVersionDetail:
    """One commit, template and declared variables included.

    ``commit`` accepts the commit hash, the registry's version id or the human
    version label.
    """
    return await service.get_version(session, principal, prompt_id, commit)


@router.post(
    "/{prompt_id}/restore/{version}",
    response_model=PromptActionResponse,
    summary="Restore an earlier version",
)
async def restore_version(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    version: str,
    request: Request,
) -> PromptActionResponse:
    """Re-commit an earlier version as the head.

    History is not rewritten — the registry appends the restore as a new commit
    — and the prompt returns to Draft for re-review. Requires the operator role.
    """
    prompt = await service.restore_version(
        session, principal, prompt_id, version, request=request
    )
    return PromptActionResponse(
        prompt=prompt,
        message=f"{prompt.name} restored to an earlier version and returned to Draft.",
    )


@router.post(
    "/{prompt_id}/execute",
    response_model=PromptExecuteResult,
    summary="Run a prompt against a model",
)
async def execute_prompt(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptExecuteRequest,
    request: Request,
) -> PromptExecuteResult:
    """Render this prompt with one variable set and send it to a model.

    The Run button in Prompt Studio. Returns the answer, the provider's own
    token counts and the round-trip latency, so a wording change can be judged
    on output, cost and speed together.

    Requires the member role, and requires the deployment to have a model
    endpoint configured — without one this answers `model_unavailable` naming
    the missing setting rather than failing obscurely. Use `/test` instead to
    render and score a whole dataset without calling a model.
    """
    return await service.execute_prompt(session, principal, prompt_id, payload, request=request)


@router.post(
    "/{prompt_id}/test", response_model=PromptTestResult, summary="Test a prompt"
)
async def test_prompt(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptTestRequest,
    request: Request,
) -> PromptTestResult:
    """Render the template over sample variable sets and score the run.

    The response reports, per case, what rendered and which variables were
    missing or left unresolved. With `score` set the rendered cases are stored
    as a dataset and an experiment is opened against the tested commit so the
    engine's evaluation path scores them; no model is executed here because the
    telemetry adapter exposes no completion endpoint. Requires the member role.
    """
    return await service.test_prompt(session, principal, prompt_id, payload, request=request)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@router.post(
    "/{prompt_id}/submit-review",
    response_model=PromptActionResponse,
    summary="Submit a prompt for review",
)
async def submit_review(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptLifecycleRequest,
    request: Request,
) -> PromptActionResponse:
    """Move a Draft or Blocked prompt into review.

    Refused with 409 from any other state. Requires the member role.
    """
    return await service.transition(
        session, principal, prompt_id, PromptStatus.IN_REVIEW, payload, request=request
    )


@router.post(
    "/{prompt_id}/approve",
    response_model=PromptActionResponse,
    summary="Approve a prompt",
)
async def approve(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptLifecycleRequest,
    request: Request,
) -> PromptActionResponse:
    """Approve a prompt that is in review.

    The approval is written to the append-only audit trail, which is where the
    prompt's status is read from — so the sign-off is covered by the same hash
    chain as every other governed change. Requires the operator role.
    """
    return await service.transition(
        session, principal, prompt_id, PromptStatus.APPROVED, payload, request=request
    )


@router.post(
    "/{prompt_id}/block", response_model=PromptActionResponse, summary="Block a prompt"
)
async def block(
    principal: CurrentPrincipal,
    session: Db,
    prompt_id: str,
    payload: PromptLifecycleRequest,
    request: Request,
) -> PromptActionResponse:
    """Block a prompt that failed governance checks.

    Legal from In Review and from Approved; a blocked prompt can only move back
    through review. Requires the operator role.
    """
    return await service.transition(
        session, principal, prompt_id, PromptStatus.BLOCKED, payload, request=request
    )
