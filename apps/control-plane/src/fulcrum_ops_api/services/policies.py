"""Policy Center business logic.

Everything the Policy Center can do to a governance policy lives here: the
filtered list behind the table, the rule-body validation that stops an
uninterpretable policy reaching the enforcement path, the lifecycle verbs
(activate, deactivate, clone), bulk import, and the KPI aggregates.

Three rules hold throughout:

* **Tenancy.** Every statement filters on ``principal.workspace_id``. A policy
  belonging to another workspace is reported as missing, never as forbidden —
  a 403 would confirm the id exists.
* **Authority.** Policy edits require :attr:`Role.ADMIN`. Operators run and
  deploy agents but do not rewrite the rules they are governed by, so an API
  key — which tops out at operator — can read policies but never change one.
* **Evidence.** Every state change writes an audit row in the same transaction
  as the change itself, so the two commit or roll back together.

The rollup columns on ``policies`` (``violations_30d``, ``blocked_30d``,
``requests_30d``, ``approved_pct``) belong to the governance aggregation job
and are never written here. The ``applies_*`` reach counters *are* refreshed on
write, because scope is what changes them and a write is the one moment the new
reach is known — that is a single COUNT on an admin action, not per page load.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, case, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..models.governance import (
    Policy,
    PolicyBinding,
    PolicyCategory,
    PolicyEnforcement,
    PolicyScope,
    PolicyStatus,
    PolicyViolation,
    RiskLevel,
    ViolationSeverity,
)
from ..models.identity import Role
from ..models.registry import Agent, AgentConnector, Connector, EnvironmentType
from ..schemas.policies import (
    PolicyAction,
    PolicyCondition,
    PolicyCreate,
    PolicyImportIssue,
    PolicyImportRequest,
    PolicyImportResult,
    PolicyRules,
    PolicySummary,
    PolicyUpdate,
)
from . import audit

SOURCE_SCREEN: Final[str] = "Policy Center"
ENTITY_TYPE: Final[str] = "policy"

DEFAULT_WINDOW_DAYS: Final[int] = 30

#: Hard ceiling on an export so one click cannot pull an unbounded result set
#: into memory. Well above any real workspace's policy count.
EXPORT_LIMIT: Final[int] = 5_000

COPY_SUFFIX: Final[str] = " (Copy)"
MAX_COPY_ATTEMPTS: Final[int] = 50
NAME_MAX_LENGTH: Final[int] = 160

#: Grounding score below which a guardrail policy fires, used only to seed the
#: opening rule of a policy created without an explicit rule body.
DEFAULT_SAFETY_THRESHOLD: Final[float] = 0.82

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_SEVERITY_FOR_RISK: Final[dict[RiskLevel, ViolationSeverity]] = {
    RiskLevel.LOW: ViolationSeverity.LOW,
    RiskLevel.MEDIUM: ViolationSeverity.MEDIUM,
    RiskLevel.HIGH: ViolationSeverity.HIGH,
}

#: Column keys the table's sort headers send. Both the console's key and the
#: underlying column name are accepted so a link built by hand still works.
SORTABLE: Final[dict[str, InstrumentedAttribute]] = {
    "name": Policy.name,
    "category": Policy.category,
    "scope": Policy.scope_label,
    "risk": Policy.risk_level,
    "risk_level": Policy.risk_level,
    "status": Policy.status,
    "enforcement": Policy.enforcement,
    "modified": Policy.updated_at,
    "updated_at": Policy.updated_at,
    "created_at": Policy.created_at,
    "violations_30d": Policy.violations_30d,
    "blocked_30d": Policy.blocked_30d,
    "last_triggered_at": Policy.last_triggered_at,
}

SEARCHABLE: Final[tuple[InstrumentedAttribute, ...]] = (
    Policy.name,
    Policy.description,
    Policy.category,
    Policy.scope_label,
    Policy.version,
)

VIOLATION_SORTABLE: Final[dict[str, InstrumentedAttribute]] = {
    "occurred_at": PolicyViolation.occurred_at,
    "severity": PolicyViolation.severity,
    "action": PolicyViolation.action_taken,
    "action_taken": PolicyViolation.action_taken,
    "resolved_at": PolicyViolation.resolved_at,
    "policy": Policy.name,
    "agent": Agent.name,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _window_start(window_days: int) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=window_days)


def _next_version(current: str | None) -> str:
    """Bump the minor version. A rule-body change is a new version of the policy."""
    match = _VERSION_RE.match(current or "")
    if match is None:
        return current or "v1.0.0"
    major, minor, _patch = (int(part) for part in match.groups())
    return f"v{major}.{minor + 1}.0"


def _coerce_scope(value: str) -> PolicyScope:
    try:
        return PolicyScope(value)
    except ValueError as exc:
        raise ValidationFailed(
            "This policy's stored scope is not recognised; send 'scope' explicitly.",
            details={"scope": value, "allowed": [s.value for s in PolicyScope]},
        ) from exc


def _scope_clause(scope: str, scope_ref: str | None) -> ColumnElement[bool]:
    """Predicate selecting the agents a policy reaches through its scope alone."""
    if scope == PolicyScope.GLOBAL.value:
        return true()
    if not scope_ref:
        return false()
    if scope == PolicyScope.AGENT.value:
        return Agent.id == scope_ref
    if scope == PolicyScope.ENVIRONMENT.value:
        return Agent.environment == scope_ref
    if scope == PolicyScope.CONNECTOR.value:
        return Agent.id.in_(
            select(AgentConnector.agent_id).where(AgentConnector.connector_id == scope_ref)
        )
    return false()


def _starter_rules(
    *, category: PolicyCategory, risk_level: RiskLevel, enforcement: PolicyEnforcement
) -> PolicyRules:
    """Opening rule body for a policy created without one.

    The create form asks for category, risk and enforcement before the rule
    editor is ever opened. Those three answers describe exactly one condition,
    so the policy starts with that condition rather than with an empty body
    that would match nothing while claiming to be enforced.
    """
    if category is PolicyCategory.GUARDRAILS:
        condition = PolicyCondition(
            signal="safety_score", operator="lt", value=DEFAULT_SAFETY_THRESHOLD
        )
    else:
        condition = PolicyCondition(
            signal="action_risk", operator="gte", value=risk_level.value
        )
    return PolicyRules(
        conditions=[condition],
        action=PolicyAction(mode=enforcement),
        severity=_SEVERITY_FOR_RISK[risk_level],
    )


async def _name_taken(
    session: AsyncSession, workspace_id: str, name: str, *, exclude_id: str | None = None
) -> bool:
    stmt = select(func.count(Policy.id)).where(
        Policy.workspace_id == workspace_id,
        func.lower(Policy.name) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Policy.id != exclude_id)
    return bool((await session.execute(stmt)).scalar_one())


async def _assert_name_available(
    session: AsyncSession, workspace_id: str, name: str, *, exclude_id: str | None = None
) -> None:
    if await _name_taken(session, workspace_id, name, exclude_id=exclude_id):
        raise Conflict(
            f"A policy named '{name}' already exists in this workspace.",
            details={"field": "name"},
        )


async def _copy_name(session: AsyncSession, workspace_id: str, source_name: str) -> str:
    """``X`` becomes ``X (Copy)``, then ``X (Copy 2)`` once that is taken."""
    headroom = NAME_MAX_LENGTH - len(COPY_SUFFIX) - 4
    base = source_name[:headroom].rstrip() if len(source_name) > headroom else source_name
    candidate = f"{base}{COPY_SUFFIX}"
    attempt = 2
    while await _name_taken(session, workspace_id, candidate):
        candidate = f"{base} (Copy {attempt})"
        attempt += 1
        if attempt > MAX_COPY_ATTEMPTS:
            raise Conflict(
                "Too many copies of this policy already exist; rename one before cloning again."
            )
    return candidate


async def _resolve_scope(
    session: AsyncSession,
    principal: Principal,
    *,
    scope: PolicyScope,
    scope_ref: str | None,
    scope_label: str | None,
) -> tuple[str, str | None, str]:
    """Validate the scope target and derive the label the table renders.

    A scope pointing at an agent or connector that does not exist in this
    workspace is a bad request, not a missing policy, so it fails as a field
    validation error and never reveals whether that id exists elsewhere.
    """
    workspace_id = principal.workspace_id

    if scope is PolicyScope.GLOBAL:
        return scope.value, None, (scope_label or "All Agents")

    ref = (scope_ref or "").strip()
    if not ref:
        raise ValidationFailed(
            f"A {scope.value} scope needs a scope_ref naming its target.",
            details={"field": "scope_ref"},
        )

    if scope is PolicyScope.AGENT:
        row = (
            await session.execute(
                select(Agent.id, Agent.name).where(
                    Agent.id == ref, Agent.workspace_id == workspace_id
                )
            )
        ).first()
        if row is None:
            raise ValidationFailed(
                "No agent with that id exists in this workspace.",
                details={"field": "scope_ref", "value": ref},
            )
        return scope.value, row.id, (scope_label or row.name)

    if scope is PolicyScope.CONNECTOR:
        row = (
            await session.execute(
                select(Connector.id, Connector.name).where(
                    Connector.id == ref, Connector.workspace_id == workspace_id
                )
            )
        ).first()
        if row is None:
            raise ValidationFailed(
                "No connector with that id exists in this workspace.",
                details={"field": "scope_ref", "value": ref},
            )
        return scope.value, row.id, (scope_label or row.name)

    # Environment scope targets the environment class an agent is deployed to,
    # which is the vocabulary stored on ``agents.environment``.
    allowed = {env.value for env in EnvironmentType}
    if ref not in allowed:
        raise ValidationFailed(
            "Unknown environment.",
            details={"field": "scope_ref", "value": ref, "allowed": sorted(allowed)},
        )
    return scope.value, ref, (scope_label or f"{ref} Only")


async def _reach(
    session: AsyncSession, workspace_id: str, scope: str, scope_ref: str | None
) -> tuple[int, int, int]:
    """Count the agents, environments and connectors a scope reaches.

    Two aggregate statements, run only on the write path. ``applies_tools`` is
    deliberately not computed: there is no tool inventory in the control plane,
    so that counter stays with the aggregation job that owns it.
    """
    clause = _scope_clause(scope, scope_ref)

    agents, environments = (
        await session.execute(
            select(
                func.count(func.distinct(Agent.id)),
                func.count(func.distinct(Agent.environment)),
            ).where(Agent.workspace_id == workspace_id, clause)
        )
    ).one()

    if scope == PolicyScope.CONNECTOR.value:
        connectors = 1 if scope_ref else 0
    else:
        connectors = (
            await session.execute(
                select(func.count(func.distinct(AgentConnector.connector_id)))
                .select_from(AgentConnector)
                .join(Agent, Agent.id == AgentConnector.agent_id)
                .where(Agent.workspace_id == workspace_id, clause)
            )
        ).scalar_one()

    return int(agents), int(environments), int(connectors)


async def _bound_agent_count(session: AsyncSession, policy: Policy) -> int:
    """Agents explicitly bound to the policy, ignoring scope-wide matches."""
    stmt = select(func.count(PolicyBinding.id)).where(
        PolicyBinding.policy_id == policy.id,
        PolicyBinding.workspace_id == policy.workspace_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _agents_affected(session: AsyncSession, policy: Policy) -> int:
    """Distinct agents that gain or lose enforcement when this policy flips.

    The union of the explicit bindings and the scope match, counted in SQL so a
    policy scoped to every agent costs the same as one scoped to a single agent.
    """
    bound = select(PolicyBinding.agent_id).where(
        PolicyBinding.policy_id == policy.id,
        PolicyBinding.workspace_id == policy.workspace_id,
    )
    stmt = select(func.count(func.distinct(Agent.id))).where(
        Agent.workspace_id == policy.workspace_id,
        or_(Agent.id.in_(bound), _scope_clause(policy.scope, policy.scope_ref)),
    )
    return int((await session.execute(stmt)).scalar_one())


async def _get(session: AsyncSession, principal: Principal, policy_id: str) -> Policy:
    """Load one policy inside the caller's workspace, or report it missing."""
    stmt = select(Policy).where(
        Policy.id == policy_id, Policy.workspace_id == principal.workspace_id
    )
    policy = (await session.execute(stmt)).scalar_one_or_none()
    if policy is None:
        raise NotFound(f"No policy with id '{policy_id}'.")
    return policy


def _assert_enforceable(policy: Policy) -> None:
    conditions = (policy.rules or {}).get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise PreconditionFailed(
            "This policy has no conditions to evaluate. Add at least one rule before "
            "activating it.",
            details={"policy_id": policy.id},
        )


def _check_optimistic_lock(policy: Policy, expected: dt.datetime | None) -> None:
    if expected is None:
        return
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=dt.UTC)
    current = policy.updated_at
    if current is None:
        return
    # One second of slack absorbs sub-second rounding in transit; anything
    # larger means somebody else saved between the read and this write.
    if abs((current - expected).total_seconds()) > 1:
        raise Conflict(
            "This policy changed since you loaded it. Reload and reapply your edit.",
            details={
                "updated_at": current.isoformat(),
                "expected_updated_at": expected.isoformat(),
            },
        )


def _filtered_stmt(
    principal: Principal,
    params: ListParams,
    *,
    status: Sequence[str] | None,
    category: Sequence[str] | None,
    scope: str | None,
    risk: Sequence[str] | None,
    enforcement: Sequence[str] | None,
    owner_user_id: str | None,
) -> Select[tuple[Policy]]:
    """The statement behind both the table and the export, filters included."""
    stmt = select(Policy).where(Policy.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            Policy.status: list(status) if status else None,
            Policy.category: list(category) if category else None,
            Policy.risk_level: list(risk) if risk else None,
            Policy.enforcement: list(enforcement) if enforcement else None,
            Policy.owner_user_id: owner_user_id,
        },
    )
    if scope:
        # The console's Scope dropdown is built from whichever vocabulary the
        # workspace uses, so accept either the scope class ("Agent") or the
        # rendered label ("Finance Agents").
        stmt = stmt.where(or_(Policy.scope == scope, Policy.scope_label == scope))
    stmt = apply_search(stmt, params, SEARCHABLE)
    return apply_sort(stmt, params, SORTABLE, Policy.updated_at)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_policies(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: Sequence[str] | None = None,
    category: Sequence[str] | None = None,
    scope: str | None = None,
    risk: Sequence[str] | None = None,
    enforcement: Sequence[str] | None = None,
    owner_user_id: str | None = None,
) -> tuple[Sequence[Policy], int]:
    """One page of policies plus the total matching the same filters."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        category=category,
        scope=scope,
        risk=risk,
        enforcement=enforcement,
        owner_user_id=owner_user_id,
    )
    return await paginate(session, stmt, params)


async def get_policy(session: AsyncSession, principal: Principal, policy_id: str) -> Policy:
    """One policy, or :class:`NotFound` if it is missing or owned elsewhere."""
    return await _get(session, principal, policy_id)


async def export_policies(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: Sequence[str] | None = None,
    category: Sequence[str] | None = None,
    scope: str | None = None,
    risk: Sequence[str] | None = None,
    enforcement: Sequence[str] | None = None,
    owner_user_id: str | None = None,
) -> Sequence[Policy]:
    """Every policy matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        category=category,
        scope=scope,
        risk=risk,
        enforcement=enforcement,
        owner_user_id=owner_user_id,
    ).limit(EXPORT_LIMIT)
    return (await session.execute(stmt)).scalars().all()


async def list_violations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    policy_id: str | None = None,
    agent_id: str | None = None,
    severity: Sequence[str] | None = None,
    action: Sequence[str] | None = None,
    resolved: bool | None = None,
    window_days: int | None = None,
) -> tuple[list[tuple[PolicyViolation, str | None, str | None]], int]:
    """Violations across the workspace, each paired with its policy and agent name.

    The names are resolved in two lookups over the ids on the page rather than
    by joining every row into Python, so the page cost does not grow with the
    size of the violation table.
    """
    stmt = (
        select(PolicyViolation)
        .outerjoin(Policy, Policy.id == PolicyViolation.policy_id)
        .outerjoin(Agent, Agent.id == PolicyViolation.agent_id)
        .where(PolicyViolation.workspace_id == principal.workspace_id)
    )
    stmt = apply_filters(
        stmt,
        {
            PolicyViolation.policy_id: policy_id,
            PolicyViolation.agent_id: agent_id,
            PolicyViolation.severity: list(severity) if severity else None,
            PolicyViolation.action_taken: list(action) if action else None,
        },
    )
    if resolved is True:
        stmt = stmt.where(PolicyViolation.resolved_at.is_not(None))
    elif resolved is False:
        stmt = stmt.where(PolicyViolation.resolved_at.is_(None))
    if window_days:
        stmt = stmt.where(PolicyViolation.occurred_at >= _window_start(window_days))

    stmt = apply_search(stmt, params, (PolicyViolation.trace_id, Policy.name, Agent.name))
    stmt = apply_sort(stmt, params, VIOLATION_SORTABLE, PolicyViolation.occurred_at)

    rows, total = await paginate(session, stmt, params)
    violations: list[PolicyViolation] = list(rows)

    policy_ids = sorted({v.policy_id for v in violations if v.policy_id})
    agent_ids = sorted({v.agent_id for v in violations if v.agent_id})

    policy_names = await _label_map(
        session,
        select(Policy.id, Policy.name).where(
            Policy.workspace_id == principal.workspace_id, Policy.id.in_(policy_ids)
        ),
        ids=policy_ids,
    )
    agent_names = await _label_map(
        session,
        select(Agent.id, Agent.name).where(
            Agent.workspace_id == principal.workspace_id, Agent.id.in_(agent_ids)
        ),
        ids=agent_ids,
    )

    paired = [
        (
            v,
            policy_names.get(v.policy_id),
            agent_names.get(v.agent_id) if v.agent_id else None,
        )
        for v in violations
    ]
    return paired, total


async def _label_map(
    session: AsyncSession, stmt: Select[tuple[str, str]], *, ids: Sequence[str]
) -> dict[str, str]:
    """Resolve id to display name for the ids on one page; skips the round trip when empty."""
    if not ids:
        return {}
    return {row.id: row.name for row in (await session.execute(stmt)).all()}


async def summary(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> PolicySummary:
    """The KPI cards, computed as two aggregate statements.

    Status counts come from ``policies``; the windowed numbers come from
    ``policy_violations`` rather than from the cached rollups on ``policies``,
    because the cards must agree with the violation feed shown beside them.
    """
    workspace_id = principal.workspace_id

    total, active, warning, inactive, pending = (
        await session.execute(
            select(
                func.count(Policy.id),
                func.count(case((Policy.status == PolicyStatus.ACTIVE.value, Policy.id))),
                func.count(case((Policy.status == PolicyStatus.WARNING.value, Policy.id))),
                func.count(case((Policy.status == PolicyStatus.INACTIVE.value, Policy.id))),
                func.count(
                    case((Policy.status == PolicyStatus.PENDING_REVIEW.value, Policy.id))
                ),
            ).where(Policy.workspace_id == workspace_id)
        )
    ).one()

    violations, blocked, distinct_policies = (
        await session.execute(
            select(
                func.count(PolicyViolation.id),
                func.count(
                    case(
                        (
                            PolicyViolation.action_taken == PolicyEnforcement.BLOCK.value,
                            PolicyViolation.id,
                        )
                    )
                ),
                func.count(func.distinct(PolicyViolation.policy_id)),
            ).where(
                PolicyViolation.workspace_id == workspace_id,
                PolicyViolation.occurred_at >= _window_start(window_days),
            )
        )
    ).one()

    return PolicySummary(
        total=int(total),
        active=int(active),
        warning=int(warning),
        inactive=int(inactive),
        pending_review=int(pending),
        active_pct=round(int(active) * 100 / int(total)) if total else 0,
        blocked_actions_30d=int(blocked),
        policies_violated_30d=int(distinct_policies),
        violations_30d=int(violations),
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_policy(
    session: AsyncSession,
    principal: Principal,
    data: PolicyCreate,
    *,
    request: Request | None = None,
) -> Policy:
    """Create a policy, validating its rule body and resolving its scope target."""
    principal.require(Role.ADMIN)
    await _assert_name_available(session, principal.workspace_id, data.name)

    scope, scope_ref, scope_label = await _resolve_scope(
        session,
        principal,
        scope=data.scope,
        scope_ref=data.scope_ref,
        scope_label=data.scope_label,
    )
    rules = data.rules or _starter_rules(
        category=data.category, risk_level=data.risk_level, enforcement=data.enforcement
    )
    agents, environments, connectors = await _reach(
        session, principal.workspace_id, scope, scope_ref
    )

    policy = Policy(
        workspace_id=principal.workspace_id,
        name=data.name,
        description=data.description,
        category=data.category.value,
        scope=scope,
        scope_ref=scope_ref,
        scope_label=scope_label,
        risk_level=data.risk_level.value,
        status=data.status.value,
        enforcement=data.enforcement.value,
        rules=rules.model_dump(mode="json"),
        version=data.version,
        owner_user_id=data.owner_user_id or principal.user_id,
        applies_agents=agents,
        applies_envs=environments,
        applies_connectors=connectors,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(policy)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Policy created",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Created as {policy.status} with {policy.enforcement} enforcement.",
        metadata={
            "category": policy.category,
            "scope": policy.scope,
            "scope_ref": policy.scope_ref,
            "risk_level": policy.risk_level,
            "applies_agents": policy.applies_agents,
        },
        request=request,
    )
    return policy


async def update_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    data: PolicyUpdate,
    *,
    request: Request | None = None,
) -> Policy:
    """Apply a partial update. A change to the rule body bumps the version."""
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)
    _check_optimistic_lock(policy, data.expected_updated_at)

    fields = data.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not fields:
        raise ValidationFailed("No fields to update.")

    changed: dict[str, Any] = {}

    if "name" in fields and data.name is not None and data.name != policy.name:
        await _assert_name_available(
            session, principal.workspace_id, data.name, exclude_id=policy.id
        )
        changed["name"] = (policy.name, data.name)
        policy.name = data.name

    if "description" in fields:
        policy.description = data.description

    for attribute, value in (
        ("category", data.category),
        ("risk_level", data.risk_level),
        ("status", data.status),
        ("enforcement", data.enforcement),
    ):
        if attribute in fields and value is not None:
            previous = getattr(policy, attribute)
            if previous != value.value:
                changed[attribute] = (previous, value.value)
            setattr(policy, attribute, value.value)

    if "owner_user_id" in fields:
        policy.owner_user_id = data.owner_user_id

    rescoped = False
    if "scope" in fields or "scope_ref" in fields:
        scope_enum = data.scope or _coerce_scope(policy.scope)
        scope_ref = data.scope_ref if "scope_ref" in fields else policy.scope_ref
        scope_label = data.scope_label if "scope_label" in fields else None
        resolved_scope, resolved_ref, resolved_label = await _resolve_scope(
            session,
            principal,
            scope=scope_enum,
            scope_ref=scope_ref,
            scope_label=scope_label,
        )
        rescoped = (resolved_scope, resolved_ref) != (policy.scope, policy.scope_ref)
        if rescoped:
            changed["scope"] = (
                f"{policy.scope}:{policy.scope_ref or '*'}",
                f"{resolved_scope}:{resolved_ref or '*'}",
            )
        policy.scope, policy.scope_ref, policy.scope_label = (
            resolved_scope,
            resolved_ref,
            resolved_label,
        )
    elif "scope_label" in fields:
        policy.scope_label = data.scope_label

    if "rules" in fields and data.rules is not None:
        rules = data.rules.model_dump(mode="json")
        if rules != policy.rules:
            policy.rules = rules
            previous_version = policy.version
            policy.version = _next_version(policy.version)
            changed["rules"] = (previous_version, policy.version)

    if rescoped:
        agents, environments, connectors = await _reach(
            session, principal.workspace_id, policy.scope, policy.scope_ref
        )
        policy.applies_agents = agents
        policy.applies_envs = environments
        policy.applies_connectors = connectors

    policy.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Policy updated",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            "Updated " + ", ".join(sorted(changed)) if changed else "Updated with no field changes."
        ),
        metadata={key: {"from": before, "to": after} for key, (before, after) in changed.items()},
        request=request,
    )
    return policy


async def delete_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Delete a policy that is no longer enforced.

    An active policy must be deactivated first: deletion cascades to the
    policy's bindings and violation history, and that history is evidence. The
    counts are copied into the audit row before the rows go.
    """
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)

    if policy.status == PolicyStatus.ACTIVE.value:
        raise PreconditionFailed(
            "Deactivate this policy before deleting it; it is still being enforced.",
            details={"policy_id": policy.id, "status": policy.status},
        )

    bound = await _bound_agent_count(session, policy)
    violation_count = (
        await session.execute(
            select(func.count(PolicyViolation.id)).where(
                PolicyViolation.policy_id == policy.id,
                PolicyViolation.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one()

    await audit.record(
        session,
        principal=principal,
        action="Policy deleted",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Deleted with {bound} binding(s) and {violation_count} recorded violation(s)."
        ),
        metadata={
            "category": policy.category,
            "scope": policy.scope,
            "status": policy.status,
            "bound_agents": bound,
            "violations": int(violation_count),
        },
        request=request,
    )

    await session.delete(policy)
    await session.flush()


async def activate_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    *,
    request: Request | None = None,
) -> tuple[Policy, int, int]:
    """Start enforcing a policy. Returns the policy, agents affected, and bindings."""
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)

    if policy.status == PolicyStatus.ACTIVE.value:
        raise Conflict(
            f"'{policy.name}' is already active.", details={"policy_id": policy.id}
        )
    _assert_enforceable(policy)

    previous = policy.status
    policy.status = PolicyStatus.ACTIVE.value
    policy.updated_by = principal.actor

    agents, environments, connectors = await _reach(
        session, principal.workspace_id, policy.scope, policy.scope_ref
    )
    policy.applies_agents = agents
    policy.applies_envs = environments
    policy.applies_connectors = connectors

    bound = await _bound_agent_count(session, policy)
    affected = await _agents_affected(session, policy)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Policy activated",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Enforcement started for {affected} agent(s).",
        metadata={
            "status": {"from": previous, "to": policy.status},
            "agents_affected": affected,
            "bound_agents": bound,
        },
        request=request,
    )
    return policy, affected, bound


async def deactivate_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> tuple[Policy, int, int]:
    """Stop enforcing a policy.

    Bindings are never severed: a bound policy deactivates like any other, and
    the caller is told how many agents lose enforcement so the console can say
    so before and after the change.
    """
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)

    if policy.status == PolicyStatus.INACTIVE.value:
        raise Conflict(
            f"'{policy.name}' is already inactive.", details={"policy_id": policy.id}
        )

    bound = await _bound_agent_count(session, policy)
    affected = await _agents_affected(session, policy)

    previous = policy.status
    policy.status = PolicyStatus.INACTIVE.value
    policy.updated_by = principal.actor
    await session.flush()

    detail = f"Enforcement stopped for {affected} agent(s)."
    if reason:
        detail = f"{detail} Reason: {reason}"

    await audit.record(
        session,
        principal=principal,
        action="Policy deactivated",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "status": {"from": previous, "to": policy.status},
            "agents_affected": affected,
            "bound_agents": bound,
            "reason": reason,
        },
        request=request,
    )
    return policy, affected, bound


async def clone_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    *,
    name: str | None = None,
    request: Request | None = None,
) -> Policy:
    """Copy a policy as an inactive draft.

    The copy carries the rule body and scope but none of the source's history:
    counters start at zero, bindings are not copied, and the version resets, so
    nothing about the draft claims to have been enforced.
    """
    principal.require(Role.ADMIN)
    source = await _get(session, principal, policy_id)

    if name:
        await _assert_name_available(session, principal.workspace_id, name)
        copy_name = name
    else:
        copy_name = await _copy_name(session, principal.workspace_id, source.name)

    agents, environments, connectors = await _reach(
        session, principal.workspace_id, source.scope, source.scope_ref
    )

    clone = Policy(
        workspace_id=principal.workspace_id,
        name=copy_name,
        description=source.description,
        category=source.category,
        scope=source.scope,
        scope_ref=source.scope_ref,
        scope_label=source.scope_label,
        risk_level=source.risk_level,
        status=PolicyStatus.INACTIVE.value,
        enforcement=source.enforcement,
        rules=copy.deepcopy(source.rules or {}),
        version="v1.0.0",
        owner_user_id=principal.user_id or source.owner_user_id,
        applies_agents=agents,
        applies_envs=environments,
        applies_connectors=connectors,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(clone)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="Policy cloned",
        entity_type=ENTITY_TYPE,
        entity_id=clone.id,
        entity_label=clone.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Cloned from '{source.name}' as an inactive draft.",
        metadata={"source_policy_id": source.id, "source_name": source.name},
        request=request,
    )
    return clone


async def import_policies(
    session: AsyncSession,
    principal: Principal,
    payload: PolicyImportRequest,
    *,
    request: Request | None = None,
) -> PolicyImportResult:
    """Load policy definitions from a file the operator uploaded.

    One malformed or colliding definition does not sink the batch: it is
    reported in ``issues`` and the rest are applied. ``on_conflict='fail'`` is
    the exception — it aborts the whole request, which rolls the transaction
    back so nothing is half-imported.
    """
    principal.require(Role.ADMIN)
    workspace_id = principal.workspace_id

    submitted = [item.name.lower() for item in payload.policies]
    existing = (
        await session.execute(
            select(Policy).where(
                Policy.workspace_id == workspace_id,
                func.lower(Policy.name).in_(submitted),
            )
        )
    ).scalars().all()
    by_name = {row.name.lower(): row for row in existing}

    result = PolicyImportResult(
        submitted=len(payload.policies), created=0, replaced=0, skipped=0
    )
    seen: set[str] = set()

    for index, item in enumerate(payload.policies):
        key = item.name.lower()
        if key in seen:
            result.skipped += 1
            result.issues.append(
                PolicyImportIssue(
                    index=index,
                    name=item.name,
                    reason="Duplicate name within the imported file.",
                )
            )
            continue
        seen.add(key)

        target = by_name.get(key)
        if target is not None and payload.on_conflict == "fail":
            raise Conflict(
                f"A policy named '{item.name}' already exists in this workspace.",
                details={"index": index, "name": item.name},
            )
        if target is not None and payload.on_conflict == "skip":
            result.skipped += 1
            result.issues.append(
                PolicyImportIssue(
                    index=index, name=item.name, reason="A policy of that name already exists."
                )
            )
            continue

        try:
            scope, scope_ref, scope_label = await _resolve_scope(
                session,
                principal,
                scope=item.scope,
                scope_ref=item.scope_ref,
                scope_label=item.scope_label,
            )
        except ValidationFailed as exc:
            result.skipped += 1
            result.issues.append(
                PolicyImportIssue(index=index, name=item.name, reason=exc.message)
            )
            continue

        rules = item.rules or _starter_rules(
            category=item.category, risk_level=item.risk_level, enforcement=item.enforcement
        )
        status = PolicyStatus.ACTIVE.value if payload.activate else item.status.value
        agents, environments, connectors = await _reach(
            session, workspace_id, scope, scope_ref
        )

        if target is None:
            policy = Policy(
                workspace_id=workspace_id,
                name=item.name,
                created_by=principal.actor,
            )
            session.add(policy)
            action = "Policy imported"
            result.created += 1
        else:
            policy = target
            action = "Policy replaced by import"
            result.replaced += 1

        policy.description = item.description
        policy.category = item.category.value
        policy.scope = scope
        policy.scope_ref = scope_ref
        policy.scope_label = scope_label
        policy.risk_level = item.risk_level.value
        policy.status = status
        policy.enforcement = item.enforcement.value
        policy.rules = rules.model_dump(mode="json")
        policy.version = item.version if target is None else _next_version(policy.version)
        policy.owner_user_id = item.owner_user_id or principal.user_id
        policy.applies_agents = agents
        policy.applies_envs = environments
        policy.applies_connectors = connectors
        policy.updated_by = principal.actor

        await session.flush()
        result.policy_ids.append(policy.id)

        await audit.record(
            session,
            principal=principal,
            action=action,
            entity_type=ENTITY_TYPE,
            entity_id=policy.id,
            entity_label=policy.name,
            source_screen=SOURCE_SCREEN,
            detail=f"Imported from '{payload.source or 'uploaded definition'}' as {status}.",
            metadata={"source": payload.source, "category": policy.category},
            request=request,
        )

    await audit.record(
        session,
        principal=principal,
        action="Policies imported",
        entity_type=ENTITY_TYPE,
        entity_label=payload.source or "Policy import",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{result.created} created, {result.replaced} replaced, "
            f"{result.skipped} skipped of {result.submitted} submitted."
        ),
        metadata={
            "source": payload.source,
            "on_conflict": payload.on_conflict,
            "policy_ids": result.policy_ids,
        },
        request=request,
    )
    return result
