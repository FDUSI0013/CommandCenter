"""Memory & State Management business logic.

Two sources of truth meet here.

*Ours* is the ``memory_stores`` table: the registry of every store an agent may
read or write, who owns it, and the retention policy its deletion SLA is
audited against. Nothing about that lives anywhere else, so it is queried,
mutated and audited like any other governance row.

*Theirs* is the telemetry engine, which holds the conversation and session
records themselves as trace threads. Every session, conversation and record on
this screen is read live from the engine through the adapter; none of it is
estimated or reconstructed. The one thing remembered is a project's thread
*count*, for ``settings.memory_counts_cache_seconds``, because the KPI row and
every store row ask for it on every load; a purge forgets the counts of the
projects it swept. Where the engine reports nothing — a store type it does not
hold, a usage figure it does not track — the field is null and the operation is
refused with a precondition rather than answered with a plausible number.

A store owns the threads of the agents whose memory policy names it, and
nothing else: that binding is the whole scope of its records, its purge and its
backup count. A backup holds the store's governed state and never its threads.

Two structural notes:

* agents map one-to-one onto engine projects, so a workspace-wide thread read is
  a bounded fan-out across the projects of the workspace's provisioned agents,
  merged and re-sorted here. The fan-out is capped; deep pagination is expected
  to narrow by agent.
* backups have no table of their own, so a backup *is* its audit row: the
  manifest travels in ``event_metadata`` and the audit id is the backup id.
  That makes the backup ledger append-only and tamper-evident for free.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, and_, case, func, nullslast, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import (
    AppError,
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..core.ttlcache import SingleFlightCache
from ..db.base import stamp
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    deadline,
    get_engine_client,
)
from ..models.governance import AuditEvent
from ..models.identity import Role, User
from ..models.registry import Agent, MemoryStore, MemoryStoreStatus, MemoryStoreType
from ..schemas.memory import (
    ACTIVE_SESSION_WINDOW_HOURS,
    BACKUP_CAPTURES,
    LIVE_SESSION_WINDOW_MINUTES,
    THREAD_BACKED_TYPES,
    AgentStateRead,
    BackupKind,
    BackupStatus,
    MemoryBackupRead,
    MemoryBackupRequest,
    MemoryBackupResult,
    MemoryConversationRead,
    MemoryCountSlice,
    MemoryPurgeRequest,
    MemoryPurgeResult,
    MemoryRecordRead,
    MemoryRestoreRequest,
    MemoryRestoreResult,
    MemorySessionRead,
    MemoryStoreCreate,
    MemoryStoreRead,
    MemoryStoreUpdate,
    MemorySummary,
    RetentionPolicyRead,
    RetentionPolicyUpdate,
    SessionState,
    SyncState,
    retention_label,
)
from . import audit, telemetry_cache

SOURCE_SCREEN: Final[str] = "Memory & State Management"
ENTITY_TYPE: Final[str] = "memory_store"

#: The audit action that *is* the backup ledger.
BACKUP_ACTION: Final[str] = "memory.backup_created"
PURGE_ACTION: Final[str] = "memory.purged"

#: How far back the "Expired / Purged" card counts.
PURGE_WINDOW_DAYS: Final[int] = 30

#: Usage level at which the table flags a store amber.
USAGE_WARNING_PERCENT: Final[int] = 80

#: An export is a report, not a bulk data channel.
MAX_EXPORT_ROWS: Final[int] = 5000

#: Engine projects one request may fan out across, and how many of those calls
#: may be in flight at once. Both exist to keep a tab render bounded.
MAX_PROJECT_FANOUT: Final[int] = 32
CONCURRENT_ENGINE_CALLS: Final[int] = 8

#: Deepest merged thread window a paged read will materialise before it asks the
#: caller to narrow by agent.
MAX_THREAD_WINDOW: Final[int] = 200

#: Purge sweep sizes. The batch is one engine round trip; the batch count bounds
#: one listing, and ``settings.memory_purge_budget_seconds`` bounds the run.
PURGE_BATCH: Final[int] = 500
MAX_PURGE_BATCHES: Final[int] = 20

#: How long the store table waits on telemetry for its live Records and Sessions
#: figures before it is served without them. The table is our own data.
STORE_COUNTS_DEADLINE_SECONDS: Final[float] = 8.0

#: First key of the two-int advisory lock that keeps a store to one purge at a
#: time across workers. A key space of its own: the audit chain's and the
#: scheduler's locks can never collide with it.
PURGE_LOCK_NAMESPACE: Final[int] = 0x4D505247  # "MPRG"

#: Rows the retention-policy tab resolves labels for.
MAX_POLICY_DETAIL_ROWS: Final[int] = 500

#: Two clocks are never aligned and clients round timestamps when they echo
#: them back, so the optimistic check tolerates a second of drift.
CONCURRENCY_TOLERANCE_SECONDS: Final[float] = 1.0

SORTABLE: Final[dict[str, Any]] = {
    "name": MemoryStore.name,
    "store_type": MemoryStore.store_type,
    "type": MemoryStore.store_type,
    "environment": MemoryStore.environment,
    "env": MemoryStore.environment,
    "status": MemoryStore.status,
    "usage_percent": MemoryStore.usage_percent,
    "usage": MemoryStore.usage_percent,
    "record_count": MemoryStore.record_count,
    "records": MemoryStore.record_count,
    "retention_policy": MemoryStore.retention_policy,
    "retention": MemoryStore.retention_policy,
    "retention_days": MemoryStore.retention_days,
    "last_updated_at": MemoryStore.last_updated_at,
    "lastUpdated": MemoryStore.last_updated_at,
    "owner_user_id": MemoryStore.owner_user_id,
    "owner": MemoryStore.owner_user_id,
    "active_session_count": MemoryStore.active_session_count,
    "avg_retrieval_latency_ms": MemoryStore.avg_retrieval_latency_ms,
    "last_backup_at": MemoryStore.last_backup_at,
    "created_at": MemoryStore.created_at,
    "updated_at": MemoryStore.updated_at,
}

AGENT_SORTABLE: Final[dict[str, Any]] = {
    "agent_name": Agent.name,
    "name": Agent.name,
    "environment": Agent.environment,
    "memory_policy": Agent.memory_policy,
    "last_activity_at": Agent.last_used_at,
    "status": Agent.status,
}

#: CSV layout: the table as it is rendered, then the governance detail the
#: table has no room for.
EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "Memory Store"),
    ("store_type", "Type"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("usage_percent", "Usage %"),
    ("record_count", "Records"),
    ("retention_policy", "Retention Policy"),
    ("retention_days", "Retention Days"),
    ("last_updated_at", "Last Updated"),
    ("owner_name", "Owner"),
    ("owner_team", "Team"),
    ("backend", "Backend"),
    ("active_session_count", "Sessions Attached"),
    ("avg_retrieval_latency_ms", "Avg Retrieval Latency (ms)"),
    ("backup_count", "Backups"),
    ("last_backup_at", "Last Backup"),
    ("created_at", "Created"),
)


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EngineProject:
    """One engine project this screen reads threads from, and whose agent it is."""

    project_name: str
    project_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None


@dataclasses.dataclass(frozen=True)
class ThreadRow:
    """A trace thread, normalised into the fields this screen renders."""

    thread_id: str
    agent_id: str | None
    agent_name: str | None
    user: str | None
    message_count: int
    context_tokens: int | None
    size_bytes: int | None
    started_at: dt.datetime | None
    last_activity_at: dt.datetime | None
    duration_ms: int | None
    state: SessionState

    @property
    def sort_key(self) -> tuple[float, str]:
        moment = self.last_activity_at or self.started_at
        return (moment.timestamp() if moment else 0.0, self.thread_id)


def _engine() -> EngineClient:
    """The process-wide adapter. Never construct HTTP calls outside it."""
    return get_engine_client()


def _unavailable(exc: EngineError) -> TelemetryBackendUnavailable:
    """Translate an adapter failure into the API's 503 envelope.

    The engine is never named to the caller, and no number is invented to paper
    over the gap: the screen shows an error rather than a fiction.
    """
    return TelemetryBackendUnavailable(
        "Memory records are held by the telemetry store, which did not answer. "
        "Retry in a moment.",
        details={"reason": type(exc).__name__},
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive instant as UTC; clients are not required to send an offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _instant(value: Any) -> dt.datetime | None:
    """Parse an engine timestamp. Anything unparseable is reported as absent."""
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _as_utc(dt.datetime.fromisoformat(text))
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _rows_of(payload: Any) -> list[dict[str, Any]]:
    """The engine's paged envelope carries its rows under ``content``."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("content", "threads", "traces", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _total_of(payload: Any, fallback: int) -> int:
    if isinstance(payload, dict):
        total = _int_or_none(payload.get("total"))
        if total is not None:
            return total
    return fallback


def _thread_state(last_activity: dt.datetime | None, reported: Any) -> SessionState:
    """Prefer the engine's own verdict; fall back to how recent the thread is."""
    if isinstance(reported, str) and reported.strip():
        normalised = reported.strip().lower()
        if normalised in ("active", "open", "live"):
            return SessionState.ACTIVE
        if normalised in ("inactive", "idle"):
            return SessionState.IDLE
        if normalised in ("closed", "ended", "completed"):
            return SessionState.CLOSED
    if last_activity is None:
        return SessionState.CLOSED
    age = dt.datetime.now(dt.UTC) - last_activity
    if age <= dt.timedelta(minutes=LIVE_SESSION_WINDOW_MINUTES):
        return SessionState.ACTIVE
    if age <= dt.timedelta(hours=ACTIVE_SESSION_WINDOW_HOURS):
        return SessionState.IDLE
    return SessionState.CLOSED


def _tokens(payload: Mapping[str, Any]) -> int | None:
    """Total context tokens, when the engine reported usage for the thread."""
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    total = _int_or_none(usage.get("total_tokens"))
    if total is not None:
        return total
    parts = [
        _int_or_none(usage.get(key))
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    ]
    present = [part for part in parts if part is not None]
    return sum(present) if present else None


def _thread_row(payload: Mapping[str, Any], project: EngineProject) -> ThreadRow:
    """Normalise one engine thread into the shape every tab on this screen uses."""
    thread_id = str(
        payload.get("thread_id") or payload.get("id") or payload.get("thread_model_id") or ""
    )
    started_at = _instant(payload.get("start_time") or payload.get("created_at"))
    last_activity_at = _instant(
        payload.get("last_updated_at") or payload.get("end_time") or payload.get("start_time")
    )
    duration = payload.get("duration")
    duration_ms = None
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        duration_ms = int(duration)
    elif started_at is not None and last_activity_at is not None:
        duration_ms = int((last_activity_at - started_at).total_seconds() * 1000)

    created_by = payload.get("created_by") or payload.get("last_updated_by")
    return ThreadRow(
        thread_id=thread_id,
        agent_id=project.agent_id,
        agent_name=project.agent_name,
        user=str(created_by) if isinstance(created_by, str) and created_by.strip() else None,
        message_count=_int_or_none(payload.get("number_of_messages")) or 0,
        context_tokens=_tokens(payload),
        size_bytes=_int_or_none(payload.get("total_bytes")),
        started_at=started_at,
        last_activity_at=last_activity_at,
        duration_ms=duration_ms,
        state=_thread_state(last_activity_at, payload.get("status")),
    )


async def _projects(
    session: AsyncSession, principal: Principal, *, agent_id: str | None = None
) -> list[EngineProject]:
    """Engine projects this workspace's threads live in.

    Agents map one-to-one onto projects, so the workspace-wide view is the union
    of its provisioned agents' projects, most recently used first and capped. A
    workspace with nothing provisioned falls back to its own namespace, which is
    where telemetry lands before the first agent is registered.
    """
    stmt = (
        select(Agent.id, Agent.name, Agent.engine_project_name, Agent.engine_project_id)
        .where(
            Agent.workspace_id == principal.workspace_id,
            Agent.engine_project_name.is_not(None),
        )
        .order_by(nullslast(Agent.last_used_at.desc()), Agent.name.asc())
        .limit(MAX_PROJECT_FANOUT)
    )
    if agent_id is not None:
        stmt = stmt.where(Agent.id == agent_id)

    rows = (await session.execute(stmt)).all()
    if rows:
        return [
            EngineProject(
                project_name=str(project_name),
                project_id=project_id,
                agent_id=identifier,
                agent_name=name,
            )
            for identifier, name, project_name, project_id in rows
        ]

    if agent_id is not None:
        exists = (
            await session.execute(
                select(Agent.id).where(
                    Agent.workspace_id == principal.workspace_id, Agent.id == agent_id
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise NotFound(f"Agent '{agent_id}' does not exist.")
        raise PreconditionFailed(
            "That agent has not been provisioned in the telemetry store yet, so it "
            "holds no sessions."
        )

    return [EngineProject(project_name=principal.engine_workspace)]


async def _store_projects(
    session: AsyncSession,
    principal: Principal,
    store: MemoryStore,
    *,
    agent_id: str | None = None,
    limit: int | None = MAX_PROJECT_FANOUT,
) -> list[EngineProject]:
    """Engine projects of the agents bound to one store — and of nobody else.

    An agent is bound to the store its memory policy names, which is the same
    pairing the Agent State tab shows in its Store column. It is the only link
    between a store and the threads in the telemetry store that the registry
    holds, so it is the whole scope of anything done *to* a store.

    Purge used to sweep :func:`_projects` — every provisioned agent in the
    workspace — so purging a 7-day Development store deleted the Production
    agents' history with it. There is deliberately no fallback here: a store no
    agent names owns no threads, and the answer is an empty list, never the
    workspace.

    ``limit`` is the screen's fan-out cap, and a read keeps it. The purge passes
    ``None``: see :func:`purge_store`.
    """
    stmt = (
        select(Agent.id, Agent.name, Agent.engine_project_name, Agent.engine_project_id)
        .where(
            Agent.workspace_id == principal.workspace_id,
            Agent.memory_policy == store.name,
            Agent.engine_project_name.is_not(None),
        )
        .order_by(nullslast(Agent.last_used_at.desc()), Agent.name.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if agent_id is not None:
        stmt = stmt.where(Agent.id == agent_id)
    return [
        EngineProject(
            project_name=str(project_name),
            project_id=project_id,
            agent_id=identifier,
            agent_name=name,
        )
        for identifier, name, project_name, project_id in (await session.execute(stmt)).all()
    ]


def _scope(project: EngineProject) -> dict[str, str]:
    """Address a project by id when the registry knows it.

    A name costs the engine a name-to-id lookup on every call, and a fan-out
    makes that call once per agent.
    """
    if project.project_id:
        return {"project_id": project.project_id}
    return {"project_name": project.project_name}


async def _gather(calls: Iterable[Any]) -> list[Any]:
    """Run bounded-concurrency engine calls, surfacing the first failure."""
    semaphore = asyncio.Semaphore(CONCURRENT_ENGINE_CALLS)

    async def _guarded(coroutine: Any) -> Any:
        async with semaphore:
            return await coroutine

    return await asyncio.gather(*(_guarded(call) for call in calls))


async def _fetch_threads(
    projects: Sequence[EngineProject],
    *,
    want: int,
    search: str | None = None,
    from_time: dt.datetime | None = None,
) -> tuple[list[ThreadRow], int]:
    """Merge one page of threads from every project, newest activity first.

    ``total`` is the sum of what each project reported, so the count describes
    the whole workspace even though only the merged head is materialised.
    """
    client = _engine()
    size = max(1, min(want, MAX_THREAD_WINDOW))

    async def page_of(project: EngineProject) -> Any:
        try:
            return await client.list_threads(
                page=1,
                size=size,
                **_scope(project),
                search=search,
                from_time=from_time,
                truncate=True,
            )
        except EngineNotFound:
            # A project that has never received telemetry does not exist yet.
            # That is this project's answer, not the workspace's: it used to
            # empty the whole tab for every other agent as well.
            return None

    try:
        # One slow project must not hold the tab past the point the console
        # stops listening; the fan-out answers together or refuses together.
        async with deadline(what="reading conversation threads"):
            payloads = await _gather([page_of(project) for project in projects])
    except EngineError as exc:
        raise _unavailable(exc) from exc

    rows: list[ThreadRow] = []
    total = 0
    for project, payload in zip(projects, payloads, strict=True):
        project_rows = _rows_of(payload)
        total += _total_of(payload, len(project_rows))
        rows.extend(
            _thread_row(row, project)
            for row in project_rows
            if row.get("thread_id") or row.get("id")
        )

    rows.sort(key=lambda row: row.sort_key, reverse=True)
    return rows, total


#: (project, "all" | "active") -> how many threads it holds. Even at ``size=1``
#: a thread listing is an aggregation over the project's traces, and the KPI
#: row, the Agent State tab and every thread-backed store row each asked for one
#: per agent on every load and after every action. They now share an answer for
#: ``settings.memory_counts_cache_seconds``; a purge forgets the projects it
#: swept, so its own figures are never the remembered ones.
_thread_counts: SingleFlightCache[int] = SingleFlightCache(
    ttl=lambda: settings.memory_counts_cache_seconds, max_entries=512
)


def _count_key(project: EngineProject) -> str:
    return project.project_id or project.project_name


async def _project_thread_count(project: EngineProject, *, active: bool) -> int:
    """One project's thread count: every thread, or those inside the active window."""

    async def measure() -> int:
        since = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=ACTIVE_SESSION_WINDOW_HOURS)
            if active
            else None
        )
        try:
            payload = await _engine().list_threads(
                page=1, size=1, **_scope(project), from_time=since, truncate=True
            )
        except EngineNotFound:
            return 0  # never received telemetry, so it holds no threads
        return _total_of(payload, len(_rows_of(payload)))

    return await _thread_counts.get(
        (_count_key(project), "active" if active else "all"), measure
    )


def _forget_counts(projects: Iterable[EngineProject]) -> None:
    keys = {_count_key(project) for project in projects}
    _thread_counts.invalidate(
        lambda key: isinstance(key, tuple) and bool(key) and key[0] in keys
    )


async def _count_threads(projects: Sequence[EngineProject], *, active: bool = False) -> int:
    """How many threads the projects hold, without materialising any.

    Fails closed: a project that cannot be counted makes the sum wrong, and a
    wrong number on a KPI card is worse than the 503 this raises instead.
    """
    try:
        async with deadline(what="counting conversation threads"):
            counts = await _gather(
                [_project_thread_count(project, active=active) for project in projects]
            )
    except EngineError as exc:
        raise _unavailable(exc) from exc
    return sum(counts)


def _slice(rows: Sequence[ThreadRow], params: ListParams) -> list[ThreadRow]:
    return list(rows[params.offset : params.offset + params.page_size])


# ---------------------------------------------------------------------------
# Store reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(MemoryStore).where(MemoryStore.workspace_id == principal.workspace_id)


def _filtered(
    principal: Principal,
    params: ListParams,
    *,
    store_type: MemoryStoreType | None,
    status: MemoryStoreStatus | None,
    environment: str | None,
    owner_user_id: str | None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt,
        params,
        [
            MemoryStore.name,
            MemoryStore.store_type,
            MemoryStore.backend,
            MemoryStore.retention_policy,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            MemoryStore.store_type: store_type.value if store_type else None,
            MemoryStore.status: status.value if status else None,
            MemoryStore.environment: environment,
            MemoryStore.owner_user_id: owner_user_id,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=MemoryStore.name, default_desc=False)


async def _owners(
    session: AsyncSession, user_ids: Iterable[str | None]
) -> dict[str, tuple[str | None, str | None]]:
    """Resolve owner names and teams for a page in one statement."""
    wanted = {user_id for user_id in user_ids if user_id}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.team).where(User.id.in_(wanted))
        )
    ).all()
    return {user_id: (full_name, team) for user_id, full_name, team in rows}


async def _backup_counts(
    session: AsyncSession, principal: Principal, store_ids: Sequence[str]
) -> dict[str, int]:
    """Backups per store, counted off the audit ledger in one statement."""
    if not store_ids:
        return {}
    rows = (
        await session.execute(
            select(AuditEvent.entity_id, func.count(AuditEvent.id))
            .where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.action == BACKUP_ACTION,
                AuditEvent.entity_type == ENTITY_TYPE,
                AuditEvent.entity_id.in_(list(store_ids)),
            )
            .group_by(AuditEvent.entity_id)
        )
    ).all()
    return {entity_id: int(count) for entity_id, count in rows if entity_id}


async def _live_counts(
    session: AsyncSession, principal: Principal, stores: Sequence[MemoryStore]
) -> dict[str, tuple[int | None, int | None, int]]:
    """``(records, active sessions, bound agents)`` for each thread-backed store.

    Nothing ever wrote these stores' counter columns — no job, no ingest hook,
    no SDK verb — so Records and Sessions read 0 for ever however busy the
    agents were. The telemetry store already knows both figures, so they are
    read from it, over the agents bound to each store and through the same
    short memory the KPI row uses.

    The store table is our own data and keeps answering when telemetry does
    not: a read that fails or runs long leaves the two figures null — not
    measured — which the console renders as a dash, never as a zero.
    """
    names = {store.name: store.id for store in stores if store.store_type in THREAD_BACKED_TYPES}
    if not names:
        return {}
    rows = (
        await session.execute(
            select(Agent.memory_policy, Agent.engine_project_name, Agent.engine_project_id)
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.memory_policy.in_(list(names)),
                Agent.engine_project_name.is_not(None),
            )
            .order_by(nullslast(Agent.last_used_at.desc()), Agent.name.asc())
        )
    ).all()
    bound: dict[str, list[EngineProject]] = {store_id: [] for store_id in names.values()}
    for policy, project_name, project_id in rows:
        bound[names[str(policy)]].append(
            EngineProject(project_name=str(project_name), project_id=project_id)
        )

    # The fan-out is capped like every other on this screen. A store whose
    # agents do not fit under the cap is left unmeasured rather than half
    # counted: a partial sum looks exactly like a total.
    projects: list[EngineProject] = []
    unmeasured: set[str] = set()
    for store_id, group in bound.items():
        if len(projects) + len(group) > MAX_PROJECT_FANOUT:
            unmeasured.add(store_id)
            continue
        projects.extend(group)
    measured: dict[tuple[str, bool], int] = {}
    try:
        async with deadline(STORE_COUNTS_DEADLINE_SECONDS, what="counting store records"):
            counts = await _gather(
                [
                    _project_thread_count(project, active=active)
                    for project in projects
                    for active in (False, True)
                ]
            )
        keys = [(_count_key(project), active) for project in projects for active in (False, True)]
        measured = dict(zip(keys, counts, strict=True))
    except EngineError:
        measured = {}

    live: dict[str, tuple[int | None, int | None, int]] = {}
    for store_id, group in bound.items():
        if store_id in unmeasured or (projects and not measured):
            live[store_id] = (None, None, len(group))
            continue
        live[store_id] = (
            sum(measured[(_count_key(project), False)] for project in group),
            sum(measured[(_count_key(project), True)] for project in group),
            len(group),
        )
    return live


async def _decorate(
    session: AsyncSession, principal: Principal, stores: Sequence[MemoryStore]
) -> list[MemoryStoreRead]:
    owners = await _owners(session, (store.owner_user_id for store in stores))
    backups = await _backup_counts(session, principal, [store.id for store in stores])
    live = await _live_counts(session, principal, stores)
    payload: list[MemoryStoreRead] = []
    for store in stores:
        owner_name, owner_team = owners.get(store.owner_user_id or "", (None, None))
        payload.append(
            MemoryStoreRead.from_model(
                store,
                owner_name=owner_name,
                owner_team=owner_team,
                backup_count=backups.get(store.id, 0),
                live=live.get(store.id),
            )
        )
    return payload


async def list_stores(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    store_type: MemoryStoreType | None = None,
    status: MemoryStoreStatus | None = None,
    environment: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[list[MemoryStoreRead], int]:
    """One page of the workspace's stores, filtered the way the table is."""
    stmt = _filtered(
        principal,
        params,
        store_type=store_type,
        status=status,
        environment=environment,
        owner_user_id=owner_user_id,
    )
    rows, total = await paginate(session, stmt, params)
    return await _decorate(session, principal, rows), total


async def get_store(
    session: AsyncSession, principal: Principal, store_id: str
) -> MemoryStore:
    """Load one store, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as a row that never existed —
    a 403 would confirm the id is real.
    """
    store = (
        await session.execute(_scoped(principal).where(MemoryStore.id == store_id))
    ).scalar_one_or_none()
    if store is None:
        raise NotFound(f"Memory store '{store_id}' does not exist.")
    return store


async def read_store(
    session: AsyncSession, principal: Principal, store_id: str
) -> MemoryStoreRead:
    """One store, decorated with its owner and backup count."""
    store = await get_store(session, principal, store_id)
    rows = await _decorate(session, principal, [store])
    return rows[0]


async def export_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    store_type: MemoryStoreType | None = None,
    status: MemoryStoreStatus | None = None,
    environment: str | None = None,
    owner_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every store matching the current filters, flattened for CSV."""
    principal.require(Role.VIEWER)
    stmt = _filtered(
        principal,
        params,
        store_type=store_type,
        status=status,
        environment=environment,
        owner_user_id=owner_user_id,
    ).limit(MAX_EXPORT_ROWS)
    stores = (await session.execute(stmt)).scalars().all()
    return [row.model_dump(mode="json") for row in await _decorate(session, principal, stores)]


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


def _slices(counts: Mapping[str, int], total: int) -> list[MemoryCountSlice]:
    return [
        MemoryCountSlice(
            label=label,
            count=count,
            percent=round(count / total * 100, 1) if total else 0.0,
        )
        for label, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


async def _purge_totals(
    session: AsyncSession, principal: Principal, since: dt.datetime
) -> tuple[int, int]:
    """Records removed by retention purges in the window, and how many runs.

    The counts live inside each audit row's metadata document, which no portable
    SQL aggregate can sum across SQLite and Postgres alike, so the rows — one
    per purge, not one per record — are summed here. Dry runs deleted nothing and
    are not counted as runs.
    """
    rows = (
        await session.execute(
            select(AuditEvent.event_metadata).where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.action == PURGE_ACTION,
                AuditEvent.occurred_at >= since,
            )
        )
    ).scalars().all()
    purged = 0
    runs = 0
    for metadata in rows:
        if not isinstance(metadata, Mapping) or metadata.get("dry_run"):
            continue
        purged += _int_or_none(metadata.get("purged_records")) or 0
        runs += 1
    return purged, runs


async def _bound_projects(session: AsyncSession, principal: Principal) -> list[EngineProject]:
    """Projects of every agent bound to *some* thread-backed store in the workspace.

    An agent has one memory policy, so it is bound to at most one store and no
    thread is counted twice.
    """
    rows = (
        await session.execute(
            select(Agent.id, Agent.name, Agent.engine_project_name, Agent.engine_project_id)
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.engine_project_name.is_not(None),
                Agent.memory_policy.in_(
                    select(MemoryStore.name).where(
                        MemoryStore.workspace_id == principal.workspace_id,
                        MemoryStore.store_type.in_(sorted(THREAD_BACKED_TYPES)),
                    )
                ),
            )
            .order_by(nullslast(Agent.last_used_at.desc()), Agent.name.asc())
            .limit(MAX_PROJECT_FANOUT)
        )
    ).all()
    return [
        EngineProject(
            project_name=str(project_name),
            project_id=project_id,
            agent_id=identifier,
            agent_name=name,
        )
        for identifier, name, project_name, project_id in rows
    ]


async def summarise(session: AsyncSession, principal: Principal) -> MemorySummary:
    """The six KPI cards and the three breakdowns, in one round trip.

    Every store-side number is a SQL aggregate over the whole workspace rather
    than over the page on screen. Active Sessions and the thread-backed share of
    Stored Memories are live thread counts from the telemetry store, so the row
    is empty-but-honest rather than stale when the engine is unreachable — that
    condition surfaces as a 503.

    The reported figures — records, usage, latency — are aggregated over the
    stores that *have* a reporter. A thread-backed store's counter columns are
    never written, and folding their zeros in dragged every average towards
    nothing.
    """
    workspace = MemoryStore.workspace_id == principal.workspace_id
    reported = MemoryStore.store_type.not_in(sorted(THREAD_BACKED_TYPES))
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(MemoryStore.record_count), 0).label("records"),
                func.avg(MemoryStore.avg_retrieval_latency_ms).label("latency"),
                func.avg(MemoryStore.usage_percent).label("usage"),
            ).where(workspace, reported)
        )
    ).one()

    # One grouped statement instead of three GROUP BYs and three counts: the
    # breakdowns are folded from it here.
    over = case((MemoryStore.usage_percent >= USAGE_WARNING_PERCENT, 1), else_=0)
    groups = (
        await session.execute(
            select(
                MemoryStore.status,
                MemoryStore.store_type,
                MemoryStore.environment,
                func.count(MemoryStore.id),
                func.coalesce(func.sum(over), 0),
                func.max(MemoryStore.last_backup_at),
            )
            .where(workspace)
            .group_by(MemoryStore.status, MemoryStore.store_type, MemoryStore.environment)
        )
    ).all()
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_environment: dict[str, int] = {}
    stores = over_threshold = thread_backed = 0
    last_backup_at: dt.datetime | None = None
    for status, store_type, environment, count, over_count, backed_up in groups:
        count = int(count)
        stores += count
        by_status[str(status)] = by_status.get(str(status), 0) + count
        by_type[str(store_type)] = by_type.get(str(store_type), 0) + count
        by_environment[str(environment)] = by_environment.get(str(environment), 0) + count
        if store_type in THREAD_BACKED_TYPES:
            thread_backed += count
        else:
            over_threshold += int(over_count or 0)
        if backed_up is not None and (last_backup_at is None or backed_up > last_backup_at):
            last_backup_at = backed_up

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=PURGE_WINDOW_DAYS)
    purged, purge_runs = await _purge_totals(session, principal, since)
    backups_in_window = (
        await session.execute(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.action == BACKUP_ACTION,
                AuditEvent.occurred_at >= since,
            )
        )
    ).scalar_one()

    projects = await _projects(session, principal)
    bound = await _bound_projects(session, principal) if thread_backed else []
    active_sessions, threads = await asyncio.gather(
        _count_threads(projects, active=True), _count_threads(bound)
    )

    active = int(by_status.get(MemoryStoreStatus.ACTIVE.value, 0))
    return MemorySummary(
        stores=stores,
        active_sessions=active_sessions,
        stored_memories=int(totals.records or 0) + threads,
        avg_retrieval_latency_ms=(
            round(float(totals.latency), 1) if totals.latency is not None else None
        ),
        state_sync_success_percent=round(active / stores * 100, 1) if stores else 0.0,
        expired_purged_records=purged,
        purge_window_days=PURGE_WINDOW_DAYS,
        purge_runs=purge_runs,
        active_stores=active,
        paused_stores=int(by_status.get(MemoryStoreStatus.PAUSED.value, 0)),
        degraded_stores=int(by_status.get(MemoryStoreStatus.DEGRADED.value, 0)),
        avg_usage_percent=round(float(totals.usage), 1) if totals.usage is not None else None,
        stores_over_capacity_threshold=over_threshold,
        thread_backed_stores=thread_backed,
        last_backup_at=last_backup_at,
        backups_in_window=int(backups_in_window or 0),
        by_type=_slices(by_type, stores),
        by_status=_slices(by_status, stores),
        by_environment=_slices(by_environment, stores),
    )


# ---------------------------------------------------------------------------
# Store writes
# ---------------------------------------------------------------------------


async def create_store(
    session: AsyncSession,
    principal: Principal,
    payload: MemoryStoreCreate,
    *,
    request: Request | None = None,
) -> MemoryStoreRead:
    """Provision a store. It starts Active and empty; nothing is back-filled."""
    principal.require(Role.OPERATOR)

    clash = (
        await session.execute(_scoped(principal).where(MemoryStore.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A memory store named '{payload.name}' already exists.")

    label = payload.retention_policy or retention_label(payload.retention_days)
    store = MemoryStore(
        workspace_id=principal.workspace_id,
        name=payload.name,
        store_type=payload.store_type.value,
        environment=payload.environment.value,
        status=MemoryStoreStatus.ACTIVE.value,
        usage_percent=0,
        record_count=0,
        retention_policy=label,
        retention_days=payload.retention_days,
        last_updated_at=dt.datetime.now(dt.UTC),
        owner_user_id=payload.owner_user_id or principal.user_id,
        backend=payload.backend,
        active_session_count=0,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(store)
    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(f"A memory store named '{payload.name}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="memory.store_created",
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Provisioned {store.store_type} store with {label} retention",
        metadata={"store_type": store.store_type, "retention_days": store.retention_days},
        request=request,
    )
    await session.flush()
    # Server-side timestamp defaults land on the row, not on the instance.
    await session.refresh(store)
    rows = await _decorate(session, principal, [store])
    return rows[0]


async def update_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryStoreUpdate,
    *,
    request: Request | None = None,
) -> MemoryStoreRead:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.OPERATOR)
    store = await get_store(session, principal, store_id)

    if payload.expected_updated_at is not None:
        current = store.updated_at
        expected = _as_utc(payload.expected_updated_at)
        drift = abs((_as_utc(current) - expected).total_seconds()) if current else 0.0
        if current is not None and drift > CONCURRENCY_TOLERANCE_SECONDS:
            raise Conflict(
                f"'{store.name}' was changed by someone else. Reload and try again."
            )

    # ``mode="json"`` renders the enum fields as the strings the columns store.
    changes = payload.model_dump(
        mode="json", exclude_unset=True, exclude={"expected_updated_at"}
    )
    if not changes:
        rows = await _decorate(session, principal, [store])
        return rows[0]

    renamed_from: str | None = None
    if "name" in changes and changes["name"] != store.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    MemoryStore.name == changes["name"], MemoryStore.id != store.id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A memory store named '{changes['name']}' already exists.")
        renamed_from = store.name
        width = Agent.memory_policy.type.length or 0
        if width and len(changes["name"]) > width:
            bound = (
                await session.execute(
                    select(func.count(Agent.id)).where(
                        Agent.workspace_id == principal.workspace_id,
                        Agent.memory_policy == store.name,
                    )
                )
            ).scalar_one()
            if bound:
                raise ValidationFailed(
                    f"{bound} agent(s) name this store as their memory policy, which "
                    f"holds at most {width} characters. Choose a shorter name.",
                    details={"field": "name", "max_length": width},
                )

    counters = {
        "usage_percent",
        "record_count",
        "active_session_count",
        "avg_retrieval_latency_ms",
    }
    for field, value in changes.items():
        setattr(store, field, value)
    if counters & set(changes):
        # The runtime just reported on itself, so the store's clock moves with it.
        store.last_updated_at = dt.datetime.now(dt.UTC)
    store.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A memory store named '{changes.get('name')}' already exists.") from exc

    if renamed_from is not None:
        # Agents are bound to a store by its name, and that binding is the whole
        # scope of the store's records and purge. A rename that left them naming
        # the old one would quietly unbind every agent, so it is carried through
        # — as a consequence of this edit, not an edit of theirs, hence the
        # untouched concurrency token.
        await session.execute(
            sa_update(Agent)
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.memory_policy == renamed_from,
            )
            .values(memory_policy=store.name, updated_at=Agent.updated_at)
            .execution_options(synchronize_session=False)
        )

    await audit.record(
        session,
        principal=principal,
        action="memory.store_updated",
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    # ``updated_at`` is a server-side onupdate: read it back before serialising.
    await session.refresh(store)
    rows = await _decorate(session, principal, [store])
    return rows[0]


async def delete_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a store from the registry. The audit trail survives the deletion.

    Only the registry row goes: nothing is deleted from the telemetry store or
    from any backend, and the store's backups stay on the ledger because they
    are audit rows. A store that agents still name is refused — removing it
    would silently leave those agents' memory under no retention policy at all.
    """
    principal.require(Role.ADMIN)
    store = await get_store(session, principal, store_id)

    bound = (
        await session.execute(
            select(Agent.name)
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.memory_policy == store.name,
            )
            .order_by(Agent.name.asc())
        )
    ).scalars().all()
    if bound:
        shown = ", ".join(bound[:5]) + (" and others" if len(bound) > 5 else "")
        raise PreconditionFailed(
            f"'{store.name}' is still named as the memory policy of {len(bound)} agent(s): "
            f"{shown}. Point them at another store first.",
            details={"bound_agents": len(bound)},
        )
    if store.id in _purging:
        raise Conflict(f"A purge of '{store.name}' is running. Wait for it to finish.")

    await audit.record(
        session,
        principal=principal,
        action="memory.store_deleted",
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Removed {store.store_type} store ({store.environment}, "
            f"{store.retention_policy or 'no'} retention); no records were deleted"
        ),
        metadata={
            "store_type": store.store_type,
            "environment": store.environment,
            "retention_days": store.retention_days,
            "backend": store.backend,
        },
        request=request,
    )
    await session.delete(store)
    await session.flush()


async def update_retention(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: RetentionPolicyUpdate,
    *,
    request: Request | None = None,
) -> MemoryStoreRead:
    """Set the deletion SLA the store is audited against.

    Retention has its own endpoint rather than riding along on PATCH because it
    is the one field a regulator asks about: every change to it lands in the
    audit trail under one action with the old and new window on the row.
    """
    principal.require(Role.ADMIN)
    store = await get_store(session, principal, store_id)

    previous_days = store.retention_days
    previous_label = store.retention_policy
    if previous_days == payload.retention_days and previous_label == payload.retention_policy:
        rows = await _decorate(session, principal, [store])
        return rows[0]

    store.retention_days = payload.retention_days
    store.retention_policy = payload.retention_policy
    store.updated_by = principal.actor

    detail = f"Retention {previous_label or 'unset'} -> {payload.retention_policy}"
    if payload.reason:
        detail = f"{detail} ({payload.reason})"
    await audit.record(
        session,
        principal=principal,
        action="memory.retention_updated",
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "previous_retention_days": previous_days,
            "retention_days": payload.retention_days,
            "previous_policy": previous_label,
            "policy": payload.retention_policy,
            "reason": payload.reason,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(store)
    rows = await _decorate(session, principal, [store])
    return rows[0]


# ---------------------------------------------------------------------------
# Records, sessions, conversations, agent state
# ---------------------------------------------------------------------------


def _require_thread_backed(store: MemoryStore, action: str) -> None:
    """Refuse operations the server cannot honestly perform.

    Only conversation and session stores are held in the telemetry store. Every
    other type lives in the backend named on the row, which this service does
    not address — so it says so instead of answering with an empty page that
    reads like "there is nothing here".
    """
    if store.store_type in THREAD_BACKED_TYPES:
        return
    backend = store.backend or "its own backend"
    raise PreconditionFailed(
        f"'{store.name}' is a {store.store_type} store; its records are held by "
        f"{backend}, which FD AI Command Center governs but does not address. "
        f"{action} it there and report the result with PATCH /memory/{store.id}.",
        details={"store_type": store.store_type, "backend": store.backend},
    )


def _expires_at(
    last_activity: dt.datetime | None, retention_days: int | None
) -> dt.datetime | None:
    if last_activity is None or not retention_days:
        return None
    return last_activity + dt.timedelta(days=retention_days)


async def _governing_retention(
    session: AsyncSession, principal: Principal, agent_ids: Iterable[str]
) -> dict[str, tuple[int | None, str | None]]:
    """``agent id -> (retention days, label)`` of the store each agent is bound to.

    The binding is the one a purge follows (:func:`_store_projects`): the
    thread-backed store the agent's memory policy names. Store names are unique
    in a workspace, so an agent resolves to at most one.
    """
    wanted = sorted(set(agent_ids))
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(Agent.id, MemoryStore.retention_days, MemoryStore.retention_policy)
            .join(
                MemoryStore,
                and_(
                    MemoryStore.workspace_id == Agent.workspace_id,
                    MemoryStore.name == Agent.memory_policy,
                ),
            )
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.id.in_(wanted),
                MemoryStore.store_type.in_(sorted(THREAD_BACKED_TYPES)),
            )
        )
    ).all()
    return {agent_id: (days, label) for agent_id, days, label in rows}


async def list_records(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    params: ListParams,
    *,
    agent_id: str | None = None,
) -> tuple[list[MemoryRecordRead], int]:
    """One page of the store's records, read live from the telemetry store.

    Records are the conversation threads of the agents bound to the store — the
    same scope its Records figure counts and its purge sweeps, so what this
    lists as expiring is exactly what a purge would remove. It used to list the
    whole workspace's threads under every store. ``agent_id`` narrows it to one
    of those agents; a store no agent names holds no records.
    """
    store = await get_store(session, principal, store_id)
    _require_thread_backed(store, "Browse")

    projects = await _store_projects(session, principal, store, agent_id=agent_id)
    if not projects:
        return [], 0
    rows, total = await _fetch_threads(
        projects, want=params.offset + params.page_size, search=params.q
    )
    page = _slice(rows, params)
    return [
        MemoryRecordRead(
            record_key=f"mem:{row.thread_id}",
            session_id=row.thread_id,
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            message_count=row.message_count,
            context_tokens=row.context_tokens,
            size_bytes=row.size_bytes,
            created_at=row.started_at,
            last_activity_at=row.last_activity_at,
            expires_at=_expires_at(row.last_activity_at, store.retention_days),
            retention_policy=store.retention_policy,
            state=row.state,
        )
        for row in page
    ], total


async def list_sessions(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    agent_id: str | None = None,
    state: SessionState | None = None,
) -> tuple[list[MemorySessionRead], int]:
    """The Sessions tab: live conversation threads across the workspace.

    Filtering by state is applied to the merged window rather than pushed into
    the engine, so ``total`` stays the unfiltered thread count the KPI card
    quotes; the page itself is filtered.
    """
    projects = await _projects(session, principal, agent_id=agent_id)
    rows, total = await _fetch_threads(
        projects, want=params.offset + params.page_size, search=params.q
    )
    if state is not None:
        rows = [row for row in rows if row.state is state]
        total = len(rows)
    page = _slice(rows, params)
    return [
        MemorySessionRead(
            session_id=row.thread_id,
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            user=row.user,
            turns=row.message_count,
            started_at=row.started_at,
            last_activity_at=row.last_activity_at,
            duration_ms=row.duration_ms,
            state=row.state,
        )
        for row in page
    ], total


async def list_conversations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    agent_id: str | None = None,
) -> tuple[list[MemoryConversationRead], int]:
    """The Conversation State tab: threads with their context size and expiry.

    Expiry is the thread's last activity plus the retention window of the store
    its agent is bound to — the one store whose purge can reach it. A thread
    whose agent names no store is under no policy, and the two columns are null.

    It used to quote the tightest conversation store in the workspace against
    every thread, which promised an expiry to conversations no purge would ever
    remove and the wrong date to most of the rest.
    """
    projects = await _projects(session, principal, agent_id=agent_id)
    rows, total = await _fetch_threads(
        projects, want=params.offset + params.page_size, search=params.q
    )
    page = _slice(rows, params)
    governing = await _governing_retention(
        session, principal, {row.agent_id for row in page if row.agent_id}
    )
    return [
        MemoryConversationRead(
            conversation_id=row.thread_id,
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            messages=row.message_count,
            context_tokens=row.context_tokens,
            retention_policy=governing.get(row.agent_id or "", (None, None))[1],
            last_activity_at=row.last_activity_at,
            expires_at=_expires_at(
                row.last_activity_at, governing.get(row.agent_id or "", (None, None))[0]
            ),
        )
        for row in page
    ], total


async def list_agent_state(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    environment: str | None = None,
) -> tuple[list[AgentStateRead], int]:
    """The Agent State tab: which store each agent checkpoints into, and whether it is keeping up.

    Session counts are read from the engine, one call per provisioned agent on
    the page and at most :data:`MAX_PROJECT_FANOUT` of them; agents past that cap
    and agents with no project yet report ``null`` rather than a guess.
    """
    stmt = select(Agent).where(Agent.workspace_id == principal.workspace_id)
    stmt = apply_search(stmt, params, [Agent.name, Agent.memory_policy, Agent.environment])
    stmt = apply_filters(stmt, {Agent.environment: environment})
    stmt = apply_sort(stmt, params, AGENT_SORTABLE, default=Agent.name, default_desc=False)
    agents, total = await paginate(session, stmt, params)

    policies = {
        str(name): (str(store_status), str(name))
        for name, store_status in (
            await session.execute(
                select(MemoryStore.name, MemoryStore.status).where(
                    MemoryStore.workspace_id == principal.workspace_id
                )
            )
        ).all()
    }

    provisioned = [agent for agent in agents if agent.engine_project_name][:MAX_PROJECT_FANOUT]
    counts: dict[str, int] = {}
    if provisioned:
        try:
            async with deadline(what="counting agent sessions"):
                totals = await _gather(
                    [
                        _project_thread_count(
                            EngineProject(
                                project_name=str(agent.engine_project_name),
                                project_id=agent.engine_project_id,
                            ),
                            active=False,
                        )
                        for agent in provisioned
                    ]
                )
        except EngineError as exc:
            raise _unavailable(exc) from exc
        counts = {agent.id: total for agent, total in zip(provisioned, totals, strict=True)}

    rows: list[AgentStateRead] = []
    for agent in agents:
        store_status, store_name = policies.get(agent.memory_policy or "", (None, None))
        if not agent.engine_project_name:
            sync = SyncState.NOT_PROVISIONED
        elif store_status == MemoryStoreStatus.PAUSED.value:
            sync = SyncState.PAUSED
        elif store_status == MemoryStoreStatus.DEGRADED.value:
            sync = SyncState.DEGRADED
        else:
            sync = SyncState.SYNCED
        rows.append(
            AgentStateRead(
                agent_id=agent.id,
                agent_name=agent.name,
                environment=agent.environment,
                state_store=store_name,
                memory_policy=agent.memory_policy,
                session_count=counts.get(agent.id),
                last_activity_at=agent.last_used_at,
                sync_state=sync,
            )
        )
    return rows, total


async def list_retention_policies(
    session: AsyncSession, principal: Principal
) -> list[RetentionPolicyRead]:
    """The Retention Policies tab: every distinct policy and what it governs.

    The counts are SQL aggregates; the labels beside them are resolved in a
    second, capped statement so a workspace with thousands of stores still
    answers in a handful of queries.

    Records is the one figure that is not all ours. A thread-backed store's
    counter column is never written (see :func:`_live_counts`), so a policy that
    governs conversation stores read Records 0 however much they held. Their
    share is read live, the same way the store table reads it, and added to what
    the other stores' runtimes reported; when it cannot be read the figure is
    null — not measured — rather than the reported part passed off as the whole.
    """
    workspace = MemoryStore.workspace_id == principal.workspace_id
    threaded = MemoryStore.store_type.in_(sorted(THREAD_BACKED_TYPES))
    aggregates = (
        await session.execute(
            select(
                MemoryStore.retention_policy,
                MemoryStore.retention_days,
                func.count(MemoryStore.id),
                func.coalesce(func.sum(case((threaded, 0), else_=MemoryStore.record_count)), 0),
                func.coalesce(func.sum(case((threaded, 1), else_=0)), 0),
            )
            .where(workspace)
            .group_by(MemoryStore.retention_policy, MemoryStore.retention_days)
            .order_by(MemoryStore.retention_days.asc())
        )
    ).all()

    thread_stores = (
        (
            await session.execute(
                select(MemoryStore)
                .where(workspace, threaded)
                .order_by(MemoryStore.name.asc())
                .limit(MAX_POLICY_DETAIL_ROWS)
            )
        )
        .scalars()
        .all()
    )
    live = await _live_counts(session, principal, thread_stores)
    # policy -> (thread-backed stores measured, threads they hold)
    held: dict[tuple[str | None, int | None], tuple[int, int]] = {}
    for store in thread_stores:
        records = live.get(store.id, (None, None, 0))[0]
        if records is None:
            continue
        key = (store.retention_policy, store.retention_days)
        measured, threads = held.get(key, (0, 0))
        held[key] = (measured + 1, threads + records)

    detail = (
        await session.execute(
            select(
                MemoryStore.retention_policy,
                MemoryStore.retention_days,
                MemoryStore.store_type,
                MemoryStore.name,
                MemoryStore.status,
            )
            .where(workspace)
            .order_by(MemoryStore.name.asc())
            .limit(MAX_POLICY_DETAIL_ROWS)
        )
    ).all()

    types: dict[tuple[str | None, int | None], set[str]] = {}
    names: dict[tuple[str | None, int | None], list[str]] = {}
    active: dict[tuple[str | None, int | None], bool] = {}
    for policy, days, store_type, name, status in detail:
        key = (policy, days)
        types.setdefault(key, set()).add(str(store_type))
        names.setdefault(key, []).append(str(name))
        active[key] = active.get(key, False) or status == MemoryStoreStatus.ACTIVE.value

    rows: list[RetentionPolicyRead] = []
    for policy, days, store_count, reported, threaded_stores in aggregates:
        key = (policy, days)
        measured, threads = held.get(key, (0, 0))
        rows.append(
            RetentionPolicyRead(
                policy=policy or (retention_label(days) if days else "No policy set"),
                retention_days=days,
                applies_to=sorted(types.get(key, set())),
                store_count=int(store_count),
                # Every thread-backed store under the policy was measured, or
                # the sum is not a total and is not shown as one.
                record_count=(
                    int(reported or 0) + threads
                    if measured == int(threaded_stores or 0)
                    else None
                ),
                stores=sorted(names.get(key, [])),
                status="Active" if active.get(key, False) else "Inactive",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _PurgeProgress:
    """What one purge has done so far.

    Kept outside the sweep so that a sweep which fails half way still knows what
    it removed before it failed. Those deletions happened; the audit row and the
    response have to say so whatever became of the rest.
    """

    candidates: int = 0
    records: int = 0
    traces: int = 0
    capped: bool = False
    failure: EngineError | None = None
    written: set[str] = dataclasses.field(default_factory=set)


#: Stores with a purge running in this process. The advisory lock below covers
#: the other workers; this covers databases that have no such lock.
_purging: set[str] = set()


async def _claim_purge(session: AsyncSession, store: MemoryStore) -> None:
    """Keep a store to one purge at a time, or answer 409.

    The console gives up on a request after 30 s and the operator clicks again;
    two sweeps of the same threads then race, and each reports the other's
    deletions as its own. The lock is transaction-scoped, so it is released by
    the commit or rollback that ends the purge and cannot be leaked by a
    cancelled request. It is tried, never waited on: the second caller is told,
    not parked on a pooled connection.
    """
    busy = Conflict(f"A purge of '{store.name}' is already running. Wait for it to finish.")
    if store.id in _purging:
        raise busy
    if session.get_bind().dialect.name == "postgresql":
        digest = hashlib.sha256(store.id.encode()).digest()
        key = int.from_bytes(digest[:4], "big", signed=True)
        held = await session.execute(
            select(func.pg_try_advisory_xact_lock(PURGE_LOCK_NAMESPACE, key))
        )
        if not held.scalar():
            raise busy
    _purging.add(store.id)


def _last_moment(row: Mapping[str, Any]) -> dt.datetime | None:
    """The latest instant a thread row mentions, whichever field carries it.

    Expiry is judged on the most recent of them, so a thread is only ever called
    expired when *nothing* about it is newer than the cutoff.
    """
    moments = [
        moment
        for moment in (
            _instant(row.get(key))
            for key in ("last_updated_at", "end_time", "start_time", "created_at")
        )
        if moment is not None
    ]
    return max(moments) if moments else None


async def _expired_threads(
    client: EngineClient, project: EngineProject, cutoff: dt.datetime
) -> tuple[list[str], bool]:
    """Ids of one project's threads whose last activity is past retention.

    The store is asked for the project's threads and nothing more: no time
    window is sent, because which window property this endpoint honours has
    never been verified, and a window that is silently ignored would hand back
    the newest threads as though they were the oldest. Each row is judged here,
    on its own timestamps.
    """
    expired: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PURGE_BATCHES):
        batch = await client.search_threads(
            **_scope(project),
            limit=PURGE_BATCH,
            last_retrieved_thread_model_id=cursor,
            truncate=True,
        )
        rows = [row for row in batch if isinstance(row, dict)]
        for row in rows:
            thread_id = str(row.get("thread_id") or row.get("id") or "")
            last = _last_moment(row)
            if thread_id and last is not None and last < cutoff:
                expired.append(thread_id)
        cursor = _thread_cursor(rows)
        if len(rows) < PURGE_BATCH or cursor is None:
            return expired, False
    return expired, True


def _thread_cursor(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Where the next page of a thread search resumes.

    The engine pages threads by ``thread_model_id``, its own surrogate UUID. A
    thread's ``id`` is the *caller's* business id — any string at all — and
    sending that as the cursor is refused with a 400 on the second page of any
    project that has one.
    """
    if not rows:
        return None
    return str(rows[-1].get("thread_model_id") or "") or None


async def _thread_traces(
    client: EngineClient, project: EngineProject, thread_id: str, cutoff: dt.datetime
) -> list[str]:
    """Ids of the traces one expired thread is made of; empty if it is not expired.

    Every row is checked again before its id is handed to a delete. The filter
    is a request, and a store that ignored it would answer with the whole
    project: a row that does not carry this thread's id is never deleted. And a
    thread with even one trace newer than the cutoff is still being written to,
    whatever its own row said, so none of it goes.
    """
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PURGE_BATCHES):
        batch = await client.search_traces(
            **_scope(project),
            filters=[{"field": "thread_id", "operator": "=", "value": thread_id}],
            limit=PURGE_BATCH,
            last_retrieved_id=cursor,
            truncate=True,
        )
        rows = [row for row in batch if isinstance(row, dict) and row.get("id")]
        for row in rows:
            if str(row.get("thread_id") or "") != thread_id:
                continue
            started = _instant(row.get("start_time"))
            if started is None or started >= cutoff:
                return []
            ids.append(str(row["id"]))
        if len(rows) < PURGE_BATCH:
            break
        cursor = str(rows[-1]["id"])
    return ids


async def _sweep(
    client: EngineClient,
    project: EngineProject,
    cutoff: dt.datetime,
    progress: _PurgeProgress,
    *,
    delete: bool,
    out_of_time: Callable[[], bool],
) -> None:
    """Remove one project's expired threads, or just count them.

    Only traces that belong to an expired conversation thread are ever
    addressed. An agent's ordinary runs carry no thread id; they are run
    history, not memory, and no retention policy on a memory store reaches them.
    """
    expired, capped = await _expired_threads(client, project, cutoff)
    progress.candidates += len(expired)
    progress.capped = progress.capped or capped
    if not delete:
        return

    for start in range(0, len(expired), CONCURRENT_ENGINE_CALLS):
        if out_of_time():
            progress.capped = True
            return
        group = expired[start : start + CONCURRENT_ENGINE_CALLS]
        found = await _gather(
            [_thread_traces(client, project, thread_id, cutoff) for thread_id in group]
        )
        # One delete per batch of ids rather than per thread, but counted per
        # thread: a record is gone when the last of its traces is.
        left = [len(traces) for traces in found]
        pending = [(index, trace_id) for index, traces in enumerate(found) for trace_id in traces]
        for offset in range(0, len(pending), PURGE_BATCH):
            chunk = pending[offset : offset + PURGE_BATCH]
            await client.delete_traces(
                [trace_id for _, trace_id in chunk], project_id=project.project_id
            )
            progress.traces += len(chunk)
            if project.project_id:
                progress.written.add(project.project_id)
            for index, _ in chunk:
                left[index] -= 1
                if left[index] == 0:
                    progress.records += 1


async def _threads_held(projects: Sequence[EngineProject]) -> int | None:
    """Threads the projects hold right now, or None when the store cannot say."""
    try:
        return await _count_threads(projects)
    except AppError:
        return None


async def purge_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryPurgeRequest,
    *,
    request: Request | None = None,
) -> MemoryPurgeResult:
    """Delete the conversation records past the store's retention window.

    This is the real deletion, not a marker, so it is fenced on every side:

    * **Whose.** Only the agents bound to this store (:func:`_store_projects`)
      are swept. A store no agent names is refused: it owns nothing the server
      can identify, and "everything in the workspace" is not an answer.
    * **What.** Only conversation threads whose *last* activity is past the
      window, removed whole. Runs that belong to no thread are never touched.
    * **On whose word.** A real purge carries the store's name in ``confirm``.
      ``dry_run`` walks the same threads, needs no confirmation, and reports
      what would go and whose it is without touching anything.
    * **On the record.** Whatever was deleted is audited by this request, even
      when the telemetry store fails part way through or the time budget runs
      out: the response then says ``partial`` or ``capped`` and the run is
      simply repeated. Only a purge that deleted nothing is allowed to fail.
    """
    principal.require(Role.OPERATOR)
    store = await get_store(session, principal, store_id)
    _require_thread_backed(store, "Purge")

    if not store.retention_days:
        raise PreconditionFailed(
            f"'{store.name}' has no retention policy, so nothing is expired. "
            f"Set one with PUT /memory/{store.id}/retention first."
        )
    if store.status == MemoryStoreStatus.PAUSED.value:
        raise PreconditionFailed(
            f"'{store.name}' is paused. Resume it before running a purge."
        )
    if not payload.dry_run and (payload.confirm or "").strip() != store.name:
        raise ValidationFailed(
            "A purge deletes conversation records permanently. Confirm it by sending "
            f"'confirm' set to the store's exact name, '{store.name}'.",
            details={"field": "confirm"},
        )

    # Every bound agent, not the screen's fan-out cap. The cap keeps the most
    # recently used agents, so the ones it dropped — the idle ones, whose
    # threads are the likeliest to have expired — were never swept however often
    # the purge was repeated, and the response still read as complete. The time
    # budget bounds the run instead: a repeat passes quickly over the projects
    # that are already clean and carries on where the last one stopped.
    projects = await _store_projects(session, principal, store, limit=None)
    if not projects:
        width = Agent.memory_policy.type.length or 0
        too_long = (
            f" An agent's memory policy holds at most {width} characters, so rename "
            "the store to fit first."
            if width and len(store.name) > width
            else ""
        )
        raise PreconditionFailed(
            f"No provisioned agent names '{store.name}' as its memory policy, so the "
            "server cannot tell which conversation records belong to it and "
            "will not delete any. Set the memory policy of the agents that use this "
            f"store to its name, then run the purge again.{too_long}",
            details={"bound_agents": 0},
        )

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=store.retention_days)
    client = _engine()
    progress = _PurgeProgress()
    stop_at = time.monotonic() + settings.memory_purge_budget_seconds

    def out_of_time() -> bool:
        return time.monotonic() >= stop_at

    claimed = not payload.dry_run
    if claimed:
        await _claim_purge(session, store)
    try:
        for project in projects:
            if out_of_time():
                progress.capped = True
                break
            try:
                await _sweep(
                    client,
                    project,
                    cutoff,
                    progress,
                    delete=not payload.dry_run,
                    out_of_time=out_of_time,
                )
            except EngineNotFound:
                # A project that has never received telemetry holds nothing to expire.
                continue
    except EngineError as exc:
        progress.failure = exc
    finally:
        if claimed:
            _purging.discard(store.id)

    if progress.failure is not None and not progress.traces:
        # Nothing was removed, so there is nothing to account for: fail whole.
        if isinstance(progress.failure, EngineBadRequest):
            raise ValidationFailed(
                "The telemetry store refused the purge.",
                details={"cutoff": cutoff.isoformat()},
            ) from progress.failure
        raise _unavailable(progress.failure) from progress.failure
    partial = progress.failure is not None

    if progress.traces:
        # The run screens remember these projects' rows for a few seconds.
        for project_id in progress.written:
            telemetry_cache.project_written(project_id)
        # A purge acts on the store; it does not edit it, so the row's
        # concurrency token stays where the console last read it.
        await stamp(session, [store], last_updated_at=dt.datetime.now(dt.UTC))
        _forget_counts(projects)
    remaining = await _threads_held(projects)

    agents = [project.agent_name or project.project_name for project in projects]
    whose = f"{len(agents)} agent(s)"
    if payload.dry_run:
        message = (
            f"{progress.candidates} record(s) held by {whose} are past "
            f"{store.retention_policy} retention. Nothing was deleted."
        )
        if progress.capped:
            message += " The count stopped at the run's limit, so there may be more."
    else:
        message = (
            f"Removed {progress.records} expired record(s) "
            f"({progress.traces} trace(s)) held by {whose}."
        )
        if partial:
            message += (
                " The telemetry store stopped answering part way; run the purge "
                "again to finish."
            )
        elif progress.capped:
            message += " More remain; run the purge again."

    detail = (
        f"Dry run: {progress.candidates} record(s) past {store.retention_policy} retention"
        if payload.dry_run
        else (
            f"Purged {progress.records} record(s) ({progress.traces} trace(s)) "
            f"past {store.retention_policy} retention"
        )
    )
    # The sentence names a handful; the metadata below carries every one.
    shown = ", ".join(agents[:5]) + (f" and {len(agents) - 5} others" if len(agents) > 5 else "")
    detail = f"{detail}; agents: {shown}"
    if partial:
        detail = f"{detail}; stopped early, the telemetry store failed part way"
    if payload.reason:
        detail = f"{detail} ({payload.reason})"
    await audit.record(
        session,
        principal=principal,
        action=PURGE_ACTION,
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "purged_records": progress.records,
            "purged_traces": progress.traces,
            "candidate_records": progress.candidates,
            "dry_run": payload.dry_run,
            "cutoff": cutoff.isoformat(),
            "retention_days": store.retention_days,
            "projects": len(projects),
            "agents": agents,
            "agent_ids": [project.agent_id for project in projects],
            "capped": progress.capped,
            "partial": partial,
            "failure": type(progress.failure).__name__ if progress.failure else None,
        },
        request=request,
    )
    await session.flush()

    return MemoryPurgeResult(
        store_id=store.id,
        name=store.name,
        dry_run=payload.dry_run,
        cutoff=cutoff,
        retention_days=store.retention_days,
        candidate_records=progress.candidates,
        purged_records=progress.records,
        purged_traces=progress.traces,
        remaining_records=remaining,
        usage_percent=None,
        projects=len(projects),
        agents=agents,
        capped=progress.capped,
        partial=partial,
        message=message,
    )


# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------


def _manifest(event: AuditEvent) -> dict[str, Any]:
    metadata = event.event_metadata if isinstance(event.event_metadata, Mapping) else {}
    manifest = metadata.get("manifest")
    return dict(manifest) if isinstance(manifest, Mapping) else {}


def _backup_kind(value: Any) -> BackupKind:
    try:
        return BackupKind(str(value))
    except ValueError:
        return BackupKind.FULL


def _backup_status(value: Any) -> BackupStatus:
    try:
        return BackupStatus(str(value))
    except ValueError:
        return BackupStatus.COMPLETED


def _backup_read(event: AuditEvent) -> MemoryBackupRead:
    metadata = event.event_metadata if isinstance(event.event_metadata, Mapping) else {}
    manifest = _manifest(event)
    return MemoryBackupRead(
        id=event.id,
        store_id=event.entity_id or "",
        store_name=event.entity_label,
        created_at=event.occurred_at,
        created_by=event.actor,
        kind=_backup_kind(metadata.get("kind")),
        status=_backup_status(metadata.get("status")),
        record_count=_int_or_none(metadata.get("record_count")) or 0,
        payload_bytes=_int_or_none(metadata.get("payload_bytes")),
        window_from=_instant(metadata.get("window_from")),
        projects=_int_or_none(manifest.get("projects")) or 0,
        # True of every row, old and new: no backup ever kept a thread.
        threads_captured=False,
        captured=list(BACKUP_CAPTURES),
    )


#: Said on every backup and restore response. The console's copy used to
#: promise that a backup "captures the store's current records" and that a
#: restore "replaces the store's current contents"; neither was ever true.
BACKUP_NOTICE: Final[str] = (
    "This snapshot holds the store's governed state (retention policy and status) "
    "and a count of its conversation threads. The threads themselves are not "
    "copied, so a purge cannot be undone from it."
)
RESTORE_NOTICE: Final[str] = (
    "Conversation threads were not restored: a backup holds the store's governed "
    "state, never its records, so anything purged since it was taken stays deleted."
)


async def backup_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryBackupRequest,
    *,
    request: Request | None = None,
) -> MemoryBackupResult:
    """Snapshot a store's governed state, and say plainly that is all it is.

    What is recorded is the manifest: the retention policy and status a restore
    can put back, and how many conversation threads the store's agents held at
    that moment. The threads are counted, not copied — the server has
    nowhere to keep a second copy of them — and the response, the ledger row and
    the audit detail all say so, because the one thing a backup must never do is
    let someone purge on the strength of it.

    This used to page every thread of every project in the workspace into one
    list, serialise it to measure a "payload size", and throw it away: up to
    160,000 rows held and a blocking ``json.dumps`` on the event loop, to
    produce a number describing data that was not kept. The count now costs one
    cheap call per bound agent and nothing is held.
    """
    principal.require(Role.OPERATOR)
    store = await get_store(session, principal, store_id)
    _require_thread_backed(store, "Back up")

    projects = await _store_projects(session, principal, store)
    threads = await _count_threads(projects) if projects else 0
    agents = [project.agent_name or project.project_name for project in projects]
    now = dt.datetime.now(dt.UTC)

    manifest = {
        "record_count": threads,
        "projects": len(projects),
        "agents": agents,
        "store_status": store.status,
        "retention_policy": store.retention_policy,
        "retention_days": store.retention_days,
        "threads_captured": False,
    }
    event = await audit.record(
        session,
        principal=principal,
        action=BACKUP_ACTION,
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Snapshot of governed state ({store.retention_policy or 'no'} retention, "
            f"{store.status}); {threads} thread(s) counted, none copied"
            + (f" ({payload.note})" if payload.note else "")
        ),
        metadata={
            "kind": BackupKind.FULL.value,
            "status": BackupStatus.COMPLETED.value,
            "record_count": threads,
            "payload_bytes": None,
            "window_from": None,
            "note": payload.note,
            "threads_captured": False,
            "manifest": manifest,
        },
        request=request,
    )

    # Bookkeeping about the store, not an edit of it: the concurrency token the
    # console holds must survive taking a snapshot.
    await stamp(session, [store], last_backup_at=now)
    await session.flush()

    return MemoryBackupResult(
        backup_id=event.id,
        store_id=store.id,
        name=store.name,
        kind=BackupKind.FULL,
        status=BackupStatus.COMPLETED,
        record_count=threads,
        payload_bytes=None,
        window_from=None,
        created_at=event.occurred_at,
        projects=len(projects),
        agents=agents,
        threads_captured=False,
        captured=list(BACKUP_CAPTURES),
        notice=BACKUP_NOTICE,
    )


def _backup_query(principal: Principal, store_id: str | None) -> Select:
    stmt = select(AuditEvent).where(
        AuditEvent.workspace_id == principal.workspace_id,
        AuditEvent.action == BACKUP_ACTION,
        AuditEvent.entity_type == ENTITY_TYPE,
    )
    if store_id is not None:
        stmt = stmt.where(AuditEvent.entity_id == store_id)
    return stmt


async def list_backups(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    store_id: str | None = None,
) -> tuple[list[MemoryBackupRead], int]:
    """The backup ledger, newest first.

    Backups are audit rows, so this list cannot disagree with the audit trail and
    no backup can be edited or removed after it was taken.
    """
    if store_id is not None:
        # Prove the store is ours before filtering on it, so an id from another
        # tenant 404s instead of returning an empty ledger.
        await get_store(session, principal, store_id)

    stmt = _backup_query(principal, store_id)
    stmt = apply_search(stmt, params, [AuditEvent.entity_label, AuditEvent.detail])
    stmt = stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
    rows, total = await paginate(session, stmt, params)
    return [_backup_read(event) for event in rows], total


async def restore_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryRestoreRequest,
    *,
    request: Request | None = None,
) -> MemoryRestoreResult:
    """Put a store's governed state back to what one backup captured.

    Governed state is the retention policy and the status, and that is all that
    comes back. Threads deleted from the telemetry store since the snapshot are
    not resurrected — no backup ever held them — and the response says so in
    ``threads_restored`` and ``notice`` rather than leaving a toast to imply it.

    Counters are deliberately *not* restored. They used to be: the record count
    jumped back to its pre-purge figure while the records stayed deleted, which
    made the screen agree with the operator's hope instead of with the store.
    """
    principal.require(Role.ADMIN)
    store = await get_store(session, principal, store_id)

    event = (
        await session.execute(
            _backup_query(principal, store.id).where(AuditEvent.id == payload.backup_id)
        )
    ).scalar_one_or_none()
    if event is None:
        raise NotFound(f"Backup '{payload.backup_id}' does not exist for '{store.name}'.")

    manifest = _manifest(event)
    if not manifest:
        raise PreconditionFailed(
            "That backup carries no manifest, so there is nothing to restore from."
        )

    restored: list[str] = []
    previous = {
        "retention_days": store.retention_days,
        "retention_policy": store.retention_policy,
        "status": store.status,
    }
    retention_days = _int_or_none(manifest.get("retention_days"))
    if retention_days is not None and retention_days != store.retention_days:
        store.retention_days = retention_days
        store.retention_policy = str(
            manifest.get("retention_policy") or retention_label(retention_days)
        )
        restored.append("retention_policy")
    status = manifest.get("store_status")
    if (
        isinstance(status, str)
        and status != store.status
        and status in {member.value for member in MemoryStoreStatus}
    ):
        store.status = status
        restored.append("status")

    now = dt.datetime.now(dt.UTC)
    if restored:
        store.last_updated_at = now
        store.updated_by = principal.actor

    detail = (
        f"Restored governed state ({', '.join(restored) or 'nothing had changed'}) from "
        f"backup {event.id} taken {event.occurred_at.isoformat()}; no threads restored"
    )
    if payload.reason:
        detail = f"{detail} ({payload.reason})"
    await audit.record(
        session,
        principal=principal,
        action="memory.restored",
        entity_type=ENTITY_TYPE,
        entity_id=store.id,
        entity_label=store.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "backup_id": event.id,
            "fields_restored": restored,
            "threads_restored": False,
            "previous": previous,
            "backup_taken_at": event.occurred_at.isoformat(),
        },
        request=request,
    )
    await session.flush()
    await session.refresh(store)

    projects = await _store_projects(session, principal, store)
    return MemoryRestoreResult(
        backup_id=event.id,
        store_id=store.id,
        name=store.name,
        restored_at=now,
        record_count=(
            (await _threads_held(projects) if projects else 0)
            if store.store_type in THREAD_BACKED_TYPES
            else store.record_count
        ),
        usage_percent=None,
        retention_policy=store.retention_policy,
        status=MemoryStoreStatus(store.status),
        fields_restored=restored,
        threads_restored=False,
        notice=RESTORE_NOTICE,
    )
