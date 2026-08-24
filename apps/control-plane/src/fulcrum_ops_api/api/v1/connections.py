"""Connection Center routes.

Nine endpoints back one screen: the tile grid and its filters, the activity
feed under it, the KPI cards and health donut beside it, the per-tile Test and
Sync buttons, and the two fan-out buttons in Integration Quick Actions.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
audit writes and the network probe all live in ``services.connections``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from ...models.registry import ConnectionStatus, HealthState
from ...schemas.connections import (
    ActivityStatus,
    ConnectionActivityRead,
    ConnectionCreate,
    ConnectionRead,
    ConnectionsSummary,
    ConnectionSyncOutcome,
    ConnectionTestOutcome,
    ConnectionTraffic,
    ConnectionUpdate,
)
from ...services import connections as service
from ..common import ActionResult, ListParams, Page, list_params
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/connections", tags=["Connections"])


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{connection_id}.
# ---------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=ConnectionsSummary,
    summary="Connection KPI summary",
)
async def get_summary(principal: CurrentPrincipal, session: Db) -> ConnectionsSummary:
    """Totals by status, the most recent sync, and the health donut breakdown.

    Every number is a SQL aggregate over the workspace's connections, so the
    cards stay correct on a workspace with thousands of integrations.
    """
    return await service.summarise(session, principal)


@router.get(
    "/activity",
    response_model=Page[ConnectionActivityRead],
    summary="List connection activity",
)
async def list_activity(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    connection_id: Annotated[
        str | None, Query(description="Restrict the feed to one connection")
    ] = None,
    activity_status: Annotated[
        ActivityStatus | None, Query(alias="status", description="Success, Warning or Error")
    ] = None,
    event: Annotated[
        str | None, Query(description="Exact event name, e.g. 'Sync Completed'")
    ] = None,
) -> Page[ConnectionActivityRead]:
    """The append-only sync/error feed, newest first.

    Rows carry the connection's name and logo key so the feed table renders
    without a second request per row.
    """
    rows, total = await service.list_activity(
        session,
        principal,
        params,
        connection_id=connection_id,
        status=activity_status,
        event=event,
    )
    return Page.build(
        [ConnectionActivityRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.post(
    "/test-all",
    response_model=ActionResult,
    summary="Test every connection",
)
async def test_all(principal: CurrentPrincipal, session: Db, request: Request) -> ActionResult:
    """Probe every enabled connection that has an endpoint, concurrently.

    Disabled tiles and tiles with no endpoint are reported as skipped rather
    than failed. Requires the operator role.
    """
    outcomes, summary = await service.test_all(session, principal, request=request)
    if not summary.requested:
        return ActionResult(
            message="No connections to test.", data={"summary": summary.model_dump(mode="json")}
        )
    return ActionResult(
        message=(
            f"{summary.requested} connection(s) tested - {summary.succeeded} healthy, "
            f"{summary.warned} warning, {summary.failed} unreachable, "
            f"{summary.skipped} skipped."
        ),
        data={
            "summary": summary.model_dump(mode="json"),
            "results": [outcome.model_dump(mode="json") for outcome in outcomes],
        },
    )


@router.post(
    "/sync-all",
    response_model=ActionResult,
    summary="Sync every connection",
)
async def sync_all(principal: CurrentPrincipal, session: Db, request: Request) -> ActionResult:
    """Record a sync against every connected, enabled tile.

    Disconnected tiles are skipped: there is nothing to synchronise through a
    link that is down. Requires the operator role.
    """
    outcomes, summary = await service.sync_all(session, principal, request=request)
    if not summary.requested:
        return ActionResult(
            message="No connections to sync.", data={"summary": summary.model_dump(mode="json")}
        )
    return ActionResult(
        message=(
            f"{summary.succeeded} connection(s) synchronised, {summary.skipped} skipped."
        ),
        data={
            "summary": summary.model_dump(mode="json"),
            "results": [outcome.model_dump(mode="json") for outcome in outcomes],
        },
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[ConnectionRead], summary="List connections")
async def list_connections(
    principal: CurrentPrincipal,
    session: Db,
    params: Annotated[ListParams, Depends(list_params)],
    connection_status: Annotated[
        ConnectionStatus | None,
        Query(alias="status", description="Connected, Warning or Disconnected"),
    ] = None,
    health: Annotated[
        HealthState | None, Query(description="Healthy, Warning or Unhealthy")
    ] = None,
    kind: Annotated[str | None, Query(description="Integration type, e.g. 'MCP Server'")] = None,
    enabled: Annotated[
        bool | None, Query(description="Only enabled or only disabled tiles")
    ] = None,
) -> Page[ConnectionRead]:
    """One page of the workspace's integrations, ordered by name by default.

    Free-text search covers the name, the integration type and the operator
    note; `sort` accepts any column key shown on the tile.
    """
    rows, total = await service.list_connections(
        session,
        principal,
        params,
        status=connection_status,
        health=health,
        kind=kind,
        enabled=enabled,
    )
    return Page.build(
        [ConnectionRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.post(
    "",
    response_model=ConnectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a connection",
)
async def create_connection(
    principal: CurrentPrincipal,
    session: Db,
    payload: ConnectionCreate,
    request: Request,
) -> ConnectionRead:
    """Register a new integration.

    The row is created Disconnected and Unhealthy on purpose — only a real
    probe may mark it healthy, so call `POST /connections/{id}/test` next.
    Requires the admin role.
    """
    connection = await service.create_connection(session, principal, payload, request=request)
    return ConnectionRead.model_validate(connection)


# ---------------------------------------------------------------------------
# Single connection
# ---------------------------------------------------------------------------


@router.get("/{connection_id}", response_model=ConnectionRead, summary="Get a connection")
async def get_connection(
    principal: CurrentPrincipal, session: Db, connection_id: str
) -> ConnectionRead:
    """One integration. A connection in another workspace answers 404, not 403."""
    connection = await service.get_connection(session, principal, connection_id)
    return ConnectionRead.model_validate(connection)


@router.patch(
    "/{connection_id}", response_model=ConnectionRead, summary="Update a connection"
)
async def update_connection(
    principal: CurrentPrincipal,
    session: Db,
    connection_id: str,
    payload: ConnectionUpdate,
    request: Request,
) -> ConnectionRead:
    """Partially update an integration.

    Send `expected_updated_at` to make the write conditional: if the row moved
    since you read it the request is refused with 409. Requires the admin role.
    """
    connection = await service.update_connection(
        session, principal, connection_id, payload, request=request
    )
    return ConnectionRead.model_validate(connection)


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a connection",
)
async def delete_connection(
    principal: CurrentPrincipal, session: Db, connection_id: str, request: Request
) -> None:
    """Remove an integration and its activity feed.

    Refused with 412 while agents are still linked to it. The audit trail
    survives the deletion. Requires the admin role.
    """
    await service.delete_connection(session, principal, connection_id, request=request)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@router.get(
    "/{connection_id}/traffic",
    response_model=ConnectionTraffic,
    summary="What has flowed through this connection",
)
async def connection_traffic(
    principal: CurrentPrincipal,
    session: Db,
    connection_id: str,
    window_days: Annotated[
        int, Query(ge=1, le=365, description="Days to look back")
    ] = 30,
) -> ConnectionTraffic:
    """The tool calls and data operations observed on this connection.

    Health says the endpoint answers; this says whether anything is using it.
    Rows are derived from the spans of agents whose platform matches this
    connection's kind — nothing is declared on the connection itself, so an
    empty result means no agent reported through it in the window rather than
    that the connection is misconfigured.
    """
    return await service.connection_traffic(
        session, principal, connection_id, window_days=window_days
    )


@router.post(
    "/{connection_id}/test",
    response_model=ActionResult,
    summary="Test a connection",
)
async def test_connection(
    principal: CurrentPrincipal, session: Db, connection_id: str, request: Request
) -> ActionResult:
    """Probe the configured endpoint and report the measured latency.

    The tile's status, health and latency are set from what the probe observed;
    a reachable endpoint answering 4xx/5xx degrades it to Warning, and only a
    transport failure marks it Disconnected. Requires the operator role.
    """
    outcome: ConnectionTestOutcome = await service.test_connection(
        session, principal, connection_id, request=request
    )
    latency = f"{outcome.latency_ms}ms" if outcome.latency_ms is not None else "no response"
    return ActionResult(
        ok=outcome.reachable,
        message=f"{outcome.name}: {outcome.status.value} ({latency}).",
        entity_id=outcome.connection_id,
        data=outcome.model_dump(mode="json"),
    )


@router.post(
    "/{connection_id}/sync",
    response_model=ActionResult,
    summary="Sync a connection",
)
async def sync_connection(
    principal: CurrentPrincipal, session: Db, connection_id: str, request: Request
) -> ActionResult:
    """Record a completed sync against one integration.

    Bumps today's sync counter, stamps `last_sync_at` and appends the feed row.
    Refused with 412 while the connection is disconnected or disabled.
    Requires the operator role.
    """
    outcome: ConnectionSyncOutcome = await service.sync_connection(
        session, principal, connection_id, request=request
    )
    return ActionResult(
        message=f"{outcome.name} synchronised.",
        entity_id=outcome.connection_id,
        data=outcome.model_dump(mode="json"),
    )
