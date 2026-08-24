"""Connector & MCP Governance endpoints.

Everything an agent may call - APIs, tools, data sources, MCP servers - is
registered and governed through this router. Handlers stay thin: they read the
console's filter parameters, hand them to the service and shape what comes
back. Authorisation, workspace scoping and the audit trail live in the service.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ...schemas.connectors import (
    ConnectorBlockRequest,
    ConnectorCreate,
    ConnectorGrantRequest,
    ConnectorRead,
    ConnectorSummary,
    ConnectorTestResult,
    ConnectorUpdate,
)
from ...services import connectors as connectors_service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/connectors", tags=["Connectors"])

#: Page, page_size, q and sort - identical on every collection in this API.
ListArgs = Annotated[ListParams, Depends(list_params)]

TypeFilter = Annotated[
    str | None,
    Query(
        alias="type",
        description="Connector, Tool, MCP Server, Data Source, API, Database, "
        "Vector Store or Custom Tool.",
    ),
]
StatusFilter = Annotated[
    str | None,
    Query(description="Active, Warning, Blocked, Inactive or Deprecated."),
]
RiskFilter = Annotated[str | None, Query(description="Low, Medium or High.")]
AccessFilter = Annotated[
    str | None,
    Query(
        description="Internal or External (the tenancy boundary), or a grant "
        "level: Read, Read-Write, Admin.",
    ),
]


@router.get("", response_model=Page[ConnectorRead], summary="List connectors and MCP servers")
async def list_connectors(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    connector_type: TypeFilter = None,
    status: StatusFilter = None,
    risk: RiskFilter = None,
    access: AccessFilter = None,
) -> Page[ConnectorRead]:
    """One page of governed connectors, each with the agents that hold a grant on it.

    Supports free-text search over name, provider, type and endpoint, the four
    dropdown filters the governance screen offers, and sorting by any column
    including "Used By Agents".
    """
    items, total = await connectors_service.list_connectors(
        session,
        principal,
        params,
        connector_type=connector_type,
        status=status,
        risk=risk,
        access=access,
    )
    return Page.build(items, total, params.page, params.page_size)


@router.get("/summary", response_model=ConnectorSummary, summary="Connector KPI summary")
async def connector_summary(principal: CurrentPrincipal, session: Db) -> ConnectorSummary:
    """The five KPI cards above the table, plus the counts its tab bar shows.

    Every number is a filtered SQL aggregate over the workspace, so the cards
    describe the whole estate rather than the page currently on screen.
    """
    return await connectors_service.summarize(session, principal)


@router.get("/export", summary="Export connectors as CSV")
async def export_connectors(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    connector_type: TypeFilter = None,
    status: StatusFilter = None,
    risk: RiskFilter = None,
    access: AccessFilter = None,
) -> StreamingResponse:
    """Download the filtered connector inventory, including block reasons and grants.

    Honours the same search, filters and sort as the list, so what downloads is
    what the operator is looking at.
    """
    rows = await connectors_service.export_rows(
        session,
        principal,
        params,
        connector_type=connector_type,
        status=status,
        risk=risk,
        access=access,
    )
    body = to_csv(rows, connectors_service.EXPORT_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    filename = f"connectors-{stamp}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "", response_model=ConnectorRead, status_code=201, summary="Register a connector"
)
async def create_connector(
    principal: CurrentPrincipal,
    session: Db,
    payload: ConnectorCreate,
    request: Request,
) -> ConnectorRead:
    """Register a tool, API, data source or MCP server.

    The connector starts with no agent grants, so registering it does not by
    itself let anything call it.
    """
    return await connectors_service.create_connector(
        session, principal, payload, request=request
    )


@router.get("/{connector_id}", response_model=ConnectorRead, summary="Get one connector")
async def get_connector(
    principal: CurrentPrincipal, session: Db, connector_id: str
) -> ConnectorRead:
    """Full governance record for one connector, with the agents granted it."""
    return await connectors_service.get_connector(session, principal, connector_id)


@router.patch("/{connector_id}", response_model=ConnectorRead, summary="Update a connector")
async def update_connector(
    principal: CurrentPrincipal,
    session: Db,
    connector_id: str,
    payload: ConnectorUpdate,
    request: Request,
) -> ConnectorRead:
    """Edit the fields supplied in the body; anything omitted is left alone.

    Status cannot be moved into or out of Blocked here - that goes through the
    block and unblock endpoints, which record a reason.
    """
    return await connectors_service.update_connector(
        session, principal, connector_id, payload, request=request
    )


@router.delete("/{connector_id}", response_model=ActionResult, summary="Delete a connector")
async def delete_connector(
    principal: CurrentPrincipal, session: Db, connector_id: str, request: Request
) -> ActionResult:
    """Remove a connector that no agent holds a grant on.

    A connector still in use is refused; block it instead, which is reversible
    and keeps the history.
    """
    name = await connectors_service.delete_connector(
        session, principal, connector_id, request=request
    )
    return ActionResult(message=f"{name} deleted.", entity_id=connector_id)


@router.post("/{connector_id}/block", response_model=ActionResult, summary="Block a connector")
async def block_connector(
    principal: CurrentPrincipal,
    session: Db,
    connector_id: str,
    payload: ConnectorBlockRequest,
    request: Request,
) -> ActionResult:
    """Block a connector for every agent in the workspace.

    The reason is required and is stored on the row alongside who blocked it and
    when, and written to the audit trail. Agents calling it then fail closed.
    """
    connector = await connectors_service.block_connector(
        session, principal, connector_id, payload, request=request
    )
    return ActionResult(
        message=f"{connector.name} is now blocked for all agents.",
        entity_id=connector.id,
        data=connector.model_dump(mode="json"),
    )


@router.post(
    "/{connector_id}/unblock", response_model=ActionResult, summary="Unblock a connector"
)
async def unblock_connector(
    principal: CurrentPrincipal, session: Db, connector_id: str, request: Request
) -> ActionResult:
    """Lift a block and return the connector to Active.

    The original reason stays in the audit trail; the row itself is cleared.
    """
    connector = await connectors_service.unblock_connector(
        session, principal, connector_id, request=request
    )
    return ActionResult(
        message=f"{connector.name} restored.",
        entity_id=connector.id,
        data=connector.model_dump(mode="json"),
    )


@router.post(
    "/{connector_id}/grants",
    response_model=ActionResult,
    status_code=201,
    summary="Grant a connector to an agent",
)
async def grant_connector(
    principal: CurrentPrincipal,
    session: Db,
    connector_id: str,
    payload: ConnectorGrantRequest,
    request: Request,
) -> ActionResult:
    """Grant this connector to one agent.

    The grant is what connector-scoped policies and blocks act on: a policy
    with Connector scope reaches the agent through it, and blocking the
    connector then refuses the agent's telemetry at ingest until the block is
    lifted or the grant revoked.
    """
    connector = await connectors_service.grant_connector(
        session, principal, connector_id, payload, request=request
    )
    return ActionResult(
        message=f"{connector.name} granted.",
        entity_id=connector.id,
        data=connector.model_dump(mode="json"),
    )


@router.delete(
    "/{connector_id}/grants/{agent_id}",
    response_model=ActionResult,
    summary="Revoke a connector grant",
)
async def revoke_connector_grant(
    principal: CurrentPrincipal,
    session: Db,
    connector_id: str,
    agent_id: str,
    request: Request,
) -> ActionResult:
    """Take this connector away from one agent."""
    connector = await connectors_service.revoke_grant(
        session, principal, connector_id, agent_id, request=request
    )
    return ActionResult(
        message=f"{connector.name} grant revoked.",
        entity_id=connector.id,
        data=connector.model_dump(mode="json"),
    )


@router.post(
    "/{connector_id}/test",
    response_model=ConnectorTestResult,
    summary="Test a connector endpoint",
)
async def test_connector(
    principal: CurrentPrincipal, session: Db, connector_id: str, request: Request
) -> ConnectorTestResult:
    """Probe the registered endpoint and report reachability and latency.

    The probe is unauthenticated, so an endpoint that answers 401 or 403 counts
    as reachable. A blocked connector is reported as blocked without being
    called, and one with no endpoint registered cannot be tested at all.
    """
    return await connectors_service.test_connector(
        session, principal, connector_id, request=request
    )
