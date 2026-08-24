"""Guardrails routes.

Thirteen endpoints back one screen: the five KPI cards, the eight-column table
and its two filters, the CSV export, the New Guardrail modal, the per-row Test,
Tune, Enable and Disable actions, and the recent-detections feed the inspector
shows.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
audit writes and every call to the engine's content checker live in
``services.guardrails``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.quality import (
    GuardrailAction,
    GuardrailScope,
    GuardrailStatus,
    GuardrailType,
)
from ...schemas.guardrails import (
    EVENT_EXPORT_COLUMNS,
    EXPORT_COLUMNS,
    GuardrailCreate,
    GuardrailEventRead,
    GuardrailRead,
    GuardrailsSummary,
    GuardrailTestRequest,
    GuardrailTestResult,
    GuardrailThresholdUpdate,
    GuardrailUpdate,
)
from ...services import guardrails as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/guardrails", tags=["Guardrails"])


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{guardrail_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=GuardrailsSummary, summary="Guardrail KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> GuardrailsSummary:
    """The five KPI cards: active guardrails, triggers, blocks, PII items masked
    and the average added latency.

    Trigger and block counts are ``GROUP BY`` aggregates over the event table
    for the window, so they fall as activity ages out. The PII card counts
    masked *entities* rather than masked calls, because one call can carry
    several.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/events", response_model=Page[GuardrailEventRead], summary="List guardrail events")
async def list_events(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    guardrail_id: Annotated[str | None, Query(description="Restrict to one guardrail")] = None,
    agent_id: Annotated[str | None, Query(description="Restrict to one agent")] = None,
    guardrail_type: Annotated[
        GuardrailType | None, Query(alias="type", description="PII, Toxicity, ...")
    ] = None,
    action: Annotated[GuardrailAction | None, Query()] = None,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> Page[GuardrailEventRead]:
    """Recent triggers, newest first.

    Rows carry the guardrail's name and type and the agent's name so the feed
    renders without a request per row. An event for a guardrail in another
    workspace cannot appear: the feed is scoped before it is filtered.
    """
    rows, total = await service.list_events(
        session,
        principal,
        params,
        guardrail_id=guardrail_id,
        agent_id=agent_id,
        guardrail_type=guardrail_type.value if guardrail_type else None,
        action=action.value if action else None,
        window_days=window_days,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.get("/events/export", summary="Export guardrail events as CSV")
async def export_events(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    guardrail_id: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    guardrail_type: Annotated[GuardrailType | None, Query(alias="type")] = None,
    action: Annotated[GuardrailAction | None, Query()] = None,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> StreamingResponse:
    """The detections feed as CSV, honouring the same filters as the feed."""
    export_params = params.model_copy(update={"page": 1, "page_size": 200})
    rows, _ = await service.list_events(
        session,
        principal,
        export_params,
        guardrail_id=guardrail_id,
        agent_id=agent_id,
        guardrail_type=guardrail_type.value if guardrail_type else None,
        action=action.value if action else None,
        window_days=window_days,
    )
    payload: list[dict[str, Any]] = [
        {
            "occurred_at": row.occurred_at.isoformat(),
            "guardrail_name": row.guardrail_name,
            "guardrail_type": row.guardrail_type.value if row.guardrail_type else None,
            "agent_name": row.agent_name,
            "action_taken": row.action_taken.value,
            "score": row.score,
            "match_count": row.match_count,
            "trace_id": row.trace_id,
            "sample": row.sample,
        }
        for row in rows
    ]
    return StreamingResponse(
        iter([to_csv(payload, EVENT_EXPORT_COLUMNS)]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="guardrail-events-{_stamp()}.csv"'
        },
    )


@router.get("/export", summary="Export guardrails as CSV")
async def export_guardrails(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    guardrail_status: Annotated[
        GuardrailStatus | None, Query(alias="status", description="Active, Disabled or Tuning")
    ] = None,
    guardrail_type: Annotated[GuardrailType | None, Query(alias="type")] = None,
    action: Annotated[GuardrailAction | None, Query()] = None,
    scope: Annotated[GuardrailScope | None, Query()] = None,
    owner_user_id: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The current view as CSV, with the same search and filters as the table."""
    rows = await service.export_guardrails(
        session,
        principal,
        params,
        status=guardrail_status.value if guardrail_status else None,
        guardrail_type=guardrail_type.value if guardrail_type else None,
        action=action.value if action else None,
        scope=scope.value if scope else None,
        owner_user_id=owner_user_id,
    )
    records = await service.read_many(session, principal, rows)
    payload: list[dict[str, Any]] = [
        {
            "name": record.name,
            "guardrail_type": record.guardrail_type.value,
            "coverage": record.coverage,
            "status": record.status.value,
            "action": record.action.value,
            "threshold": record.threshold,
            "triggers_30d": record.triggers_30d,
            "blocked_30d": record.blocked_30d,
            "masked_30d": record.masked_30d,
            "effectiveness": record.effectiveness,
            "added_latency_ms": record.added_latency_ms,
            "last_triggered_at": (
                record.last_triggered_at.isoformat() if record.last_triggered_at else None
            ),
        }
        for record in records
    ]
    return StreamingResponse(
        iter([to_csv(payload, EXPORT_COLUMNS)]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="guardrails-{_stamp()}.csv"'},
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[GuardrailRead], summary="List guardrails")
async def list_guardrails(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    guardrail_status: Annotated[
        GuardrailStatus | None, Query(alias="status", description="Active, Disabled or Tuning")
    ] = None,
    guardrail_type: Annotated[
        GuardrailType | None, Query(alias="type", description="Prompt Injection, PII, ...")
    ] = None,
    action: Annotated[
        GuardrailAction | None, Query(description="Block, Mask, Warn or Log")
    ] = None,
    scope: Annotated[GuardrailScope | None, Query()] = None,
    owner_user_id: Annotated[str | None, Query()] = None,
) -> Page[GuardrailRead]:
    """One page of the workspace's guardrails, ordered by name by default.

    The activity columns — triggers, blocked, masked and last triggered — are
    counted over the last 30 days from the event table in a single statement
    for the whole page, so the numbers on screen are what actually happened
    rather than a counter somebody may have reset.
    """
    rows, total = await service.list_guardrails(
        session,
        principal,
        params,
        status=guardrail_status.value if guardrail_status else None,
        guardrail_type=guardrail_type.value if guardrail_type else None,
        action=action.value if action else None,
        scope=scope.value if scope else None,
        owner_user_id=owner_user_id,
    )
    records = await service.read_many(session, principal, rows)
    return Page.build(records, total, params.page, params.page_size)


@router.post(
    "",
    response_model=GuardrailRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a guardrail",
)
async def create_guardrail(
    principal: CurrentPrincipal,
    session: Db,
    payload: GuardrailCreate,
    request: Request,
) -> GuardrailRead:
    """Register a guardrail and mirror it into the engine's rule store.

    A guardrail scoped to an agent or an environment must name one that exists
    in this workspace. Requires the admin role.
    """
    guardrail = await service.create_guardrail(session, principal, payload, request=request)
    records = await service.read_many(session, principal, [guardrail])
    return records[0]


# ---------------------------------------------------------------------------
# Single guardrail
# ---------------------------------------------------------------------------


@router.get("/{guardrail_id}", response_model=GuardrailRead, summary="Get a guardrail")
async def get_guardrail(
    principal: CurrentPrincipal, session: Db, guardrail_id: str
) -> GuardrailRead:
    """One guardrail. A guardrail in another workspace answers 404, not 403."""
    guardrail = await service.get_guardrail(session, principal, guardrail_id)
    records = await service.read_many(session, principal, [guardrail])
    return records[0]


@router.patch("/{guardrail_id}", response_model=GuardrailRead, summary="Update a guardrail")
async def update_guardrail(
    principal: CurrentPrincipal,
    session: Db,
    guardrail_id: str,
    payload: GuardrailUpdate,
    request: Request,
) -> GuardrailRead:
    """Partially update a guardrail, then re-publish it to the engine.

    Send `expected_updated_at` to make the write conditional: if the row moved
    since you read it the request is refused with 409. Requires the admin role.
    """
    guardrail = await service.update_guardrail(
        session, principal, guardrail_id, payload, request=request
    )
    records = await service.read_many(session, principal, [guardrail])
    return records[0]


@router.delete(
    "/{guardrail_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a guardrail"
)
async def delete_guardrail(
    principal: CurrentPrincipal, session: Db, guardrail_id: str, request: Request
) -> None:
    """Remove a guardrail here and in the engine.

    Its events go with it; the audit trail survives. Requires the admin role.
    """
    await service.delete_guardrail(session, principal, guardrail_id, request=request)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@router.post(
    "/{guardrail_id}/test", response_model=GuardrailTestResult, summary="Test a guardrail"
)
async def test_guardrail(
    principal: CurrentPrincipal,
    session: Db,
    guardrail_id: str,
    payload: GuardrailTestRequest,
    request: Request,
) -> GuardrailTestResult:
    """Run the guardrail against a sample and report what the checker found.

    The check is real: the verdict, the matched spans and the added latency all
    come from one measured call to the engine's content checker. The result is
    audited but deliberately writes no event, so a rehearsal never moves the
    30-day counters. Requires the operator role.
    """
    return await service.test_guardrail(
        session, principal, guardrail_id, payload.input, request=request
    )


@router.patch(
    "/{guardrail_id}/threshold", response_model=GuardrailRead, summary="Tune a guardrail"
)
async def set_threshold(
    principal: CurrentPrincipal,
    session: Db,
    guardrail_id: str,
    payload: GuardrailThresholdUpdate,
    request: Request,
) -> GuardrailRead:
    """Move the confidence threshold, and the action with it if asked.

    The previous threshold is recorded in the audit detail so a tuning history
    can be reconstructed from the trail. Requires the admin role.
    """
    guardrail = await service.set_threshold(
        session, principal, guardrail_id, payload, request=request
    )
    records = await service.read_many(session, principal, [guardrail])
    return records[0]


@router.post("/{guardrail_id}/enable", response_model=ActionResult, summary="Enable a guardrail")
async def enable_guardrail(
    principal: CurrentPrincipal, session: Db, guardrail_id: str, request: Request
) -> ActionResult:
    """Put the guardrail back into enforcement, here and in the engine.

    Refused with 412 when it is already active. Requires the operator role.
    """
    guardrail = await service.set_status(
        session, principal, guardrail_id, GuardrailStatus.ACTIVE, request=request
    )
    return ActionResult(
        message=f"{guardrail.name} is active.",
        entity_id=guardrail.id,
        data={"status": guardrail.status},
    )


@router.post(
    "/{guardrail_id}/disable", response_model=ActionResult, summary="Disable a guardrail"
)
async def disable_guardrail(
    principal: CurrentPrincipal, session: Db, guardrail_id: str, request: Request
) -> ActionResult:
    """Stop enforcing the guardrail, here and in the engine.

    Its history is kept, so the 30-day counters still show what it caught while
    it was on. Requires the operator role.
    """
    guardrail = await service.set_status(
        session, principal, guardrail_id, GuardrailStatus.DISABLED, request=request
    )
    return ActionResult(
        message=f"{guardrail.name} is disabled.",
        entity_id=guardrail.id,
        data={"status": guardrail.status},
    )
