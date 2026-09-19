"""Approval queue routes: the requests, their decisions, and the rules behind them.

Handlers here are deliberately thin. They parse query strings into the filter
carriers the service understands, call one service function, and shape the
result — the state machine, the role checks and the audit writes all live in
``services.approvals`` so an SDK, a scheduled job and this router cannot drift
apart.

Static paths are declared before ``/{request_id}`` so ``/approvals/summary`` and
``/approvals/rules`` are never mistaken for a request reference.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi import status as http_status
from fastapi.responses import StreamingResponse

from ...models.governance import ApprovalStatus, PolicyScope, PolicyStatus, RiskLevel
from ...schemas.approvals import (
    ApprovalCommentCreate,
    ApprovalCommentRead,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalEscalationRequest,
    ApprovalRequestCreate,
    ApprovalRequestRead,
    ApprovalRequestUpdate,
    ApprovalRisk,
    ApprovalRuleCreate,
    ApprovalRuleRead,
    ApprovalRuleUpdate,
    ApprovalsSummary,
    ApprovalTrigger,
)
from ...services import approvals as service
from ...services import deployments as deployments_service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/approvals", tags=["Approvals"])

#: Column order of the approval-queue CSV, matching the on-screen table.
EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("request_ref", "Request ID"),
    ("requested_at", "Requested"),
    ("agent_name", "Agent"),
    ("agent_platform", "Platform"),
    ("action", "Action"),
    ("action_detail", "Detail"),
    ("resource", "Resource"),
    ("risk", "Risk"),
    ("policy_name", "Policy"),
    ("requested_by_name", "Requested By"),
    ("sla_label", "SLA"),
    ("sla_due_at", "SLA Due"),
    ("sla_breached", "SLA Breached"),
    ("status", "Status"),
    ("decided_by_name", "Decided By"),
    ("decided_at", "Decided At"),
    ("decision_note", "Decision Note"),
    ("comment_count", "Comments"),
]

ListQuery = Annotated[ListParams, Depends(list_params)]


def _filters(
    status: str | None,
    agent_id: str | None,
    agent: str | None,
    risk: str | None,
    action: str | None,
    policy_id: str | None,
    requested_by_user_id: str | None,
    overdue: bool | None,
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> service.ApprovalFilters:
    return service.ApprovalFilters(
        status=status,
        agent_id=agent_id,
        agent=agent,
        risk=risk,
        action=action,
        policy_id=policy_id,
        requested_by_user_id=requested_by_user_id,
        overdue=overdue,
        since=since,
        until=until,
    )


def _csv_response(body: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# KPI cards and export — declared before /{request_id}
# ---------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=ApprovalsSummary,
    summary="Approval KPI summary",
)
async def get_summary(principal: CurrentPrincipal, session: Db) -> ApprovalsSummary:
    """Pending count, 30-day decision counts, mean time to approve and SLA attainment.

    Every figure is a SQL aggregate over the workspace's requests, so the cards
    stay honest on a queue of any size.
    """
    return await service.summary(session, principal)


@router.get(
    "/export",
    summary="Export approval requests as CSV",
    response_class=StreamingResponse,
)
async def export_requests(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    status: Annotated[ApprovalStatus | None, Query(description="Queue tab")] = None,
    agent_id: Annotated[str | None, Query(description="Filter by agent id")] = None,
    agent: Annotated[str | None, Query(description="Filter by agent name")] = None,
    risk: Annotated[ApprovalRisk | None, Query(description="Risk level")] = None,
    action: Annotated[str | None, Query(description="Requested action")] = None,
    policy_id: Annotated[str | None, Query(description="Policy that raised it")] = None,
    requested_by_user_id: Annotated[str | None, Query(description="Requester")] = None,
    overdue: Annotated[bool | None, Query(description="Only SLA-breached requests")] = None,
    since: Annotated[dt.datetime | None, Query(description="Requested on or after")] = None,
    until: Annotated[dt.datetime | None, Query(description="Requested on or before")] = None,
) -> StreamingResponse:
    """Stream the filtered queue as CSV, using the same filters as the table."""
    rows = await service.export_requests(
        session,
        principal,
        params=params,
        filters=_filters(
            status.value if status else None,
            agent_id,
            agent,
            risk.value if risk else None,
            action,
            policy_id,
            requested_by_user_id,
            overdue,
            since,
            until,
        ),
    )
    payload: list[dict[str, Any]] = [row.model_dump(mode="json") for row in rows]
    return _csv_response(
        to_csv(payload, EXPORT_COLUMNS), f"approval-requests-{_stamp()}.csv"
    )


# ---------------------------------------------------------------------------
# Approval rules
# ---------------------------------------------------------------------------


@router.get(
    "/rules",
    response_model=Page[ApprovalRuleRead],
    summary="List approval rules",
)
async def list_rules(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    status: Annotated[PolicyStatus | None, Query(description="Rule status")] = None,
    risk_level: Annotated[RiskLevel | None, Query(description="Risk level")] = None,
    scope: Annotated[PolicyScope | None, Query(description="What the rule attaches to")] = None,
    trigger: Annotated[ApprovalTrigger | None, Query(description="What makes it fire")] = None,
) -> Page[ApprovalRuleRead]:
    """The rules new requests are filed under: each sets the SLA and names the
    approvers for the requests that fall under its trigger. `requests_30d`,
    `approved_pct` and `last_triggered_at` say how often that has happened."""
    items, total = await service.list_rules(
        session,
        principal,
        params=params,
        filters=service.RuleFilters(
            status=status.value if status else None,
            risk_level=risk_level.value if risk_level else None,
            scope=scope.value if scope else None,
            trigger=trigger.value if trigger else None,
        ),
    )
    return Page.build(items, total, params.page, params.page_size)


@router.post(
    "/rules",
    response_model=ApprovalRuleRead,
    status_code=http_status.HTTP_201_CREATED,
    summary="Create an approval rule",
)
async def create_rule(
    principal: CurrentPrincipal,
    session: Db,
    data: ApprovalRuleCreate,
    request: Request,
) -> ApprovalRuleRead:
    """Add a rule. Requires the admin role; the name must be unique in the workspace."""
    return await service.create_rule(session, principal, data, request=request)


@router.get(
    "/rules/{rule_id}",
    response_model=ApprovalRuleRead,
    summary="Get an approval rule",
)
async def get_rule(principal: CurrentPrincipal, session: Db, rule_id: str) -> ApprovalRuleRead:
    """One rule, including its trigger, approver groups and SLA window."""
    return await service.get_rule(session, principal, rule_id)


@router.patch(
    "/rules/{rule_id}",
    response_model=ApprovalRuleRead,
    summary="Update an approval rule",
)
async def update_rule(
    principal: CurrentPrincipal,
    session: Db,
    rule_id: str,
    data: ApprovalRuleUpdate,
    request: Request,
) -> ApprovalRuleRead:
    """Edit a rule and patch-bump its version. Send `expected_updated_at` to guard
    against overwriting someone else's concurrent edit."""
    return await service.update_rule(session, principal, rule_id, data, request=request)


@router.delete(
    "/rules/{rule_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Delete an approval rule",
)
async def delete_rule(
    principal: CurrentPrincipal, session: Db, rule_id: str, request: Request
) -> None:
    """Remove a rule. Requests it already raised keep their full decision record."""
    await service.delete_rule(session, principal, rule_id, request=request)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Page[ApprovalRequestRead],
    summary="List approval requests",
)
async def list_requests(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    status: Annotated[ApprovalStatus | None, Query(description="Queue tab")] = None,
    agent_id: Annotated[str | None, Query(description="Filter by agent id")] = None,
    agent: Annotated[str | None, Query(description="Filter by agent name")] = None,
    risk: Annotated[ApprovalRisk | None, Query(description="Risk level")] = None,
    action: Annotated[str | None, Query(description="Requested action")] = None,
    policy_id: Annotated[str | None, Query(description="Policy that raised it")] = None,
    requested_by_user_id: Annotated[str | None, Query(description="Requester")] = None,
    overdue: Annotated[bool | None, Query(description="Only SLA-breached requests")] = None,
    since: Annotated[dt.datetime | None, Query(description="Requested on or after")] = None,
    until: Annotated[dt.datetime | None, Query(description="Requested on or before")] = None,
) -> Page[ApprovalRequestRead]:
    """The approval queue, newest first, filtered by the console's dropdowns.

    Free-text search covers the reference, action, resource, reason and agent
    name. Sort with `sort=` on any of: request_ref, requested_at, action,
    resource, risk, status, sla_due_at, decided_at, agent.
    """
    items, total = await service.list_requests(
        session,
        principal,
        params=params,
        filters=_filters(
            status.value if status else None,
            agent_id,
            agent,
            risk.value if risk else None,
            action,
            policy_id,
            requested_by_user_id,
            overdue,
            since,
            until,
        ),
    )
    return Page.build(items, total, params.page, params.page_size)


@router.post(
    "",
    response_model=ApprovalRequestRead,
    status_code=http_status.HTTP_201_CREATED,
    summary="Raise an approval request",
)
async def create_request(
    principal: CurrentPrincipal,
    session: Db,
    data: ApprovalRequestCreate,
    request: Request,
) -> ApprovalRequestRead:
    """Open a request. The reference is allocated automatically.

    A request that falls under an approval rule in force is filed under it
    (`policy_id`), and the response names the rule's `approvers`. The SLA
    deadline is `sla_minutes` when given, else the rule's, else the risk level's.
    A request falls under a rule when it names it as `policy_id`; when the rule's
    trigger is financial and the request's amount (`payload.amount`, else
    `impact.financial`) is above the rule's threshold; or when its `action` or
    `payload.trigger` is the rule's trigger label.
    """
    return await service.create_request(session, principal, data, request=request)


@router.get(
    "/{request_id}",
    response_model=ApprovalRequestRead,
    summary="Get an approval request",
)
async def get_request(
    principal: CurrentPrincipal, session: Db, request_id: str
) -> ApprovalRequestRead:
    """One request by id or by its human reference, with impact and workflow."""
    return await service.get_request(session, principal, request_id)


@router.patch(
    "/{request_id}",
    response_model=ApprovalRequestRead,
    summary="Amend an undecided request",
)
async def update_request(
    principal: CurrentPrincipal,
    session: Db,
    request_id: str,
    data: ApprovalRequestUpdate,
    request: Request,
) -> ApprovalRequestRead:
    """Correct a request that has not been decided. Changing the risk re-bases the
    SLA from the original request time, never from now."""
    return await service.update_request(session, principal, request_id, data, request=request)


@router.post(
    "/{request_id}/approve",
    response_model=ApprovalDecisionResponse,
    summary="Approve a request",
)
async def approve_request(
    principal: CurrentPrincipal,
    session: Db,
    request_id: str,
    request: Request,
    background: BackgroundTasks,
    data: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Approve. Requires the approver role; returns the follow-on action to carry
    out when the request's payload names one.

    Approving the request a release is parked on resumes that release: its
    Approval stage is marked approved here and the pipeline carries on.
    """
    body = data or ApprovalDecisionRequest()
    decision, resume_deployment_id = await service.approve_request(
        session, principal, request_id, note=body.note, request=request
    )
    if resume_deployment_id is not None:
        # The runner works on its own session, so the approved stage has to be
        # committed before it is allowed to look: a task scheduled first races
        # this request's COMMIT, sees the gate still shut, and exits for good.
        await session.commit()
        background.add_task(
            deployments_service.run_pipeline, resume_deployment_id, principal.workspace_id
        )
    return decision


@router.post(
    "/{request_id}/reject",
    response_model=ApprovalDecisionResponse,
    summary="Reject a request",
)
async def reject_request(
    principal: CurrentPrincipal,
    session: Db,
    request_id: str,
    request: Request,
    data: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Reject. The note is mandatory — it is quoted verbatim in the audit trail.

    Rejecting the request a release is parked on halts that release.
    """
    body = data or ApprovalDecisionRequest()
    return await service.reject_request(
        session, principal, request_id, note=body.note, request=request
    )


@router.post(
    "/{request_id}/escalate",
    response_model=ApprovalDecisionResponse,
    summary="Escalate a request",
)
async def escalate_request(
    principal: CurrentPrincipal,
    session: Db,
    request_id: str,
    request: Request,
    data: ApprovalEscalationRequest | None = None,
) -> ApprovalDecisionResponse:
    """Hand the decision to a senior reviewer. The request stays open and keeps
    its original SLA deadline."""
    body = data or ApprovalEscalationRequest()
    return await service.escalate_request(
        session,
        principal,
        request_id,
        note=body.note,
        escalate_to_user_id=body.escalate_to_user_id,
        request=request,
    )


@router.get(
    "/{request_id}/comments",
    response_model=list[ApprovalCommentRead],
    summary="List comments on a request",
)
async def list_comments(
    principal: CurrentPrincipal, session: Db, request_id: str
) -> list[ApprovalCommentRead]:
    """The reviewer thread, oldest first. Returns an empty list when nobody has
    commented yet."""
    return await service.list_comments(session, principal, request_id)


@router.post(
    "/{request_id}/comments",
    response_model=ApprovalCommentRead,
    status_code=http_status.HTTP_201_CREATED,
    summary="Comment on a request",
)
async def add_comment(
    principal: CurrentPrincipal,
    session: Db,
    request_id: str,
    data: ApprovalCommentCreate,
    request: Request,
) -> ApprovalCommentRead:
    """Append to the thread. Comments are part of the decision record and are
    themselves recorded in the audit trail."""
    return await service.add_comment(session, principal, request_id, data, request=request)
