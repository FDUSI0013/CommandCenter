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

Some requests are not only a record. Every release parks at an approval gate
and files its request here, and this queue is where the Deployments screen sends
the approver -- so a decision taken here is carried through to the deployment it
gates: approving resumes the pipeline, rejecting (or letting the SLA lapse)
halts it. See ``_settle_gate``.

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
import inspect
import logging
import re
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from sqlalchemy import Select, and_, case, extract, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
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
    PolicyScope,
    PolicyStatus,
    default_workflow,
)
from ..models.identity import Membership, Role, User
from ..models.operations import (
    Deployment,
    DeploymentStage,
    DeploymentStageStatus,
    DeploymentStatus,
)
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
    ApprovalTrigger,
    AuditChainStatus,
    AuditEventRead,
    FollowOnAction,
)
from . import audit

log = logging.getLogger(__name__)

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
REQUEST_REF_DIGITS = 5

#: Prefixes the allocators own -- this one, and the deployment gate's "REQ-".
#: A caller may bring its own reference, but not one that looks allocated:
#: "APR-2026-hotfix" sorted above every real number, the sequence restarted
#: at 1, and every automatic create after it collided.
RESERVED_REF_PREFIXES = (f"{REQUEST_REF_PREFIX}-", "REQ-")
ALLOCATED_REF = re.compile(rf"^{REQUEST_REF_PREFIX}-\d{{4}}-\d{{{REQUEST_REF_DIGITS}}}$")

#: How many times a create re-allocates when another worker takes the
#: reference between the read and the insert.
REF_ALLOCATION_ATTEMPTS = 5

#: Statuses in which an approval rule is in force. Warning is "firing more than
#: its baseline" -- a rule that is still switched on.
RULE_IN_FORCE = (PolicyStatus.ACTIVE.value, PolicyStatus.WARNING.value)

#: Rules read when a new request looks for the one it falls under. A workspace
#: has a handful; the ceiling only keeps one create from reading a whole table.
MAX_RULES_MATCHED = 200

#: A sum of money written the way the decision card shows it: "$2,450",
#: "USD 1,200.00". A figure with a magnitude suffix ("2.4k") is not read at all
#: rather than read as 2.4 -- a threshold compared with a guess is not a control.
_AMOUNT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w.])")

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
    session: AsyncSession, principal: Principal, request_id: str, *, for_update: bool = False
) -> ApprovalRequest:
    """Fetch one request or raise NotFound — including for another tenant's row.

    ``for_update`` is for the paths that read the status, check it and then
    write. Without the row lock two deciders on two workers both read Pending,
    both pass the state machine, and the second commit silently replaces the
    first -- after the first caller was told "approved" and handed the
    follow-on action. With it the second waits, re-reads the committed status,
    and gets the 409 the state machine was written to give. SQLite has a single
    writer and ignores the clause.
    """
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.workspace_id == principal.workspace_id,
        or_(ApprovalRequest.id == request_id, ApprovalRequest.request_ref == request_id),
    )
    if for_update:
        # populate_existing: the lock is only worth having if the row in hand
        # is the one that was locked, not a copy the session loaded earlier.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
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
                    "approvers": _named_approvers(row),
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
# Deployment gates
#
# The pipeline runner opens a request (action "Deploy") when a release reaches
# its Approval stage, then exits; nothing re-examines a parked stage. Until a
# decision here reached the deployment, approving in this queue -- which is
# where the Deployments screen's only row action leads -- changed the request
# and left the release "Awaiting approval" for ever, with its environment
# locked; rejecting left it free to be pushed through from the other screen.
# ---------------------------------------------------------------------------

#: The deployments service may own this step. When it exposes a coroutine of
#: this name taking exactly these keywords, it is used; otherwise the writes
#: below are made here. Both leave the same rows behind.
GATE_HOOK = "apply_gate_decision"


async def _gated_deployment(session: AsyncSession, row: ApprovalRequest) -> Deployment | None:
    """The deployment parked on ``row``, if ``row`` is a release gate.

    Found through the deployment's own pointer, never through the request's
    payload. The payload is the requester's to write, and a hand-raised request
    that merely *names* a deployment must not be able to release it.
    """
    return (
        (
            await session.execute(
                select(Deployment).where(
                    Deployment.workspace_id == row.workspace_id,
                    Deployment.approval_request_id == row.id,
                )
            )
        )
        .scalars()
        .first()
    )


async def _settle_gate(
    session: AsyncSession,
    principal: Principal,
    row: ApprovalRequest,
    *,
    approved: bool,
    note: str | None,
    request: Request | None = None,
) -> str | None:
    """Carry a closed request through to the release it gates.

    Returns the id of the deployment whose pipeline should now resume, which
    the caller schedules only *after* committing: the runner works on its own
    session and must be able to see the approved stage. Returns None when there
    is nothing to resume -- not a gate, a release that already finished, a gate
    that is not open, or a refusal (which halts the release here and now).
    """
    deployment = await _gated_deployment(session, row)
    if deployment is None or deployment.is_terminal:
        return None

    # Imported here so the deployments service stays free to import this module.
    from . import deployments as deployments_service

    hook = getattr(deployments_service, GATE_HOOK, None)
    if hook is not None:
        offered = {
            "session": session,
            "principal": principal,
            "approval_request": row,
            "approved": approved,
            "note": note,
            "request": request,
        }
        try:
            inspect.signature(hook).bind(**offered)
        except TypeError:
            log.warning("deployments.%s does not take the gate contract; ignoring it", GATE_HOOK)
        else:
            outcome = await hook(**offered)
            resume = outcome[-1] if isinstance(outcome, tuple) else outcome
            return deployment.id if resume else None

    stage = (
        await session.execute(
            select(DeploymentStage).where(
                DeploymentStage.deployment_id == deployment.id,
                DeploymentStage.name == deployments_service.STAGE_APPROVAL,
            )
        )
    ).scalar_one_or_none()
    if stage is None or stage.status != DeploymentStageStatus.RUNNING.value:
        return None

    decided_at = _now()
    actor = principal.actor
    if approved:
        stage.status = DeploymentStageStatus.APPROVED.value
        stage.finished_at = decided_at
        stage.log = note or f"Approved by {actor} (request {row.request_ref})."
        stage.detail = {
            **(stage.detail or {}),
            "approved_by": actor,
            "approved_at": decided_at.isoformat(),
        }
        deployment.updated_by = actor
        await session.flush()
        await audit.record(
            session,
            principal=principal,
            action="deployment.approve",
            entity_type="Deployment",
            entity_id=deployment.id,
            entity_label=deployment.deployment_ref,
            source_screen=SOURCE_SCREEN,
            detail=f"Approved {deployment.version} for release via {row.request_ref}.",
            metadata={"approval_request_id": row.id},
            request=request,
        )
        return deployment.id

    # Refused, or nobody decided in time: the gate stays shut, which is a halt
    # rather than a failure. The runner is parked, so cancel is a formality.
    deployments_service.runner.cancel(deployment.id)
    reason = note or f"Rejected by {actor} (request {row.request_ref})."
    stage.status = DeploymentStageStatus.FAILED.value
    stage.finished_at = decided_at
    stage.log = reason
    stage.detail = {
        **(stage.detail or {}),
        "rejected_by": actor,
        "rejected_at": decided_at.isoformat(),
    }
    waiting = (
        (
            await session.execute(
                select(DeploymentStage).where(
                    DeploymentStage.deployment_id == deployment.id,
                    DeploymentStage.status == DeploymentStageStatus.PENDING.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for pending in waiting:
        pending.status = DeploymentStageStatus.SKIPPED.value
        pending.finished_at = decided_at
        pending.log = "Skipped: approval was not given."

    deployment.status = DeploymentStatus.HALTED.value
    deployment.finished_at = decided_at
    if deployment.started_at is not None:
        deployment.duration_seconds = max(
            0, int(round((decided_at - deployment.started_at).total_seconds()))
        )
    deployment.updated_by = actor
    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="deployment.reject",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=reason,
        metadata={"approval_request_id": row.id, "request_status": row.status},
        request=request,
    )
    return None


async def settle_decided_gates(session: AsyncSession, principal: Principal) -> list[str]:
    """Releases still parked on a request that has already been closed.

    Requests decided in this queue before decisions reached the deployment left
    exactly that behind, and nothing else ever looks at a parked gate again.
    Safe to call repeatedly: a gate that is not open is left alone. Returns the
    deployments to resume once the caller has committed.
    """
    parked = (
        (
            await session.execute(
                select(ApprovalRequest)
                .join(
                    Deployment,
                    and_(
                        Deployment.approval_request_id == ApprovalRequest.id,
                        Deployment.workspace_id == ApprovalRequest.workspace_id,
                    ),
                )
                .where(
                    ApprovalRequest.workspace_id == principal.workspace_id,
                    ApprovalRequest.status.in_(
                        (
                            ApprovalStatus.APPROVED.value,
                            ApprovalStatus.REJECTED.value,
                            ApprovalStatus.EXPIRED.value,
                        )
                    ),
                    Deployment.status.in_(
                        (DeploymentStatus.QUEUED.value, DeploymentStatus.RUNNING.value)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    resume: list[str] = []
    for row in parked:
        approved = row.status == ApprovalStatus.APPROVED.value
        note = row.decision_note
        if note is None and not approved:
            note = f"Request {row.request_ref} was {row.status.lower()}."
        deployment_id = await _settle_gate(session, principal, row, approved=approved, note=note)
        if deployment_id is not None:
            resume.append(deployment_id)
    return resume


# ---------------------------------------------------------------------------
# Requests: write
# ---------------------------------------------------------------------------


async def _next_request_ref(session: AsyncSession, workspace_id: str) -> str:
    """Allocate the next APR-<year>-NNNNN reference for this workspace."""
    prefix = f"{REQUEST_REF_PREFIX}-{_now().year}-"
    # Only references of the allocated shape count. max() over everything that
    # merely starts with the prefix let one hand-written reference with a
    # non-numeric tail reset the sequence.
    candidates = (
        (
            await session.execute(
                select(ApprovalRequest.request_ref)
                .where(
                    ApprovalRequest.workspace_id == workspace_id,
                    ApprovalRequest.request_ref.like(f"{prefix}{'_' * REQUEST_REF_DIGITS}"),
                )
                .order_by(ApprovalRequest.request_ref.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    highest = next((ref for ref in candidates if ref[len(prefix) :].isdigit()), None)
    sequence = int(highest[len(prefix) :]) + 1 if highest else 1
    return f"{prefix}{sequence:0{REQUEST_REF_DIGITS}d}"


def _assert_reference_not_reserved(reference: str) -> None:
    """A caller-supplied reference may not squat in an allocator's namespace."""
    if reference.upper().startswith(RESERVED_REF_PREFIXES) and not ALLOCATED_REF.match(reference):
        raise ValidationFailed(
            "References starting APR- or REQ- are allocated by the system; "
            "choose a different prefix, or omit it to have one allocated.",
            details={"field": "request_ref", "value": reference},
        )


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
    session: AsyncSession,
    principal: Principal,
    user_id: str,
    *,
    field: str = "escalate_to_user_id",
    who: str = "reviewer",
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
            f"That {who} is not a member of this workspace.",
            details={"field": field, "value": user_id},
        )


async def _requester_for(
    session: AsyncSession, principal: Principal, claimed: str | None
) -> str | None:
    """Whose name a new request is raised in.

    Four eyes compares the decider with the requester, so the requester cannot
    be whatever the request body says. A signed-in person raises in their own
    name, always. A key has no name of its own, so an integration may say which
    member it is asking for -- and that has to be a member, not any 36
    characters that happen not to be the approver's id.
    """
    if principal.kind == "user":
        if claimed and claimed != principal.user_id:
            raise ValidationFailed(
                "A request is raised in your own name; it cannot name another requester.",
                details={"field": "requested_by_user_id", "value": claimed},
            )
        return principal.user_id
    if claimed:
        await _assert_workspace_member(
            session, principal, claimed, field="requested_by_user_id", who="requester"
        )
    return claimed


def _raised_by(row: ApprovalRequest) -> set[str]:
    """Everyone who may not decide ``row``: its requester and whoever filed it."""
    opened = (row.workflow or [{}])[0]
    filed_by = opened.get("raised_by_user_id") if isinstance(opened, dict) else None
    return {user_id for user_id in (row.requested_by_user_id, filed_by) if user_id}


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

    requested_by = await _requester_for(session, principal, data.requested_by_user_id)

    if data.request_ref:
        _assert_reference_not_reserved(data.request_ref)
        clash = (
            await session.execute(
                select(ApprovalRequest.id).where(
                    ApprovalRequest.workspace_id == principal.workspace_id,
                    ApprovalRequest.request_ref == data.request_ref,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"Request reference '{data.request_ref}' is already in use.")

    # The rule this request falls under, if there is one. A rule used to be
    # stored and then read by nothing: it named approvers nobody was shown and
    # an SLA no request was ever given. This is where it takes effect -- the
    # request is filed under the rule, runs on the rule's clock unless the
    # caller set its own, and says who the rule wants to decide it.
    rule = await _rule_for(session, principal, data)
    rule_body = (rule.rules or {}) if rule is not None else {}
    sla_minutes = data.sla_minutes if data.sla_minutes is not None else _rule_sla(rule_body)

    now = _now()
    window = sla_window_for(data.risk.value, sla_minutes)
    actor = _actor_name(principal)

    workflow = default_workflow()
    workflow[0].update(step=ApprovalStep.REQUESTED.value, done=True, by=actor, ts=now.isoformat())
    approvers = _rule_approvers(rule_body)
    if approvers:
        # On the review rung, because that is whose rung it is. Not in the
        # payload: that is handed back verbatim as the action to carry out.
        workflow[1]["approvers"] = approvers
    # Who actually filed it, next to the name it was filed in: the decision
    # guard reads both, so "raised on behalf of" is not a way round four eyes.
    if principal.user_id:
        workflow[0]["raised_by_user_id"] = principal.user_id
    if principal.api_key_id:
        workflow[0]["raised_by_api_key_id"] = principal.api_key_id

    # The reference is max()+1, read without a lock, so two workers raising a
    # request in the same instant pick the same one and the unique constraint
    # refuses the second. That used to surface as a 500. The insert runs in a
    # savepoint so losing the race costs a re-read, not the transaction.
    row: ApprovalRequest | None = None
    for _attempt in range(REF_ALLOCATION_ATTEMPTS):
        reference = data.request_ref or await _next_request_ref(
            session, principal.workspace_id
        )
        candidate = ApprovalRequest(
            workspace_id=principal.workspace_id,
            request_ref=reference,
            agent_id=data.agent_id,
            source=data.source or SOURCE_SCREEN,
            action=data.action,
            action_detail=data.action_detail,
            resource=data.resource,
            risk=data.risk.value,
            policy_id=rule.id if rule is not None else data.policy_id,
            reason=data.reason,
            requested_by_user_id=requested_by,
            requested_at=now,
            sla_due_at=now + window,
            sla_label=format_duration(window.total_seconds()),
            status=ApprovalStatus.PENDING.value,
            payload=data.payload,
            impact=data.impact.model_dump(exclude_none=True),
            workflow=workflow,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
        except IntegrityError as exc:
            if data.request_ref:
                raise Conflict(
                    f"Request reference '{data.request_ref}' is already in use."
                ) from exc
            continue
        row = candidate
        break
    if row is None:
        raise Conflict(
            "A request reference could not be allocated because other requests were "
            "being raised at the same moment. Try again."
        )

    metadata: dict[str, Any] = {"risk": data.risk.value, "sla_due_at": row.sla_due_at.isoformat()}
    if rule is not None:
        metadata.update(rule_id=rule.id, rule=rule.name, approvers=approvers)

    await audit.record(
        session,
        principal=principal,
        action="Approval requested",
        entity_type=ENTITY_REQUEST,
        entity_id=row.id,
        entity_label=reference,
        source_screen=SOURCE_SCREEN,
        detail=f"{data.action} — {data.resource or 'no resource'} ({data.risk.value} risk)",
        metadata=metadata,
        request=request,
    )
    if rule is not None:
        # After the audit row, as ``_decide`` and the sweeper do it: the audit
        # turn first and the policy row second, so two requests that need both
        # ask for them in the same order and cannot hold one each.
        await _rule_fired(session, rule, now)
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
    row = await _load_request(session, principal, request_id, for_update=True)

    if not row.is_open:
        raise Conflict(
            f"Request {row.request_ref} was already {row.status.lower()} and can no longer "
            "be edited.",
            details={"status": row.status},
        )
    # "Optional" on the update model means "may be left out", not "may be
    # cleared": these columns are NOT NULL, and an explicit null used to reach
    # the database (or the read model) and come back as a 500.
    for field in ("action", "payload"):
        if field in data.model_fields_set and getattr(data, field) is None:
            raise ValidationFailed(
                f"'{field}' cannot be cleared; leave it out to keep the current value.",
                details={"field": field},
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
) -> tuple[ApprovalDecisionResponse, str | None]:
    """The single write path for approve, reject and escalate.

    Returns the decision and, when the request gated a release that may now
    proceed, the id of the deployment to resume once this transaction commits.
    """
    principal.require(Role.APPROVER)
    row = await _load_request(session, principal, request_id, for_update=True)
    _assert_transition(row, target)

    # Separation of duties: the four-eyes gate is the whole point of this
    # screen, so raising a request and deciding it must be two people.
    # Escalating your own request is allowed - that is asking for eyes, not
    # supplying them.
    if target is not ApprovalStatus.ESCALATED:
        # ...and they must be *people*. A key has no user id, so every
        # comparison below would wave it through: an agent holding an
        # admin-scope key could raise its own human-in-the-loop gate and open it.
        if principal.kind != "user" or principal.user_id is None:
            raise PermissionDenied("Approvals must be decided by a signed-in person.")
        if principal.user_id in _raised_by(row):
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

    # An escalation leaves the request open, so the release it gates keeps waiting.
    resume_deployment_id = None
    if target is not ApprovalStatus.ESCALATED:
        await _rule_outcomes(session, principal.workspace_id, {row.policy_id})
        resume_deployment_id = await _settle_gate(
            session,
            principal,
            row,
            approved=target is ApprovalStatus.APPROVED,
            note=cleaned_note,
            request=request,
        )

    hydrated = (await _hydrate_requests(session, [row]))[0]
    follow_on = (
        follow_on_from_payload(row.payload) if target is ApprovalStatus.APPROVED else None
    )
    message = f"Request {row.request_ref} {verb}."
    if follow_on is not None:
        message = f"{message} Carry out '{follow_on.action}' under this approval."
    decision = ApprovalDecisionResponse(message=message, request=hydrated, follow_on=follow_on)
    return decision, resume_deployment_id


async def approve_request(
    session: AsyncSession,
    principal: Principal,
    request_id: str,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> tuple[ApprovalDecisionResponse, str | None]:
    """Approve a request and hand back whatever it unlocks.

    The second value is the deployment to resume when the request was a release
    gate; the route schedules the pipeline after it has committed.
    """
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
    """Reject a request. The reason is mandatory and is quoted in the audit row.

    A rejected release gate halts its deployment in the same transaction.
    """
    decision, _nothing_to_resume = await _decide(
        session,
        principal,
        request_id,
        target=ApprovalStatus.REJECTED,
        note=note,
        request=request,
    )
    return decision


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
    decision, _nothing_to_resume = await _decide(
        session,
        principal,
        request_id,
        target=ApprovalStatus.ESCALATED,
        note=note,
        escalate_to_user_id=escalate_to_user_id,
        request=request,
    )
    return decision


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
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.workspace_id == principal.workspace_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                    ApprovalRequest.sla_due_at.is_not(None),
                    ApprovalRequest.sla_due_at < now,
                )
                # A row somebody is deciding this instant is theirs: skip it
                # rather than wait, and let the next tick find it still
                # Pending if they did not. The predicate is re-checked under
                # the lock, so a row decided a moment ago is not expired over.
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
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
        # A release whose gate nobody opened in time does not go out, and does
        # not sit on its environment for ever either: it is halted, like a
        # rejection, and says why.
        await _settle_gate(
            session,
            principal,
            row,
            approved=False,
            note=f"Approval SLA elapsed: {row.request_ref} expired with no decision.",
            request=request,
        )
    await session.flush()
    await _rule_outcomes(session, principal.workspace_id, {row.policy_id for row in overdue})
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
# A rule lives in ``policies`` under the "Approval & Escalation" category, so
# the Policy Center lists it beside every other control. It does not raise
# requests -- nothing on the telemetry path can, because the action a request
# gates has not happened yet when telemetry about it would arrive. A rule takes
# effect when a request is *raised*: ``create_request`` files the request under
# the rule it falls under, gives it the rule's SLA and names the rule's
# approvers on it (``_rule_for``). A rule body has no ``conditions``, which is
# what keeps the ingest path from ever evaluating one.
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


def _rule_approvers(body: dict[str, Any]) -> list[str]:
    approvers = body.get("approvers") or []
    if not isinstance(approvers, list):
        approvers = [approvers]
    return [str(item) for item in approvers if item]


def _rule_sla(body: dict[str, Any]) -> int | None:
    """A rule's SLA in minutes, if it holds a usable one.

    The body is JSON the Policy Center can also edit, so it is read defensively:
    a window that is not a positive whole number is no window.
    """
    minutes = body.get("sla_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return None
    return minutes


def _named_approvers(row: ApprovalRequest) -> list[str]:
    """Who the rule behind ``row`` wants to decide it; empty when no rule applied."""
    steps = row.workflow if isinstance(row.workflow, list) else []
    review = steps[1] if len(steps) > 1 and isinstance(steps[1], dict) else {}
    return _rule_approvers(review)


def _stated_amount(data: ApprovalRequestCreate) -> float | None:
    """The money a request says it moves, or None when it does not say.

    Read from where callers already put it: ``amount`` in the payload (or in the
    parameters the payload replays), else the decision card's financial tile.
    """
    stated: list[Any] = [data.payload.get("amount")]
    for key in FOLLOW_ON_PARAM_KEYS:
        nested = data.payload.get(key)
        if isinstance(nested, dict):
            stated.append(nested.get("amount"))
    stated.append(data.impact.financial)

    for value in stated:
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            found = _AMOUNT.search(value)
            if found:
                return float(found.group(1).replace(",", ""))
    return None


def _rule_applies(rule: Policy, data: ApprovalRequestCreate, amount: float | None) -> bool:
    """Whether a request falls under ``rule``, judged only on what the request says.

    Scope first. A request carries an agent and nothing else a scope could be
    checked against, so a Global rule reaches every request, an Agent rule
    reaches that agent's, and an Environment or Connector rule applies only to a
    request that names it as its ``policy_id``.

    Then the trigger. The financial trigger is measurable: the request's amount
    against the rule's threshold. The other three describe a kind of action, and
    a request falls under one when it says so -- its ``action``, or a ``trigger``
    in its payload, is the trigger's own label. Guessing from words in a
    free-text action would put requests on the wrong clock in front of the
    wrong people.
    """
    if rule.scope == PolicyScope.AGENT.value:
        if not data.agent_id or data.agent_id != rule.scope_ref:
            return False
    elif rule.scope != PolicyScope.GLOBAL.value:
        return False

    body = rule.rules or {}
    trigger = body.get("trigger")
    if not isinstance(trigger, str) or not trigger:
        return False
    said = any(
        isinstance(value, str) and value.strip().casefold() == trigger.casefold()
        for value in (data.action, data.payload.get("trigger"))
    )
    if trigger != ApprovalTrigger.FINANCIAL_THRESHOLD.value:
        return said

    if amount is None:
        return said
    threshold = body.get("threshold_amount")
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        # No threshold set: every financial action is above it.
        return True
    return amount > float(threshold)


async def _rule_for(
    session: AsyncSession, principal: Principal, data: ApprovalRequestCreate
) -> Policy | None:
    """The approval rule in force that a new request falls under, if any.

    A request that names its policy is taken at its word: if that policy is a
    rule in force it is the rule, and if it is some other policy no rule is
    looked for -- the caller has said what demanded the approval. Otherwise the
    rules in force are tried, the agent's own before the workspace's, the
    tightest SLA before a looser one, the oldest before a newer one.
    """
    in_force = _rule_query(principal).where(Policy.status.in_(RULE_IN_FORCE))
    if data.policy_id:
        return (
            await session.execute(in_force.where(Policy.id == data.policy_id))
        ).scalar_one_or_none()

    rules = (
        (
            await session.execute(
                in_force.order_by(Policy.created_at.asc(), Policy.id.asc()).limit(
                    MAX_RULES_MATCHED
                )
            )
        )
        .scalars()
        .all()
    )
    amount = _stated_amount(data)
    applicable = [rule for rule in rules if _rule_applies(rule, data, amount)]
    if not applicable:
        return None
    return min(
        applicable,
        key=lambda rule: (
            rule.scope != PolicyScope.AGENT.value,
            _rule_sla(rule.rules or {}) or float("inf"),
        ),
    )


async def _rule_fired(session: AsyncSession, rule: Policy, when: dt.datetime) -> None:
    """Count a request against the rule it was filed under.

    An increment, so two workers raising at once do not lose each other's
    count, and ``updated_at`` named and set to itself: a rule firing is not an
    edit to it, and must not fail the save of whoever has its form open (see
    :func:`db.base.stamp`). The scheduled rollup recounts the window exactly;
    this keeps the number true in between.
    """
    await session.execute(
        sa_update(Policy)
        .where(Policy.id == rule.id)
        .values(
            requests_30d=Policy.requests_30d + 1,
            last_triggered_at=when,
            updated_at=Policy.updated_at,
        )
        .execution_options(synchronize_session=False)
    )


async def _rule_outcomes(
    session: AsyncSession, workspace_id: str, policy_ids: set[str | None]
) -> None:
    """Recount "Approved %" for the policies whose requests were just closed.

    The same whole-percentage arithmetic as ``policies.refresh_rollups``, for
    the one or two rows a decision touches rather than the table, and written
    the same way: not an edit, so ``updated_at`` stays where it was.
    """
    closed = {policy_id for policy_id in policy_ids if policy_id}
    if not closed:
        return
    since = _now() - SUMMARY_WINDOW

    def counted(*extra: Any) -> Any:
        return (
            select(func.count(ApprovalRequest.id))
            .where(
                ApprovalRequest.policy_id == Policy.id,
                ApprovalRequest.requested_at >= since,
                *extra,
            )
            .scalar_subquery()
        )

    requests = counted()
    approved = counted(ApprovalRequest.status == ApprovalStatus.APPROVED.value)
    await session.execute(
        sa_update(Policy)
        .where(Policy.workspace_id == workspace_id, Policy.id.in_(closed))
        .values(
            requests_30d=requests,
            approved_pct=case(
                (requests > 0, (approved * 200 + requests) // (requests * 2)), else_=0
            ),
            updated_at=Policy.updated_at,
        )
        .execution_options(synchronize_session=False)
    )


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
    """Create a rule. Requests raised from now on that fall under it are filed
    under it, run on its SLA and name its approvers; it raises none itself."""
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
        detail=(
            f"Rule '{name}' removed; new requests no longer take their SLA or "
            "approvers from it"
        ),
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
    """Replay the hash chain for this workspace and report where it breaks.

    Served from one shared replay per workspace for a short, hard-expiring
    interval (``audit_verify_cache_seconds``): the answer can be that much out
    of date, which is the price of the endpoint not costing a full read of the
    trail every time a tab is opened.
    """
    result = await audit.verify_chain_shared(principal.workspace_id)
    return AuditChainStatus.model_validate(result)
