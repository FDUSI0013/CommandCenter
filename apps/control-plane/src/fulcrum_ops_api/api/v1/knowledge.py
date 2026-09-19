"""RAG & Knowledge Governance routes.

Twelve operations back one screen: the KPI cards, the filtered table and its CSV
download, the Add Source dialog, Edit and Delete, the Sync Now button with the
percentage bar that polls it, the View Documents modal, and the inspector's
Grounding & Quality panel.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
the sync job and the audit trail live in ``services.knowledge``; the inspector's
audit history is served by the audit router with
``entity_type=knowledge_source``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...db.session import commit_then
from ...models.registry import (
    EnvironmentType,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    Sensitivity,
)
from ...schemas.knowledge import (
    GroundingBreakdown,
    KnowledgeActionResponse,
    KnowledgeDocumentRead,
    KnowledgeSourceCreate,
    KnowledgeSourceRead,
    KnowledgeSourceUpdate,
    KnowledgeSummary,
    KnowledgeSyncStatus,
)
from ...services import knowledge as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])


@dataclasses.dataclass(frozen=True)
class KnowledgeFilters:
    """The screen's four dropdowns."""

    source_type: str | None = None
    status: str | None = None
    environment: str | None = None
    sensitivity: str | None = None


def knowledge_filters(
    source_type: Annotated[
        KnowledgeSourceType | None, Query(alias="type", description="Type dropdown.")
    ] = None,
    status_filter: Annotated[
        KnowledgeSourceStatus | None,
        Query(alias="status", description="Active, Syncing, Failed or Paused."),
    ] = None,
    environment: Annotated[
        EnvironmentType | None, Query(alias="env", description="Environment dropdown.")
    ] = None,
    sensitivity: Annotated[
        Sensitivity | None, Query(description="Sensitivity dropdown.")
    ] = None,
) -> KnowledgeFilters:
    """Read the table's dropdown filters off the query string."""
    return KnowledgeFilters(
        source_type=source_type.value if source_type else None,
        status=status_filter.value if status_filter else None,
        environment=environment.value if environment else None,
        sensitivity=sensitivity.value if sensitivity else None,
    )


Filters = Annotated[KnowledgeFilters, Depends(knowledge_filters)]
Params = Annotated[ListParams, Depends(list_params)]
Window = Annotated[
    int, Query(ge=1, le=365, description="Rolling window over retrieval telemetry.")
]


def _csv_row(source: KnowledgeSourceRead) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, _header in service.EXPORT_COLUMNS:
        value = getattr(source, key, None)
        if value is None and key == "description":
            value = source.settings.description
        if isinstance(value, enum.Enum):
            value = value.value
        elif isinstance(value, dt.datetime):
            value = value.isoformat()
        row[key] = value
    return row


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{source_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=KnowledgeSummary, summary="Knowledge KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> KnowledgeSummary:
    """Source counts, corpus totals, average grounding score and ACL coverage.

    Every number is a SQL aggregate over the workspace's sources. The average
    grounding score is null, not zero, until at least one source has been scored.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/export", summary="Export knowledge sources as CSV")
async def export_sources(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> StreamingResponse:
    """Download the filtered source list as CSV.

    Honours exactly the search, sort and dropdowns the table is showing.
    """
    rows = await service.export_sources(
        session,
        principal,
        params,
        source_type=filters.source_type,
        status=filters.status,
        environment=filters.environment,
        sensitivity=filters.sensitivity,
    )
    body = to_csv([_csv_row(row) for row in rows], service.EXPORT_COLUMNS)
    filename = f"knowledge-sources-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[KnowledgeSourceRead], summary="List knowledge sources")
async def list_sources(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> Page[KnowledgeSourceRead]:
    """One page of the workspace's knowledge sources.

    Rows carry the retrieval policy, the sync state behind the progress bar and
    the derived indexing status. Free-text search covers the name, the type, the
    vector index, the ACL summary and the owner's name.
    """
    rows, total = await service.list_sources(
        session,
        principal,
        params,
        source_type=filters.source_type,
        status=filters.status,
        environment=filters.environment,
        sensitivity=filters.sensitivity,
    )
    return Page[KnowledgeSourceRead].build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=KnowledgeActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a knowledge source",
)
async def create_source(
    principal: CurrentPrincipal,
    session: Db,
    payload: KnowledgeSourceCreate,
    request: Request,
    background: BackgroundTasks,
) -> KnowledgeActionResponse:
    """Register a source and start its first sync.

    ACL-aware retrieval cannot be switched off for a confidential or higher
    source; that is refused with 422 rather than silently accepted. Requires the
    operator role.
    """
    source = await service.create_source(session, principal, payload, request=request)
    read = (await service.hydrate(session, [source]))[0]
    if not payload.start_sync:
        return KnowledgeActionResponse(
            source=read,
            message=f"{read.name} added; it is paused until you sync it.",
            sync=service.sync_status_of(source),
        )

    response, job_id = await service.start_sync(
        session, principal, source.id, request=request
    )
    # Commit BEFORE the job is scheduled. The session dependency commits after
    # the response has gone out, which is after BackgroundTasks have run, so
    # the job's own session would otherwise look for a row no other connection
    # can see yet, find nothing and give up -- leaving the new source Syncing
    # at 0% with nothing working on it.
    await commit_then(
        session, background, service.run_sync, source.id, principal.workspace_id, job_id
    )
    return KnowledgeActionResponse(
        source=response.source,
        message=f"{read.name} added; initial sync started.",
        sync=response.sync,
    )


# ---------------------------------------------------------------------------
# Single source
# ---------------------------------------------------------------------------


@router.get(
    "/{source_id}", response_model=KnowledgeSourceRead, summary="Get a knowledge source"
)
async def get_source(
    principal: CurrentPrincipal, session: Db, source_id: str
) -> KnowledgeSourceRead:
    """One source. A row in another workspace answers 404, not 403."""
    return await service.read_source(session, principal, source_id)


@router.patch(
    "/{source_id}",
    response_model=KnowledgeSourceRead,
    summary="Update a knowledge source",
)
async def update_source(
    principal: CurrentPrincipal,
    session: Db,
    source_id: str,
    payload: KnowledgeSourceUpdate,
    request: Request,
) -> KnowledgeSourceRead:
    """Partially update a source.

    `status` accepts Active or Paused only — Syncing and Failed belong to the
    sync job. Send `expected_updated_at` to make the write conditional.
    Requires the operator role.
    """
    source = await service.update_source(
        session, principal, source_id, payload, request=request
    )
    return (await service.hydrate(session, [source]))[0]


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a knowledge source",
)
async def delete_source(
    principal: CurrentPrincipal, session: Db, source_id: str, request: Request
) -> None:
    """Remove a source and its retrieval coverage.

    Refused with 412 while a sync is in flight. The audit trail survives the
    deletion. Requires the admin role.
    """
    await service.delete_source(session, principal, source_id, request=request)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@router.post(
    "/{source_id}/sync",
    response_model=KnowledgeActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Sync a knowledge source",
)
async def sync_source(
    principal: CurrentPrincipal,
    session: Db,
    source_id: str,
    request: Request,
    background: BackgroundTasks,
) -> KnowledgeActionResponse:
    """Start a sync and answer immediately with the job's opening state.

    The job runs in the background and persists its progress after each stage;
    poll `GET /knowledge/{id}/sync-status` for the percentage. Starting a sync
    on a source that is already syncing is refused with 409. Requires the
    operator role.
    """
    response, job_id = await service.start_sync(
        session, principal, source_id, request=request
    )
    # Commit first, for the reason given in create_source above: a job that
    # starts ahead of this commit rewrites the sync block from the snapshot
    # before it, dropping who started the sync and when.
    await commit_then(
        session, background, service.run_sync, source_id, principal.workspace_id, job_id
    )
    return response


@router.get(
    "/{source_id}/sync-status",
    response_model=KnowledgeSyncStatus,
    summary="Sync progress",
)
async def get_sync_status(
    principal: CurrentPrincipal, session: Db, source_id: str
) -> KnowledgeSyncStatus:
    """Genuine progress for the console's percentage bar.

    The number comes from `sync_progress` on the row, which the job writes and
    commits as each stage finishes — it is a report, not an animation.

    A row still Syncing well past the longest a job can live has lost its job
    to a restart. This poll closes it out — `running` false, `state` Failed,
    `error` saying it was interrupted — instead of reporting the same
    percentage for ever; Sync Now, Pause and Delete then work again.
    """
    return await service.sync_status(session, principal, source_id)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@router.get(
    "/{source_id}/documents",
    response_model=Page[KnowledgeDocumentRead],
    summary="List documents seen in retrieval",
)
async def list_documents(
    principal: CurrentPrincipal,
    session: Db,
    source_id: str,
    params: Params,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> Page[KnowledgeDocumentRead]:
    """The documents agents actually retrieved from this source in the window.

    The control plane does not index the corpus, so this lists what retrieval
    telemetry observed — with the chunks seen per document, the last retrieval
    time and which agents read it — rather than a directory listing. A source
    nothing has retrieved from answers with an empty page.
    """
    rows, total = await service.list_documents(
        session, principal, source_id, params, window_days=window_days
    )
    return Page[KnowledgeDocumentRead].build(rows, total, params.page, params.page_size)


@router.get(
    "/{source_id}/grounding",
    response_model=GroundingBreakdown,
    summary="Grounding quality breakdown",
)
async def get_grounding(
    principal: CurrentPrincipal,
    session: Db,
    source_id: str,
    window_days: Window = service.DEFAULT_WINDOW_DAYS,
) -> GroundingBreakdown:
    """The four bars behind the inspector's Grounding gauge.

    Context relevance, answer relevance, citation quality and completeness are
    averaged from the feedback scores recorded against this index's retrieval
    spans. A dimension nothing has scored reports null with a sample size of
    zero rather than a made-up figure.
    """
    return await service.grounding_breakdown(
        session, principal, source_id, window_days=window_days
    )
