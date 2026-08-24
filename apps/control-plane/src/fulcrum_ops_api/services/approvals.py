"""Approvals & Audit business logic.

An approval request is a small state machine, and this module is the only place
it is allowed to move:

    Pending   -> Approved | Rejected | Escalated | Expired
    Escalated -> Approved | Rejected
    Approved / Rejected / Expired -> (terminal)

Every transition is guarded, stamped with who decided and when, advanced through
the four-rung workflow tracker the console draws, and written to the audit
trail. A request that has already reached a terminal state can never be decided
again — that is a conflict, not a silent overwrite, because the decision record
is evidence.

The audit half of the screen is read-only by contract: this module lists,
exports and verifies rows, and never writes one except through
``services.audit.record``.

Every query in this file filters on ``principal.workspace_id``. A row belonging
to another tenant is reported as missing, never as forbidden, so the API cannot
be used to probe for the existence of another workspace's records.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from sqlalchemy import Select, and_, case, extract, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    ValidationFailed,
)
from ..models.governance import (
    ApprovalComment,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStep,
    AuditEvent,
    Policy,
    PolicyCategory,
    PolicyEnforcement,
    default_workflow,
)
from ..models.identity import Membership, Role, User
from ..models.registry import Agent
from ..schemas.approvals import (
    ApprovalCommentCreate,
    ApprovalCommentRead,
    ApprovalDecisionResponse,
    ApprovalRequestCreate,
    ApprovalRequestRead,
    ApprovalRequestUpdate,
    ApprovalRisk,
    ApprovalRuleCreate,
    ApprovalRuleRead,
    ApprovalRuleUpdate,
    ApprovalsSummary,
    AuditChainStatus,
    AuditEventRead,
    FollowOnAction,
)
from . import audit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Screen name stamped on every audit row this module writes.
SOURCE_SCREEN = "Approvals & Audit"

#: Entity types used in the audit trail, so history queries can filter on them.
ENTITY_REQUEST = "approval_request"
ENTITY_RULE = "approval_rule"

#: How long a request may sit undecided, by risk. The queue countdown, the
#: expiry sweeper and the SLA KPI all read this one table — there is no second
#: copy of the ladder anywhere in the system.
SLA_BY_RISK: dict[str, dt.timedelta] = {
    ApprovalRisk.CRITICAL.value: dt.timedelta(hours=1),
    ApprovalRisk.HIGH.value: dt.timedelta(hours=4),
    ApprovalRisk.MEDIUM.value: dt.timedelta(hours=24),
    ApprovalRisk.LOW.value: dt.timedelta(hours=72),
}
DEFAULT_SLA = SLA_BY_RISK[ApprovalRisk.MEDIUM.value]

#: Legal moves. Anything absent from a state's set raises Conflict.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ApprovalStatus.PENDING.value: frozenset(
        {
            ApprovalStatus.APPROVED.value,
            ApprovalStatus.REJECTED.value,
            ApprovalStatus.ESCALATED.value,
            ApprovalStatus.EXPIRED.value,
        }
    ),
    ApprovalStatus.ESCALATED.value: frozenset(
        {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}
    ),
    ApprovalStatus.APPROVED.value: frozenset(),
    ApprovalStatus.REJECTED.value: frozenset(),
    ApprovalStatus.EXPIRED.value: frozenset(),
}

#: Payload keys that name the follow-on action an approval unlocks, in priority
#: order. The first one present wins.
FOLLOW_ON_ACTION_KEYS = ("action", "operation", "tool", "callback")
FOLLOW_ON_TARGET_KEYS = ("target", "resource", "endpoint", "url")
FOLLOW_ON_PARAM_KEYS = ("parameters", "params", "arguments", "input")

#: Rolling window behind every "(30d)" number on the screen.
SUMMARY_WINDOW = dt.timedelta(days=30)

#: Hard ceiling on an export so one click cannot pull a whole tenant into memory.
MAX_EXPORT_ROWS = 5000

#: Reference allocated to a new request: APR-<year>-<5 digits>.
REQUEST_REF_PREFIX = "APR"

REQUEST_SEARCH_COLUMNS = (
    ApprovalRequest.request_ref,
    ApprovalRequest.action,
    ApprovalRequest.action_detail,
    ApprovalRequest.resource,
    ApprovalRequest.reason,
    Agent.name,
)

REQUEST_SORTABLE = {
    "request_ref": ApprovalRequest.request_ref,
    "requested_at": ApprovalRequest.requested_at,
    "action": ApprovalRequest.action,
    "resource": ApprovalRequest.resource,
    "risk": ApprovalRequest.risk,
    "status": ApprovalRequest.status,
    "sla_due_at": ApprovalRequest.sla_due_at,
    "decided_at": ApprovalRequest.decided_at,
    "agent": Agent.name,
}

RULE_SEARCH_COLUMNS = (Policy.name, Policy.description, Policy.scope_label)

RULE_SORTABLE = {
    "name": Policy.name,
    "status": Policy.status,
    "risk_level": Policy.risk_level,
    "scope": Policy.scope,
    "requests_30d": Policy.requests_30d,
    "approved_pct": Policy.approved_pct,
    "last_triggered_at": Policy.last_triggered_at,
    "created_at": Policy.created_at,
    "updated_at": Policy.updated_at,
}

AUDIT_SEARCH_COLUMNS = (
    AuditEvent.actor,
    AuditEvent.action,
    AuditEvent.entity_label,
    AuditEvent.entity_id,
    AuditEvent.detail,
)

AUDIT_SORTABLE = {
    "occurred_at": AuditEvent.occurred_at,
    "actor": AuditEvent.actor,
    "action": AuditEvent.action,
    "entity_type": AuditEvent.entity_type,
    "source_screen": AuditEvent.source_screen,
}


# ---------------------------------------------------------------------------
# Filter carriers — the dropdowns the console sends with every list request
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalFilters:
    """Dropdown state from the approval queue, plus the tab that is open."""

    status: str | None = None
    agent_id: str | None = None
    agent: str | None = None
    risk: str | None = None
    action: str | None = None
    policy_id: str | None = None
    requested_by_user_id: str | None = None
    overdue: bool | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RuleFilters:
    """Dropdown state from the approval-rules collection."""

    status: str | None = None
    risk_level: str | None = None
    scope: str | None = None
    trigger: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AuditFilters:
    """Dropdown state from the Audit Trail tab."""

    actor: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    action: str | None = None
    source_screen: str | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _actor_name(principal: Principal) -> str:
    """Display name for the workflow tracker and comment thread."""
    return principal.display_name or principal.email or principal.actor


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


def _elapsed_seconds(session: AsyncSession, start: Any, end: Any) -> Any:
    """SQL expression for ``end - start`` in seconds, on SQLite or Postgres.

    Kept as an expression rather than computed in Python so the averages on the
    KPI cards stay a single aggregate query no matter how large the table gets.
    """
    if _dialect_name(session) == "sqlite":
        return (func.julianday(end) - func.julianday(start)) * 86400.0
    return extract("epoch", end - start)


def format_duration(seconds: float) -> str:
    """Render a window the way the queue's SLA chip does: 15m, 1h 30m, 3d."""
    total = int(max(seconds, 0))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def sla_window_for(risk: str, sla_minutes: int | None = None) -> dt.timedelta:
    """The SLA window a request gets: explicit override, else the risk ladder."""
    if sla_minutes is not None:
        return dt.timedelta(minutes=sla_minutes)
    return SLA_BY_RISK.get(risk, DEFAULT_SLA)


def follow_on_from_payload(payload: dict[str, Any] | None) -> FollowOnAction | None:
    """Read the replayable action out of an approved request's payload.

    Returns ``None`` when the payload is purely descriptive — plenty of requests
    exist only to record a human sign-off and have nothing to execute.
    """
    if not payload:
        return None
    name = next(
        (str(payload[key]) for key in FOLLOW_ON_ACTION_KEYS if payload.get(key)),
        None,
    )
    if not name:
        return None
    target = next(
        (str(payload[key]) for key in FOLLOW_ON_TARGET_KEYS if payload.get(key)),
        None,
    )
    parameters: dict[str, Any] = {}
    for key in FOLLOW_ON_PARAM_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            parameters = candidate
            break
    return FollowOnAction(action=name, target=target, parameters=parameters)


def _advance_workflow(
    existing: list[Any] | None, *, decision: str, actor: str, when: dt.datetime
) -> list[dict[str, Any]]:
    """Move the four-rung tracker to reflect ``decision``.

    Rung three carries the decision's own label — the console renders it
    verbatim — and only an approval lights the final ``Completed`` rung, because
    nothing executes off the back of a rejection or an escalation.
    """
    steps: list[dict[str, Any]] = [
        dict(step) for step in (existing or []) if isinstance(step, dict)
    ]
    if len(steps) != len(default_workflow()):
        steps = default_workflow()
    stamp = when.isoformat()

    steps[0].update(
        done=True,
        by=steps[0].get("by") or actor,
        ts=steps[0].get("ts") or stamp,
    )
    steps[1].update(step=ApprovalStep.IN_REVIEW.value, done=True, by=actor, ts=stamp)
    steps[2].update(step=decision, done=True, by=actor, ts=stamp)

    completed = decision == ApprovalStatus.APPROVED.value
    steps[3].update(
        step=ApprovalStep.COMPLETED.value,
        done=completed,
        by=actor if completed else None,
        ts=stamp if completed else None,
    )
    return steps


def _assert_transition(request: ApprovalRequest, target: ApprovalStatus) -> None:
    """Guard the state machine. Re-deciding a closed request is a conflict."""
    allowed = ALLOWED_TRANSITIONS.get(request.status, frozenset())
    if target.value in allowed:
        return
    if not allowed:
        raise Conflict(
            f"Request {request.request_ref} was already {request.status.lower()} "
            f"and cannot be decided again.",
            details={"status": request.status, "attempted": target.value},
        )
    raise Conflict(
        f"A {request.status.lower()} request cannot move to {target.value.lower()}.",
        details={
            "status": request.status,
            "attempted": target.value,
            "allowed": sorted(allowed),
        },
    )


# ---------------------------------------------------------------------------
# Loading and hydration
# ---------------------------------------------------------------------------


def _base_request_query(principal: Principal) -> Select:
    """Requests for this workspace, joined to their agent for search and sort."""
    return (
        select(ApprovalRequest)
        .outerjoin(
            Agent,
            and_(
                Agent.id == ApprovalRequest.agent_id,
                Agent.workspace_id == ApprovalRequest.workspace_id,
            ),
        )
        .where(ApprovalRequest.workspace_id == principal.workspace_id)
    )


def _apply_request_filters(
    stmt: Select, params: ListParams, filters: ApprovalFilters
) -> Select:
    stmt = apply_filters(
        stmt,
        {
            ApprovalRequest.status: filters.status,
            ApprovalRequest.agent_id: filters.agent_id,
            ApprovalRequest.risk: filters.risk,
            ApprovalRequest.action: filters.action,
            ApprovalRequest.policy_id: filters.policy_id,
            ApprovalRequest.requested_by_user_id: filters.requested_by_user_id,
            Agent.name: filters.agent,
        },
    )
    if filters.since is not None:
        stmt = stmt.where(ApprovalRequest.requested_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(ApprovalRequest.requested_at <= filters.until)
    if filters.overdue is not None:
        breached = and_(
            ApprovalRequest.sla_due_at.is_not(None),
            ApprovalRequest.sla_due_at < _now(),
            ApprovalRequest.decided_at.is_(None),
        )
        stmt = stmt.where(breached if filters.overdue else ~breached)
    return apply_search(stmt, params, REQUEST_SEARCH_COLUMNS)


async def _load_request(
    session: AsyncSession, principal: Principal, request_id: str
) -> ApprovalRequest:
    """Fetch one request or raise NotFound — including for another tenant's row."""
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.workspace_id == principal.workspace_id,
        or_(ApprovalRequest.id == request_id, ApprovalRequest.request_ref == request_id),
    )
    found = (await session.execute(stmt)).scalars().first()
    if found is None:
        raise NotFound(f"Approval request '{request_id}' does not exist.")
    return found


async def _hydrate_requests(
    session: AsyncSession, requests: Sequence[ApprovalRequest]
) -> list[ApprovalRequestRead]:
    """Attach agent, requester, approver, policy and comment counts to a page.

    Four small lookups for the whole page rather than four per row: the queue
    renders names, not identifiers, and a 200-row page must not become 800
    queries.
    """
    if not requests:
        return []

    agent_ids = {r.agent_id for r in requests if r.agent_id}
    policy_ids = {r.policy_id for r in requests if r.policy_id}
    user_ids = {
        user_id
        for r in requests
        for user_id in (r.requested_by_user_id, r.decided_by_user_id, r.escalated_to_user_id)
        if user_id
    }
    request_ids = [r.id for r in requests]

    agents: dict[str, Any] = {}
    if agent_ids:
        rows = await session.execute(
            select(Agent.id, Agent.name, Agent.platform).where(Agent.id.in_(agent_ids))
        )
        agents = {row.id: row for row in rows}

    policies: dict[str, str] = {}
    if policy_ids:
        rows = await session.execute(
            select(Policy.id, Policy.name).where(Policy.id.in_(policy_ids))
        )
        policies = {row.id: row.name for row in rows}

    users: dict[str, Any] = {}
    if user_ids:
        rows = await session.execute(
            select(User.id, User.full_name, User.team).where(User.id.in_(user_ids))
        )
        users = {row.id: row for row in rows}

    counts = dict(
        (
            await session.execute(
                select(ApprovalComment.request_id, func.count(ApprovalComment.id))
                .where(ApprovalComment.request_id.in_(request_ids))
                .group_by(ApprovalComment.request_id)
            )
        ).all()
    )

    hydrated: list[ApprovalRequestRead] = []
    for row in requests:
        agent = agents.get(row.agent_id) if row.agent_id else None
        requester = users.get(row.requested_by_user_id) if row.requested_by_user_id else None
        decider = users.get(row.decided_by_user_id) if row.decided_by_user_id else None
        escalated_to = users.get(row.escalated_to_user_id) if row.escalated_to_user_id else None
        hydrated.append(
            ApprovalRequestRead.model_validate(row).model_copy(
                update={
                    "agent_name": agent.name if agent else None,
                    "agent_platform": agent.platform if agent else None,
                    "policy_name": policies.get(row.policy_id) if row.policy_id else None,
                    "requested_by_name": requester.full_name if requester else None,
                    "requested_by_team": requester.team if requester else None,
                    "decided_by_name": decider.full_name if decider else None,
                    "escalated_to_name": escalated_to.full_name if escalated_to else None,
                    "comment_count": int(counts.get(row.id, 0)),
                }
            )
        )
    return hydrated


# ---------------------------------------------------------------------------
# Requests: read
# ---------------------------------------------------------------------------


async def list_requests(
    session: AsyncSession,
    principal: Principal,
    *,
    params: ListParams,
    filters: ApprovalFilters,
) -> tuple[list[ApprovalRequestRead], int]:
    """One page of the approval queue, plus the unpaged total."""
    stmt = _apply_request_filters(_base_request_query(principal), params, filters)
    stmt = apply_sort(stmt, params, REQUEST_SORTABLE, ApprovalRequest.requested_at)
    rows, total = await paginate(session, stmt, params)
    return await _hydrate_requests(session, rows), total


async def get_request(
    session: AsyncSession, principal: Principal, request_id: str
) -> ApprovalRequestRead:
    """One request by id or by its human reference (REQ-1042 style)."""
    row = await _load_request(session, principal, request_id)
    return (await _hydrate_requests(session, [row]))[0]


async def export_requests(
    session: AsyncSession,
    principal: Principal,
    *,
    params: ListParams,
    filters: ApprovalFilters,
) -> list[ApprovalRequestRead]:
    """Every request matching the current filters, capped at MAX_EXPORT_ROWS."""
    stmt = _apply_request_filters(_base_request_query(principal), params, filters)
    stmt = apply_sort(stmt, params, REQUEST_SORTABLE, ApprovalRequest.requested_at)
    rows = (await session.execute(stmt.limit(MAX_EXPORT_ROWS))).scalars().all()
    return await _hydrate_requests(session, rows)


# ---------------------------------------------------------------------------
# Requests: write
# ---------------------------------------------------------------------------


async def _next_request_ref(session: AsyncSession, workspace_id: str) -> str:
    """Allocate the next APR-<year>-NNNNN reference for this workspace."""
    prefix = f"{REQUEST_REF_PREFIX}-{_now().year}-"
    highest = (
        await session.execute(
            select(func.max(ApprovalRequest.request_ref)).where(
                ApprovalRequest.workspace_id == workspace_id,
                ApprovalRequest.request_ref.like(f"{prefix}%"),
            )
        )
    ).scalar_one_or_none()

    sequence = 1
    if highest:
        tail = highest[len(prefix) :]
        if tail.isdigit():
            sequence = int(tail) + 1
    return f"{prefix}{sequence:05d}"


async def _assert_agent_exists(
    session: AsyncSession, principal: Principal, agent_id: str
) -> None:
    found = (
        await session.execute(
            select(Agent.id).where(
                Agent.id == agent_id, Agent.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise ValidationFailed(
            "That agent is not registered in this workspace.",
            details={"field": "agent_id", "value": agent_id},
        )


async def _assert_policy_exists(
    session: AsyncSession, principal: Principal, policy_id: str
) -> None:
    found = (
        await session.execute(
            select(Policy.id).where(
                Policy.id == policy_id, Policy.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise ValidationFailed(
            "That policy does not exist in this workspace.",
            details={"field": "policy_id", "value": policy_id},
        )


async def _assert_workspace_member(
    session: AsyncSession, principal: Principal, user_id: str
) -> None:
    found = (
        await session.execute(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise ValidationFailed(
            "That reviewer is not a member of this workspace.",
            details={"field": "escalate_to_user_id", "value": user_id},
        )


async def create_request(
    session: AsyncSession,
    principal: Principal,
    data: ApprovalRequestCreate,
    *,
    request: Request | None = None,
) -> ApprovalRequestRead:
    """Raise a request, stamp its SLA from the risk ladder, and open the tracker."""
    principal.require(Role.MEMBER)

    if data.agent_id:
        await _assert_agent_exists(session, principal, data.agent_id)
    if data.policy_id:
        await _assert_policy_exists(session, principal, data.policy_id)

    reference = data.request_ref or await _next_request_ref(session, principal.workspace_id)
    clash = (
        await session.execute(
            select(ApprovalRequest.id).where(
                ApprovalRequest.workspace_id == principal.workspace_id,
                ApprovalRequest.request_ref == reference,
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"Request reference '{reference}' is already in use.")

    now = _now()
    window = sla_window_for(data.risk.value, data.sla_minutes)
    actor = _actor_name(principal)

    workflow = default_workflow()
    workflow[0].update(step=ApprovalStep.REQUESTED.value, done=True, by=actor, ts=now.isoformat())

    row = ApprovalRequest(
        workspace_id=principal.workspace_id,
        request_ref=reference,
        agent_id=data.agent_id,
        source=data.source or SOURCE_SCREEN,
        action=data.action,
        action_detail=data.action_detail,
        resource=data.resource,
        risk=data.risk.value,
        policy_id=data.policy_id,
        reason=data.reason,
        requested_by_user_id=data.requested_by_user_id or principal.user_id,
        requested_at=now,
        sla_due_at=now + window,
        sla_label=format_duration(window.total_seconds()),
        status=ApprovalStatus.PENDING.value,
        payload=data.payload,
        impact=data.impact.model_dump(exclude_none=True),
        workflow=workflow,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Approval requested",
        entity_type=ENTITY_REQUEST,
        entity_id=row.id,
        entity_label=reference,
        source_screen=SOURCE_SCREEN,
        detail=f"{data.action} — {data.resource or 'no resource'} ({data.risk.value} risk)",
        metadata={"risk": data.risk.value, "sla_due_at": row.sla_due_at.isoformat()},
        request=request,
    )
    return (await _hydrate_requests(session, [row]))[0]


async def update_request(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    data: ApprovalRequestUpdate,
    *,
    request: Request | None = None,
) -> ApprovalRequestRead:
    """Amend an undecided request. A decided request is frozen — it is evidence."""
    principal.require(Role.MEMBER)
    row = await _load_request(session, principal, request_id)

    if not row.is_open:
        raise Conflict(
            f"Request {row.request_ref} was already {row.status.lower()} and can no longer "
            "be edited.",
            details={"status": row.status},
        )
    if data.expected_updated_at is not None and row.updated_at != data.expected_updated_at:
        raise PreconditionFailed(
            "This request changed since you loaded it. Reload and try again.",
            details={"updated_at": row.updated_at.isoformat()},
        )
    if data.policy_id:
        await _assert_policy_exists(session, principal, data.policy_id)

    changes = data.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return (await _hydrate_requests(session, [row]))[0]

    if "risk" in changes and data.risk is not None:
        row.risk = data.risk.value
    if "impact" in changes and data.impact is not None:
        row.impact = data.impact.model_dump(exclude_none=True)
    for field in ("action", "action_detail", "resource", "reason", "policy_id", "payload"):
        if field in changes:
            setattr(row, field, changes[field])

    # Any change to risk or to the explicit window re-bases the SLA clock from
    # the original request time, never from "now" — moving the deadline forward
    # by editing a request would make the SLA meaningless.
    if "risk" in changes or "sla_minutes" in changes:
        window = sla_window_for(row.risk, data.sla_minutes)
        row.sla_due_at = row.requested_at + window
        row.sla_label = format_duration(window.total_seconds())

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Approval request updated",
        entity_type=ENTITY_REQUEST,
        entity_id=row.id,
        entity_label=row.request_ref,
        source_screen=SOURCE_SCREEN,
        detail=f"Updated {', '.join(sorted(changes))}",
        metadata={"fields": sorted(changes)},
        request=request,
    )
    return (await _hydrate_requests(session, [row]))[0]


async def _decide(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    *,
    target: ApprovalStatus,
    note: str | None,
    escalate_to_user_id: str | None = None,
    request: Request | None = None,
) -> ApprovalDecisionResponse:
    """The single write path for approve, reject and escalate."""
    principal.require(Role.APPROVER)
    row = await _load_request(session, principal, request_id)
    _assert_transition(row, target)

    # Separation of duties: the four-eyes gate is the whole point of this
    # screen, so raising a request and deciding it must be two people.
    # Escalating your own request is allowed - that is asking for eyes, not
    # supplying them.
    if (
        target is not ApprovalStatus.ESCALATED
        and principal.user_id is not None
        and row.requested_by_user_id == principal.user_id
    ):
        raise PermissionDenied(
            "You raised this request; a different reviewer has to decide it."
        )

    cleaned_note = (note or "").strip() or None
    if target is ApprovalStatus.REJECTED and cleaned_note is None:
        raise ValidationFailed(
            "A rejection must carry a reason; it is quoted in the audit trail.",
            details={"field": "note"},
        )
    if escalate_to_user_id:
        await _assert_workspace_member(session, principal, escalate_to_user_id)

    now = _now()
    actor = _actor_name(principal)
    previous = row.status

    row.status = target.value
    row.decided_by_user_id = principal.user_id
    row.decided_at = now
    row.decision_note = cleaned_note
    if target is ApprovalStatus.ESCALATED:
        row.escalated_to_user_id = escalate_to_user_id
    row.workflow = _advance_workflow(row.workflow, decision=target.value, actor=actor, when=now)

    # The note is also the last word in the discussion thread, so a reviewer
    # reading the request later sees the reasoning in context.
    if cleaned_note:
        session.add(
            ApprovalComment(
                workspace_id=principal.workspace_id,
                request_id=row.id,
                author_user_id=principal.user_id,
                body=cleaned_note,
            )
        )
    await session.flush()

    verb = target.value.lower()
    await audit.record(
        session,
        principal=principal,
        action=f"Request {verb}",
        entity_type=ENTITY_REQUEST,
        entity_id=row.id,
        entity_label=row.request_ref,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{row.action} — {row.resource or 'no resource'}"
            + (f" · Note: {cleaned_note}" if cleaned_note else "")
        ),
        metadata={
            "from": previous,
            "to": target.value,
            "risk": row.risk,
            "within_sla": row.sla_due_at is None or now <= row.sla_due_at,
        },
        request=request,
    )

    hydrated = (await _hydrate_requests(session, [row]))[0]
    follow_on = (
        follow_on_from_payload(row.payload) if target is ApprovalStatus.APPROVED else None
    )
    message = f"Request {row.request_ref} {verb}."
    if follow_on is not None:
        message = f"{message} Carry out '{follow_on.action}' under this approval."
    return ApprovalDecisionResponse(message=message, request=hydrated, follow_on=follow_on)


async def approve_request(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> ApprovalDecisionResponse:
    """Approve a request and hand back whatever it unlocks."""
    return await _decide(
        session,
        principal,
        request_id,
        target=ApprovalStatus.APPROVED,
        note=note,
        request=request,
    )


async def reject_request(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> ApprovalDecisionResponse:
    """Reject a request. The reason is mandatory and is quoted in the audit row."""
    return await _decide(
        session,
        principal,
        request_id,
        target=ApprovalStatus.REJECTED,
        note=note,
        request=request,
    )


async def escalate_request(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    *,
    note: str | None = None,
    escalate_to_user_id: str | None = None,
    request: Request | None = None,
) -> ApprovalDecisionResponse:
    """Hand a request to a senior reviewer; it stays open and keeps its SLA."""
    return await _decide(
        session,
        principal,
        request_id,
        target=ApprovalStatus.ESCALATED,
        note=note,
        escalate_to_user_id=escalate_to_user_id,
        request=request,
    )


async def expire_overdue(
    session: AsyncSession, principal: Principal, *, request: Request | None = None
) -> list[ApprovalRequestRead]:
    """Close out requests whose SLA elapsed with nobody deciding.

    Called by the SLA sweeper, not by a route: expiry is something time does to
    a request, not something a person asks for. Escalated requests are left
    alone — they are already in front of a human.
    """
    now = _now()
    overdue = (
        (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.workspace_id == principal.workspace_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                    ApprovalRequest.sla_due_at.is_not(None),
                    ApprovalRequest.sla_due_at < now,
                )
            )
        )
        .scalars()
        .all()
    )
    if not overdue:
        return []

    for row in overdue:
        _assert_transition(row, ApprovalStatus.EXPIRED)
        row.status = ApprovalStatus.EXPIRED.value
        row.decided_at = now
        row.workflow = _advance_workflow(
            row.workflow, decision=ApprovalStatus.EXPIRED.value, actor="system", when=now
        )
        await audit.record(
            session,
            principal=principal,
            action="Request expired",
            entity_type=ENTITY_REQUEST,
            entity_id=row.id,
            entity_label=row.request_ref,
            source_screen=SOURCE_SCREEN,
            detail=f"SLA elapsed at {row.sla_due_at.isoformat()} with no decision",
            metadata={"from": ApprovalStatus.PENDING.value, "to": ApprovalStatus.EXPIRED.value},
            request=request,
        )
    await session.flush()
    return await _hydrate_requests(session, overdue)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


async def list_comments(
    session: AsyncSession, principal: Principal, request_id: str
) -> list[ApprovalCommentRead]:
    """The full discussion thread, oldest first. Threads are short by design."""
    row = await _load_request(session, principal, request_id)
    comments = (
        (
            await session.execute(
                select(ApprovalComment)
                .where(
                    ApprovalComment.workspace_id == principal.workspace_id,
                    ApprovalComment.request_id == row.id,
                )
                .order_by(ApprovalComment.created_at.asc(), ApprovalComment.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not comments:
        return []

    author_ids = {c.author_user_id for c in comments if c.author_user_id}
    authors: dict[str, str] = {}
    if author_ids:
        rows = await session.execute(
            select(User.id, User.full_name).where(User.id.in_(author_ids))
        )
        authors = {row_.id: row_.full_name for row_ in rows}

    return [
        ApprovalCommentRead.model_validate(comment).model_copy(
            update={"author_name": authors.get(comment.author_user_id or "")}
        )
        for comment in comments
    ]


async def add_comment(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    data: ApprovalCommentCreate,
    *,
    request: Request | None = None,
) -> ApprovalCommentRead:
    """Append to the thread. Comments are part of the decision record."""
    principal.require(Role.MEMBER)
    row = await _load_request(session, principal, request_id)

    body = data.body.strip()
    if not body:
        raise ValidationFailed("A comment cannot be empty.", details={"field": "body"})

    comment = ApprovalComment(
        workspace_id=principal.workspace_id,
        request_id=row.id,
        author_user_id=principal.user_id,
        body=body,
    )
    session.add(comment)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Comment added",
        entity_type=ENTITY_REQUEST,
        entity_id=row.id,
        entity_label=row.request_ref,
        source_screen=SOURCE_SCREEN,
        detail=body,
        request=request,
    )
    return ApprovalCommentRead.model_validate(comment).model_copy(
        update={"author_name": _actor_name(principal)}
    )


# ---------------------------------------------------------------------------
# Approval rules
#
# A rule lives in ``policies`` under the "Approval & Escalation" category: it is
# the thing that turns an agent action into a request in this queue, so it has
# to be the same row the enforcement path evaluates.
# ---------------------------------------------------------------------------


def _rule_query(principal: Principal) -> Select:
    return select(Policy).where(
        Policy.workspace_id == principal.workspace_id,
        Policy.category == PolicyCategory.APPROVAL_ESCALATION.value,
    )


def _rule_to_read(policy: Policy) -> ApprovalRuleRead:
    body = policy.rules or {}
    approvers = body.get("approvers") or []
    if not isinstance(approvers, list):
        approvers = [str(approvers)]
    return ApprovalRuleRead.model_validate(policy).model_copy(
        update={
            "trigger": body.get("trigger"),
            "approvers": [str(item) for item in approvers],
            "sla_minutes": body.get("sla_minutes"),
            "threshold_amount": body.get("threshold_amount"),
        }
    )


def _bump_version(current: str | None) -> str:
    """Patch-bump a vMAJOR.MINOR.PATCH string; rules are versioned like policies."""
    parts = (current or "v1.0.0").lstrip("vV").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return "v1.0.1"
    major, minor, patch = (int(part) for part in parts)
    return f"v{major}.{minor}.{patch + 1}"


async def _load_rule(session: AsyncSession, principal: Principal, rule_id: str) -> Policy:
    found = (
        await session.execute(_rule_query(principal).where(Policy.id == rule_id))
    ).scalar_one_or_none()
    if found is None:
        raise NotFound(f"Approval rule '{rule_id}' does not exist.")
    return found


async def list_rules(
    session: AsyncSession,
    principal: Principal,
    *,
    params: ListParams,
    filters: RuleFilters,
) -> tuple[list[ApprovalRuleRead], int]:
    """One page of approval rules, plus the unpaged total."""
    stmt = apply_filters(
        _rule_query(principal),
        {
            Policy.status: filters.status,
            Policy.risk_level: filters.risk_level,
            Policy.scope: filters.scope,
        },
    )
    stmt = apply_search(stmt, params, RULE_SEARCH_COLUMNS)
    stmt = apply_sort(stmt, params, RULE_SORTABLE, Policy.created_at)
    rows, total = await paginate(session, stmt, params)

    rules = [_rule_to_read(row) for row in rows]
    if filters.trigger:
        # The trigger lives inside the policy body, so it is filtered after the
        # page is materialised; the page is at most MAX_PAGE_SIZE rows.
        rules = [rule for rule in rules if rule.trigger == filters.trigger]
    return rules, total


async def get_rule(
    session: AsyncSession, principal: Principal, rule_id: str
) -> ApprovalRuleRead:
    """One approval rule by id."""
    return _rule_to_read(await _load_rule(session, principal, rule_id))


async def create_rule(
    session: AsyncSession,
    principal: Principal,
    data: ApprovalRuleCreate,
    *,
    request: Request | None = None,
) -> ApprovalRuleRead:
    """Create a rule. New requests matching its trigger land in this queue."""
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(
            select(Policy.id).where(
                Policy.workspace_id == principal.workspace_id, Policy.name == data.name
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A policy named '{data.name}' already exists in this workspace.")

    actor = principal.actor
    row = Policy(
        workspace_id=principal.workspace_id,
        name=data.name,
        description=data.description,
        category=PolicyCategory.APPROVAL_ESCALATION.value,
        scope=data.scope.value,
        scope_ref=data.scope_ref,
        scope_label=data.scope_label,
        risk_level=data.risk_level.value,
        status=data.status.value,
        enforcement=PolicyEnforcement.REQUIRE_APPROVAL.value,
        rules={
            "trigger": data.trigger.value,
            "approvers": data.approvers,
            "sla_minutes": data.sla_minutes,
            "threshold_amount": data.threshold_amount,
        },
        version="v1.0.0",
        owner_user_id=principal.user_id,
        created_by=actor,
        updated_by=actor,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Approval rule created",
        entity_type=ENTITY_RULE,
        entity_id=row.id,
        entity_label=row.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{data.trigger.value} · approvers: {', '.join(data.approvers)} · "
            f"SLA {format_duration(data.sla_minutes * 60)}"
        ),
        metadata={"trigger": data.trigger.value, "sla_minutes": data.sla_minutes},
        request=request,
    )
    return _rule_to_read(row)


async def update_rule(
    session: AsyncSession,
    principal: Principal,
    rule_id: str,
    data: ApprovalRuleUpdate,
    *,
    request: Request | None = None,
) -> ApprovalRuleRead:
    """Edit a rule and patch-bump its version."""
    principal.require(Role.ADMIN)
    row = await _load_rule(session, principal, rule_id)

    if data.expected_updated_at is not None and row.updated_at != data.expected_updated_at:
        raise PreconditionFailed(
            "This rule changed since you loaded it. Reload and try again.",
            details={"updated_at": row.updated_at.isoformat()},
        )

    changes = data.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return _rule_to_read(row)

    if data.name is not None and data.name != row.name:
        clash = (
            await session.execute(
                select(Policy.id).where(
                    Policy.workspace_id == principal.workspace_id,
                    Policy.name == data.name,
                    Policy.id != row.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A policy named '{data.name}' already exists in this workspace.")
        row.name = data.name

    if "description" in changes:
        row.description = data.description
    if "scope_ref" in changes:
        row.scope_ref = data.scope_ref
    if "scope_label" in changes:
        row.scope_label = data.scope_label
    if data.scope is not None:
        row.scope = data.scope.value
    if data.risk_level is not None:
        row.risk_level = data.risk_level.value
    if data.status is not None:
        row.status = data.status.value

    body = dict(row.rules or {})
    if data.trigger is not None:
        body["trigger"] = data.trigger.value
    if data.approvers is not None:
        body["approvers"] = data.approvers
    if data.sla_minutes is not None:
        body["sla_minutes"] = data.sla_minutes
    if "threshold_amount" in changes:
        body["threshold_amount"] = data.threshold_amount
    row.rules = body

    row.version = _bump_version(row.version)
    row.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Approval rule updated",
        entity_type=ENTITY_RULE,
        entity_id=row.id,
        entity_label=row.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Updated {', '.join(sorted(changes))} (now {row.version})",
        metadata={"fields": sorted(changes), "version": row.version},
        request=request,
    )
    return _rule_to_read(row)


async def delete_rule(
    session: AsyncSession,
    principal: Principal,
    rule_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a rule. Requests it already raised keep their decision record."""
    principal.require(Role.ADMIN)
    row = await _load_rule(session, principal, rule_id)
    name, rule_pk = row.name, row.id

    await session.delete(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Approval rule deleted",
        entity_type=ENTITY_RULE,
        entity_id=rule_pk,
        entity_label=name,
        source_screen=SOURCE_SCREEN,
        detail=f"Rule '{name}' removed; matching actions no longer require approval",
        request=request,
    )


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


async def summary(session: AsyncSession, principal: Principal) -> ApprovalsSummary:
    """The six KPI numbers, computed by the database in one pass.

    Nothing is loaded into Python to count it: the queue is unbounded and the
    cards sit above the table on every page load.
    """
    now = _now()
    since = now - SUMMARY_WINDOW

    decided_in_window = and_(
        ApprovalRequest.decided_at.is_not(None), ApprovalRequest.decided_at >= since
    )
    approved = and_(
        ApprovalRequest.status == ApprovalStatus.APPROVED.value, decided_in_window
    )
    rejected = and_(
        ApprovalRequest.status == ApprovalStatus.REJECTED.value, decided_in_window
    )
    escalated = and_(
        ApprovalRequest.status == ApprovalStatus.ESCALATED.value,
        func.coalesce(ApprovalRequest.decided_at, ApprovalRequest.requested_at) >= since,
    )
    within_sla = or_(
        ApprovalRequest.sla_due_at.is_(None),
        ApprovalRequest.decided_at <= ApprovalRequest.sla_due_at,
    )
    elapsed = _elapsed_seconds(
        session, ApprovalRequest.requested_at, ApprovalRequest.decided_at
    )

    sla_ratio_expr = func.avg(
        case((and_(decided_in_window, within_sla), 1.0), (decided_in_window, 0.0))
    )

    row = (
        await session.execute(
            select(
                func.sum(
                    case((ApprovalRequest.status == ApprovalStatus.PENDING.value, 1), else_=0)
                ),
                func.sum(case((approved, 1), else_=0)),
                func.sum(case((rejected, 1), else_=0)),
                func.sum(case((escalated, 1), else_=0)),
                func.avg(case((approved, elapsed))),
                sla_ratio_expr,
            ).where(ApprovalRequest.workspace_id == principal.workspace_id)
        )
    ).one()

    pending, approved_30d, rejected_30d, escalated_30d, avg_seconds, sla_ratio = row
    return ApprovalsSummary(
        pending=int(pending or 0),
        approved_30d=int(approved_30d or 0),
        rejected_30d=int(rejected_30d or 0),
        escalated_30d=int(escalated_30d or 0),
        avg_time_to_approve_seconds=(
            round(float(avg_seconds), 1) if avg_seconds is not None else None
        ),
        sla_met_percent=round(float(sla_ratio) * 100, 1) if sla_ratio is not None else None,
    )


# ---------------------------------------------------------------------------
# Audit trail (read-only)
# ---------------------------------------------------------------------------


def _apply_audit_filters(stmt: Select, params: ListParams, filters: AuditFilters) -> Select:
    stmt = apply_filters(
        stmt,
        {
            AuditEvent.actor: filters.actor,
            AuditEvent.entity_type: filters.entity_type,
            AuditEvent.entity_id: filters.entity_id,
            AuditEvent.action: filters.action,
            AuditEvent.source_screen: filters.source_screen,
        },
    )
    if filters.since is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(AuditEvent.occurred_at <= filters.until)
    return apply_search(stmt, params, AUDIT_SEARCH_COLUMNS)


async def list_audit_events(
    session: AsyncSession,
    principal: Principal,
    *,
    params: ListParams,
    filters: AuditFilters,
) -> tuple[list[AuditEventRead], int]:
    """One page of the trail, newest first, plus the unpaged total."""
    stmt = _apply_audit_filters(
        select(AuditEvent).where(AuditEvent.workspace_id == principal.workspace_id),
        params,
        filters,
    )
    stmt = apply_sort(stmt, params, AUDIT_SORTABLE, AuditEvent.occurred_at)
    rows, total = await paginate(session, stmt, params)
    return [AuditEventRead.model_validate(row) for row in rows], total


async def export_audit_events(
    session: AsyncSession,
    principal: Principal,
    *,
    params: ListParams,
    filters: AuditFilters,
) -> list[AuditEventRead]:
    """Every audit row matching the current filters, capped at MAX_EXPORT_ROWS."""
    stmt = _apply_audit_filters(
        select(AuditEvent).where(AuditEvent.workspace_id == principal.workspace_id),
        params,
        filters,
    )
    stmt = apply_sort(stmt, params, AUDIT_SORTABLE, AuditEvent.occurred_at)
    rows = (await session.execute(stmt.limit(MAX_EXPORT_ROWS))).scalars().all()
    return [AuditEventRead.model_validate(row) for row in rows]


async def verify_audit_chain(
    session: AsyncSession, principal: Principal
) -> AuditChainStatus:
    """Replay the hash chain for this workspace and report where it breaks."""
    result = await audit.verify_chain(session, principal.workspace_id)
    return AuditChainStatus.model_validate(result)
