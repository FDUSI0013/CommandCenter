"""Alerts routes: the triage queue, its KPI row, and the rules behind it.

One screen, two collections. The table at the top is the alert queue with the
console's three dropdowns (Severity, Status, Source), its search box and its
Export button; the modal behind the "Alert Rules" button is a full CRUD editor
over the rules that govern those alerts, matched to them by source and severity.

The unread badge in the console's sidebar polls ``GET /alerts/summary``, so the
``open`` figure in that payload has to be exact rather than page-scoped — it is
a SQL aggregate over the whole workspace, computed in
:func:`fulcrum_ops_api.services.alerts.summary`.

Handlers here parse, delegate and shape. Workspace scoping, the triage state
machine, the timings MTTA and MTTR are measured from, and every audit row live
in ``services.alerts``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from ...models.operations import AlertSeverity, AlertStatus
from ...schemas.alerts import (
    AlertAcknowledgeAllRequest,
    AlertAcknowledgeRequest,
    AlertAssignRequest,
    AlertCreate,
    AlertMuteRequest,
    AlertRead,
    AlertResolveRequest,
    AlertRuleCreate,
    AlertRuleRead,
    AlertRuleUpdate,
    AlertsSummary,
    AlertUpdate,
)
from ...services import alerts as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/alerts", tags=["Alerts"])

ListQuery = Annotated[ListParams, Depends(list_params)]

SeverityFilter = Annotated[
    list[AlertSeverity] | None,
    Query(alias="severity", description="Repeatable: severity=Critical&severity=High"),
]
StatusFilter = Annotated[
    list[AlertStatus] | None,
    Query(alias="status", description="Repeatable: status=Open&status=Investigating"),
]
SourceFilter = Annotated[
    list[str] | None,
    Query(alias="source", description="Repeatable source screen, e.g. source=Connections"),
]
AssigneeFilter = Annotated[str | None, Query(description="Only alerts routed to this user")]
SinceFilter = Annotated[
    dt.datetime | None, Query(description="Only alerts raised on or after this instant")
]
OpenOnlyFilter = Annotated[
    bool | None, Query(description="Only alerts that are not resolved (muted included)")
]


def _filters(
    severity: list[AlertSeverity] | None,
    alert_status: list[AlertStatus] | None,
    source: list[str] | None,
    assigned_to_user_id: str | None,
    raised_since: dt.datetime | None,
    open_only: bool | None,
) -> dict[str, Any]:
    """Normalise the screen's dropdowns into the service's keyword arguments.

    The list and export endpoints must agree exactly — an export that does not
    match the grid the operator is looking at is worse than no export — so both
    build their filters here.
    """
    return {
        "severity": [item.value for item in severity] if severity else None,
        "status": [item.value for item in alert_status] if alert_status else None,
        "source": [item for item in source if item] if source else None,
        "assigned_to_user_id": assigned_to_user_id,
        "raised_since": raised_since,
        "open_only": bool(open_only),
    }


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


_Row = TypeVar("_Row")


async def _refreshed(session: Db, row: _Row) -> _Row:
    """Re-read the server-populated columns a write leaves unfetched.

    ``created_at`` and ``updated_at`` carry SQL defaults, so after the service's
    flush they are expired on the instance. Building a read model would then
    attempt IO from Pydantic's synchronous validation and fail; one refresh here
    means every mutation answers with the row as the database now holds it.
    """
    await session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Fixed paths first: they would otherwise be swallowed by /{alert_id}.
# --------------------------------------------------------------------------- #


@router.get("/summary", response_model=AlertsSummary, summary="Alert KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> AlertsSummary:
    """The five KPI cards — Open, Critical, Investigating, Acknowledged, MTTA.

    Also carries the muted, resolved-in-24h and total tallies, mean time to
    resolve, and the distinct source screens that populate the Source dropdown.
    The console's sidebar badge polls this endpoint, so ``open`` counts every
    open alert in the workspace, not just the page being shown.
    """
    return await service.summary(session, principal)


@router.get(
    "/export",
    summary="Export alerts as CSV",
    response_class=StreamingResponse,
)
async def export_alerts(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    severity: SeverityFilter = None,
    alert_status: StatusFilter = None,
    source: SourceFilter = None,
    assigned_to_user_id: AssigneeFilter = None,
    raised_since: SinceFilter = None,
    open_only: OpenOnlyFilter = None,
) -> StreamingResponse:
    """Stream the filtered queue as CSV, in the table's current sort order.

    Search, dropdowns and sort are honoured, so the file matches the grid on
    screen. The row count is capped by the service so one click cannot pull an
    unbounded table.
    """
    rows = await service.export_rows(
        session,
        principal,
        params,
        **_filters(
            severity, alert_status, source, assigned_to_user_id, raised_since, open_only
        ),
    )
    return StreamingResponse(
        iter([to_csv(rows, service.EXPORT_COLUMNS)]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="alerts-{_stamp()}.csv"'},
    )


@router.post(
    "/acknowledge-all",
    response_model=ActionResult,
    summary="Acknowledge every unclaimed alert",
)
async def acknowledge_all(
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    payload: AlertAcknowledgeAllRequest | None = None,
) -> ActionResult:
    """The header's "Acknowledge All" button.

    Sweeps every Open and Investigating alert, optionally narrowed to a set of
    severities or sources. Alerts that were already acknowledged keep their
    original ``acknowledged_at`` so MTTA still measures the first human
    response. One audit row records the sweep. Requires the operator role.
    """
    count = await service.acknowledge_all(session, principal, payload, request=request)
    if not count:
        return ActionResult(
            message="No unacknowledged alerts matched.", data={"acknowledged": 0}
        )
    return ActionResult(
        message=f"{count} alert(s) acknowledged.", data={"acknowledged": count}
    )


# --------------------------------------------------------------------------- #
# Alert rules — declared before /{alert_id} so "rules" is never read as an id.
# --------------------------------------------------------------------------- #


@router.get("/rules", response_model=Page[AlertRuleRead], summary="List alert rules")
async def list_rules(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    source: SourceFilter = None,
    severity: SeverityFilter = None,
    enabled: Annotated[bool | None, Query(description="Only enabled or only disabled")] = None,
) -> Page[AlertRuleRead]:
    """One page of the rules editor, ordered by name by default.

    Free-text search covers the rule name, its description and the source screen
    it watches.
    """
    rows, total = await service.list_rules(
        session,
        principal,
        params,
        source=[item for item in source if item] if source else None,
        severity=[item.value for item in severity] if severity else None,
        enabled=enabled,
    )
    return Page.build(
        [AlertRuleRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.post(
    "/rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert rule",
)
async def create_rule(
    principal: CurrentPrincipal,
    session: Db,
    payload: AlertRuleCreate,
    request: Request,
) -> AlertRuleRead:
    """Say what the workspace wants done with one kind of alert.

    A rule governs the alerts raised from its ``source`` screen at its
    ``severity``; those two fields are the whole match. Each such alert names
    the rule in its payload (``alert_rule``, ``alert_rule_id``). ``condition``
    is stored exactly as written and is documentation: the screens decide when
    something is wrong. ``notify_channels`` and ``throttle_minutes`` are kept
    for delivery, which this API does not perform. Rule names are unique within
    a workspace, so a duplicate answers 409. Requires the admin role.
    """
    rule = await service.create_rule(session, principal, payload, request=request)
    return AlertRuleRead.model_validate(await _refreshed(session, rule))


@router.get("/rules/{rule_id}", response_model=AlertRuleRead, summary="Get an alert rule")
async def get_rule(
    principal: CurrentPrincipal, session: Db, rule_id: str
) -> AlertRuleRead:
    """One rule. A rule in another workspace answers 404, not 403."""
    rule = await service.get_rule(session, principal, rule_id)
    return AlertRuleRead.model_validate(rule)


@router.patch(
    "/rules/{rule_id}", response_model=AlertRuleRead, summary="Update an alert rule"
)
async def update_rule(
    principal: CurrentPrincipal,
    session: Db,
    rule_id: str,
    payload: AlertRuleUpdate,
    request: Request,
) -> AlertRuleRead:
    """Partially update a rule; omitted fields are left alone.

    Disabling a rule with ``enabled: false`` silences its source at its severity
    without losing the definition: what the screen raises from then on is still
    recorded, as a Muted alert, and is reopened when the rule is enabled again,
    deleted, or moved elsewhere. Alerts posted by hand are never silenced.
    Requires the admin role.
    """
    rule = await service.update_rule(session, principal, rule_id, payload, request=request)
    return AlertRuleRead.model_validate(await _refreshed(session, rule))


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an alert rule",
)
async def delete_rule(
    principal: CurrentPrincipal, session: Db, rule_id: str, request: Request
) -> None:
    """Remove a rule. Alerts it governed are evidence and are kept.

    Alerts a disabled rule had silenced are reopened, since nothing silences
    them any more. Requires the admin role.
    """
    await service.delete_rule(session, principal, rule_id, request=request)


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


@router.get("", response_model=Page[AlertRead], summary="List alerts")
async def list_alerts(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    severity: SeverityFilter = None,
    alert_status: StatusFilter = None,
    source: SourceFilter = None,
    assigned_to_user_id: AssigneeFilter = None,
    raised_since: SinceFilter = None,
    open_only: OpenOnlyFilter = None,
) -> Page[AlertRead]:
    """One page of the triage queue, newest first.

    Free-text search covers the title, body, source and alert reference. `sort`
    accepts any column key the table shows; sorting by severity orders by
    meaning (Critical first, Info last) rather than alphabetically.
    """
    rows, total = await service.list_alerts(
        session,
        principal,
        params,
        **_filters(
            severity, alert_status, source, assigned_to_user_id, raised_since, open_only
        ),
    )
    return Page.build(
        [AlertRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.post(
    "",
    response_model=AlertRead,
    status_code=status.HTTP_201_CREATED,
    summary="Raise an alert",
)
async def create_alert(
    principal: CurrentPrincipal,
    session: Db,
    payload: AlertCreate,
    request: Request,
    response: Response,
) -> AlertRead:
    """Raise an alert by hand, or forward one from an external monitor.

    Supplying ``dedupe_key`` opts into flood control: a recurring condition
    bumps the occurrence count on the alert that is already live instead of
    creating another row, and the response is 200 rather than 201 to say so.
    Requires the operator role.
    """
    alert, created = await service.create_alert(session, principal, payload, request=request)
    if not created:
        response.status_code = status.HTTP_200_OK
    return AlertRead.model_validate(await _refreshed(session, alert))


@router.get("/{alert_id}", response_model=AlertRead, summary="Get an alert")
async def get_alert(principal: CurrentPrincipal, session: Db, alert_id: str) -> AlertRead:
    """One alert, with everything the inspector panel renders.

    An alert belonging to another workspace answers 404, not 403.
    """
    alert = await service.get_alert(session, principal, alert_id)
    return AlertRead.model_validate(alert)


@router.patch("/{alert_id}", response_model=AlertRead, summary="Update an alert")
async def update_alert(
    principal: CurrentPrincipal,
    session: Db,
    alert_id: str,
    payload: AlertUpdate,
    request: Request,
) -> AlertRead:
    """Edit an alert's descriptive fields, or move it to/from Investigating.

    Acknowledging, resolving and muting also stamp the timings the KPI row is
    computed from, so they go through their own endpoints and are refused here.
    Requires the operator role.
    """
    alert = await service.update_alert(session, principal, alert_id, payload, request=request)
    return AlertRead.model_validate(await _refreshed(session, alert))


@router.delete(
    "/{alert_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an alert"
)
async def delete_alert(
    principal: CurrentPrincipal, session: Db, alert_id: str, request: Request
) -> None:
    """Remove an alert. Reserved for admins: the row is incident evidence.

    The audit trail survives the deletion.
    """
    await service.delete_alert(session, principal, alert_id, request=request)


# --------------------------------------------------------------------------- #
# Triage verbs
# --------------------------------------------------------------------------- #


@router.post(
    "/{alert_id}/acknowledge",
    response_model=ActionResult,
    summary="Acknowledge an alert",
)
async def acknowledge_alert(
    principal: CurrentPrincipal,
    session: Db,
    alert_id: str,
    request: Request,
    payload: AlertAcknowledgeRequest | None = None,
) -> ActionResult:
    """Take ownership of an alert.

    Idempotent — acknowledging twice is a double-click, not an error, and the
    second call leaves ``acknowledged_at`` alone so MTTA keeps measuring the
    first human response. Refused with 409 once the alert is resolved. Requires
    the operator role.
    """
    alert, changed = await service.acknowledge(
        session, principal, alert_id, payload, request=request
    )
    return ActionResult(
        message=(
            f"{alert.alert_ref} acknowledged."
            if changed
            else f"{alert.alert_ref} was already acknowledged."
        ),
        entity_id=alert.id,
        data={
            "status": alert.status,
            "acknowledged_at": (
                alert.acknowledged_at.isoformat() if alert.acknowledged_at else None
            ),
            "changed": changed,
        },
    )


@router.post("/{alert_id}/resolve", response_model=ActionResult, summary="Resolve an alert")
async def resolve_alert(
    principal: CurrentPrincipal,
    session: Db,
    alert_id: str,
    request: Request,
    payload: AlertResolveRequest | None = None,
) -> ActionResult:
    """Close an alert and freeze its time-to-resolve.

    ``mttr_seconds`` is computed once, here, from the moment it was raised, so
    the KPI cannot drift if a timestamp is corrected later. Requires the
    operator role.
    """
    alert = await service.resolve(session, principal, alert_id, payload, request=request)
    return ActionResult(
        message=f"{alert.alert_ref} resolved.",
        entity_id=alert.id,
        data={
            "status": alert.status,
            "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
            "mttr_seconds": alert.mttr_seconds,
        },
    )


@router.post("/{alert_id}/assign", response_model=ActionResult, summary="Assign an alert")
async def assign_alert(
    principal: CurrentPrincipal,
    session: Db,
    alert_id: str,
    payload: AlertAssignRequest,
    request: Request,
) -> ActionResult:
    """Route an alert to a named member of this workspace.

    The assignee must already be a member; assigning outside the workspace is
    refused with 422. A resolved alert cannot be reassigned. Requires the
    operator role.
    """
    alert = await service.assign(session, principal, alert_id, payload, request=request)
    return ActionResult(
        message=f"{alert.alert_ref} assigned.",
        entity_id=alert.id,
        data={
            "assigned_to_user_id": alert.assigned_to_user_id,
            "status": alert.status,
        },
    )


@router.post("/{alert_id}/mute", response_model=ActionResult, summary="Mute an alert")
async def mute_alert(
    principal: CurrentPrincipal,
    session: Db,
    alert_id: str,
    request: Request,
    payload: AlertMuteRequest | None = None,
) -> ActionResult:
    """Silence an alert for a window without closing it — the "Mute for 24h" action.

    The alert stays live for deduplication, so recurrences keep landing on this
    row rather than escaping as new alerts while the maintenance window runs.
    Requires the operator role.
    """
    alert, muted_until = await service.mute(
        session, principal, alert_id, payload, request=request
    )
    return ActionResult(
        message=f"{alert.alert_ref} muted until {muted_until.isoformat()}.",
        entity_id=alert.id,
        data={"status": alert.status, "muted_until": muted_until.isoformat()},
    )
