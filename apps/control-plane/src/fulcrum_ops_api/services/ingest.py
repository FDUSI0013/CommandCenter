"""SDK ingest business logic — the hot path a customer's agent writes to.

Everything a batch goes through between the wire and the telemetry store lives
here, in this order:

1. **Authorisation.** API keys only, carrying the ``ingest`` scope. A browser
   session may read telemetry but may never write it: a person is not an agent.
2. **Agent resolution.** One batched lookup for every agent name in the batch.
   A key bound to an agent may only report for that agent; an unbound key with
   the right scope may auto-register an agent it has never seen, which is what
   lets a new service start reporting without a console visit first.
3. **Governance.** Active policies are evaluated against every item, and the
   Guardrails domain is offered each item's content. ``Block`` refuses the item,
   ``Mask`` strips its content, everything else records the breach and lets it
   through. Refusals come back per item, never as a failed batch.
4. **Commercial checks.** The licence entitlement and then the workspace's
   quotas, both *before* anything is accepted, so a tenant past its ceiling is
   told so rather than billed for the overage.
5. **Hand-off.** The surviving items go to the telemetry engine through the
   adapter, in as few calls as the batch allows.
6. **Evidence.** Violations, guardrail events and quota consumption are written
   in the same transaction as the acceptance, so the two commit together.

Two performance rules shape the code. Nothing in the per-item loop touches the
database — every lookup (agents, policies, bindings, connectors, quotas) is one
statement for the whole batch, resolved into dictionaries before the loop
starts. And nothing in the per-item loop calls the network — the engine sees one
call per entity class, not one per row.

Audit rows are written for governance events (an agent registered, items
blocked, guardrails fired) and never for routine acceptance. Accepting telemetry
is not a change to governed state; it *is* the telemetry, and a row per batch
would bury the trail the audit screen exists to show.

Quota enforcement is implemented here, directly against the ``quotas`` table,
rather than through a service call per item: the check has to be one indexed
read plus one update for a batch of a thousand spans, and the enforcement
decision has to be made before the engine hand-off so a store outage never
consumes a tenant's allowance.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import (
    PayloadTooLarge,
    PermissionDenied,
    PreconditionFailed,
    QuotaExceeded,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.base import new_id
from ..engine import EngineBadRequest, EngineError, EngineUnavailable, get_engine_client
from ..models.governance import (
    Policy,
    PolicyBinding,
    PolicyEnforcement,
    PolicyScope,
    PolicyStatus,
    PolicyViolation,
    ViolationSeverity,
)
from ..models.identity import ApiKey, Workspace
from ..models.operations import (
    LimitPeriod,
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
    QuotaResource,
)
from ..models.quality import (
    FeedbackItem,
    FeedbackSource,
    GuardrailAction,
    GuardrailConfig,
    GuardrailEvent,
    GuardrailScope,
    GuardrailStatus,
    Sentiment,
)
from ..models.registry import (
    Agent,
    AgentConnector,
    AgentStatus,
    AgentType,
    Connector,
    ConnectorStatus,
    EnvironmentType,
    Platform,
    RiskLevel,
)
from ..schemas.ingest import (
    REDACTION_MARKER,
    AutoRegisteredAgent,
    EventIn,
    GuardrailCandidate,
    GuardrailDescriptor,
    GuardrailVerdict,
    IngestBatchResult,
    IngestConfigRead,
    IngestEventKind,
    IngestItemResult,
    IngestQuotaState,
    ItemOutcome,
    ParsedBatch,
    RedactionRule,
    RejectionCode,
    ScoreIn,
    ScoreTarget,
    SpanIn,
    SpanType,
    TraceIn,
)
from . import audit, licensing

logger = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "SDK Ingest"
ENTITY_TYPE_AGENT: Final[str] = "agent"
ENTITY_TYPE_BATCH: Final[str] = "ingest_batch"

#: Scope an API key must carry to write telemetry.
INGEST_SCOPE: Final[str] = "ingest"

#: Scopes that additionally permit creating an agent on first sight of its name.
AUTO_REGISTER_SCOPES: Final[frozenset[str]] = frozenset({"admin", "agents:write"})

#: Entitlement consulted before a batch is accepted. A plan that hard-denies it
#: stops ingest at the door; a workspace with no licence row is not blocked,
#: because an absent licence is a data gap rather than a breached limit.
ENTITLEMENT_INGEST: Final[str] = "ingest.enabled"

#: Where a batch came from, recorded on audit rows so an operator can tell an
#: SDK report from an OpenTelemetry one.
SOURCE_SDK: Final[str] = "sdk"
SOURCE_OTLP: Final[str] = "otlp"

#: Hook the Guardrails domain may publish. When ``services.guardrails`` exposes
#: a coroutine of this name, ingest offers it every item's content and applies
#: the verdicts it returns. Until it exists — or if it raises — ingest records no
#: guardrail decision and lets the telemetry through: refusing to store the
#: record of something that already happened, because the checker is down,
#: destroys evidence rather than protecting anyone.
GUARDRAIL_HOOK: Final[str] = "evaluate_ingest_batch"

#: Ceiling on the policies evaluated for one batch. Far above any real
#: workspace's active policy count; present so a pathological configuration
#: cannot turn one request into an unbounded evaluation.
MAX_POLICIES_EVALUATED: Final[int] = 500

#: Ceiling on the evidence rows one batch may write. A batch that trips this
#: many controls has a systemic problem, and the audit row says so rather than
#: the request writing a hundred thousand near-identical violations.
MAX_EVIDENCE_ROWS: Final[int] = 500

#: Content handed to condition matching and to the guardrail evaluator, in
#: characters. Bounds both regular-expression cost and hook payload size.
MAX_CONTENT_CHARS: Final[int] = 8_192

#: Longest regular expression a policy condition may carry.
MAX_PATTERN_LENGTH: Final[int] = 500

#: Utilisation at which a quota flips to Warning. Matches the warning threshold
#: budgets ship with, so the two surfaces turn amber together.
QUOTA_WARN_PERCENT: Final[float] = 80.0

#: SDK defaults, overridable per tenant through ``workspaces.settings['ingest']``.
DEFAULT_SAMPLING_RATE: Final[float] = 1.0
DEFAULT_FLUSH_INTERVAL_SECONDS: Final[float] = 5.0
DEFAULT_RETRY_ATTEMPTS: Final[int] = 3
DEFAULT_RETRY_BACKOFF_SECONDS: Final[float] = 0.5
CONFIG_CACHE_SECONDS: Final[int] = 300

#: Enforcement modes that stop an item reaching the telemetry store. Ingest
#: cannot pause for a human, so a control demanding approval refuses the item
#: and says why, rather than silently downgrading itself to a warning.
BLOCKING_ENFORCEMENTS: Final[frozenset[str]] = frozenset(
    {PolicyEnforcement.BLOCK.value, PolicyEnforcement.REQUIRE_APPROVAL.value}
)

#: Ordered vocabularies a condition may compare with ``lt``/``gte`` and friends.
_ORDERED_LABELS: Final[dict[str, int]] = {
    "none": 0,
    "public": 0,
    "low": 1,
    "internal": 1,
    "medium": 2,
    "external": 2,
    "high": 3,
    "confidential": 3,
    "critical": 4,
    "restricted": 4,
}

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_MONTH_DAYS: Final[tuple[int, ...]] = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def authorise(principal: Principal) -> None:
    """Refuse anything that is not an ingest-scoped API key.

    Both routers depend on this, so the rule is stated once: telemetry is
    written by machines. A signed-in person reaching these endpoints is either a
    mistake or an attempt to forge another agent's history.
    """
    if principal.kind != "api_key":
        raise PermissionDenied(
            "The ingest API accepts API keys only. Sign-in sessions can read telemetry "
            "but cannot report it."
        )
    principal.require_scope(INGEST_SCOPE)


def _may_auto_register(principal: Principal) -> bool:
    return bool(AUTO_REGISTER_SCOPES & set(principal.scopes))


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug[:150] or "agent"


class _BadIdentifier(ValueError):
    """An id the telemetry store could not address."""


def _telemetry_id(value: str | None, *, field: str) -> str:
    """Normalise a caller-supplied telemetry id, or mint a time-ordered one.

    The store addresses traces and spans by UUID, so a non-UUID id is refused
    here — on its own row — rather than at the far end, where it would take the
    whole batch down with it. Supplying an id makes a retry idempotent, which is
    how the SDKs survive a network timeout without double-counting.
    """
    if not value:
        return new_id()
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise _BadIdentifier(f"{field} must be a UUID; received {value!r}.") from exc


def _text_of(*documents: Mapping[str, Any] | None) -> str:
    """Flatten payload documents into the text conditions and guardrails see."""
    parts: list[str] = []
    budget = MAX_CONTENT_CHARS
    for document in documents:
        if not document or budget <= 0:
            continue
        rendered = json.dumps(document, default=str, ensure_ascii=False)
        parts.append(rendered[:budget])
        budget -= len(rendered)
    return "\n".join(parts)


@functools.lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    """Compile a policy's regular expression once per process."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


BatchCall = Callable[[Sequence[Mapping[str, Any]]], Awaitable[Any]]


async def _push(call: BatchCall, payload: Sequence[Mapping[str, Any]]) -> str | None:
    """Hand one batch to the telemetry store.

    Returns ``None`` on success, or the reason to stamp on every item in the
    batch when the store refused it. An *unreachable* store is different: the
    batch was never seen, so the caller is told to retry rather than being told
    its data was rejected.
    """
    if not payload:
        return None
    try:
        await call(payload)
    except EngineUnavailable as exc:
        raise TelemetryBackendUnavailable(
            "The telemetry store is not accepting writes right now. Retry this batch."
        ) from exc
    except EngineBadRequest as exc:
        logger.warning("telemetry store refused an ingest batch with status %s", exc.status)
        return f"The telemetry store refused the batch (status {exc.status})."
    except EngineError as exc:  # pragma: no cover - the adapter raises the two above
        raise TelemetryBackendUnavailable() from exc
    return None


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Agents:
    """Every agent one batch needs, resolved in a single pass."""

    by_key: dict[str, Agent]
    bound: Agent | None
    registered: list[Agent] = dataclasses.field(default_factory=list)

    def lookup(self, name: str | None) -> Agent | None:
        if self.bound is not None:
            return self.bound
        if not name:
            return None
        return self.by_key.get(name.strip().lower())

    @property
    def touched(self) -> list[Agent]:
        seen: dict[str, Agent] = {}
        if self.bound is not None:
            seen[self.bound.id] = self.bound
        for agent in self.by_key.values():
            seen[agent.id] = agent
        return list(seen.values())


async def _bound_agent(session: AsyncSession, principal: Principal) -> Agent | None:
    """The single agent a bound key may report for, if it is bound to one."""
    if not principal.api_key_agent_id:
        return None
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.id == principal.api_key_agent_id,
                Agent.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise PreconditionFailed(
            "This API key is bound to an agent that no longer exists. Issue a new key "
            "or rebind this one before reporting telemetry."
        )
    return agent


async def _key_environment(session: AsyncSession, principal: Principal) -> str:
    """Environment an auto-registered agent inherits from the key that reported it."""
    allowed = {env.value.lower(): env.value for env in EnvironmentType}
    if principal.api_key_id is None:
        return EnvironmentType.DEVELOPMENT.value
    named = (
        await session.execute(select(ApiKey.environment).where(ApiKey.id == principal.api_key_id))
    ).scalar_one_or_none()
    return allowed.get((named or "").strip().lower(), EnvironmentType.DEVELOPMENT.value)


async def _unique_slug(session: AsyncSession, workspace_id: str, base: str) -> str:
    """Pick a free slug in one query rather than probing one candidate at a time."""
    taken = set(
        (
            await session.execute(
                select(Agent.slug).where(
                    Agent.workspace_id == workspace_id, Agent.slug.like(f"{base}%")
                )
            )
        )
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


async def _register_agent(
    session: AsyncSession, principal: Principal, name: str, *, request: Request | None
) -> Agent:
    """Create an agent because telemetry arrived for a name we have never seen.

    The row lands as *Pending Review* — the model's own default, and the honest
    state: something is reporting and nobody has governed it yet. Its telemetry
    is still accepted, because refusing it would hide exactly the workload an
    operator needs to see; the Agent Registry shows it as unreviewed until
    somebody signs it off.
    """
    slug = await _unique_slug(session, principal.workspace_id, _slugify(name))
    project_name = f"{principal.workspace_slug}-{slug}"

    try:
        project = await get_engine_client().ensure_project(project_name)
    except EngineUnavailable as exc:
        raise TelemetryBackendUnavailable(
            "A new agent cannot be provisioned while the telemetry store is unreachable."
        ) from exc
    except EngineBadRequest as exc:
        raise ValidationFailed(
            f"The telemetry store would not create a project for agent '{name}'.",
            details={"agent": name, "status": exc.status},
        ) from exc

    project_id = project.get("id") if isinstance(project, dict) else None
    agent = Agent(
        workspace_id=principal.workspace_id,
        name=name.strip()[:160],
        slug=slug,
        description="Registered automatically on first telemetry from the SDK.",
        platform=Platform.CUSTOM_AGENT.value,
        agent_type=AgentType.PRO_CODE.value,
        environment=await _key_environment(session, principal),
        status=AgentStatus.PENDING_REVIEW.value,
        risk=RiskLevel.LOW.value,
        tags=["auto-registered"],
        engine_project_id=str(project_id) if project_id else None,
        engine_project_name=project_name,
        last_used_at=_now(),
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(agent)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="agent.auto_registered",
        entity_type=ENTITY_TYPE_AGENT,
        entity_id=agent.id,
        entity_label=agent.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Registered '{agent.name}' on its first telemetry and provisioned its "
            "telemetry project; awaiting governance review."
        ),
        metadata={
            "slug": agent.slug,
            "environment": agent.environment,
            "engine_project_name": project_name,
        },
        request=request,
    )
    return agent


async def _resolve_agents(
    session: AsyncSession,
    principal: Principal,
    names: set[str],
    *,
    request: Request | None,
    allow_register: bool = True,
) -> _Agents:
    """Resolve every agent name in a batch with one statement, registering misses.

    Names match against both the slug and the display name, case insensitively,
    because an SDK is configured with whichever of the two the developer had to
    hand.
    """
    bound = await _bound_agent(session, principal)
    if bound is not None:
        return _Agents(by_key={}, bound=bound)

    wanted = {name.strip() for name in names if name and name.strip()}
    keys = {name.lower() for name in wanted}
    if not keys:
        return _Agents(by_key={}, bound=None)

    rows = (
        (
            await session.execute(
                select(Agent).where(
                    Agent.workspace_id == principal.workspace_id,
                    or_(func.lower(Agent.slug).in_(keys), func.lower(Agent.name).in_(keys)),
                )
            )
        )
        .scalars()
        .all()
    )

    by_key: dict[str, Agent] = {}
    for agent in rows:
        by_key.setdefault(agent.slug.lower(), agent)
        by_key.setdefault(agent.name.lower(), agent)

    resolved = _Agents(by_key=by_key, bound=None)
    if not allow_register or not _may_auto_register(principal):
        return resolved

    for name in sorted(wanted):
        if name.lower() in resolved.by_key:
            continue
        agent = await _register_agent(session, principal, name, request=request)
        resolved.by_key[agent.slug.lower()] = agent
        resolved.by_key[agent.name.lower()] = agent
        resolved.registered.append(agent)
    return resolved


async def _connector_blocks(
    session: AsyncSession, principal: Principal, agents: _Agents
) -> dict[str, list[str]]:
    """Agents that must fail closed, mapped to the blocked connectors they hold.

    A block on a connector is a statement about every agent granted it: their
    telemetry is refused until the block is lifted or the grant revoked. One
    query per batch answers for every agent the batch touches.
    """
    agent_ids = [agent.id for agent in agents.touched]
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentConnector.agent_id, Connector.name)
            .join(Connector, Connector.id == AgentConnector.connector_id)
            .where(
                AgentConnector.workspace_id == principal.workspace_id,
                AgentConnector.agent_id.in_(agent_ids),
                Connector.status == ConnectorStatus.BLOCKED.value,
            )
        )
    ).all()
    blocked: dict[str, list[str]] = {}
    for agent_id, connector_name in rows:
        blocked.setdefault(agent_id, []).append(connector_name)
    return blocked


def _block_for_connector(
    results: list[IngestItemResult],
    index: int,
    *,
    agent_id: str,
    connector_names: Sequence[str],
    spans: int = 0,
) -> None:
    names = ", ".join(sorted(connector_names))
    results[index] = results[index].model_copy(
        update={
            "outcome": ItemOutcome.BLOCKED,
            "code": RejectionCode.CONNECTOR_BLOCKED,
            "reason": f"Connector '{names}' is blocked; this agent fails closed until "
            "the block is lifted or the grant revoked.",
            "agent_id": agent_id,
            "spans": spans,
        }
    )


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Condition:
    signal: str
    operator: str
    value: Any


@dataclasses.dataclass(frozen=True)
class _Rule:
    """One active policy, compiled once per request rather than once per item."""

    policy_id: str
    name: str
    scope: str
    scope_ref: str | None
    enforcement: str
    severity: str
    match: str
    fail_mode: str
    exceptions: frozenset[str]
    conditions: tuple[_Condition, ...]
    message: str | None


@dataclasses.dataclass(frozen=True)
class _Hit:
    """A policy that matched one item, and what it does about it."""

    rule: _Rule
    matched_signals: tuple[str, ...]

    @property
    def blocks(self) -> bool:
        return self.rule.enforcement in BLOCKING_ENFORCEMENTS

    @property
    def masks(self) -> bool:
        return self.rule.enforcement == PolicyEnforcement.MASK.value


def _compile_policy(policy: Policy) -> _Rule | None:
    """Turn a stored rule body into something evaluable, or skip it.

    A body with no conditions matches nothing, so it is skipped rather than
    treated as matching everything — which is also why the Policy Center refuses
    to activate one.
    """
    body = policy.rules if isinstance(policy.rules, dict) else {}
    raw_conditions = body.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        return None

    conditions: list[_Condition] = []
    for raw in raw_conditions:
        if not isinstance(raw, dict):
            continue
        signal = str(raw.get("signal", "")).strip().lower()
        if not signal:
            continue
        conditions.append(
            _Condition(
                signal=signal,
                operator=str(raw.get("operator", "eq")).strip().lower(),
                value=raw.get("value"),
            )
        )
    if not conditions:
        return None

    action = body.get("action")
    action = action if isinstance(action, dict) else {}
    raw_exceptions = body.get("exceptions")
    exceptions = (
        frozenset(str(item) for item in raw_exceptions)
        if isinstance(raw_exceptions, list)
        else frozenset()
    )
    message = action.get("message")
    return _Rule(
        policy_id=policy.id,
        name=policy.name,
        scope=policy.scope,
        scope_ref=policy.scope_ref,
        enforcement=str(action.get("mode") or policy.enforcement),
        severity=str(body.get("severity") or ViolationSeverity.MEDIUM.value),
        match="any" if str(body.get("match", "all")).lower() == "any" else "all",
        fail_mode="open" if str(body.get("fail_mode", "closed")).lower() == "open" else "closed",
        exceptions=exceptions,
        conditions=tuple(conditions),
        message=message if isinstance(message, str) else None,
    )


@dataclasses.dataclass
class _PolicySet:
    """The policies in force for one batch, plus what they need to match on."""

    rules: tuple[_Rule, ...]
    bound: set[tuple[str, str]]
    connectors: dict[str, set[str]]

    def _reaches(self, rule: _Rule, agent: Agent) -> bool:
        """Whether one rule governs one agent, by explicit binding or by scope."""
        if (rule.policy_id, agent.id) in self.bound:
            return True
        if rule.scope == PolicyScope.GLOBAL.value:
            return True
        if rule.scope == PolicyScope.AGENT.value:
            return rule.scope_ref == agent.id
        if rule.scope == PolicyScope.ENVIRONMENT.value:
            return rule.scope_ref == agent.environment
        if rule.scope == PolicyScope.CONNECTOR.value:
            granted = self.connectors.get(agent.id, frozenset())
            return rule.scope_ref is not None and rule.scope_ref in granted
        return False

    def for_agent(self, agent: Agent) -> list[_Rule]:
        """Every rule that reaches this agent, exemptions already removed."""
        tags = set(agent.tags or ())
        return [
            rule
            for rule in self.rules
            if agent.id not in rule.exceptions
            and not (tags & rule.exceptions)
            and self._reaches(rule, agent)
        ]


async def _load_policies(
    session: AsyncSession, principal: Principal, agent_ids: Sequence[str]
) -> _PolicySet:
    """Load, in at most three statements, everything policy evaluation needs."""
    empty = _PolicySet(rules=(), bound=set(), connectors={})
    if not agent_ids:
        return empty

    policies = (
        (
            await session.execute(
                select(Policy)
                .where(
                    Policy.workspace_id == principal.workspace_id,
                    Policy.status == PolicyStatus.ACTIVE.value,
                )
                .order_by(Policy.updated_at.desc())
                .limit(MAX_POLICIES_EVALUATED)
            )
        )
        .scalars()
        .all()
    )
    rules = tuple(rule for rule in map(_compile_policy, policies) if rule is not None)
    if not rules:
        return empty

    bound = {
        (policy_id, agent_id)
        for policy_id, agent_id in (
            await session.execute(
                select(PolicyBinding.policy_id, PolicyBinding.agent_id).where(
                    PolicyBinding.workspace_id == principal.workspace_id,
                    PolicyBinding.agent_id.in_(agent_ids),
                )
            )
        ).all()
    }

    connectors: dict[str, set[str]] = {}
    if any(rule.scope == PolicyScope.CONNECTOR.value for rule in rules):
        for agent_id, connector_id in (
            await session.execute(
                select(AgentConnector.agent_id, AgentConnector.connector_id).where(
                    AgentConnector.workspace_id == principal.workspace_id,
                    AgentConnector.agent_id.in_(agent_ids),
                )
            )
        ).all():
            connectors.setdefault(agent_id, set()).add(connector_id)

    return _PolicySet(rules=rules, bound=bound, connectors=connectors)


def _orderable(value: Any) -> float | None:
    """Coerce a value onto the number line, ordered labels included."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        label = _ORDERED_LABELS.get(value.strip().lower())
        if label is not None:
            return float(label)
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _loosely_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        return str(left).strip().lower() == str(right).strip().lower()
    return bool(left == right)


def _compare(operator: str, left: Any, right: Any) -> bool | None:
    """Evaluate one clause. ``None`` means the clause could not be decided."""
    if operator == "exists":
        return left is not None
    if left is None:
        return None

    if operator == "eq":
        return _loosely_equal(left, right)
    if operator == "ne":
        return not _loosely_equal(left, right)

    if operator in ("in", "not_in"):
        if not isinstance(right, (list, tuple, set)):
            return None
        haystack = {str(item).strip().lower() for item in right}
        if isinstance(left, (list, tuple, set)):
            present = bool({str(item).strip().lower() for item in left} & haystack)
        else:
            present = str(left).strip().lower() in haystack
        return present if operator == "in" else not present

    if operator == "contains":
        needle = str(right).strip().lower()
        if isinstance(left, (list, tuple, set)):
            return any(needle in str(item).lower() for item in left)
        return needle in str(left).lower()

    if operator == "matches":
        pattern = str(right)
        if len(pattern) > MAX_PATTERN_LENGTH:
            return None
        compiled = _compiled(pattern)
        if compiled is None:
            return None
        return bool(compiled.search(str(left)[:MAX_CONTENT_CHARS]))

    if operator in ("lt", "lte", "gt", "gte"):
        a, b = _orderable(left), _orderable(right)
        if a is None or b is None:
            return None
        if operator == "lt":
            return a < b
        if operator == "lte":
            return a <= b
        if operator == "gt":
            return a > b
        return a >= b

    return None


def _evaluate(rules: Iterable[_Rule], signals: Mapping[str, Any]) -> list[_Hit]:
    """Run every applicable policy over one item's signals."""
    hits: list[_Hit] = []
    for rule in rules:
        matched: list[str] = []
        for condition in rule.conditions:
            verdict = _compare(condition.operator, signals.get(condition.signal), condition.value)
            if verdict is None:
                # A signal this deployment cannot resolve counts against the item
                # under fail-closed, which is the shipped default.
                verdict = rule.fail_mode == "closed"
            if verdict:
                matched.append(condition.signal)
        fired = (
            len(matched) == len(rule.conditions) if rule.match == "all" else len(matched) > 0
        )
        if fired:
            hits.append(_Hit(rule=rule, matched_signals=tuple(matched)))
    return hits


def _signals(
    agent: Agent,
    *,
    entity: str,
    name: str,
    span_type: str | None,
    model: str | None,
    provider: str | None,
    usage: Mapping[str, int] | None,
    cost: float | None,
    duration_ms: float | None,
    tags: Sequence[str],
    has_error: bool,
    scores: Mapping[str, float],
    thread_id: str | None,
    content: str,
) -> dict[str, Any]:
    """The values a policy condition may name, resolved for one item.

    The vocabulary is flat and closed on purpose: a reviewer writing a policy in
    the console picks a signal name, and this is the list that name is resolved
    against at ingest time. Every feedback score on the item is also exposed
    under its own name and under ``score:<name>``, which is how a safety-score
    threshold written against a custom judge keeps working.
    """
    counters = dict(usage or {})
    prompt = counters.get("prompt_tokens", counters.get("input_tokens", 0))
    completion = counters.get("completion_tokens", counters.get("output_tokens", 0))
    total = counters.get("total_tokens", prompt + completion)

    resolved: dict[str, Any] = {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "agent_slug": agent.slug,
        "agent_risk": agent.risk,
        "action_risk": agent.risk,
        "agent_status": agent.status,
        "agent_tags": list(agent.tags or ()),
        "team": agent.team,
        "environment": agent.environment,
        "platform": agent.platform,
        "entity": entity,
        "name": name,
        "span_type": span_type,
        "tool_name": name if span_type == "tool" else None,
        "model": model or agent.model,
        "provider": provider,
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
        "cost_usd": float(cost or 0.0),
        "duration_ms": duration_ms,
        "tags": list(tags),
        "has_error": has_error,
        "thread_id": thread_id,
        "content": content,
        "content_length": len(content),
    }
    for score_name, value in scores.items():
        key = score_name.strip().lower()
        resolved[key] = value
        resolved[f"score:{key}"] = value
    return resolved


# ---------------------------------------------------------------------------
# Guardrail evaluation
# ---------------------------------------------------------------------------


async def _guardrail_verdicts(
    session: AsyncSession, principal: Principal, candidates: list[GuardrailCandidate]
) -> tuple[list[GuardrailVerdict], bool]:
    """Offer the batch's content to the Guardrails domain, if it is installed.

    Returns the verdicts and whether an evaluator actually ran. Ingest owns
    extracting the content, applying the verdicts and writing the evidence; the
    evaluation itself is not ours. A missing — or failing — evaluator degrades to
    "not evaluated" rather than to a rejected batch.
    """
    if not candidates:
        return [], False
    try:
        from . import guardrails as guardrail_service
    except ImportError:
        return [], False

    hook = getattr(guardrail_service, GUARDRAIL_HOOK, None)
    if not callable(hook):
        return [], False

    try:
        raw = await hook(session, principal, candidates)
    except Exception:  # noqa: BLE001 - a broken checker must not cost the telemetry
        logger.exception("guardrail evaluation failed during ingest; items were not evaluated")
        return [], False

    verdicts: list[GuardrailVerdict] = []
    for item in raw or []:
        verdicts.append(
            item if isinstance(item, GuardrailVerdict) else GuardrailVerdict.model_validate(item)
        )
    return verdicts, True


# ---------------------------------------------------------------------------
# Entitlement and quota
# ---------------------------------------------------------------------------


def _add_months(value: dt.datetime, months: int) -> dt.datetime:
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    last = 29 if (month == 2 and _is_leap(year)) else _MONTH_DAYS[month - 1]
    return value.replace(year=year, month=month, day=min(value.day, last))


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _advance(value: dt.datetime, period: str) -> dt.datetime:
    if period == LimitPeriod.ANNUAL.value:
        return _add_months(value, 12)
    if period == LimitPeriod.QUARTERLY.value:
        return _add_months(value, 3)
    return _add_months(value, 1)


def _quota_applies(quota: Quota, agents: Sequence[Agent]) -> bool:
    if quota.scope == LimitScope.WORKSPACE.value:
        return True
    if not quota.scope_ref:
        return False
    if quota.scope == LimitScope.AGENT.value:
        return any(agent.id == quota.scope_ref for agent in agents)
    if quota.scope == LimitScope.ENVIRONMENT.value:
        return any(agent.environment == quota.scope_ref for agent in agents)
    if quota.scope == LimitScope.TEAM.value:
        return any(agent.team == quota.scope_ref for agent in agents)
    return False


def _billable_tokens(usage: Mapping[str, Any] | None) -> int:
    """The token count a quota is charged for one item.

    Reporters usually send prompt, completion *and* total counters; summing the
    raw dict would charge the tenant roughly double. An explicit total wins,
    otherwise prompt+completion, and only a dict with neither falls back to the
    sum of whatever counters it does carry.
    """
    counters = dict(usage or {})
    if not counters:
        return 0
    prompt = counters.get("prompt_tokens", counters.get("input_tokens"))
    completion = counters.get("completion_tokens", counters.get("output_tokens"))
    total = counters.get("total_tokens")
    if total is not None:
        return int(total)
    if prompt is not None or completion is not None:
        return int(prompt or 0) + int(completion or 0)
    return int(sum(value for value in counters.values() if isinstance(value, (int, float))))


@dataclasses.dataclass
class _Charge:
    quota: Quota
    amount: float


async def _prepare_quotas(
    session: AsyncSession,
    principal: Principal,
    agents: Sequence[Agent],
    *,
    requests: int,
    tokens: int,
) -> list[_Charge]:
    """Roll elapsed periods, then refuse the batch if a hard quota would break.

    Called before anything is accepted. The increments themselves are applied by
    :func:`_commit_quotas` once the telemetry store has taken the batch, so an
    outage at the far end never consumes a tenant's allowance.
    """
    rows = (
        (
            await session.execute(
                select(Quota).where(
                    Quota.workspace_id == principal.workspace_id,
                    # Exceeded quotas must stay loaded: a Block quota that
                    # stopped being read the moment it filled up would refuse
                    # exactly one batch and then wave everything through, and
                    # its period could never roll from the ingest path.
                    Quota.status.in_(
                        (
                            LimitStatus.ACTIVE.value,
                            LimitStatus.WARNING.value,
                            LimitStatus.EXCEEDED.value,
                        )
                    ),
                    Quota.resource.in_(
                        (QuotaResource.REQUESTS.value, QuotaResource.TOKENS.value)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    now = _now()
    charges: list[_Charge] = []
    for quota in rows:
        if not _quota_applies(quota, agents):
            continue

        if quota.resets_at is not None and _as_utc(quota.resets_at) <= now:
            # The period elapsed while nothing was reporting: roll it forward to
            # the first window that contains "now" and start the counter again.
            resets_at = _as_utc(quota.resets_at)
            for _ in range(64):
                if resets_at > now:
                    break
                resets_at = _advance(resets_at, quota.period)
            quota.resets_at = resets_at
            quota.used_value = 0.0
            quota.status = LimitStatus.ACTIVE.value

        amount = float(requests if quota.resource == QuotaResource.REQUESTS.value else tokens)
        if amount <= 0:
            continue

        limit = float(quota.limit_value or 0.0)
        used = float(quota.used_value or 0.0)
        blocking = quota.enforcement == QuotaEnforcement.BLOCK.value
        if blocking and limit > 0 and used + amount > limit:
            raise QuotaExceeded(
                f"Quota '{quota.name}' allows {limit:,.0f} {quota.unit} per "
                f"{quota.period.lower()} and {used:,.0f} are already used; this batch "
                f"needs {amount:,.0f} more.",
                details={
                    "quota_id": quota.id,
                    "resource": quota.resource,
                    "limit_value": limit,
                    "used_value": used,
                    "requested": amount,
                    "resets_at": _iso(quota.resets_at),
                },
            )
        charges.append(_Charge(quota=quota, amount=amount))
    return charges


def _commit_quotas(charges: Sequence[_Charge]) -> list[IngestQuotaState]:
    """Apply the batch's consumption and re-derive each quota's chip colour."""
    states: list[IngestQuotaState] = []
    for charge in charges:
        quota = charge.quota
        quota.used_value = float(quota.used_value or 0.0) + charge.amount
        limit = float(quota.limit_value or 0.0)
        utilization = round(quota.used_value / limit * 100, 1) if limit > 0 else 0.0
        if limit > 0 and utilization >= 100:
            quota.status = LimitStatus.EXCEEDED.value
        elif limit > 0 and utilization >= QUOTA_WARN_PERCENT:
            quota.status = LimitStatus.WARNING.value
        else:
            quota.status = LimitStatus.ACTIVE.value
        states.append(
            IngestQuotaState(
                id=quota.id,
                name=quota.name,
                resource=quota.resource,
                scope=quota.scope,
                scope_ref=quota.scope_ref,
                unit=quota.unit,
                limit_value=limit,
                used_value=float(quota.used_value),
                remaining=max(limit - float(quota.used_value), 0.0) if limit > 0 else 0.0,
                utilization_pct=utilization,
                enforcement=quota.enforcement,
                status=quota.status,
                resets_at=quota.resets_at,
            )
        )
    return states


async def _check_licence(session: AsyncSession, principal: Principal) -> None:
    """Refuse ingest when the tenant's licence hard-denies it.

    Delegated to the licensing service so commercial rules live in one place. A
    workspace with no licence row is deliberately let through: an absent licence
    is a data gap, not a breached limit.
    """
    await licensing.enforce_entitlement(session, principal.workspace_id, ENTITLEMENT_INGEST)


# ---------------------------------------------------------------------------
# Engine payload shaping
# ---------------------------------------------------------------------------


def _project(agent: Agent) -> str:
    return agent.engine_project_name or agent.slug


def _redacted() -> dict[str, Any]:
    return {"value": REDACTION_MARKER, "redacted": True}


def _trace_model(trace: TraceIn) -> str | None:
    """The model that produced this run, read from its own spans.

    A run's model is a measurement, not a registration: an agent may be
    configured with one model and actually call another, and a run that
    switches models mid-turn was answered by the last one. Reading it here —
    once, at ingest — means every consumer sees the model that really ran
    without re-reading the span table.
    """
    for span in reversed(trace.spans):
        if span.type is SpanType.LLM and span.model:
            return span.model
    # An agent that never types its spans still names a model on them.
    for span in reversed(trace.spans):
        if span.model:
            return span.model
    return None


def _engine_trace(trace: TraceIn, trace_id: str, agent: Agent, *, masked: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        **trace.metadata,
        "fulcrum_agent_id": agent.id,
        "fulcrum_environment": agent.environment,
    }
    # Only derived when the reporter did not state one itself.
    if not metadata.get("model"):
        model = _trace_model(trace)
        if model:
            metadata["model"] = model

    payload: dict[str, Any] = {
        "id": trace_id,
        "project_name": _project(agent),
        "name": trace.name,
        "start_time": _iso(trace.start_time),
        "end_time": _iso(trace.end_time),
        "thread_id": trace.thread_id,
        "tags": list(trace.tags),
        "metadata": metadata,
        "input": _redacted() if masked else trace.input,
        "output": _redacted() if masked else trace.output,
        "error_info": trace.error_info.model_dump(exclude_none=True) if trace.error_info else None,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _engine_span(
    span: SpanIn, *, span_id: str, trace_id: str, agent: Agent, masked: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": span_id,
        "trace_id": trace_id,
        "parent_span_id": span.parent_span_id,
        "project_name": _project(agent),
        "name": span.name,
        "type": span.type.value,
        "start_time": _iso(span.start_time),
        "end_time": _iso(span.end_time),
        "tags": list(span.tags),
        "metadata": {**span.metadata, "fulcrum_agent_id": agent.id},
        "usage": dict(span.usage) if span.usage else None,
        "model": span.model,
        "provider": span.provider,
        "total_estimated_cost": span.total_estimated_cost,
        "input": _redacted() if masked else span.input,
        "output": _redacted() if masked else span.output,
        "error_info": span.error_info.model_dump(exclude_none=True) if span.error_info else None,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _engine_score(
    name: str,
    value: float,
    *,
    target_id: str,
    agent: Agent,
    target: ScoreTarget,
    category_name: str | None = None,
    reason: str | None = None,
    source: str = "sdk",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_name": _project(agent),
        "name": name,
        "value": value,
        "source": source,
    }
    if target is ScoreTarget.THREAD:
        payload["thread_id"] = target_id
    else:
        payload["id"] = target_id
    if category_name:
        payload["category_name"] = category_name
    if reason:
        payload["reason"] = reason
    return payload


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Accepted:
    """One item that survived validation, resolution and governance."""

    index: int
    agent: Agent
    entity_id: str
    trace_id: str
    spans: int = 0
    tokens: int = 0
    masked: bool = False


async def ingest_traces(
    session: AsyncSession,
    principal: Principal,
    parsed: ParsedBatch[TraceIn],
    *,
    request: Request | None = None,
    source: str = SOURCE_SDK,
) -> IngestBatchResult:
    """Ingest a batch of traces with their nested spans and feedback scores.

    Every stage is applied to the batch as a whole and every verdict is reported
    against the individual trace, so a rejected, blocked or masked trace never
    costs its neighbours their acceptance.
    """
    started = time.perf_counter()
    results = _seed_results(parsed)

    span_total = sum(len(item.spans) for _, item in parsed.valid)
    if span_total > settings.ingest_max_batch_spans:
        raise PayloadTooLarge(
            f"This batch carries {span_total:,} spans; the limit is "
            f"{settings.ingest_max_batch_spans:,}. Split it and retry.",
            details={"spans": span_total, "max_spans": settings.ingest_max_batch_spans},
        )

    agents = await _resolve_agents(
        session,
        principal,
        {(item.agent or parsed.envelope.agent or "") for _, item in parsed.valid},
        request=request,
    )
    policies = await _load_policies(session, principal, [agent.id for agent in agents.touched])
    connector_blocks = await _connector_blocks(session, principal, agents)

    accepted: list[_Accepted] = []
    candidates: list[GuardrailCandidate] = []
    hits_by_index: dict[int, list[_Hit]] = {}

    for index, trace in parsed.valid:
        agent = _bind(agents, trace.agent or parsed.envelope.agent, results, index)
        if agent is None:
            continue
        blocked_connectors = connector_blocks.get(agent.id)
        if blocked_connectors:
            _block_for_connector(
                results,
                index,
                agent_id=agent.id,
                connector_names=blocked_connectors,
                spans=len(trace.spans),
            )
            continue
        try:
            trace_id = _telemetry_id(trace.id, field="trace id")
        except _BadIdentifier as exc:
            _reject(results, index, RejectionCode.INVALID_ID, str(exc))
            continue

        content = _text_of(trace.input, trace.output)
        usage = trace.total_usage
        hits = _evaluate(
            policies.for_agent(agent),
            _signals(
                agent,
                entity="trace",
                name=trace.name,
                span_type=None,
                model=None,
                provider=None,
                usage=usage,
                cost=trace.total_cost,
                duration_ms=trace.duration_ms,
                tags=trace.tags,
                has_error=trace.error_info is not None,
                scores={score.name: score.value for score in trace.feedback_scores},
                thread_id=trace.thread_id,
                content=content,
            ),
        )
        hits_by_index[index] = hits

        blocking = next((hit for hit in hits if hit.blocks), None)
        if blocking is not None:
            _block(results, index, blocking, agent_id=agent.id, spans=len(trace.spans))
            continue

        masked = any(hit.masks for hit in hits)
        accepted.append(
            _Accepted(
                index=index,
                agent=agent,
                entity_id=trace_id,
                trace_id=trace_id,
                spans=len(trace.spans),
                tokens=_billable_tokens(usage),
                masked=masked,
            )
        )
        _mark(
            results,
            index,
            entity_id=trace_id,
            agent_id=agent.id,
            spans=len(trace.spans),
            masked=masked,
        )
        if content:
            candidates.append(
                GuardrailCandidate(
                    index=index,
                    agent_id=agent.id,
                    environment=agent.environment,
                    trace_id=trace_id,
                    text=content[:MAX_CONTENT_CHARS],
                )
            )

    verdicts, evaluated = await _guardrail_verdicts(session, principal, candidates)
    accepted = _apply_verdicts(accepted, verdicts, results)

    spans_sent = 0
    scores_sent = 0
    quotas: list[IngestQuotaState] = []

    if accepted:
        await _check_licence(session, principal)
        charges = await _prepare_quotas(
            session,
            principal,
            agents.touched,
            requests=len(accepted),
            tokens=sum(entry.tokens for entry in accepted),
        )

        traces_payload: list[dict[str, Any]] = []
        spans_payload: list[dict[str, Any]] = []
        trace_scores: list[dict[str, Any]] = []
        span_scores: list[dict[str, Any]] = []

        for entry in accepted:
            trace = parsed.items[entry.index]
            if trace is None:  # pragma: no cover - ParsedBatch.valid guarantees this
                continue
            traces_payload.append(
                _engine_trace(trace, entry.trace_id, entry.agent, masked=entry.masked)
            )
            trace_scores.extend(
                _engine_score(
                    score.name,
                    score.value,
                    target_id=entry.trace_id,
                    agent=entry.agent,
                    target=ScoreTarget.TRACE,
                    category_name=score.category_name,
                    reason=score.reason,
                    source=score.source,
                )
                for score in trace.feedback_scores
            )
            for span in trace.spans:
                try:
                    span_id = _telemetry_id(span.id, field="span id")
                except _BadIdentifier as exc:
                    logger.info("dropping a span whose id cannot be addressed: %s", exc)
                    continue
                spans_payload.append(
                    _engine_span(
                        span,
                        span_id=span_id,
                        trace_id=entry.trace_id,
                        agent=entry.agent,
                        masked=entry.masked,
                    )
                )
                span_scores.extend(
                    _engine_score(
                        score.name,
                        score.value,
                        target_id=span_id,
                        agent=entry.agent,
                        target=ScoreTarget.SPAN,
                        category_name=score.category_name,
                        reason=score.reason,
                        source=score.source,
                    )
                    for score in span.feedback_scores
                )

        client = get_engine_client()
        refusal = await _push(client.create_traces_batch, traces_payload)
        if refusal is not None:
            for entry in accepted:
                _reject(results, entry.index, RejectionCode.TELEMETRY_REJECTED, refusal)
            accepted = []
        else:
            span_refusal = await _push(client.create_spans_batch, spans_payload)
            if span_refusal is None:
                spans_sent = len(spans_payload)
            else:
                logger.warning("spans refused after their traces were stored: %s", span_refusal)
            await _push(client.score_traces_batch, trace_scores)
            await _push(client.score_spans_batch, span_scores)
            scores_sent = len(trace_scores) + len(span_scores)
            quotas = _commit_quotas(charges)

    violations = await _record_evidence(
        session,
        principal,
        parsed,
        results,
        hits_by_index,
        verdicts,
        accepted,
        entity="trace",
        source=source,
        request=request,
    )
    await _touch_agents(session, agents, accepted=bool(accepted))

    result = _finish(results, parsed, started, agents, quotas, evaluated, violations)
    result.spans_accepted = spans_sent
    result.scores_accepted = scores_sent
    return result


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


async def ingest_spans(
    session: AsyncSession,
    principal: Principal,
    parsed: ParsedBatch[SpanIn],
    *,
    request: Request | None = None,
) -> IngestBatchResult:
    """Ingest spans for traces that were reported separately.

    Used by SDKs that stream a long-running trace's spans as they close rather
    than buffering the whole trace, so each span must name the trace it belongs
    to.
    """
    started = time.perf_counter()
    results = _seed_results(parsed)

    if len(parsed.items) > settings.ingest_max_batch_spans:
        raise PayloadTooLarge(
            f"This batch carries {len(parsed.items):,} spans; the limit is "
            f"{settings.ingest_max_batch_spans:,}.",
            details={"spans": len(parsed.items), "max_spans": settings.ingest_max_batch_spans},
        )

    agents = await _resolve_agents(
        session,
        principal,
        {(item.agent or parsed.envelope.agent or "") for _, item in parsed.valid},
        request=request,
    )
    policies = await _load_policies(session, principal, [agent.id for agent in agents.touched])
    connector_blocks = await _connector_blocks(session, principal, agents)

    accepted: list[_Accepted] = []
    candidates: list[GuardrailCandidate] = []
    hits_by_index: dict[int, list[_Hit]] = {}

    for index, span in parsed.valid:
        agent = _bind(agents, span.agent or parsed.envelope.agent, results, index)
        if agent is None:
            continue
        blocked_connectors = connector_blocks.get(agent.id)
        if blocked_connectors:
            _block_for_connector(
                results, index, agent_id=agent.id, connector_names=blocked_connectors
            )
            continue
        if not span.trace_id:
            _reject(
                results,
                index,
                RejectionCode.MISSING_TRACE_ID,
                "A span posted on its own must name the trace it belongs to.",
            )
            continue
        try:
            trace_id = _telemetry_id(span.trace_id, field="trace id")
            span_id = _telemetry_id(span.id, field="span id")
        except _BadIdentifier as exc:
            _reject(results, index, RejectionCode.INVALID_ID, str(exc))
            continue

        content = _text_of(span.input, span.output)
        hits = _evaluate(
            policies.for_agent(agent),
            _signals(
                agent,
                entity="span",
                name=span.name,
                span_type=span.type.value,
                model=span.model,
                provider=span.provider,
                usage=span.usage,
                cost=span.total_estimated_cost,
                duration_ms=span.duration_ms,
                tags=span.tags,
                has_error=span.error_info is not None,
                scores={score.name: score.value for score in span.feedback_scores},
                thread_id=None,
                content=content,
            ),
        )
        hits_by_index[index] = hits

        blocking = next((hit for hit in hits if hit.blocks), None)
        if blocking is not None:
            _block(results, index, blocking, agent_id=agent.id, spans=1)
            continue

        masked = any(hit.masks for hit in hits)
        accepted.append(
            _Accepted(
                index=index,
                agent=agent,
                entity_id=span_id,
                trace_id=trace_id,
                spans=1,
                tokens=_billable_tokens(span.usage),
                masked=masked,
            )
        )
        _mark(results, index, entity_id=span_id, agent_id=agent.id, spans=1, masked=masked)
        if content:
            candidates.append(
                GuardrailCandidate(
                    index=index,
                    agent_id=agent.id,
                    environment=agent.environment,
                    trace_id=trace_id,
                    span_id=span_id,
                    text=content[:MAX_CONTENT_CHARS],
                )
            )

    verdicts, evaluated = await _guardrail_verdicts(session, principal, candidates)
    accepted = _apply_verdicts(accepted, verdicts, results)

    quotas: list[IngestQuotaState] = []
    scores_sent = 0
    spans_sent = 0

    if accepted:
        await _check_licence(session, principal)
        charges = await _prepare_quotas(
            session,
            principal,
            agents.touched,
            requests=len(accepted),
            tokens=sum(entry.tokens for entry in accepted),
        )

        payload: list[dict[str, Any]] = []
        score_payload: list[dict[str, Any]] = []
        for entry in accepted:
            span = parsed.items[entry.index]
            if span is None:  # pragma: no cover - ParsedBatch.valid guarantees this
                continue
            payload.append(
                _engine_span(
                    span,
                    span_id=entry.entity_id,
                    trace_id=entry.trace_id,
                    agent=entry.agent,
                    masked=entry.masked,
                )
            )
            score_payload.extend(
                _engine_score(
                    score.name,
                    score.value,
                    target_id=entry.entity_id,
                    agent=entry.agent,
                    target=ScoreTarget.SPAN,
                    category_name=score.category_name,
                    reason=score.reason,
                    source=score.source,
                )
                for score in span.feedback_scores
            )

        client = get_engine_client()
        refusal = await _push(client.create_spans_batch, payload)
        if refusal is not None:
            for entry in accepted:
                _reject(results, entry.index, RejectionCode.TELEMETRY_REJECTED, refusal)
            accepted = []
        else:
            await _push(client.score_spans_batch, score_payload)
            spans_sent = len(payload)
            scores_sent = len(score_payload)
            quotas = _commit_quotas(charges)

    violations = await _record_evidence(
        session,
        principal,
        parsed,
        results,
        hits_by_index,
        verdicts,
        accepted,
        entity="span",
        source=SOURCE_SDK,
        request=request,
    )
    await _touch_agents(session, agents, accepted=bool(accepted))

    result = _finish(results, parsed, started, agents, quotas, evaluated, violations)
    result.spans_accepted = spans_sent
    result.scores_accepted = scores_sent
    return result


# ---------------------------------------------------------------------------
# Feedback scores
# ---------------------------------------------------------------------------


async def ingest_scores(
    session: AsyncSession,
    principal: Principal,
    parsed: ParsedBatch[ScoreIn],
    *,
    request: Request | None = None,
) -> IngestBatchResult:
    """Attach feedback scores to traces, spans or threads already reported.

    Scores carry no customer content of their own, so they skip guardrail
    evaluation; they are still counted against the workspace's request quota,
    because they are requests.
    """
    started = time.perf_counter()
    results = _seed_results(parsed)

    agents = await _resolve_agents(
        session,
        principal,
        {(item.agent or parsed.envelope.agent or "") for _, item in parsed.valid},
        request=request,
    )

    grouped: dict[ScoreTarget, list[dict[str, Any]]] = {target: [] for target in ScoreTarget}
    targets: dict[int, ScoreTarget] = {}
    accepted: list[_Accepted] = []

    for index, score in parsed.valid:
        agent = _bind(agents, score.agent or parsed.envelope.agent, results, index)
        if agent is None:
            continue
        target_id = score.id
        if score.target is not ScoreTarget.THREAD:
            try:
                target_id = _telemetry_id(score.id, field=f"{score.target.value} id")
            except _BadIdentifier as exc:
                _reject(results, index, RejectionCode.INVALID_ID, str(exc))
                continue

        grouped[score.target].append(
            _engine_score(
                score.name,
                score.value,
                target_id=target_id,
                agent=agent,
                target=score.target,
                category_name=score.category_name,
                reason=score.reason,
                source=score.source,
            )
        )
        targets[index] = score.target
        accepted.append(
            _Accepted(index=index, agent=agent, entity_id=target_id, trace_id=target_id)
        )
        _mark(results, index, entity_id=target_id, agent_id=agent.id, spans=0)

    quotas: list[IngestQuotaState] = []
    if accepted:
        await _check_licence(session, principal)
        charges = await _prepare_quotas(
            session, principal, agents.touched, requests=len(accepted), tokens=0
        )
        client = get_engine_client()
        calls: dict[ScoreTarget, BatchCall] = {
            ScoreTarget.TRACE: client.score_traces_batch,
            ScoreTarget.SPAN: client.score_spans_batch,
            ScoreTarget.THREAD: client.score_threads_batch,
        }
        for target, payload in grouped.items():
            refusal = await _push(calls[target], payload)
            if refusal is None:
                continue
            for index, item_target in targets.items():
                if item_target is target:
                    _reject(results, index, RejectionCode.TELEMETRY_REJECTED, refusal)
        accepted = [
            entry for entry in accepted if results[entry.index].outcome is ItemOutcome.ACCEPTED
        ]
        if accepted:
            # One target class can be refused while the others land, so the
            # tenant is charged for what was actually stored, not for what was
            # offered before the store answered.
            for charge in charges:
                if charge.quota.resource == QuotaResource.REQUESTS.value:
                    charge.amount = float(len(accepted))
            quotas = _commit_quotas(charges)

    result = _finish(results, parsed, started, agents, quotas, False, 0)
    result.scores_accepted = len(accepted)
    return result


# ---------------------------------------------------------------------------
# Governance events
# ---------------------------------------------------------------------------

#: Reported guardrail actions, normalised to the closed vocabulary the rest of
#: the product renders. Agents report in prose ("Warned", "blocked"); the
#: stored value is always one of the four enum spellings.
_GUARDRAIL_ACTION_SYNONYMS: Final[dict[str, str]] = {
    "block": GuardrailAction.BLOCK.value,
    "blocked": GuardrailAction.BLOCK.value,
    "mask": GuardrailAction.MASK.value,
    "masked": GuardrailAction.MASK.value,
    "redacted": GuardrailAction.MASK.value,
    "warn": GuardrailAction.WARN.value,
    "warned": GuardrailAction.WARN.value,
    "warning": GuardrailAction.WARN.value,
    "log": GuardrailAction.LOG.value,
    "logged": GuardrailAction.LOG.value,
    "log only": GuardrailAction.LOG.value,
}


def _guardrail_action(reported: str | None, *, fallback: str) -> str | None:
    """The stored action for a reported one; None when it cannot be normalised."""
    if reported is None or not reported.strip():
        return fallback
    return _GUARDRAIL_ACTION_SYNONYMS.get(reported.strip().lower())


async def ingest_events(
    session: AsyncSession,
    principal: Principal,
    parsed: ParsedBatch[EventIn],
    *,
    request: Request | None = None,
) -> IngestBatchResult:
    """Record governance events the SDK observed inside the customer's process.

    These do not belong in the telemetry store: a guardrail that fired, a policy
    the SDK enforced locally and end-user feedback are all control-plane state,
    and they land in the same tables the Guardrails, Policy Center and Feedback
    screens read. Every reference is resolved with one batched lookup, and an
    event naming a guardrail or policy this workspace does not have is rejected
    on its own row.
    """
    started = time.perf_counter()
    results = _seed_results(parsed)

    agents = await _resolve_agents(
        session,
        principal,
        {(item.agent or parsed.envelope.agent or "") for _, item in parsed.valid},
        request=request,
        allow_register=False,
    )

    guardrail_refs = {
        (item.guardrail or "").strip().lower()
        for _, item in parsed.valid
        if item.kind is IngestEventKind.GUARDRAIL_TRIGGERED
    }
    policy_refs = {
        (item.policy or "").strip().lower()
        for _, item in parsed.valid
        if item.kind is IngestEventKind.POLICY_VIOLATION
    }
    feedback_refs = {item.ref for _, item in parsed.valid if item.ref}

    guardrails = await _resolve_named(
        session,
        select(GuardrailConfig).where(GuardrailConfig.workspace_id == principal.workspace_id),
        GuardrailConfig,
        guardrail_refs,
    )
    policies = await _resolve_named(
        session,
        select(Policy).where(Policy.workspace_id == principal.workspace_id),
        Policy,
        policy_refs,
    )
    seen_refs = await _existing_feedback_refs(session, principal, feedback_refs)

    now = _now()
    recorded = 0
    violations = 0

    for index, event in parsed.valid:
        agent = agents.lookup(event.agent or parsed.envelope.agent)
        agent_id = agent.id if agent is not None else None
        occurred_at = _as_utc(event.occurred_at) if event.occurred_at else now

        if event.kind is IngestEventKind.GUARDRAIL_TRIGGERED:
            guardrail = guardrails.get((event.guardrail or "").strip().lower())
            if guardrail is None:
                _reject(
                    results,
                    index,
                    RejectionCode.UNKNOWN_GUARDRAIL,
                    f"No guardrail named '{event.guardrail}' exists in this workspace.",
                )
                continue
            action_taken = _guardrail_action(event.action_taken, fallback=guardrail.action)
            if action_taken is None:
                # Stored actions render through a closed vocabulary; accepting
                # an unknown spelling here would poison every later read of
                # the events table.
                _reject(
                    results,
                    index,
                    RejectionCode.MALFORMED,
                    f"'{event.action_taken}' is not a guardrail action; "
                    "use Block, Mask, Warn or Log.",
                )
                continue
            session.add(
                GuardrailEvent(
                    workspace_id=principal.workspace_id,
                    guardrail_id=guardrail.id,
                    agent_id=agent_id,
                    trace_id=event.trace_id,
                    occurred_at=occurred_at,
                    action_taken=action_taken,
                    score=event.score,
                    matched=dict(event.matched),
                    sample=event.sample,
                )
            )
            guardrail.last_triggered_at = occurred_at
            recorded += 1
            results[index] = results[index].model_copy(
                update={"agent_id": agent_id, "guardrail_id": guardrail.id}
            )
            continue

        if event.kind is IngestEventKind.POLICY_VIOLATION:
            policy = policies.get((event.policy or "").strip().lower())
            if policy is None:
                _reject(
                    results,
                    index,
                    RejectionCode.UNKNOWN_POLICY,
                    f"No policy named '{event.policy}' exists in this workspace.",
                )
                continue
            session.add(
                PolicyViolation(
                    workspace_id=principal.workspace_id,
                    policy_id=policy.id,
                    agent_id=agent_id,
                    trace_id=event.trace_id,
                    severity=event.severity or ViolationSeverity.MEDIUM.value,
                    action_taken=event.action_taken or policy.enforcement,
                    detail={**event.detail, "source": SOURCE_SCREEN, "reported_by_sdk": True},
                    occurred_at=occurred_at,
                )
            )
            policy.last_triggered_at = occurred_at
            recorded += 1
            violations += 1
            results[index] = results[index].model_copy(
                update={"agent_id": agent_id, "policy_id": policy.id}
            )
            continue

        ref = event.ref or new_id()
        if ref in seen_refs:
            _reject(
                results,
                index,
                RejectionCode.DUPLICATE,
                f"Feedback '{ref}' has already been recorded.",
            )
            continue
        seen_refs.add(ref)
        session.add(
            FeedbackItem(
                workspace_id=principal.workspace_id,
                feedback_ref=ref,
                agent_id=agent_id,
                trace_id=event.trace_id,
                rating=event.rating,
                sentiment=_sentiment(event),
                body=event.body,
                source=_feedback_source(event),
                submitted_by=event.submitted_by,
                submitted_at=occurred_at,
                event_metadata=dict(event.detail),
            )
        )
        recorded += 1
        results[index] = results[index].model_copy(update={"id": ref, "agent_id": agent_id})

    await session.flush()
    if recorded:
        await audit.record(
            session,
            principal=principal,
            action="ingest.events.recorded",
            entity_type=ENTITY_TYPE_BATCH,
            entity_label=f"{recorded} governance event(s)",
            source_screen=SOURCE_SCREEN,
            detail=(
                f"{recorded} SDK-reported governance event(s) recorded, "
                f"{violations} of them policy violations."
            ),
            metadata={"recorded": recorded, "violations": violations, "source": SOURCE_SDK},
            request=request,
        )

    result = _finish(results, parsed, started, agents, [], False, violations)
    result.events_recorded = recorded
    return result


def _sentiment(event: EventIn) -> str:
    """Normalise the sentiment chip, deriving it from the rating when absent.

    The wire field is free text because SDKs in several languages write it, and
    they do not agree on case. Every read parses this column back into the
    :class:`Sentiment` enum, so a value that does not match one of its members
    is not merely untidy — it makes the row unreadable and takes the whole
    feedback list down with it. Accept what an SDK plausibly sends, store only
    what the rest of the system can read.
    """
    if event.sentiment:
        wanted = event.sentiment.strip().casefold()
        for member in Sentiment:
            if member.value.casefold() == wanted:
                return member.value
        # Unrecognised: fall through to the rating rather than store a value no
        # reader can parse. The submitted text stays in the event metadata.
    if event.rating is None:
        return Sentiment.NEUTRAL.value
    if event.rating >= 4:
        return Sentiment.POSITIVE.value
    if event.rating <= 2:
        return Sentiment.NEGATIVE.value
    return Sentiment.NEUTRAL.value


def _feedback_source(event: EventIn) -> str:
    """Same contract as :func:`_sentiment`, for the source chip."""
    if event.source:
        wanted = event.source.strip().casefold()
        for member in FeedbackSource:
            if member.value.casefold() == wanted:
                return member.value
    return FeedbackSource.END_USER.value


async def _resolve_named(
    session: AsyncSession, stmt: Select[Any], model: Any, refs: set[str]
) -> dict[str, Any]:
    """Resolve a set of ids-or-names to rows in a single statement."""
    refs = {ref for ref in refs if ref}
    if not refs:
        return {}
    rows = (
        (
            await session.execute(
                stmt.where(or_(func.lower(model.name).in_(refs), model.id.in_(refs)))
            )
        )
        .scalars()
        .all()
    )
    index: dict[str, Any] = {}
    for row in rows:
        index.setdefault(row.id.lower(), row)
        index.setdefault(row.name.lower(), row)
    return {ref: index[ref] for ref in refs if ref in index}


async def _existing_feedback_refs(
    session: AsyncSession, principal: Principal, refs: set[str]
) -> set[str]:
    """Idempotency keys already used, so a resent batch reports duplicates."""
    if not refs:
        return set()
    return set(
        (
            await session.execute(
                select(FeedbackItem.feedback_ref).where(
                    FeedbackItem.workspace_id == principal.workspace_id,
                    FeedbackItem.feedback_ref.in_(refs),
                )
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# SDK start-up configuration
# ---------------------------------------------------------------------------


async def sdk_config(session: AsyncSession, principal: Principal) -> IngestConfigRead:
    """Everything an SDK needs to configure itself, in one small document.

    Tenant overrides live in ``workspaces.settings['ingest']`` so an operator can
    dial sampling down for a noisy tenant without a deployment; the transport
    limits come from the service configuration, because they are properties of
    this deployment rather than of the tenant.
    """
    workspace = await session.get(Workspace, principal.workspace_id)
    overrides: Mapping[str, Any] = {}
    if workspace is not None and isinstance(workspace.settings, dict):
        raw = workspace.settings.get("ingest")
        if isinstance(raw, dict):
            overrides = raw

    agent = await _bound_agent(session, principal)
    scope_refs = {agent.id, agent.environment} if agent is not None else set()

    guardrail_rows = (
        (
            await session.execute(
                select(GuardrailConfig)
                .where(
                    GuardrailConfig.workspace_id == principal.workspace_id,
                    GuardrailConfig.status.in_(
                        (GuardrailStatus.ACTIVE.value, GuardrailStatus.TUNING.value)
                    ),
                )
                .order_by(GuardrailConfig.name.asc())
            )
        )
        .scalars()
        .all()
    )
    # An unbound key may report as any agent in the workspace, so every
    # guardrail could end up applying to what it sends; it receives them all so
    # client-side masking works whichever agent a batch names. A bound key gets
    # only its own agent's scope.
    applicable = [
        row
        for row in guardrail_rows
        if row.scope == GuardrailScope.GLOBAL.value
        or agent is None
        or row.scope_ref in scope_refs
    ]

    prefix = settings.api_prefix.rstrip("/")
    config = IngestConfigRead(
        workspace=principal.workspace_slug,
        environment=agent.environment if agent is not None else None,
        agent_id=agent.id if agent is not None else None,
        agent_name=agent.name if agent is not None else None,
        agent_bound=agent is not None,
        sampling_rate=_bounded(overrides.get("sampling_rate"), DEFAULT_SAMPLING_RATE, 0.0, 1.0),
        batch_max_spans=settings.ingest_max_batch_spans,
        batch_max_bytes=settings.ingest_max_body_bytes,
        flush_interval_seconds=_bounded(
            overrides.get("flush_interval_seconds"), DEFAULT_FLUSH_INTERVAL_SECONDS, 0.1, 300.0
        ),
        max_queue_size=settings.ingest_queue_max_pending,
        retry_max_attempts=int(
            _bounded(overrides.get("retry_max_attempts"), DEFAULT_RETRY_ATTEMPTS, 0, 10)
        ),
        retry_backoff_seconds=_bounded(
            overrides.get("retry_backoff_seconds"), DEFAULT_RETRY_BACKOFF_SECONDS, 0.05, 60.0
        ),
        capture_input=bool(overrides.get("capture_input", True)),
        capture_output=bool(overrides.get("capture_output", True)),
        endpoints={
            "traces": f"{prefix}/ingest/traces",
            "spans": f"{prefix}/ingest/spans",
            "scores": f"{prefix}/ingest/scores",
            "events": f"{prefix}/ingest/events",
            "config": f"{prefix}/ingest/config",
            "otlp_traces": "/v1/traces",
        },
        guardrails=[
            GuardrailDescriptor(
                id=row.id,
                name=row.name,
                type=row.guardrail_type,
                action=row.action,
                threshold=row.threshold,
                scope=row.scope,
                scope_ref=row.scope_ref,
                status=row.status,
            )
            for row in applicable
        ],
        redaction=_redaction_rules(overrides, applicable),
        revision="",
        refresh_after_seconds=CONFIG_CACHE_SECONDS,
    )
    config.revision = _revision(config)
    return config


def _bounded(value: Any, default: float, low: float, high: float) -> float:
    """Clamp a tenant override into a range the SDK can actually honour."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def _redaction_rules(
    overrides: Mapping[str, Any], guardrails: Sequence[GuardrailConfig]
) -> list[RedactionRule]:
    """What the SDK must strip before content ever leaves the customer's process.

    Two real sources: rules an operator wrote into the workspace settings, and
    every guardrail configured to mask rather than block — masking at our end is
    too late, so the SDK is told to do it at source.
    """
    rules: list[RedactionRule] = []
    raw = overrides.get("redaction")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            rules.append(
                RedactionRule(
                    id=str(item.get("id") or item["name"]),
                    name=str(item["name"]),
                    source="workspace",
                    entity_types=[str(t) for t in item.get("entity_types", []) if t],
                    pattern=str(item["pattern"]) if item.get("pattern") else None,
                    replacement=str(item.get("replacement") or REDACTION_MARKER),
                    applies_to=[str(f) for f in item.get("applies_to", ["input", "output"]) if f],
                )
            )

    for guardrail in guardrails:
        if guardrail.action != GuardrailAction.MASK.value:
            continue
        config = guardrail.config if isinstance(guardrail.config, dict) else {}
        rules.append(
            RedactionRule(
                id=guardrail.id,
                name=guardrail.name,
                source="guardrail",
                entity_types=[str(t) for t in config.get("entity_types", []) if t],
                pattern=str(config["pattern"]) if config.get("pattern") else None,
                replacement=str(config.get("replacement") or REDACTION_MARKER),
                applies_to=[str(f) for f in config.get("applies_to", ["input", "output"]) if f],
            )
        )
    return rules


def _revision(config: IngestConfigRead) -> str:
    """Stable fingerprint of the document, so the SDK can revalidate cheaply."""
    payload = config.model_dump(mode="json", exclude={"revision"})
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest[:32]


# ---------------------------------------------------------------------------
# Result plumbing shared by every endpoint
# ---------------------------------------------------------------------------


def _seed_results(parsed: ParsedBatch[Any]) -> list[IngestItemResult]:
    """One result row per submitted item, malformed ones already accounted for."""
    results: list[IngestItemResult] = []
    for index in range(parsed.received):
        reason = parsed.errors.get(index)
        results.append(
            IngestItemResult(
                index=index,
                outcome=ItemOutcome.REJECTED if reason else ItemOutcome.ACCEPTED,
                code=RejectionCode.MALFORMED if reason else None,
                reason=reason,
            )
        )
    return results


def _reject(results: list[IngestItemResult], index: int, code: RejectionCode, reason: str) -> None:
    results[index] = results[index].model_copy(
        update={"outcome": ItemOutcome.REJECTED, "code": code, "reason": reason}
    )


def _mark(
    results: list[IngestItemResult],
    index: int,
    *,
    entity_id: str,
    agent_id: str,
    spans: int,
    masked: bool = False,
) -> None:
    results[index] = results[index].model_copy(
        update={"id": entity_id, "agent_id": agent_id, "spans": spans, "masked": masked}
    )


def _block(
    results: list[IngestItemResult], index: int, hit: _Hit, *, agent_id: str, spans: int
) -> None:
    signals = ", ".join(hit.matched_signals) or "all conditions matched"
    reason = hit.rule.message or f"Policy '{hit.rule.name}' blocked this item ({signals})."
    results[index] = results[index].model_copy(
        update={
            "outcome": ItemOutcome.BLOCKED,
            "code": RejectionCode.POLICY_BLOCKED,
            "reason": reason,
            "agent_id": agent_id,
            "policy_id": hit.rule.policy_id,
            "spans": spans,
        }
    )


def _bind(
    agents: _Agents, wanted: str | None, results: list[IngestItemResult], index: int
) -> Agent | None:
    """Attach an item to its agent, refusing a cross-agent write.

    A key bound to one agent may not report for another, and saying so plainly
    is safe: the caller already knows which agent its key was issued for.
    """
    if agents.bound is not None:
        bound = agents.bound
        known = {bound.slug.lower(), bound.name.lower()}
        if wanted and wanted.strip().lower() not in known:
            _reject(
                results,
                index,
                RejectionCode.AGENT_NOT_PERMITTED,
                f"This API key may only report for '{bound.name}'.",
            )
            return None
        return bound

    agent = agents.lookup(wanted)
    if agent is None:
        _reject(
            results,
            index,
            RejectionCode.UNKNOWN_AGENT,
            (
                f"No agent named '{wanted}' exists in this workspace."
                if wanted
                else "The item names no agent and the batch sets no default."
            ),
        )
        return None
    if not agent.engine_project_name:
        _reject(
            results,
            index,
            RejectionCode.AGENT_UNPROVISIONED,
            f"'{agent.name}' has no telemetry project yet; provision it before reporting.",
        )
        return None
    return agent


def _apply_verdicts(
    accepted: list[_Accepted],
    verdicts: Sequence[GuardrailVerdict],
    results: list[IngestItemResult],
) -> list[_Accepted]:
    """Turn guardrail decisions into blocks and masks on the accepted items."""
    if not verdicts:
        return accepted

    blocked: dict[int, GuardrailVerdict] = {}
    masked: dict[int, GuardrailVerdict] = {}
    for verdict in verdicts:
        if verdict.action == GuardrailAction.BLOCK.value:
            blocked.setdefault(verdict.index, verdict)
        elif verdict.action == GuardrailAction.MASK.value:
            masked.setdefault(verdict.index, verdict)

    survivors: list[_Accepted] = []
    for entry in accepted:
        verdict = blocked.get(entry.index)
        if verdict is not None:
            named = verdict.guardrail_name or verdict.guardrail_id
            results[entry.index] = results[entry.index].model_copy(
                update={
                    "outcome": ItemOutcome.BLOCKED,
                    "code": RejectionCode.GUARDRAIL_BLOCKED,
                    "reason": verdict.reason or f"Guardrail '{named}' blocked this item.",
                    "guardrail_id": verdict.guardrail_id,
                }
            )
            continue
        mask = masked.get(entry.index)
        if mask is not None:
            entry.masked = True
            results[entry.index] = results[entry.index].model_copy(
                update={"masked": True, "guardrail_id": mask.guardrail_id}
            )
        survivors.append(entry)
    return survivors


async def _record_evidence(
    session: AsyncSession,
    principal: Principal,
    parsed: ParsedBatch[Any],
    results: Sequence[IngestItemResult],
    hits_by_index: Mapping[int, list[_Hit]],
    verdicts: Sequence[GuardrailVerdict],
    accepted: Sequence[_Accepted],
    *,
    entity: str,
    source: str,
    request: Request | None,
) -> int:
    """Write the violation and guardrail rows this batch earned, then audit them.

    Only governance outcomes are written. Routine acceptance leaves no audit
    row: it is telemetry, not a change to governed state, and a row per batch
    would bury the trail the audit screen exists to show.
    """
    now = _now()
    trace_by_index = {entry.index: entry.trace_id for entry in accepted}

    rows: list[Any] = []
    blocked_count = 0
    truncated = False

    for index, hits in hits_by_index.items():
        result = results[index]
        if result.outcome is ItemOutcome.REJECTED or not result.agent_id:
            # A rejected item never reached the store, so a violation naming its
            # trace id would point at nothing.
            continue
        if result.outcome is ItemOutcome.BLOCKED:
            blocked_count += 1
        for hit in hits:
            if len(rows) >= MAX_EVIDENCE_ROWS:
                truncated = True
                break
            rows.append(
                PolicyViolation(
                    workspace_id=principal.workspace_id,
                    policy_id=hit.rule.policy_id,
                    agent_id=result.agent_id,
                    trace_id=trace_by_index.get(index),
                    severity=hit.rule.severity,
                    action_taken=hit.rule.enforcement,
                    detail={
                        "source": SOURCE_SCREEN,
                        "entity": entity,
                        "policy_name": hit.rule.name,
                        "matched_signals": list(hit.matched_signals),
                        "message": hit.rule.message,
                        "blocked": result.outcome is ItemOutcome.BLOCKED and hit.blocks,
                    },
                    occurred_at=now,
                )
            )

    guardrail_rows = 0
    for verdict in verdicts:
        # Log verdicts are recorded too: a Tuning guardrail's whole point is
        # that its shadow trials show up on the Guardrails screen.
        if len(rows) >= MAX_EVIDENCE_ROWS:
            truncated = True
            break
        agent_id = results[verdict.index].agent_id if verdict.index < len(results) else None
        rows.append(
            GuardrailEvent(
                workspace_id=principal.workspace_id,
                guardrail_id=verdict.guardrail_id,
                agent_id=agent_id,
                trace_id=trace_by_index.get(verdict.index),
                occurred_at=now,
                action_taken=verdict.action,
                score=verdict.score,
                matched=dict(verdict.matched),
                sample=verdict.sample,
            )
        )
        guardrail_rows += 1

    # Connector blocks carry no policy hit, so they are counted off the results
    # directly; without this the audit trail would show nothing for a batch the
    # platform refused wholesale.
    connector_blocked = sum(
        1 for row in results if row.code is RejectionCode.CONNECTOR_BLOCKED
    )
    blocked_count += connector_blocked

    if not rows and not connector_blocked:
        return 0

    session.add_all(rows)
    # The Policy Center's "last triggered" reads this column; a pipeline hit
    # counts as a trigger just as much as an SDK-reported one does.
    policy_ids = {row.policy_id for row in rows if isinstance(row, PolicyViolation)}
    if policy_ids:
        await session.execute(
            sa_update(Policy)
            .where(Policy.workspace_id == principal.workspace_id, Policy.id.in_(policy_ids))
            .values(last_triggered_at=now)
        )
    await session.flush()
    violations = sum(1 for row in rows if isinstance(row, PolicyViolation))

    if blocked_count or guardrail_rows:
        await audit.record(
            session,
            principal=principal,
            action="ingest.items.blocked" if blocked_count else "ingest.guardrails.triggered",
            entity_type=ENTITY_TYPE_BATCH,
            entity_label=f"{parsed.received} item(s)",
            source_screen=SOURCE_SCREEN,
            detail=(
                f"{blocked_count} of {parsed.received} item(s) refused by governance; "
                f"{violations} policy violation(s) and {guardrail_rows} guardrail "
                "event(s) recorded."
            ),
            metadata={
                "source": source,
                "received": parsed.received,
                "blocked": blocked_count,
                "connector_blocked": connector_blocked,
                "violations": violations,
                "guardrail_events": guardrail_rows,
                "evidence_truncated": truncated,
            },
            request=request,
        )
    return violations


async def _touch_agents(session: AsyncSession, agents: _Agents, *, accepted: bool) -> None:
    """Stamp ``last_used_at`` once per batch, not once per item."""
    if not accepted:
        return
    now = _now()
    for agent in agents.touched:
        agent.last_used_at = now
    await session.flush()


def _finish(
    results: list[IngestItemResult],
    parsed: ParsedBatch[Any],
    started: float,
    agents: _Agents,
    quotas: Sequence[IngestQuotaState],
    guardrails_evaluated: bool,
    violations: int,
) -> IngestBatchResult:
    """Assemble the response every ingest endpoint returns."""
    accepted = sum(1 for row in results if row.outcome is ItemOutcome.ACCEPTED)
    rejected = sum(1 for row in results if row.outcome is ItemOutcome.REJECTED)
    blocked = sum(1 for row in results if row.outcome is ItemOutcome.BLOCKED)
    return IngestBatchResult(
        received=parsed.received,
        accepted=accepted,
        rejected=rejected,
        blocked=blocked,
        guardrails_evaluated=guardrails_evaluated,
        violations_recorded=violations,
        agents=sorted({row.agent_id for row in results if row.agent_id}),
        auto_registered=[
            AutoRegisteredAgent(
                id=agent.id,
                name=agent.name,
                slug=agent.slug,
                environment=agent.environment,
                status=agent.status,
                engine_project_name=agent.engine_project_name,
            )
            for agent in agents.registered
        ],
        quotas=list(quotas),
        results=results,
        duration_ms=int(round((time.perf_counter() - started) * 1000)),
    )
