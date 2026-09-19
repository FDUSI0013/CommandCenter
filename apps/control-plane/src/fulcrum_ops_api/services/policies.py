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
``requests_30d``, ``approved_pct``) are never computed per page load — counting
a busy policy's violations for every row of every table render is too slow.
They are recomputed in bulk by :func:`refresh_rollups`, which the platform
scheduler calls on its own clock. The ``applies_*`` reach counters are refreshed
on write, because a write is the one moment a new scope's reach is known, and
again by the same sweep, because agents register themselves through ingest long
after the policy that governs them was saved.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, case, delete, false, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..db.base import stamp
from ..models.governance import (
    ApprovalRequest,
    ApprovalStatus,
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
    KNOWN_SIGNALS,
    SCORE_SIGNAL_PREFIX,
    PolicyAction,
    PolicyCondition,
    PolicyCreate,
    PolicyImportIssue,
    PolicyImportRequest,
    PolicyImportResult,
    PolicyRules,
    PolicySummary,
    PolicyUpdate,
    enforced_mode,
    is_resolvable_signal,
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

_ENFORCEMENT_VALUES: Final[frozenset[str]] = frozenset(e.value for e in PolicyEnforcement)

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
    # Every header of the console's table has to be here: the table sends the
    # key of whichever one was clicked, and an unknown key is a 422 that sticks
    # until a different header is clicked.
    "version": Policy.version,
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

    It is a draft for a reviewer, which is why the callers never save it as
    enforcing (see :func:`_status_for`). The guardrail draft addresses the
    safety judge as a feedback score, because that is the only place a safety
    score reaches the enforcement path from; and both drafts fail open, so a
    trace that carries no such score is not a trace that failed it.
    """
    if category is PolicyCategory.GUARDRAILS:
        condition = PolicyCondition(
            signal=f"{SCORE_SIGNAL_PREFIX}safety_score",
            operator="lt",
            value=DEFAULT_SAFETY_THRESHOLD,
        )
    else:
        condition = PolicyCondition(
            signal="action_risk", operator="gte", value=risk_level.value
        )
    return PolicyRules(
        conditions=[condition],
        action=PolicyAction(mode=enforcement),
        severity=_SEVERITY_FOR_RISK[risk_level],
        fail_mode="open",
    )


def _status_for(requested: PolicyStatus, *, derived: bool) -> str:
    """The status a new policy is saved with.

    A policy whose rule body was derived rather than written is never saved as
    enforcing. The derived clause is a guess from three dropdowns: with Block it
    refused every trace from every agent at or above the chosen risk, the moment
    somebody pressed Create on the form's defaults. It waits, inactive, until a
    reviewer has read it and activated it on purpose.
    """
    if derived and requested not in (PolicyStatus.INACTIVE, PolicyStatus.PENDING_REVIEW):
        return PolicyStatus.INACTIVE.value
    return requested.value


def _agreed_enforcement(data: PolicyCreate) -> PolicyEnforcement:
    """The one enforcement a new policy carries, in its column and its rule body.

    The same fact is stored twice: ``policies.enforcement`` is what the table
    shows and ``rules.action.mode`` is what ingest applies. Stored apart, a
    policy can read "Log Only" while it blocks. So a definition that states both
    must state them alike, and one that leaves the field out takes the mode its
    rule body names.
    """
    if data.rules is None or data.rules.action.mode is data.enforcement:
        return data.enforcement
    if "enforcement" not in data.model_fields_set:
        return data.rules.action.mode
    raise ValidationFailed(
        f"'enforcement' says {data.enforcement.value} but the rule body's action.mode says "
        f"{data.rules.action.mode.value}. They are the same setting: make them agree, or "
        "send only one.",
        details={
            "field": "enforcement",
            "enforcement": data.enforcement.value,
            "rules.action.mode": data.rules.action.mode.value,
        },
    )


def _stored_mode(rules: Any) -> str | None:
    """``rules.action.mode`` as stored, or ``None`` when the body names none."""
    action = rules.get("action") if isinstance(rules, dict) else None
    mode = action.get("mode") if isinstance(action, dict) else None
    return mode if isinstance(mode, str) and mode else None


def _resolve_enforcement(
    policy: Policy,
    *,
    column: PolicyEnforcement | None,
    mode: PolicyEnforcement | None,
) -> tuple[str, str]:
    """Settle an edit's enforcement: what is enforced now, and what will be.

    An edit can speak through the Enforcement field, through the rule body's
    ``action.mode``, or both — and the console always sends both, the body
    untouched. Each is compared with what is enforced *now*, so whichever one
    the editor actually moved wins and the other is brought along. Only an edit
    that moves them to different places is refused.
    """
    enforced = enforced_mode(policy.rules, policy.enforcement)
    column_moved = column is not None and column.value != enforced
    mode_moved = mode is not None and mode.value != enforced
    if (
        column is not None
        and mode is not None
        and column_moved
        and mode_moved
        and column is not mode
    ):
        raise ValidationFailed(
            f"'enforcement' says {column.value} but the rule body's action.mode says "
            f"{mode.value}. They are the same setting: change one, or make them agree.",
            details={
                "field": "enforcement",
                "enforcement": column.value,
                "rules.action.mode": mode.value,
            },
        )
    if column is not None and column_moved:
        return enforced, column.value
    if mode is not None and mode_moved:
        return enforced, mode.value
    return enforced, enforced


def _unresolvable_signals(rules: Any) -> list[str]:
    """Signals in a rule body that the enforcement path can never resolve."""
    conditions = rules.get("conditions") if isinstance(rules, dict) else None
    if not isinstance(conditions, list):
        return []
    unknown: list[str] = []
    for condition in conditions:
        signal = str(condition.get("signal", "")) if isinstance(condition, dict) else ""
        if signal.strip() and not is_resolvable_signal(signal) and signal not in unknown:
            unknown.append(signal)
    return unknown


def _signal_details(unknown: Sequence[str]) -> dict[str, Any]:
    return {
        "field": "rules.conditions",
        "unknown_signals": list(unknown),
        "allowed": [*sorted(KNOWN_SIGNALS), f"{SCORE_SIGNAL_PREFIX}<name>"],
    }


def _assert_signals_known(rules: PolicyRules, *, stored: Any = None) -> None:
    """Refuse a rule body that names a signal ingest does not resolve.

    Checked when a body is written rather than in the schema, so a policy stored
    before the vocabulary was enforced can still be read, renamed or switched
    off. For the same reason a name the ``stored`` body already carries is let
    through on an edit — the console sends the whole body back every time — and
    is caught instead when somebody tries to activate it.
    """
    tolerated = set(_unresolvable_signals(stored))
    unknown = [
        signal
        for signal in _unresolvable_signals(rules.model_dump(mode="json"))
        if signal not in tolerated
    ]
    if unknown:
        raise ValidationFailed(
            f"Unknown signal {', '.join(repr(s) for s in unknown)}. A condition names a "
            f"signal the enforcement path resolves, or a feedback score written as "
            f"'{SCORE_SIGNAL_PREFIX}<name>'.",
            details=_signal_details(unknown),
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


async def _flush_named(session: AsyncSession, name: str) -> None:
    """Flush a write that claims a policy name, answering a lost race as 409.

    :func:`_assert_name_available` reads before the write, so two admins saving
    the same name a moment apart both pass it; the unique constraint then
    refuses the second flush. That is a conflict the caller can act on, not a
    server error.
    """
    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(
            f"A policy named '{name}' already exists in this workspace.",
            details={"field": "name"},
        ) from exc


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
    session: AsyncSession,
    workspace_id: str,
    scope: str,
    scope_ref: str | None,
    *,
    policy_id: str | None = None,
) -> tuple[int, int, int]:
    """Count the agents, environments and connectors a policy reaches.

    Two aggregate statements, run on the write path and by
    :func:`refresh_rollups`. ``applies_tools`` is deliberately not computed:
    there is no tool inventory in the control plane, so nothing can count it and
    the column keeps its default rather than being given an invented number.

    Given ``policy_id``, agents explicitly bound to that policy count as well as
    the ones its scope matches — the same union ingest enforces against.
    """
    clause = _scope_clause(scope, scope_ref)
    if policy_id is not None:
        clause = or_(
            clause,
            Agent.id.in_(
                select(PolicyBinding.agent_id).where(
                    PolicyBinding.policy_id == policy_id,
                    PolicyBinding.workspace_id == workspace_id,
                )
            ),
        )

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
    # A stored body may predate the signal vocabulary. It stays readable, but it
    # does not get switched on: a clause ingest cannot resolve is not a control.
    unknown = _unresolvable_signals(policy.rules)
    if unknown:
        raise PreconditionFailed(
            f"This policy names {', '.join(repr(s) for s in unknown)}, which the enforcement "
            f"path does not resolve. Edit the rule body before activating it; a feedback "
            f"score is written as '{SCORE_SIGNAL_PREFIX}<name>'.",
            details={"policy_id": policy.id, **_signal_details(unknown)},
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
# Rollups
# ---------------------------------------------------------------------------


async def refresh_rollups(
    session: AsyncSession,
    *,
    workspace_id: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> int:
    """Recompute the cached counters on ``policies``; returns the rows refreshed.

    These columns feed the table's "Violations (30d)" column and its sort, the
    inspector's 30-day summary, the CSV export, Agent Detail's policy rows and
    the ordering of approval rules. Nothing used to write them, so all of those
    read zero while the KPI cards beside them — which count the violation table
    directly — did not.

    Meant for the scheduler, not for a request: it is one UPDATE with correlated
    counts over every policy (or one workspace's), plus a reach recount per
    distinct scope target. Every statement names ``updated_at`` and sets it to
    itself, because a counter moving is not an edit and must not invalidate the
    optimistic-lock token an open edit form is holding. The caller commits.

    ``last_evaluated_at`` and ``applies_tools`` are left alone: the enforcement
    path reports no "evaluated" event and there is no tool inventory, so there
    is nothing true to put in them.
    """
    cutoff = _window_start(window_days)

    def _violations(*extra: ColumnElement[bool]) -> Any:
        return (
            select(func.count(PolicyViolation.id))
            .where(
                PolicyViolation.policy_id == Policy.id,
                PolicyViolation.occurred_at >= cutoff,
                *extra,
            )
            .scalar_subquery()
        )

    def _requests(*extra: ColumnElement[bool]) -> Any:
        return (
            select(func.count(ApprovalRequest.id))
            .where(
                ApprovalRequest.policy_id == Policy.id,
                ApprovalRequest.requested_at >= cutoff,
                *extra,
            )
            .scalar_subquery()
        )

    requests = _requests()
    approved = _requests(ApprovalRequest.status == ApprovalStatus.APPROVED.value)

    rollup = update(Policy).values(
        violations_30d=_violations(),
        blocked_30d=_violations(
            PolicyViolation.action_taken == PolicyEnforcement.BLOCK.value
        ),
        requests_30d=requests,
        # A whole percentage, rounded half up, in integer arithmetic so SQLite
        # and Postgres agree. No requests means no rate, stored as 0.
        approved_pct=case(
            (requests > 0, (approved * 200 + requests) // (requests * 2)), else_=0
        ),
        updated_at=Policy.updated_at,
    )
    if workspace_id is not None:
        rollup = rollup.where(Policy.workspace_id == workspace_id)
    result = await session.execute(rollup.execution_options(synchronize_session=False))
    refreshed = int(result.rowcount or 0)

    # Reach drifts without any policy being touched: an agent that registers
    # itself through ingest is governed by every Global policy from its first
    # trace. One recount per distinct target, however many policies share it.
    targets = select(Policy.workspace_id, Policy.scope, Policy.scope_ref).distinct()
    if workspace_id is not None:
        targets = targets.where(Policy.workspace_id == workspace_id)
    for target_workspace, scope, scope_ref in (await session.execute(targets)).all():
        agents, environments, connectors = await _reach(
            session, target_workspace, scope, scope_ref
        )
        await session.execute(
            update(Policy)
            .where(
                Policy.workspace_id == target_workspace,
                Policy.scope == scope,
                Policy.scope_ref.is_(None) if scope_ref is None else Policy.scope_ref == scope_ref,
                or_(
                    Policy.applies_agents != agents,
                    Policy.applies_envs != environments,
                    Policy.applies_connectors != connectors,
                ),
            )
            .values(
                applies_agents=agents,
                applies_envs=environments,
                applies_connectors=connectors,
                updated_at=Policy.updated_at,
            )
            .execution_options(synchronize_session=False)
        )

    # A policy with explicit bindings reaches further than its scope target, so
    # it cannot share a recount with its neighbours. There are few of them.
    bound = select(Policy).where(
        Policy.id.in_(select(PolicyBinding.policy_id).distinct())
    )
    if workspace_id is not None:
        bound = bound.where(Policy.workspace_id == workspace_id)
    for policy in (await session.execute(bound)).scalars().all():
        agents, environments, connectors = await _reach(
            session, policy.workspace_id, policy.scope, policy.scope_ref, policy_id=policy.id
        )
        await stamp(
            session,
            [policy],
            applies_agents=agents,
            applies_envs=environments,
            applies_connectors=connectors,
        )

    return refreshed


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
    derived = data.rules is None
    if data.rules is not None:
        _assert_signals_known(data.rules)
    enforcement = _agreed_enforcement(data)
    rules = data.rules or _starter_rules(
        category=data.category, risk_level=data.risk_level, enforcement=enforcement
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
        status=_status_for(data.status, derived=derived),
        enforcement=enforcement.value,
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
    await _flush_named(session, data.name)

    await audit.record(
        session,
        principal=principal,
        action="Policy created",
        entity_type=ENTITY_TYPE,
        entity_id=policy.id,
        entity_label=policy.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Created as {policy.status} with {policy.enforcement} enforcement."
            + (" The rule body was derived and awaits review." if derived else "")
        ),
        metadata={
            "category": policy.category,
            "scope": policy.scope,
            "scope_ref": policy.scope_ref,
            "risk_level": policy.risk_level,
            "applies_agents": policy.applies_agents,
            "rules_derived": derived,
            "requested_status": data.status.value,
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
        elif "scope_label" not in fields and policy.scope_label:
            # The console's form has no label field but sends scope and
            # scope_ref with every save. Same target, nothing said about the
            # label: a label somebody chose ("Finance Agents") is kept rather
            # than quietly reset to the derived one. A real rescope still
            # re-derives it, and an explicit null still resets it.
            resolved_label = policy.scope_label
        policy.scope, policy.scope_ref, policy.scope_label = (
            resolved_scope,
            resolved_ref,
            resolved_label,
        )
    elif "scope_label" in fields:
        policy.scope_label = data.scope_label

    submitted = data.rules if "rules" in fields else None
    enforced, target = _resolve_enforcement(
        policy,
        column=data.enforcement if "enforcement" in fields else None,
        mode=submitted.action.mode if submitted is not None else None,
    )
    if enforced != target:
        changed["enforcement"] = (enforced, target)
    if policy.enforcement != target and target in _ENFORCEMENT_VALUES:
        # Also brings an older row's label into line with what it enforces.
        changed.setdefault("enforcement", (policy.enforcement, target))
        policy.enforcement = target

    rules: dict[str, Any] | None = None
    if submitted is not None:
        rules = submitted.model_dump(mode="json")
        rules["action"]["mode"] = target
    elif _stored_mode(policy.rules) not in (None, target):
        # The dropdown alone moved. ``rules`` is a plain JSON column, so the body
        # is replaced rather than mutated in place, which would not be persisted.
        rules = copy.deepcopy(policy.rules)
        rules["action"] = {**rules["action"], "mode": target}

    if rules is not None and rules != policy.rules:
        if submitted is not None:
            _assert_signals_known(submitted, stored=policy.rules)
        policy.rules = rules
        previous_version = policy.version
        policy.version = _next_version(policy.version)
        changed["rules"] = (previous_version, policy.version)

    if rescoped:
        agents, environments, connectors = await _reach(
            session, principal.workspace_id, policy.scope, policy.scope_ref, policy_id=policy.id
        )
        policy.applies_agents = agents
        policy.applies_envs = environments
        policy.applies_connectors = connectors

    # Switching a policy on from the edit form is still an activation, so it
    # answers to the same precondition as the Activate button.
    if "status" in changed and policy.status == PolicyStatus.ACTIVE.value:
        _assert_enforceable(policy)

    policy.updated_by = principal.actor
    await _flush_named(session, policy.name)

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

    # ``session.delete`` would honour the ORM cascade on ``Policy.violations`` by
    # loading every violation into memory and deleting the rows one at a time,
    # and a policy that misfired for an afternoon owns hundreds of thousands of
    # them. The foreign keys already say ON DELETE CASCADE (SET NULL for the
    # approvals that cite it), so the row goes in one statement and the
    # database takes the children with it.
    await session.execute(
        delete(Policy).where(
            Policy.id == policy.id, Policy.workspace_id == principal.workspace_id
        )
    )
    session.expunge(policy)


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
        session, principal.workspace_id, policy.scope, policy.scope_ref, policy_id=policy.id
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


async def _agent_in_workspace(
    session: AsyncSession, principal: Principal, agent_id: str
) -> tuple[str, str]:
    """``(id, name)`` of an agent in the caller's workspace, or :class:`NotFound`."""
    row = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.id == agent_id, Agent.workspace_id == principal.workspace_id
            )
        )
    ).first()
    if row is None:
        raise NotFound(f"No agent with id '{agent_id}'.")
    return row.id, row.name


async def _binding(session: AsyncSession, policy: Policy, agent_id: str) -> PolicyBinding | None:
    return (
        await session.execute(
            select(PolicyBinding).where(
                PolicyBinding.policy_id == policy.id,
                PolicyBinding.agent_id == agent_id,
                PolicyBinding.workspace_id == policy.workspace_id,
            )
        )
    ).scalar_one_or_none()


async def _restamp_reach(session: AsyncSession, policy: Policy) -> None:
    """Recount reach after a binding changed. Bookkeeping, not an edit."""
    agents, environments, connectors = await _reach(
        session, policy.workspace_id, policy.scope, policy.scope_ref, policy_id=policy.id
    )
    await stamp(
        session,
        [policy],
        applies_agents=agents,
        applies_envs=environments,
        applies_connectors=connectors,
    )


async def list_bindings(
    session: AsyncSession, principal: Principal, policy_id: str
) -> list[tuple[PolicyBinding, str | None]]:
    """A policy's explicit bindings, oldest first, each with its agent's name."""
    policy = await _get(session, principal, policy_id)
    rows = (
        await session.execute(
            select(PolicyBinding, Agent.name)
            .outerjoin(Agent, Agent.id == PolicyBinding.agent_id)
            .where(
                PolicyBinding.policy_id == policy.id,
                PolicyBinding.workspace_id == principal.workspace_id,
            )
            .order_by(PolicyBinding.bound_at, PolicyBinding.id)
        )
    ).all()
    return [(binding, name) for binding, name in rows]


async def bind_agent(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    agent_id: str,
    *,
    request: Request | None = None,
) -> tuple[Policy, int, int, bool]:
    """Attach a policy to one agent, on top of whatever its scope matches.

    Ingest has always honoured a binding; until this verb existed nothing could
    create one. Returns the policy, agents affected, bindings, and whether a new
    binding was made — binding an agent twice is a no-op, not an error, so a
    console that retries does not have to care.
    """
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)
    _agent_id, agent_name = await _agent_in_workspace(session, principal, agent_id)

    created = await _binding(session, policy, agent_id) is None
    if created:
        try:
            # A savepoint, so losing the race to a second click costs this
            # insert and not the request's whole transaction.
            async with session.begin_nested():
                session.add(
                    PolicyBinding(
                        workspace_id=principal.workspace_id,
                        policy_id=policy.id,
                        agent_id=agent_id,
                        bound_by=principal.actor,
                    )
                )
                await session.flush()
        except IntegrityError:  # bound in the meantime: the outcome asked for
            created = False

    if created:
        await _restamp_reach(session, policy)
        await audit.record(
            session,
            principal=principal,
            action="Policy bound to agent",
            entity_type=ENTITY_TYPE,
            entity_id=policy.id,
            entity_label=policy.name,
            source_screen=SOURCE_SCREEN,
            detail=f"Bound to '{agent_name}'.",
            metadata={"agent_id": agent_id, "agent_name": agent_name, "status": policy.status},
            request=request,
        )

    bound = await _bound_agent_count(session, policy)
    affected = await _agents_affected(session, policy)
    return policy, affected, bound, created


async def unbind_agent(
    session: AsyncSession,
    principal: Principal,
    policy_id: str,
    agent_id: str,
    *,
    request: Request | None = None,
) -> tuple[Policy, int, int, bool]:
    """Remove an explicit binding. The policy's scope still applies if it matches.

    The agent is not looked up: a binding can outlive the agent it names, and
    that is exactly the binding somebody needs to be able to remove.
    """
    principal.require(Role.ADMIN)
    policy = await _get(session, principal, policy_id)

    binding = await _binding(session, policy, agent_id)
    removed = binding is not None
    if binding is not None:
        await session.delete(binding)
        await session.flush()
        await _restamp_reach(session, policy)
        await audit.record(
            session,
            principal=principal,
            action="Policy unbound from agent",
            entity_type=ENTITY_TYPE,
            entity_id=policy.id,
            entity_label=policy.name,
            source_screen=SOURCE_SCREEN,
            detail=f"Binding to agent '{agent_id}' removed.",
            metadata={"agent_id": agent_id, "status": policy.status},
            request=request,
        )

    bound = await _bound_agent_count(session, policy)
    affected = await _agents_affected(session, policy)
    return policy, affected, bound, removed


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
    # The copy is labelled with what its rule body enforces, so an older source
    # whose label had drifted does not hand the drift on.
    enforced = enforced_mode(source.rules, source.enforcement)

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
        enforcement=enforced if enforced in _ENFORCEMENT_VALUES else source.enforcement,
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
    await _flush_named(session, copy_name)

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

        derived = item.rules is None
        try:
            if item.rules is not None:
                _assert_signals_known(item.rules)
            enforcement = _agreed_enforcement(item)
        except ValidationFailed as exc:
            result.skipped += 1
            result.issues.append(
                PolicyImportIssue(index=index, name=item.name, reason=exc.message)
            )
            continue

        rules = item.rules or _starter_rules(
            category=item.category, risk_level=item.risk_level, enforcement=enforcement
        )
        # ``activate`` forces a definition on, but never one whose rule body had
        # to be derived: the file said nothing about what it should match.
        requested = PolicyStatus.ACTIVE if payload.activate else item.status
        status = _status_for(requested, derived=derived)
        if status != requested.value:
            result.issues.append(
                PolicyImportIssue(
                    index=index,
                    name=item.name,
                    reason=(
                        f"Imported as {status}, not {requested.value}: the definition has no "
                        "rule body, so a starter rule was derived. Review it, then activate."
                    ),
                )
            )
        agents, environments, connectors = await _reach(
            session,
            workspace_id,
            scope,
            scope_ref,
            policy_id=target.id if target is not None else None,
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
        policy.enforcement = enforcement.value
        policy.rules = rules.model_dump(mode="json")
        policy.version = item.version if target is None else _next_version(policy.version)
        policy.owner_user_id = item.owner_user_id or principal.user_id
        policy.applies_agents = agents
        policy.applies_envs = environments
        policy.applies_connectors = connectors
        policy.updated_by = principal.actor

        # A lost race here fails the request, and the rollback takes the whole
        # batch with it, which is the documented meaning of a Conflict on import.
        await _flush_named(session, item.name)
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
