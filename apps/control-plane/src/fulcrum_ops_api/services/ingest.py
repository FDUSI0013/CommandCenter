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

import asyncio
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
from typing import Any, Final, TypeVar

from fastapi import Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified, set_committed_value

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
from ..db.base import new_id, stamp
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
    ErrorInfoIn,
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
from ..schemas.quota import WATCH_PERCENT
from . import audit, licensing, telemetry_cache

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

#: Most calls one refused batch may cost while the rows at fault are found.
MAX_ISOLATION_CALLS: Final[int] = 40

#: Content handed to condition matching and to the guardrail evaluator, in
#: characters. Bounds both regular-expression cost and hook payload size.
MAX_CONTENT_CHARS: Final[int] = 8_192

#: Longest regular expression a policy condition may carry.
MAX_PATTERN_LENGTH: Final[int] = 500

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

#: Enforcement modes that only record a match. They leave the run "Allowed".
SILENT_ENFORCEMENTS: Final[frozenset[str]] = frozenset(
    {PolicyEnforcement.LOG_ONLY.value, PolicyEnforcement.ALLOW.value}
)

#: Policy states in which a policy is evaluated against telemetry.
ENFORCED_POLICY_STATUSES: Final[tuple[str, ...]] = (
    PolicyStatus.ACTIVE.value,
    PolicyStatus.WARNING.value,
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

    The store is stricter than this: the UUID has to be version 7, which is what
    both SDKs and :func:`new_id` mint. A well-formed id of another version still
    passes here and is refused there; :func:`_push_rows` then narrows the refusal
    down to the row that carries it, and :func:`_explained` says why.
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


async def _push(
    call: BatchCall, payload: Sequence[Mapping[str, Any]]
) -> tuple[int, str] | None:
    """Hand one batch to the telemetry store.

    Returns ``None`` on success, or the status and the reason to stamp on the
    items when the store refused them. An *unreachable* store is different: the
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
        return exc.status, f"The telemetry store refused the batch (status {exc.status})."
    except EngineError as exc:  # pragma: no cover - the adapter raises the two above
        raise TelemetryBackendUnavailable() from exc
    return None


#: Refusals that are about what a batch *contains*, and so worth narrowing down.
#: Anything else -- a throttle, a credential, a size limit -- is about the call,
#: and asking again in smaller pieces only adds to it.
_CONTENT_REFUSALS: Final[frozenset[int]] = frozenset({400, 409, 422})


async def _push_rows(call: BatchCall, rows: Sequence[Mapping[str, Any]]) -> list[str | None]:
    """Hand rows to the telemetry store and learn which of them it would not take.

    The store validates a batch as a whole: one row it dislikes -- an id that is
    not version 7, an error without a traceback, a rule this service has never
    heard of -- and it refuses every row sent with it. Reporting that refusal
    against all of them cost forty healthy traces their acceptance for one bad
    one, which is the opposite of what the per-item results promise.

    So a refused batch is divided and asked again until the rows at fault stand
    alone. The common case is still one call. The first cut is not blind: the
    one refusal that can be seen from here is an id that is not version 7, so
    the rows that carry one are set apart from the rows that do not. A reporter
    that mints its own ids does so for *every* row, and halving its batch would
    have spent the whole search -- forty refused calls, against a store that may
    already be behind -- rediscovering that one by one; this way it costs three.
    Whatever else the store dislikes is found by halving, about twenty calls for
    one bad row among a thousand. The search is capped, and what is still
    unresolved at the cap is reported refused together, which is no worse than
    before. Returns one entry per row: ``None`` for stored, else the reason.
    """
    refusals: list[str | None] = [None] * len(rows)
    budget = MAX_ISOLATION_CALLS
    pending: list[list[int]] = [list(range(len(rows)))] if rows else []
    while pending:
        group = pending.pop()
        refusal = await _push(call, [rows[at] for at in group])
        if refusal is None:
            continue
        status, reason = refusal
        suspects = [at for at in group if _unaddressable(rows[at])]
        if (
            len(group) == 1
            or budget < 2
            or status not in _CONTENT_REFUSALS
            # Every row here has the fault the store is known to refuse for,
            # and it has just refused them: there is nothing left to narrow.
            or len(suspects) == len(group)
        ):
            for at in group:
                refusals[at] = reason
            continue
        budget -= 2
        if suspects:
            apart = set(suspects)
            pending.append(suspects)
            pending.append([at for at in group if at not in apart])
        else:
            middle = len(group) // 2
            pending.append(group[middle:])
            pending.append(group[:middle])
    return refusals


def _is_v7(value: str | None) -> bool:
    try:
        return uuid.UUID(str(value)).version == 7
    except (ValueError, AttributeError, TypeError):
        return False


def _unaddressable(row: Mapping[str, Any]) -> bool:
    """Whether a row names a trace or a span by an id the store will not address."""
    return any(value and not _is_v7(value) for value in (row.get("id"), row.get("trace_id")))


def _explained(reason: str, *ids: str | None) -> str:
    """Add the likeliest cause to a store refusal, when the row shows it."""
    if any(value and not _is_v7(value) for value in ids):
        return (
            f"{reason} The store addresses traces and spans by version 7 UUIDs only; "
            "send one, or omit the id and one is minted."
        )
    return reason


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


def _reported_environments(items: Iterable[Any], envelope: Any) -> dict[str, str]:
    """agent name (lower-cased) -> the environment its own telemetry states.

    The SDKs stamp every trace with the environment they were configured with.
    It is read only to decide where a *never-seen* agent is filed; an agent that
    is already registered keeps whatever an operator set.
    """
    allowed = {env.value.lower(): env.value for env in EnvironmentType}
    reported: dict[str, str] = {}
    for item in items:
        name = (getattr(item, "agent", None) or getattr(envelope, "agent", None) or "").strip()
        metadata = getattr(item, "metadata", None)
        stated = metadata.get("environment") if isinstance(metadata, Mapping) else None
        if name and isinstance(stated, str) and stated.strip().lower() in allowed:
            reported.setdefault(name.lower(), allowed[stated.strip().lower()])
    return reported


async def _key_environment(
    session: AsyncSession, principal: Principal, reported: str | None = None
) -> str:
    """Environment an auto-registered agent is filed under.

    What the agent says about itself wins: a service configured as Production
    and filed as Development lists under the wrong tile, inherits the wrong
    policy set, and reads as a mistake to whoever goes looking for it. The key's
    environment is the fallback, and Development the fallback to that.
    """
    allowed = {env.value.lower(): env.value for env in EnvironmentType}
    if reported and reported.strip().lower() in allowed:
        return allowed[reported.strip().lower()]
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
    session: AsyncSession,
    principal: Principal,
    name: str,
    *,
    request: Request | None,
    reported_environment: str | None = None,
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
        environment=await _key_environment(session, principal, reported_environment),
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
    environments: Mapping[str, str] | None = None,
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
        agent = await _register_agent(
            session,
            principal,
            name,
            request=request,
            reported_environment=(environments or {}).get(name.lower()),
        )
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
                    # Warning is "firing more than its baseline", which is a
                    # policy that is still in force -- the Policy Center labels
                    # it "Enforcing with warnings". Reading Active alone meant
                    # flagging a policy quietly switched it off.
                    Policy.status.in_(ENFORCED_POLICY_STATUSES),
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
    """Run every applicable policy over one item's signals.

    A clause about a signal the item does not carry is *not matched*, whatever
    the rule's fail mode. Absence is the normal case, not an evasion: a trace
    has no ``tool_name``, a span that is not a model call has no ``provider``,
    and a run nobody has scored has no ``safety_score``. Counting absence as a
    match made a rule like "safety_score below 0.82" -- the body the Policy
    Center derives for a blank form -- fire on every item in the workspace and,
    with the default enforcement, refuse all of its telemetry. ``exists`` is
    the operator for a rule that really is about presence.

    Fail-closed still means what it says for a clause that *is* present and
    cannot be decided: an expression that does not compile, a value that cannot
    be ordered.
    """
    hits: list[_Hit] = []
    for rule in rules:
        matched: list[str] = []
        for condition in rule.conditions:
            value = signals.get(condition.signal)
            if value is None and condition.operator != "exists":
                continue
            verdict = _compare(condition.operator, value, condition.value)
            if verdict is None:
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


#: Signals that describe a span rather than a run. A trace reported with its
#: spans nested inside it -- which is how both SDKs report -- is asked about
#: these through each of those spans.
_SPAN_SIGNALS: Final[frozenset[str]] = frozenset({"span_type", "tool_name", "provider", "model"})
_CONTENT_SIGNALS: Final[frozenset[str]] = frozenset({"content", "content_length"})


def _nested_hits(
    rules: Sequence[_Rule], agent: Agent, trace: TraceIn, fired: set[str]
) -> list[_Hit]:
    """Policies a trace breaks through one of the spans it carries.

    A rule about a tool, a provider or a span type can only ever be answered by
    a span, and a trace-level evaluation has none of those to offer -- so such a
    rule never fired on ``/ingest/traces``, the endpoint nearly all telemetry
    arrives on. Only the rules that name a span signal are run here, each fires
    at most once per trace, and the hit is reported against the trace: that is
    the item the caller sent and the one a block refuses.
    """
    pending = [
        rule
        for rule in rules
        if rule.policy_id not in fired
        and any(condition.signal in _SPAN_SIGNALS for condition in rule.conditions)
    ]
    if not pending or not trace.spans:
        return []
    wants_content = any(
        condition.signal in _CONTENT_SIGNALS for rule in pending for condition in rule.conditions
    )
    hits: list[_Hit] = []
    for span in trace.spans:
        if not pending:
            break
        found = _evaluate(
            pending,
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
                thread_id=trace.thread_id,
                content=_text_of(span.input, span.output) if wants_content else "",
            ),
        )
        if found:
            hits.extend(found)
            done = {hit.rule.policy_id for hit in found}
            pending = [rule for rule in pending if rule.policy_id not in done]
    return hits


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


def _quota_covers(quota: Quota, agent: Agent) -> bool:
    """Whether one agent's traffic counts against one quota."""
    if quota.scope == LimitScope.WORKSPACE.value:
        return True
    if not quota.scope_ref:
        return False
    if quota.scope == LimitScope.AGENT.value:
        return agent.id == quota.scope_ref
    if quota.scope == LimitScope.ENVIRONMENT.value:
        return agent.environment == quota.scope_ref
    if quota.scope == LimitScope.TEAM.value:
        return agent.team == quota.scope_ref
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


def _quota_amount(quota: Quota, entries: Sequence[_Accepted]) -> float:
    """What a set of items costs one quota: only the items it actually covers.

    A batch from an unbound key may carry several agents. A quota scoped to one
    of them -- or to its team or environment -- is charged for that agent's items
    and nobody else's, not for the whole batch because the agent appears in it.
    """
    covered = [entry for entry in entries if _quota_covers(quota, entry.agent)]
    if quota.resource == QuotaResource.REQUESTS.value:
        return float(len(covered))
    return float(sum(entry.tokens for entry in covered))


def _quota_status(used: float, limit: float) -> tuple[float, str]:
    """Utilisation and chip colour, on the thresholds the Quota screen uses.

    Ingest used to keep a threshold of its own (80%) beside the service's (70%),
    so an edit at 75% turned a quota amber and the next batch turned it back.
    """
    utilization = round(used / limit * 100, 1) if limit > 0 else 0.0
    if limit > 0 and utilization >= 100:
        return utilization, LimitStatus.EXCEEDED.value
    if limit > 0 and utilization >= WATCH_PERCENT:
        return utilization, LimitStatus.WARNING.value
    return utilization, LimitStatus.ACTIVE.value


#: States in which a quota is metered and its chip follows its usage. The others
#: -- disabled, expired -- are somebody's decision and only a person undoes them.
_METERED_STATUSES: Final[tuple[str, ...]] = (
    LimitStatus.ACTIVE.value,
    LimitStatus.WARNING.value,
    LimitStatus.EXCEEDED.value,
)


async def _roll_period(session: AsyncSession, quota: Quota, now: dt.datetime) -> None:
    """Start a quota's next period, if nobody else already has.

    The period elapsed while nothing was reporting: roll it forward to the first
    window that contains "now" and start the counter again. The reset is guarded
    on the old ``resets_at`` so that of two workers meeting the same expired
    quota only one zeroes it; the other matches no row, and neither can wipe out
    usage the first has counted since. The row is then re-read, so the hard-limit
    check that follows sees what is stored rather than what this request assumed.
    """
    expired = quota.resets_at
    resets_at = _as_utc(expired)
    for _ in range(64):
        if resets_at > now:
            break
        resets_at = _advance(resets_at, quota.period)
    await session.execute(
        sa_update(Quota)
        .where(Quota.id == quota.id, Quota.resets_at == expired)
        .values(
            used_value=0.0,
            status=LimitStatus.ACTIVE.value,
            resets_at=resets_at,
            # Housekeeping, not an edit: see ``db.base.stamp``.
            updated_at=Quota.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(quota)


async def _prepare_quotas(
    session: AsyncSession,
    principal: Principal,
    entries: Sequence[_Accepted],
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
                    Quota.status.in_(_METERED_STATUSES),
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
        if not any(_quota_covers(quota, entry.agent) for entry in entries):
            continue

        if quota.resets_at is not None and _as_utc(quota.resets_at) <= now:
            await _roll_period(session, quota, now)

        amount = _quota_amount(quota, entries)
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


async def _commit_quotas(
    session: AsyncSession,
    charges: Sequence[_Charge],
    entries: Sequence[_Accepted],
    *,
    request: Request | None = None,
) -> list[IngestQuotaState]:
    """Count what the store actually took, and say so when a threshold is crossed.

    The counter is incremented *in the database*. It used to be read before the
    hand-off to the telemetry store and written back, as an absolute value,
    after it -- seconds later, on four workers -- so two batches in flight both
    started from the same number and the second overwrote the first. Usage was
    under-reported for the rest of the period and a Block quota let through
    whatever was lost. ``used_value = used_value + n`` cannot lose an update.

    ``entries`` are the items that survived the hand-off, so a trace the store
    refused, or the tokens of spans it would not take, are not billed. Quotas are
    visited in id order so two workers holding several of the same rows cannot
    deadlock each other.
    """
    states: list[IngestQuotaState] = []
    crossed: list[Quota] = []
    for charge in sorted(charges, key=lambda item: item.quota.id):
        quota = charge.quota
        amount = _quota_amount(quota, entries)
        if amount <= 0:
            continue
        row = (
            await session.execute(
                sa_update(Quota)
                .where(Quota.id == quota.id)
                .values(
                    used_value=func.coalesce(Quota.used_value, 0.0) + amount,
                    # Metering is not an edit to the quota: see ``db.base.stamp``.
                    updated_at=Quota.updated_at,
                )
                .returning(Quota.used_value, Quota.limit_value, Quota.status)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if row is None:  # deleted while the batch was in flight
            continue
        used, limit, stored = float(row[0] or 0.0), float(row[1] or 0.0), row[2]
        utilization, status = _quota_status(used, limit)
        set_committed_value(quota, "used_value", used)
        # The status is judged against what is stored *now* -- the increment has
        # just locked the row -- not against what this request loaded before the
        # round trip. A quota somebody disabled meanwhile stays disabled, and a
        # threshold another worker has already crossed is not announced twice.
        if stored not in _METERED_STATUSES:
            status = stored
        elif status != stored:
            await session.execute(
                sa_update(Quota)
                .where(Quota.id == quota.id)
                .values(status=status, updated_at=Quota.updated_at)
                .execution_options(synchronize_session=False)
            )
            crossed.append(quota)
        states.append(
            IngestQuotaState(
                id=quota.id,
                name=quota.name,
                resource=quota.resource,
                scope=quota.scope,
                scope_ref=quota.scope_ref,
                unit=quota.unit,
                limit_value=limit,
                used_value=used,
                remaining=max(limit - used, 0.0) if limit > 0 else 0.0,
                utilization_pct=utilization,
                enforcement=quota.enforcement,
                status=status,
                resets_at=quota.resets_at,
            )
        )
    for quota in crossed:
        await _announce_quota(session, quota, request=request)
    return states


async def _announce_quota(
    session: AsyncSession, quota: Quota, *, request: Request | None
) -> None:
    """Raise the Quota screen's own alert for a threshold this batch crossed.

    The alert -- its wording, severity and de-duplication -- belongs to the quota
    service, which until now was the only place that raised it and was reachable
    only from an admin's edit. Ingest is where quotas actually fill up, so a
    quota went amber, then red, then started refusing telemetry, and nobody was
    told. The instance still carries the status it was loaded with, which is what
    lets the service see the transition.

    Strictly best effort, inside a savepoint: the telemetry store has already
    taken this batch, and a fault in alerting must not turn that into a 500 the
    reporter answers by sending it all again.
    """
    try:
        from . import quota as quota_service
    except ImportError:  # pragma: no cover - the service ships with this one
        return
    evaluate = getattr(quota_service, "_evaluate_quota", None)
    if not callable(evaluate):
        return
    try:
        async with session.begin_nested():
            # The service moves the status on the instance, and the flush that
            # follows writes it as an edit -- moving ``updated_at`` under the
            # admin who opened the form *because* the quota is filling up.
            # Naming the column in that UPDATE, at the value it already has, is
            # what stops ``onupdate``: see ``db.base.stamp``.
            flag_modified(quota, "updated_at")
            await evaluate(session, quota, request=request)
            await session.flush()
    except Exception:  # noqa: BLE001 - see above: never at the batch's expense
        logger.exception("quota threshold alert could not be raised for quota %s", quota.id)


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


#: Containers are walked this deep when redacting; anything deeper is replaced
#: whole rather than searched, so a hostile payload cannot make this expensive.
_REDACTION_DEPTH: Final[int] = 12


def _scrub(value: Any, needles: Sequence[str], depth: int = 0) -> Any:
    """Return ``value`` with every occurrence of each needle replaced by the marker."""
    if isinstance(value, str):
        for needle in needles:
            if needle in value:
                value = value.replace(needle, REDACTION_MARKER)
        return value
    if depth >= _REDACTION_DEPTH:
        return _redacted() if isinstance(value, (dict, list, tuple)) else value
    if isinstance(value, dict):
        return {key: _scrub(item, needles, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, needles, depth + 1) for item in value]
    return value


def _masked_payload(value: Any, masked: bool, redactions: Sequence[str]) -> Any:
    """What a masked item stores in place of ``value``.

    A guardrail that masks has found *something* in the content, not condemned
    all of it. When the checker says what it found, exactly that is removed and
    the rest of the run stays readable -- a support transcript with an email
    address in it is still a support transcript. Only a verdict that cannot say
    what it matched (a topic classifier has no span to point at) falls back to
    withholding the whole payload, which is the safe reading of "mask this".
    """
    if not masked or value is None:
        return value
    if not redactions:
        return _redacted()
    # Longest first, so a needle that contains another is removed as one piece.
    needles = sorted({needle for needle in redactions if needle}, key=len, reverse=True)
    return _scrub(value, needles)


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


#: The store requires a traceback on every error it is given and refuses the
#: batch without one. Plenty of failures have none to give -- a status code from
#: an OpenTelemetry span, a non-Error value thrown in JavaScript -- so the gap is
#: stated rather than left to cost the batch.
NO_TRACEBACK: Final[str] = "No traceback was reported with this error."


def _engine_error(error: ErrorInfoIn | None) -> dict[str, Any] | None:
    if error is None:
        return None
    payload = error.model_dump(exclude_none=True)
    if not (payload.get("traceback") or "").strip():
        payload["traceback"] = NO_TRACEBACK
    return payload


def _parent_id(value: str | None) -> str | None:
    """A parent reference the store can bind, or none at all.

    The store reads this field as a UUID and refuses the whole batch when it
    cannot. A span that names an unusable parent is still a span: it is stored
    at the top of its trace rather than costing every span sent with it.
    """
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


#: Most tool names stamped on one run; the table draws a handful of chips.
MAX_RUN_TOOLS: Final[int] = 20


def _trace_tools(trace: TraceIn) -> list[str]:
    """The tools this run called, in call order, each named once."""
    names: dict[str, None] = {}
    for span in trace.spans:
        if span.type is SpanType.TOOL:
            names.setdefault(span.name, None)
    return list(names)[:MAX_RUN_TOOLS]


def _engine_trace(
    trace: TraceIn,
    trace_id: str,
    agent: Agent,
    *,
    masked: bool,
    redactions: Sequence[str] = (),
    warned: bool = False,
    guardrails: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
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
    # The run screens read a run's tools and its enforcement verdict off the
    # trace, so that the table never has to open every run's spans. Nothing
    # wrote them: a run with five tool calls listed none, and a run a policy had
    # warned about or masked still read "Allowed". A blocked item is never
    # stored, so "Warned" is the only verdict there is to record here -- and it
    # is ours to state, whatever the reporter's metadata says.
    if not metadata.get("tools") and not metadata.get("tool_calls"):
        tools = _trace_tools(trace)
        if tools:
            metadata["tools"] = tools
    if warned or masked:
        metadata["policy"] = "Warned"
    if guardrails:
        metadata["guardrails"] = [dict(row) for row in guardrails]

    payload: dict[str, Any] = {
        "id": trace_id,
        "project_name": _project(agent),
        "name": trace.name,
        "start_time": _iso(trace.start_time),
        "end_time": _iso(trace.end_time),
        "thread_id": trace.thread_id,
        "tags": list(trace.tags),
        "metadata": metadata,
        "input": _masked_payload(trace.input, masked, redactions),
        "output": _masked_payload(trace.output, masked, redactions),
        "error_info": _engine_error(trace.error_info),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _engine_span(
    span: SpanIn,
    *,
    span_id: str,
    trace_id: str,
    agent: Agent,
    masked: bool,
    redactions: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": span_id,
        "trace_id": trace_id,
        "parent_span_id": _parent_id(span.parent_span_id),
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
        "input": _masked_payload(span.input, masked, redactions),
        "output": _masked_payload(span.output, masked, redactions),
        "error_info": _engine_error(span.error_info),
    }
    return {key: value for key, value in payload.items() if value is not None}


#: The only provenances the engine's score enum accepts. Anything else —
#: "experiment", "judge", whatever an SDK invents — 400s the WHOLE score batch,
#: so unknown values are folded to "sdk" rather than costing their neighbours.
_ENGINE_SCORE_SOURCES: Final[frozenset[str]] = frozenset({"ui", "sdk", "online_scoring"})


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
    if source.lower() not in _ENGINE_SCORE_SOURCES:
        source = "sdk"
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
    #: What to remove when ``masked``; empty means "withhold the whole payload".
    redactions: list[str] = dataclasses.field(default_factory=list)
    #: Set once any masking verdict could not say what it matched.
    mask_everything: bool = False
    #: Spans of this trace that were not stored, and the last reason given.
    spans_rejected: int = 0
    span_problem: str | None = None
    #: What governance decided about an item it let through, for the run's row.
    warned: bool = False
    guardrails: list[dict[str, Any]] = dataclasses.field(default_factory=list)


T = TypeVar("T")

#: The hand-off always gets at least this long, however slow governance was.
MIN_STORE_SECONDS: Final[float] = 5.0


async def _within_budget(started: float, work: Awaitable[T]) -> T:
    """Run the hand-off to the telemetry store inside what is left of the budget.

    Each store call inherits the adapter's 30 s timeout and a batch makes up to
    four of them, so a slow store held a request for a minute or more -- past the
    30 s at which the SDK gives up and sends the same batch again, to a store
    that is already behind. Answering 503 inside the reporter's own timeout
    turns that pile-up into one orderly retry. Abandoning the wait is safe:
    ids make the retry idempotent, nothing has been charged yet, and the adapter
    lets an exchange that is already in flight finish on its own.
    """
    budget = settings.ingest_budget_seconds
    if budget <= 0:
        return await work
    remaining = max(budget - (time.perf_counter() - started), min(MIN_STORE_SECONDS, budget))
    try:
        async with asyncio.timeout(remaining):
            return await work
    except TimeoutError as exc:
        raise TelemetryBackendUnavailable(
            "The telemetry store did not take this batch in time. Retry this batch."
        ) from exc


@dataclasses.dataclass
class _Bundle:
    """One accepted trace shaped for the store, with everything that hangs off it."""

    entry: _Accepted
    trace: dict[str, Any]
    trace_scores: list[dict[str, Any]]
    spans: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    #: Index-aligned with ``spans``: what each costs a token quota, and its scores.
    span_tokens: list[int] = dataclasses.field(default_factory=list)
    span_scores: list[list[dict[str, Any]]] = dataclasses.field(default_factory=list)


def _score_rows(
    scores: Iterable[Any], *, target_id: str, agent: Agent, target: ScoreTarget
) -> list[dict[str, Any]]:
    return [
        _engine_score(
            score.name,
            score.value,
            target_id=target_id,
            agent=agent,
            target=target,
            category_name=score.category_name,
            reason=score.reason,
            source=score.source,
        )
        for score in scores
    ]


def _lose_span(entry: _Accepted, tokens: int, problem: str) -> None:
    """Account for a span that will not be stored: counted, explained, not billed."""
    entry.spans_rejected += 1
    entry.tokens = max(entry.tokens - tokens, 0)
    entry.span_problem = problem


def _bundle(trace: TraceIn, entry: _Accepted) -> _Bundle:
    bundle = _Bundle(
        entry=entry,
        trace=_engine_trace(
            trace,
            entry.trace_id,
            entry.agent,
            masked=entry.masked,
            redactions=entry.redactions,
            warned=entry.warned,
            guardrails=entry.guardrails,
        ),
        trace_scores=_score_rows(
            trace.feedback_scores,
            target_id=entry.trace_id,
            agent=entry.agent,
            target=ScoreTarget.TRACE,
        ),
    )
    for span in trace.spans:
        try:
            span_id = _telemetry_id(span.id, field="span id")
        except _BadIdentifier as exc:
            _lose_span(entry, _billable_tokens(span.usage), str(exc))
            continue
        bundle.spans.append(
            _engine_span(
                span,
                span_id=span_id,
                trace_id=entry.trace_id,
                agent=entry.agent,
                masked=entry.masked,
                redactions=entry.redactions,
            )
        )
        bundle.span_tokens.append(_billable_tokens(span.usage))
        bundle.span_scores.append(
            _score_rows(
                span.feedback_scores, target_id=span_id, agent=entry.agent, target=ScoreTarget.SPAN
            )
        )
    return bundle


async def _store_traces(
    bundles: Sequence[_Bundle], results: list[IngestItemResult]
) -> tuple[list[_Accepted], int, int]:
    """Hand traces, their spans and their scores to the store; report what it took.

    Returns the entries whose trace was stored, and how many spans and scores
    went with them. A trace the store refuses takes its spans and scores with it
    and is rejected on its own row. A *span* the store refuses does not unmake
    its trace: the trace stays accepted, and the row says how many of its spans
    are missing and why -- before, that loss was a log line, the caller was told
    "accepted", the tenant was billed for the tokens, and the run arrived with
    no model, no cost and no steps.

    Once the traces are in, their scores and their spans do not depend on each
    other, so they are sent side by side rather than one after the other.
    """
    client = get_engine_client()
    refusals = await _push_rows(client.create_traces_batch, [bundle.trace for bundle in bundles])
    for project_id in {str(bundle.entry.agent.engine_project_id) for bundle in bundles}:
        telemetry_cache.project_written(project_id)

    stored: list[_Bundle] = []
    for bundle, refusal in zip(bundles, refusals, strict=True):
        if refusal is None:
            stored.append(bundle)
            continue
        _reject(
            results,
            bundle.entry.index,
            RejectionCode.TELEMETRY_REJECTED,
            _explained(refusal, bundle.trace.get("id")),
        )

    async def spans_then_their_scores() -> tuple[int, int]:
        flat = [(bundle, at) for bundle in stored for at in range(len(bundle.spans))]
        span_refusals = await _push_rows(
            client.create_spans_batch, [bundle.spans[at] for bundle, at in flat]
        )
        scores: list[dict[str, Any]] = []
        sent = 0
        for (bundle, at), refusal in zip(flat, span_refusals, strict=True):
            if refusal is None:
                sent += 1
                scores.extend(bundle.span_scores[at])
                continue
            row = bundle.spans[at]
            _lose_span(
                bundle.entry,
                bundle.span_tokens[at],
                _explained(refusal, row.get("id"), row.get("parent_span_id")),
            )
        score_refusals = await _push_rows(client.score_spans_batch, scores)
        return sent, sum(1 for refusal in score_refusals if refusal is None)

    async def trace_scores() -> int:
        rows = [score for bundle in stored for score in bundle.trace_scores]
        score_refusals = await _push_rows(client.score_traces_batch, rows)
        return sum(1 for refusal in score_refusals if refusal is None)

    # Both are awaited to the end before either failure is raised, so nothing is
    # left running against the store after the request has answered.
    outcomes = await asyncio.gather(
        spans_then_their_scores(), trace_scores(), return_exceptions=True
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    (spans_sent, span_scores_sent), trace_scores_sent = outcomes

    for bundle in bundles:
        entry = bundle.entry
        if entry.spans_rejected and results[entry.index].outcome is ItemOutcome.ACCEPTED:
            results[entry.index] = results[entry.index].model_copy(
                update={
                    "spans_rejected": entry.spans_rejected,
                    "reason": (
                        f"The trace was stored, but {entry.spans_rejected} of its "
                        f"{entry.spans} span(s) were not. {entry.span_problem or ''}"
                    ).strip(),
                }
            )
    return [bundle.entry for bundle in stored], spans_sent, span_scores_sent + trace_scores_sent


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
        environments=_reported_environments(
            (item for _, item in parsed.valid), parsed.envelope
        ),
    )
    policies = await _load_policies(session, principal, [agent.id for agent in agents.touched])
    connector_blocks = await _connector_blocks(session, principal, agents)

    accepted: list[_Accepted] = []
    candidates: list[GuardrailCandidate] = []
    hits_by_index: dict[int, list[_Hit]] = {}

    for index, trace in parsed.valid:
        agent = _bind(
            agents, trace.agent or parsed.envelope.agent, results, index, source=source
        )
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
        rules = policies.for_agent(agent)
        hits = _evaluate(
            rules,
            _signals(
                agent,
                entity="trace",
                name=trace.name,
                span_type=None,
                # The model that actually answered, where the spans say; the
                # registered one only when they do not.
                model=_trace_model(trace),
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
        hits.extend(_nested_hits(rules, agent, trace, {hit.rule.policy_id for hit in hits}))
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
                warned=any(hit.rule.enforcement not in SILENT_ENFORCEMENTS for hit in hits),
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
        charges = await _prepare_quotas(session, principal, accepted)

        bundles = [
            _bundle(trace, entry)
            for entry in accepted
            if (trace := parsed.items[entry.index]) is not None
        ]
        accepted, spans_sent, scores_sent = await _within_budget(
            started, _store_traces(bundles, results)
        )
        quotas = await _commit_quotas(session, charges, accepted, request=request)

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
    await _touch_connectors(session, principal, _tools_used(parsed, accepted))

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
        environments=_reported_environments(
            (item for _, item in parsed.valid), parsed.envelope
        ),
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
        charges = await _prepare_quotas(session, principal, accepted)

        rows: list[tuple[_Accepted, SpanIn, dict[str, Any]]] = [
            (
                entry,
                span,
                _engine_span(
                    span,
                    span_id=entry.entity_id,
                    trace_id=entry.trace_id,
                    agent=entry.agent,
                    masked=entry.masked,
                    redactions=entry.redactions,
                ),
            )
            for entry in accepted
            if (span := parsed.items[entry.index]) is not None
        ]

        async def store() -> tuple[list[_Accepted], int]:
            client = get_engine_client()
            refusals = await _push_rows(client.create_spans_batch, [row for _, _, row in rows])
            kept: list[_Accepted] = []
            scores: list[dict[str, Any]] = []
            for (entry, span, row), refusal in zip(rows, refusals, strict=True):
                if refusal is not None:
                    _reject(
                        results,
                        entry.index,
                        RejectionCode.TELEMETRY_REJECTED,
                        _explained(
                            refusal, row.get("id"), row.get("trace_id"), row.get("parent_span_id")
                        ),
                    )
                    continue
                kept.append(entry)
                scores.extend(
                    _score_rows(
                        span.feedback_scores,
                        target_id=entry.entity_id,
                        agent=entry.agent,
                        target=ScoreTarget.SPAN,
                    )
                )
            score_refusals = await _push_rows(client.score_spans_batch, scores)
            return kept, sum(1 for refusal in score_refusals if refusal is None)

        accepted, scores_sent = await _within_budget(started, store())
        spans_sent = len(accepted)
        quotas = await _commit_quotas(session, charges, accepted, request=request)

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
    await _touch_connectors(session, principal, _tools_used(parsed, accepted))

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

    grouped: dict[ScoreTarget, list[tuple[int, dict[str, Any]]]] = {
        target: [] for target in ScoreTarget
    }
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
            (
                index,
                _engine_score(
                    score.name,
                    score.value,
                    target_id=target_id,
                    agent=agent,
                    target=score.target,
                    category_name=score.category_name,
                    reason=score.reason,
                    source=score.source,
                ),
            )
        )
        accepted.append(
            _Accepted(index=index, agent=agent, entity_id=target_id, trace_id=target_id)
        )
        _mark(results, index, entity_id=target_id, agent_id=agent.id, spans=0)

    quotas: list[IngestQuotaState] = []
    if accepted:
        await _check_licence(session, principal)
        charges = await _prepare_quotas(session, principal, accepted)
        client = get_engine_client()
        calls: dict[ScoreTarget, BatchCall] = {
            ScoreTarget.TRACE: client.score_traces_batch,
            ScoreTarget.SPAN: client.score_spans_batch,
            ScoreTarget.THREAD: client.score_threads_batch,
        }

        async def store() -> None:
            for target, rows in grouped.items():
                refusals = await _push_rows(calls[target], [row for _, row in rows])
                for (index, _row), refusal in zip(rows, refusals, strict=True):
                    if refusal is not None:
                        _reject(results, index, RejectionCode.TELEMETRY_REJECTED, refusal)

        await _within_budget(started, store())
        # One score can be refused while the others land, so the tenant is
        # charged for what was actually stored, not for what was offered before
        # the store answered.
        accepted = [
            entry for entry in accepted if results[entry.index].outcome is ItemOutcome.ACCEPTED
        ]
        quotas = await _commit_quotas(session, charges, accepted, request=request)

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
    mirrors: list[tuple[FeedbackItem, dict[str, Any]]] = []
    # The control each reported hit names, and the latest moment it fired. The
    # column was written once per *event* -- five hundred UPDATEs of one hot row
    # for a batch about one policy -- and in batch order, so a queue drained
    # newest-first left "last triggered" at its oldest hit.
    triggered: dict[str, tuple[GuardrailConfig | Policy, dt.datetime]] = {}

    def fired(control: GuardrailConfig | Policy, at: dt.datetime) -> None:
        known = triggered.get(control.id)
        if known is None or at > known[1]:
            triggered[control.id] = (control, at)

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
            fired(guardrail, occurred_at)
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
            fired(policy, occurred_at)
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
        item = FeedbackItem(
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
        session.add(item)
        mirror = _feedback_mirror(item, agent)
        if mirror is not None:
            mirrors.append((item, mirror))
        recorded += 1
        results[index] = results[index].model_copy(update={"id": ref, "agent_id": agent_id})

    await _mirror_feedback(mirrors)
    await session.flush()
    for control, at in triggered.values():
        # Never backwards: a late report of an old hit is not the latest one.
        seen = control.last_triggered_at
        if seen is None or _as_utc(seen) < at:
            await stamp(session, [control], last_triggered_at=at)
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


#: Name the rating is mirrored under, and how long the mirror may take. The
#: capture is ours and durable; the mirror is a courtesy to the telemetry store
#: and must not hold the request, or its transaction, waiting for one.
FEEDBACK_SCORE_NAME: Final[str] = "user_feedback"
FEEDBACK_MIRROR_SECONDS: Final[float] = 5.0


def _feedback_mirror(item: FeedbackItem, agent: Agent | None) -> dict[str, Any] | None:
    """The feedback score one rated item puts on its trace, or None for nothing.

    The same score the console's submit path writes, so SDK- and console-
    submitted feedback read identically in the telemetry store. Built from the
    agent this batch already resolved: the per-item lookup it replaces was one
    SELECT and one store call for every event, inside the loop.

    The store files a score by project as well as by trace id, and the project
    travels as a name. Feedback from no agent this workspace knows therefore has
    nowhere to go: the fallback used to be the *workspace's* name, which is not a
    project, so the store either refused the score or filed it under a project
    of that name where no run would ever show it -- and the row still claimed to
    be scored. Such feedback is kept and simply not mirrored.
    """
    if not item.trace_id or item.rating is None or not _is_uuid(item.trace_id):
        return None
    if agent is None or not agent.engine_project_name:
        return None
    from . import feedback as feedback_service

    row: dict[str, Any] = {
        "id": item.trace_id,
        "project_name": agent.engine_project_name,
        "name": getattr(feedback_service, "TELEMETRY_SCORE_NAME", FEEDBACK_SCORE_NAME),
        "value": float(item.rating),
        "category_name": item.sentiment,
        "source": "sdk",
    }
    if item.body:
        row["reason"] = item.body[:500]
    return row


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def _mirror_feedback(mirrors: Sequence[tuple[FeedbackItem, dict[str, Any]]]) -> None:
    """Put a batch's ratings on their traces in one call, best effort, briefly.

    An outage, a slow store or a refusal degrades the mirror and never the
    capture: the item simply keeps no ``engine_feedback_score_id``, which is how
    every reader already knows the score is not in the store.
    """
    if not mirrors:
        return
    try:
        async with asyncio.timeout(FEEDBACK_MIRROR_SECONDS):
            refusals = await _push_rows(
                get_engine_client().score_traces_batch, [row for _, row in mirrors]
            )
    except (TelemetryBackendUnavailable, TimeoutError):
        logger.warning("feedback captured but not mirrored: the telemetry store is not answering")
        return
    for (item, row), refusal in zip(mirrors, refusals, strict=True):
        if refusal is None:
            item.engine_feedback_score_id = f"{item.trace_id}/{row['name']}"


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
    agents: _Agents,
    wanted: str | None,
    results: list[IngestItemResult],
    index: int,
    *,
    source: str = SOURCE_SDK,
) -> Agent | None:
    """Attach an item to its agent, refusing a cross-agent write.

    A key bound to one agent may not report for another, and saying so plainly
    is safe: the caller already knows which agent its key was issued for.

    An OpenTelemetry export is the exception. Its "agent" is ``service.name``,
    which every OTel SDK sends whether or not anybody set it (the default is
    ``unknown_service``) and which names a process, not a registration. Reading
    it as a claim to be some other agent refused every export made with a bound
    key, so there the binding simply wins, as the field's own contract says.
    """
    if agents.bound is not None:
        bound = agents.bound
        known = {bound.slug.lower(), bound.name.lower()}
        if source != SOURCE_OTLP and wanted and wanted.strip().lower() not in known:
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
    masked: dict[int, list[GuardrailVerdict]] = {}
    flagged: dict[int, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        if verdict.action == GuardrailAction.BLOCK.value:
            blocked.setdefault(verdict.index, verdict)
        elif verdict.action == GuardrailAction.MASK.value:
            # Every masking verdict counts: two guardrails that each found
            # something must both have what they found removed.
            masked.setdefault(verdict.index, []).append(verdict)
        # A Log verdict is a shadow trial; it shows on the Guardrails screen and
        # says nothing about the run.
        if verdict.action in (GuardrailAction.MASK.value, GuardrailAction.WARN.value):
            labels = verdict.matched.get("labels") if isinstance(verdict.matched, dict) else None
            flagged.setdefault(verdict.index, []).append(
                {
                    "name": verdict.guardrail_name or verdict.guardrail_id,
                    "result": "Masked"
                    if verdict.action == GuardrailAction.MASK.value
                    else "Warned",
                    # Category labels only -- never the text that matched.
                    "detail": ", ".join(str(label) for label in labels)
                    if isinstance(labels, list) and labels
                    else verdict.reason,
                }
            )

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
        if entry.index in flagged:
            entry.warned = True
            entry.guardrails = flagged[entry.index]
        masks = masked.get(entry.index)
        if masks:
            entry.masked = True
            for mask in masks:
                if mask.redactions:
                    entry.redactions.extend(mask.redactions)
                else:
                    entry.mask_everything = True
            if entry.mask_everything:
                entry.redactions = []
            results[entry.index] = results[entry.index].model_copy(
                update={"masked": True, "guardrail_id": masks[0].guardrail_id}
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
            # Named, and set to itself, so that a policy firing is not an edit to it.
            .values(last_triggered_at=now, updated_at=Policy.updated_at)
            # The policies this request loaded are still in the session. Left to
            # synchronise, the ORM expires ``updated_at`` on them, and the next
            # read of an expired attribute under asyncio is a MissingGreenlet.
            .execution_options(synchronize_session=False)
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
    await session.flush()
    await stamp(session, agents.touched, last_used_at=_now())


#: A connector's ``last_used_at`` is not rewritten more often than this. It is
#: read to the minute at best, and four workers rewriting one hot row on every
#: five-second batch is contention for nothing.
CONNECTOR_TOUCH_SECONDS: Final[float] = 60.0


def _tools_used(parsed: ParsedBatch[Any], accepted: Sequence[_Accepted]) -> dict[str, set[str]]:
    """agent id -> lower-cased names of the tool spans its stored items carried."""
    used: dict[str, set[str]] = {}
    for entry in accepted:
        item = parsed.items[entry.index]
        spans = item.spans if isinstance(item, TraceIn) else [item]
        names = {
            span.name.strip().lower()
            for span in spans
            if isinstance(span, SpanIn) and span.type is SpanType.TOOL
        }
        if names:
            used.setdefault(entry.agent.id, set()).update(names)
    return used


async def _touch_connectors(
    session: AsyncSession, principal: Principal, used: Mapping[str, set[str]]
) -> None:
    """Stamp ``last_used_at`` on the connectors an agent's tool calls went through.

    Nothing ever wrote this column, so the Connectors screen showed a dash under
    Last Used for ever, sorted on nothing and exported an empty column. The
    evidence ingest has is a tool span from an agent that holds a grant. When
    the span is named after a connector, that connector is stamped. Usually it
    is not -- a span is named after a function, a connector after a system --
    and then every connector the agent is granted is: the column means "a
    granted agent last called a tool", which is what a reviewer deciding whether
    a connector is still in use is asking. An agent that called no tool stamps
    nothing. One read and at most one write per batch, and none for a batch
    without tool calls.
    """
    if not used:
        return
    now = _now()
    stale = now - dt.timedelta(seconds=CONNECTOR_TOUCH_SECONDS)
    rows = (
        await session.execute(
            select(AgentConnector.agent_id, Connector.id, Connector.name, Connector.last_used_at)
            .join(Connector, Connector.id == AgentConnector.connector_id)
            .where(
                AgentConnector.workspace_id == principal.workspace_id,
                AgentConnector.agent_id.in_(list(used)),
                Connector.status != ConnectorStatus.BLOCKED.value,
            )
        )
    ).all()
    held: dict[str, list[tuple[str, str, dt.datetime | None]]] = {}
    for agent_id, connector_id, name, last_used_at in rows:
        held.setdefault(agent_id, []).append(
            (connector_id, (name or "").strip().lower(), last_used_at)
        )
    due: set[str] = set()
    for agent_id, grants in held.items():
        named = [grant for grant in grants if grant[1] in used[agent_id]]
        for connector_id, _name, last_used_at in named or grants:
            if last_used_at is None or _as_utc(last_used_at) < stale:
                due.add(connector_id)
    if not due:
        return
    await session.execute(
        sa_update(Connector)
        .where(Connector.id.in_(due))
        # Set to itself so that being used is not an edit: see ``db.base.stamp``.
        .values(last_used_at=now, updated_at=Connector.updated_at)
        .execution_options(synchronize_session=False)
    )


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
