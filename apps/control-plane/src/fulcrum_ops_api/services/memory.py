"""Memory & State Management business logic.

Two sources of truth meet here.

*Ours* is the ``memory_stores`` table: the registry of every store an agent may
read or write, who owns it, and the retention policy its deletion SLA is
audited against. Nothing about that lives anywhere else, so it is queried,
mutated and audited like any other governance row.

*Theirs* is the telemetry engine, which holds the conversation and session
records themselves as trace threads. Every session, conversation, record and
backup payload on this screen is read live from the engine through the adapter;
none of it is cached, estimated or reconstructed. Where the engine reports
nothing — a store type it does not hold, a usage figure it does not track — the
field is null and the operation is refused with a precondition rather than
answered with a plausible number.

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
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, func, nullslast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import (
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    get_engine_client,
)
from ..models.governance import AuditEvent
from ..models.identity import Role, User
from ..models.registry import Agent, MemoryStore, MemoryStoreStatus, MemoryStoreType
from ..schemas.memory import (
    ACTIVE_SESSION_WINDOW_HOURS,
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
from . import audit

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

#: Purge and backup sweep sizes. The batch is one engine round trip; the batch
#: count bounds a single run so one purge cannot hold a connection for minutes.
PURGE_BATCH: Final[int] = 500
MAX_PURGE_BATCHES: Final[int] = 20
BACKUP_BATCH: Final[int] = 500
MAX_BACKUP_BATCHES: Final[int] = 10

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
    try:
        payloads = await _gather(
            [
                client.list_threads(
                    page=1,
                    size=size,
                    project_name=project.project_name,
                    search=search,
                    from_time=from_time,
                    truncate=True,
                )
                for project in projects
            ]
        )
    except EngineNotFound:
        # A project that has never received telemetry does not exist yet.
        return [], 0
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


async def _count_threads(
    projects: Sequence[EngineProject], *, from_time: dt.datetime | None = None
) -> int:
    """How many threads match, without materialising them."""
    _, total = await _fetch_threads(projects, want=1, from_time=from_time)
    return total


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


async def _decorate(
    session: AsyncSession, principal: Principal, stores: Sequence[MemoryStore]
) -> list[MemoryStoreRead]:
    owners = await _owners(session, (store.owner_user_id for store in stores))
    backups = await _backup_counts(session, principal, [store.id for store in stores])
    payload: list[MemoryStoreRead] = []
    for store in stores:
        owner_name, owner_team = owners.get(store.owner_user_id or "", (None, None))
        payload.append(
            MemoryStoreRead.from_model(
                store,
                owner_name=owner_name,
                owner_team=owner_team,
                backup_count=backups.get(store.id, 0),
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


async def _grouped(
    session: AsyncSession, principal: Principal, column: Any
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(column, func.count(MemoryStore.id))
            .where(MemoryStore.workspace_id == principal.workspace_id)
            .group_by(column)
        )
    ).all()
    return {str(label): int(count) for label, count in rows}


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


async def summarise(session: AsyncSession, principal: Principal) -> MemorySummary:
    """The six KPI cards and the three breakdowns, in one round trip.

    Every store-side number is a SQL aggregate over the whole workspace rather
    than over the page on screen; Active Sessions is the live thread count from
    the telemetry store, so the card is empty-but-honest rather than stale when
    the engine is unreachable — that condition surfaces as a 503.
    """
    workspace = MemoryStore.workspace_id == principal.workspace_id
    totals = (
        await session.execute(
            select(
                func.count(MemoryStore.id).label("stores"),
                func.coalesce(func.sum(MemoryStore.record_count), 0).label("records"),
                func.avg(MemoryStore.avg_retrieval_latency_ms).label("latency"),
                func.avg(MemoryStore.usage_percent).label("usage"),
                func.max(MemoryStore.last_backup_at).label("last_backup_at"),
            ).where(workspace)
        )
    ).one()

    by_status = await _grouped(session, principal, MemoryStore.status)
    by_type = await _grouped(session, principal, MemoryStore.store_type)
    by_environment = await _grouped(session, principal, MemoryStore.environment)

    over_threshold = (
        await session.execute(
            select(func.count(MemoryStore.id)).where(
                workspace, MemoryStore.usage_percent >= USAGE_WARNING_PERCENT
            )
        )
    ).scalar_one()
    thread_backed = (
        await session.execute(
            select(func.count(MemoryStore.id)).where(
                workspace, MemoryStore.store_type.in_(sorted(THREAD_BACKED_TYPES))
            )
        )
    ).scalar_one()

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
    window_start = dt.datetime.now(dt.UTC) - dt.timedelta(
        hours=ACTIVE_SESSION_WINDOW_HOURS
    )
    active_sessions = await _count_threads(projects, from_time=window_start)

    stores = int(totals.stores or 0)
    active = int(by_status.get(MemoryStoreStatus.ACTIVE.value, 0))
    return MemorySummary(
        stores=stores,
        active_sessions=active_sessions,
        stored_memories=int(totals.records or 0),
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
        stores_over_capacity_threshold=int(over_threshold or 0),
        thread_backed_stores=int(thread_backed or 0),
        last_backup_at=totals.last_backup_at,
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
    """Refuse operations the control plane cannot honestly perform.

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
        f"{backend}, which the control plane governs but does not address. "
        f"{action} it there and report the result with PATCH /memory/{store.id}.",
        details={"store_type": store.store_type, "backend": store.backend},
    )


def _expires_at(
    last_activity: dt.datetime | None, retention_days: int | None
) -> dt.datetime | None:
    if last_activity is None or not retention_days:
        return None
    return last_activity + dt.timedelta(days=retention_days)


async def list_records(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    params: ListParams,
    *,
    agent_id: str | None = None,
) -> tuple[list[MemoryRecordRead], int]:
    """One page of the store's records, read live from the telemetry store.

    Records are the conversation threads the store holds. The control plane does
    not partition threads per store, so the view is the workspace's thread stream
    — narrow it with ``agent_id`` to get one agent's memory.
    """
    store = await get_store(session, principal, store_id)
    _require_thread_backed(store, "Browse")

    projects = await _projects(session, principal, agent_id=agent_id)
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

    Expiry is the thread's last activity plus the retention window of the
    conversation store that governs its environment; with no conversation store
    registered there is no policy to apply and the column is null.
    """
    projects = await _projects(session, principal, agent_id=agent_id)
    rows, total = await _fetch_threads(
        projects, want=params.offset + params.page_size, search=params.q
    )
    policy = (
        await session.execute(
            _scoped(principal)
            .where(
                MemoryStore.store_type == MemoryStoreType.CONVERSATION.value,
                MemoryStore.status == MemoryStoreStatus.ACTIVE.value,
            )
            .order_by(MemoryStore.retention_days.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    retention_days = policy.retention_days if policy else None
    label = policy.retention_policy if policy else None

    page = _slice(rows, params)
    return [
        MemoryConversationRead(
            conversation_id=row.thread_id,
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            messages=row.message_count,
            context_tokens=row.context_tokens,
            retention_policy=label,
            last_activity_at=row.last_activity_at,
            expires_at=_expires_at(row.last_activity_at, retention_days),
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
        client = _engine()
        try:
            payloads = await _gather(
                [
                    client.list_threads(
                        page=1, size=1, project_name=agent.engine_project_name, truncate=True
                    )
                    for agent in provisioned
                ]
            )
        except EngineNotFound:
            payloads = []
        except EngineError as exc:
            raise _unavailable(exc) from exc
        for agent, payload in zip(provisioned, payloads, strict=False):
            counts[agent.id] = _total_of(payload, len(_rows_of(payload)))

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
    answers in two queries.
    """
    workspace = MemoryStore.workspace_id == principal.workspace_id
    aggregates = (
        await session.execute(
            select(
                MemoryStore.retention_policy,
                MemoryStore.retention_days,
                func.count(MemoryStore.id),
                func.coalesce(func.sum(MemoryStore.record_count), 0),
            )
            .where(workspace)
            .group_by(MemoryStore.retention_policy, MemoryStore.retention_days)
            .order_by(MemoryStore.retention_days.asc())
        )
    ).all()

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
    for policy, days, store_count, record_count in aggregates:
        key = (policy, days)
        rows.append(
            RetentionPolicyRead(
                policy=policy or (retention_label(days) if days else "No policy set"),
                retention_days=days,
                applies_to=sorted(types.get(key, set())),
                store_count=int(store_count),
                record_count=int(record_count or 0),
                stores=sorted(names.get(key, [])),
                status="Active" if active.get(key, False) else "Inactive",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


async def _sweep(
    client: EngineClient, project: EngineProject, cutoff: dt.datetime, *, delete: bool
) -> tuple[int, bool]:
    """Walk one project's expired traces, deleting them or just counting them.

    When deleting, the cursor deliberately does not advance: the rows just
    removed are gone, so the next batch starts at the head of what is left.
    """
    swept = 0
    cursor: str | None = None
    for _ in range(MAX_PURGE_BATCHES):
        rows = await client.search_traces(
            project_name=project.project_name,
            to_time=cutoff,
            limit=PURGE_BATCH,
            last_retrieved_id=cursor,
            truncate=True,
        )
        ids = [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]
        if not ids:
            return swept, False
        if delete:
            await client.delete_traces(ids, project_id=project.project_id)
        else:
            cursor = ids[-1]
        swept += len(ids)
        if len(ids) < PURGE_BATCH:
            return swept, False
    return swept, True


async def purge_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryPurgeRequest,
    *,
    request: Request | None = None,
) -> MemoryPurgeResult:
    """Delete everything past the store's retention window.

    This is the real deletion, not a marker: expired traces are removed from the
    telemetry store in batches, and the registry's record count and usage are
    reduced by exactly what went. ``dry_run`` walks the same window and reports
    what would go without touching anything.
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

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=store.retention_days)
    projects = await _projects(session, principal)
    client = _engine()

    purged = 0
    capped = False
    try:
        for project in projects:
            swept, hit_cap = await _sweep(client, project, cutoff, delete=not payload.dry_run)
            purged += swept
            capped = capped or hit_cap
    except EngineNotFound:
        purged, capped = 0, False
    except EngineBadRequest as exc:
        raise ValidationFailed(
            "The telemetry store refused the purge window.",
            details={"cutoff": cutoff.isoformat()},
        ) from exc
    except EngineError as exc:
        raise _unavailable(exc) from exc

    before = store.record_count
    if not payload.dry_run and purged:
        remaining = max(0, before - purged)
        # Usage is a share of provisioned capacity, so it falls with the rows.
        scaled = int(round(store.usage_percent * remaining / before)) if before else 0
        store.record_count = remaining
        store.usage_percent = max(0, min(100, scaled))
        store.last_updated_at = dt.datetime.now(dt.UTC)
        store.updated_by = principal.actor
    remaining = store.record_count
    usage = store.usage_percent

    detail = (
        f"Dry run: {purged} record(s) past {store.retention_policy} retention"
        if payload.dry_run
        else f"Purged {purged} record(s) past {store.retention_policy} retention"
    )
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
            "purged_records": 0 if payload.dry_run else purged,
            "candidate_records": purged,
            "dry_run": payload.dry_run,
            "cutoff": cutoff.isoformat(),
            "retention_days": store.retention_days,
            "projects": len(projects),
            "capped": capped,
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
        purged_records=purged,
        remaining_records=remaining,
        usage_percent=usage,
        projects=len(projects),
        capped=capped,
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
    )


async def backup_store(
    session: AsyncSession,
    principal: Principal,
    store_id: str,
    payload: MemoryBackupRequest,
    *,
    request: Request | None = None,
) -> MemoryBackupResult:
    """Snapshot a store: export its threads through the adapter, record the manifest.

    The export is streamed from the telemetry store and measured here; the bytes
    stay inside the telemetry store's own storage domain, and what the control
    plane persists is the manifest — what was captured, how much of it, and the
    state the store was in — which is exactly what a restore replays.

    A store that has been backed up before is exported incrementally from the
    last backup unless ``full`` is set.
    """
    principal.require(Role.OPERATOR)
    store = await get_store(session, principal, store_id)
    _require_thread_backed(store, "Back up")

    window_from = None if payload.full else store.last_backup_at
    kind = BackupKind.FULL if window_from is None else BackupKind.INCREMENTAL
    projects = await _projects(session, principal)
    client = _engine()

    exported: list[dict[str, Any]] = []
    capped = False
    try:
        for project in projects:
            cursor: str | None = None
            for _ in range(MAX_BACKUP_BATCHES):
                batch = await client.search_threads(
                    project_name=project.project_name,
                    from_time=window_from,
                    limit=BACKUP_BATCH,
                    last_retrieved_thread_model_id=cursor,
                    truncate=True,
                )
                rows = [row for row in batch if isinstance(row, dict)]
                if not rows:
                    break
                exported.extend(rows)
                cursor = str(rows[-1].get("id") or "") or None
                if len(rows) < BACKUP_BATCH or cursor is None:
                    break
            else:
                capped = True
    except EngineNotFound:
        exported = []
    except EngineError as exc:
        raise _unavailable(exc) from exc

    payload_bytes = len(json.dumps(exported, default=str).encode("utf-8"))
    now = dt.datetime.now(dt.UTC)
    status = BackupStatus.PARTIAL if capped else BackupStatus.COMPLETED

    manifest = {
        "record_count": len(exported),
        "payload_bytes": payload_bytes,
        "projects": len(projects),
        "store_record_count": store.record_count,
        "store_usage_percent": store.usage_percent,
        "store_status": store.status,
        "retention_policy": store.retention_policy,
        "retention_days": store.retention_days,
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
            f"{kind.value} backup: {len(exported)} thread(s), {payload_bytes} byte(s)"
            + (f" ({payload.note})" if payload.note else "")
        ),
        metadata={
            "kind": kind.value,
            "status": status.value,
            "record_count": len(exported),
            "payload_bytes": payload_bytes,
            "window_from": window_from.isoformat() if window_from else None,
            "note": payload.note,
            "manifest": manifest,
        },
        request=request,
    )

    store.last_backup_at = now
    store.updated_by = principal.actor
    await session.flush()

    return MemoryBackupResult(
        backup_id=event.id,
        store_id=store.id,
        name=store.name,
        kind=kind,
        status=status,
        record_count=len(exported),
        payload_bytes=payload_bytes,
        window_from=window_from,
        created_at=event.occurred_at,
        projects=len(projects),
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
    """Put a store back into the state one backup captured.

    The manifest holds the governed state — record count, usage, retention and
    status — so that is what comes back. Threads deleted from the telemetry store
    since the snapshot are not resurrected by this call, and the response says
    so rather than implying otherwise.
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
    record_count = _int_or_none(manifest.get("store_record_count"))
    if record_count is not None and record_count != store.record_count:
        store.record_count = record_count
        restored.append("record_count")
    usage = _int_or_none(manifest.get("store_usage_percent"))
    if usage is not None and usage != store.usage_percent:
        store.usage_percent = max(0, min(100, usage))
        restored.append("usage_percent")
    retention_days = _int_or_none(manifest.get("retention_days"))
    if retention_days is not None and retention_days != store.retention_days:
        store.retention_days = retention_days
        store.retention_policy = str(
            manifest.get("retention_policy") or retention_label(retention_days)
        )
        restored.append("retention_policy")
    status = manifest.get("store_status")
    if isinstance(status, str) and status and status != store.status:
        store.status = status
        restored.append("status")

    now = dt.datetime.now(dt.UTC)
    store.last_updated_at = now
    store.updated_by = principal.actor

    detail = f"Restored from backup {event.id} taken {event.occurred_at.isoformat()}"
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
            "backup_taken_at": event.occurred_at.isoformat(),
        },
        request=request,
    )
    await session.flush()
    await session.refresh(store)

    return MemoryRestoreResult(
        backup_id=event.id,
        store_id=store.id,
        name=store.name,
        restored_at=now,
        record_count=store.record_count,
        usage_percent=store.usage_percent,
        retention_policy=store.retention_policy,
        status=MemoryStoreStatus(store.status),
        fields_restored=restored,
    )
