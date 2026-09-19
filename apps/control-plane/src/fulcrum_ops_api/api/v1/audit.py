"""Audit trail routes — read, verify, export. Nothing here writes a row.

The trail is append-only by contract: rows are written exclusively by
``services.audit.record`` inside the transaction of the change they describe.
This router therefore exposes no create, update or delete, and no route that
could reach one. That absence is the feature — an audit log a caller can edit is
not evidence.

Each row's checksum covers its own canonical form plus the previous row's, so
``GET /audit/verify`` can replay the chain and name the first row that does not
reconcile.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ...schemas.approvals import AuditChainStatus, AuditEventRead
from ...services import approvals as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/audit", tags=["Audit"])

#: Column order of the audit CSV, matching the on-screen table.
EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("occurred_at", "Time"),
    ("actor", "Actor"),
    ("action", "Action"),
    ("entity_type", "Entity Type"),
    ("entity_label", "Resource"),
    ("entity_id", "Entity ID"),
    ("detail", "Detail"),
    ("prev_value", "Previous"),
    ("new_value", "New"),
    ("source_screen", "Source Screen"),
    ("ip_address", "IP"),
    ("checksum", "Checksum"),
]

ListQuery = Annotated[ListParams, Depends(list_params)]


def _filters(
    actor: str | None,
    entity_type: str | None,
    entity_id: str | None,
    action: str | None,
    source_screen: str | None,
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> service.AuditFilters:
    return service.AuditFilters(
        actor=actor,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        source_screen=source_screen,
        since=since,
        until=until,
    )


@router.get(
    "",
    response_model=Page[AuditEventRead],
    summary="List audit events",
)
async def list_audit_events(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    actor: Annotated[str | None, Query(description="Exact actor, e.g. an email")] = None,
    entity_type: Annotated[str | None, Query(description='e.g. "approval_request"')] = None,
    entity_id: Annotated[str | None, Query(description="History for one record")] = None,
    action: Annotated[str | None, Query(description='e.g. "Request approved"')] = None,
    source_screen: Annotated[str | None, Query(description="Screen that raised it")] = None,
    since: Annotated[dt.datetime | None, Query(description="Occurred on or after")] = None,
    until: Annotated[dt.datetime | None, Query(description="Occurred on or before")] = None,
) -> Page[AuditEventRead]:
    """The workspace's audit trail, newest first.

    Free-text search covers actor, action, resource label, entity id and detail.
    Sort with `sort=` on any of: occurred_at, actor, action, entity_type,
    source_screen.
    """
    items, total = await service.list_audit_events(
        session,
        principal,
        params=params,
        filters=_filters(actor, entity_type, entity_id, action, source_screen, since, until),
    )
    return Page.build(items, total, params.page, params.page_size)


@router.get(
    "/verify",
    response_model=AuditChainStatus,
    # ``forks`` is reported only when the replay found any, so a trail without
    # them answers exactly as it always has.
    response_model_exclude_unset=True,
    summary="Verify the audit hash chain",
)
async def verify_chain(principal: CurrentPrincipal, session: Db) -> AuditChainStatus:
    """Replay every checksum for this workspace and report the first break.

    Returns how many rows reconciled; when the chain is broken it also names the
    event where the recomputed checksum stopped matching, which is where an
    investigation starts. `forks` counts rows that reconcile but chain from a row
    other than their immediate predecessor — the mark two concurrent writers left
    before writers were serialised; it is history, not tampering.
    """
    return await service.verify_audit_chain(session, principal)


@router.get(
    "/export",
    summary="Export the audit trail as CSV",
    response_class=StreamingResponse,
)
async def export_audit_events(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    actor: Annotated[str | None, Query(description="Exact actor, e.g. an email")] = None,
    entity_type: Annotated[str | None, Query(description='e.g. "approval_request"')] = None,
    entity_id: Annotated[str | None, Query(description="History for one record")] = None,
    action: Annotated[str | None, Query(description='e.g. "Request approved"')] = None,
    source_screen: Annotated[str | None, Query(description="Screen that raised it")] = None,
    since: Annotated[dt.datetime | None, Query(description="Occurred on or after")] = None,
    until: Annotated[dt.datetime | None, Query(description="Occurred on or before")] = None,
) -> StreamingResponse:
    """Stream the filtered trail as CSV, checksums included so an external
    reviewer can re-verify the chain outside this system."""
    rows = await service.export_audit_events(
        session,
        principal,
        params=params,
        filters=_filters(actor, entity_type, entity_id, action, source_screen, since, until),
    )
    payload: list[dict[str, Any]] = [row.model_dump(mode="json") for row in rows]
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return StreamingResponse(
        iter([to_csv(payload, EXPORT_COLUMNS)]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-trail-{stamp}.csv"'},
    )
