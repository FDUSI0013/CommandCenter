"""Connection Center business logic.

Everything the console can do to a platform integration lives here: register it,
edit it, retire it, probe it, sync it, and read the KPI numbers off it. The
probe is a real network call — a short-timeout HTTP GET against the endpoint in
``config`` — so ``latency_ms`` is measured, never modelled, and the status the
tile shows is the status the last probe actually observed.

Two invariants hold in every function below:

* every statement filters on ``principal.workspace_id``, so a row belonging to
  another tenant is indistinguishable from a row that does not exist;
* every state change writes both a ``ConnectionActivity`` row (the operator-
  facing feed under the tile) and an audit row (the tamper-evident record).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx
from fastapi import Request
from sqlalchemy import Select, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed
from ..engine import EngineError, get_engine_client
from ..models.identity import Role
from ..models.registry import (
    Connection,
    ConnectionActivity,
    ConnectionStatus,
    HealthState,
    Platform,
)
from ..schemas.connections import (
    ENDPOINT_CONFIG_KEYS,
    REDACTED,
    ActivityStatus,
    BulkActionSummary,
    ConnectionCreate,
    ConnectionDataFlow,
    ConnectionHealthSlice,
    ConnectionsSummary,
    ConnectionSyncOutcome,
    ConnectionTestOutcome,
    ConnectionToolCall,
    ConnectionTraffic,
    ConnectionUpdate,
)
from . import audit
from . import metrics as telemetry

SOURCE_SCREEN: Final[str] = "Hosting & Deployment"
ENTITY_TYPE: Final[str] = "connection"

#: A probe is a health check, not a workload: it must fail fast enough that
#: "Test All Connections" over a few dozen tiles still feels instant.
PROBE_TIMEOUT_SECONDS: Final[float] = 5.0
PROBE_CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0
PROBE_USER_AGENT: Final[str] = "fulcrum-ops-control-plane/1.0 (connection-probe)"

SORTABLE: Final[dict[str, Any]] = {
    "name": Connection.name,
    "kind": Connection.kind,
    "status": Connection.status,
    "health": Connection.health,
    "last_sync_at": Connection.last_sync_at,
    "latency_ms": Connection.latency_ms,
    "linked_agent_count": Connection.linked_agent_count,
    "syncs_today": Connection.syncs_today,
    "enabled": Connection.enabled,
    "created_at": Connection.created_at,
    "updated_at": Connection.updated_at,
}

ACTIVITY_SORTABLE: Final[dict[str, Any]] = {
    "occurred_at": ConnectionActivity.occurred_at,
    "event": ConnectionActivity.event,
    "status": ConnectionActivity.status,
}


# ---------------------------------------------------------------------------
# Probe plumbing
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """The measured result of one HTTP round trip against an endpoint."""

    reachable: bool
    http_status: int | None
    latency_ms: int | None
    detail: str


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive instant as UTC; clients are not required to send an offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=PROBE_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=True,
        headers={"accept": "*/*", "user-agent": PROBE_USER_AGENT},
    )


def _endpoint_for(connection: Connection) -> str | None:
    """The first configured URL the probe can call, or None if there is none."""
    config = connection.config or {}
    for key in ENDPOINT_CONFIG_KEYS:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _probe(client: httpx.AsyncClient, url: str) -> ProbeResult:
    """GET the endpoint once and time it. Never raises."""
    started = time.perf_counter()
    try:
        response = await client.get(url)
    except httpx.TimeoutException:
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail=f"No response within {PROBE_TIMEOUT_SECONDS:.0f}s",
        )
    except httpx.TransportError as exc:
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail=f"Endpoint unreachable ({type(exc).__name__})",
        )

    latency_ms = int(round((time.perf_counter() - started) * 1000))
    return ProbeResult(
        reachable=True,
        http_status=response.status_code,
        latency_ms=latency_ms,
        detail=f"HTTP {response.status_code} in {latency_ms}ms",
    )


def _verdict(probe: ProbeResult) -> tuple[ConnectionStatus, HealthState, ActivityStatus]:
    """Translate one probe into the three vocabularies the console renders.

    A 4xx/5xx answer still proves the endpoint is up, so it degrades the tile to
    Warning rather than declaring it Disconnected — only a transport failure or
    a timeout means nobody is home.
    """
    if not probe.reachable:
        return ConnectionStatus.DISCONNECTED, HealthState.UNHEALTHY, ActivityStatus.ERROR
    status_code = probe.http_status or 0
    if status_code >= 400:
        return ConnectionStatus.WARNING, HealthState.WARNING, ActivityStatus.WARNING
    return ConnectionStatus.CONNECTED, HealthState.HEALTHY, ActivityStatus.SUCCESS


def _activity_row(
    connection: Connection,
    *,
    event: str,
    status: ActivityStatus,
    details: str,
    occurred_at: dt.datetime,
) -> ConnectionActivity:
    return ConnectionActivity(
        workspace_id=connection.workspace_id,
        connection_id=connection.id,
        occurred_at=occurred_at,
        event=event,
        status=status.value,
        details=details,
    )


def _record_probe(
    session: AsyncSession,
    connection: Connection,
    probe: ProbeResult,
    principal: Principal,
) -> ConnectionTestOutcome:
    """Apply a probe to the tile and append the activity row. No audit here.

    Testing deliberately does not touch ``last_sync_at``: a reachability check
    moved no data, and the Last Sync column has to keep meaning what it says.
    """
    was_connected = connection.status == ConnectionStatus.CONNECTED.value
    status, health, activity_status = _verdict(probe)

    connection.status = status.value
    connection.health = health.value
    connection.latency_ms = probe.latency_ms
    connection.note = None if activity_status is ActivityStatus.SUCCESS else probe.detail
    connection.updated_by = principal.actor

    if not probe.reachable:
        event = "Connection Failed"
    elif status is ConnectionStatus.CONNECTED and not was_connected:
        event = "Connection Restored"
    else:
        event = "Connection Test"

    session.add(
        _activity_row(
            connection,
            event=event,
            status=activity_status,
            details=probe.detail,
            occurred_at=dt.datetime.now(dt.UTC),
        )
    )

    return ConnectionTestOutcome(
        connection_id=connection.id,
        name=connection.name,
        reachable=probe.reachable,
        status=status,
        health=health,
        latency_ms=probe.latency_ms,
        http_status=probe.http_status,
        detail=probe.detail,
    )


def _record_sync(
    session: AsyncSession, connection: Connection, principal: Principal
) -> ConnectionSyncOutcome:
    """Stamp a completed sync onto the tile and append the activity row."""
    now = dt.datetime.now(dt.UTC)
    previous = connection.last_sync_at
    if previous is None or _as_utc(previous).date() != now.date():
        # The counter is "today", so it rolls over rather than growing forever.
        connection.syncs_today = 1
    else:
        connection.syncs_today += 1
    connection.last_sync_at = now
    connection.updated_by = principal.actor

    detail = (
        f"Synchronised at {now.strftime('%H:%M:%SZ')} "
        f"({connection.syncs_today} sync(s) today)"
    )
    session.add(
        _activity_row(
            connection,
            event="Sync Completed",
            status=ActivityStatus.SUCCESS,
            details=detail,
            occurred_at=now,
        )
    )

    return ConnectionSyncOutcome(
        connection_id=connection.id,
        name=connection.name,
        synced_at=now,
        syncs_today=connection.syncs_today,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(Connection).where(Connection.workspace_id == principal.workspace_id)


async def list_connections(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: ConnectionStatus | None = None,
    health: HealthState | None = None,
    kind: str | None = None,
    enabled: bool | None = None,
) -> tuple[Sequence[Connection], int]:
    """One page of the workspace's connections, filtered the way the tiles are."""
    stmt = _scoped(principal)
    stmt = apply_search(stmt, params, [Connection.name, Connection.kind, Connection.note])
    stmt = apply_filters(
        stmt,
        {
            Connection.status: status.value if status else None,
            Connection.health: health.value if health else None,
            Connection.kind: kind,
            Connection.enabled: enabled,
        },
    )
    stmt = apply_sort(stmt, params, SORTABLE, default=Connection.name, default_desc=False)
    return await paginate(session, stmt, params)


async def get_connection(
    session: AsyncSession, principal: Principal, connection_id: str
) -> Connection:
    """Load one connection, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as a row that never existed —
    a 403 would confirm the id is real.
    """
    connection = (
        await session.execute(_scoped(principal).where(Connection.id == connection_id))
    ).scalar_one_or_none()
    if connection is None:
        raise NotFound(f"Connection '{connection_id}' does not exist.")
    return connection


async def list_activity(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    connection_id: str | None = None,
    status: ActivityStatus | None = None,
    event: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The workspace-wide activity feed, newest first, with tile names attached.

    Names are resolved in a second statement over just the ids on the page, so a
    long feed never joins the whole connections table.
    """
    if connection_id is not None:
        # Prove the connection exists in this workspace before filtering on it,
        # so an id from another tenant 404s instead of returning an empty feed.
        await get_connection(session, principal, connection_id)

    stmt = select(ConnectionActivity).where(
        ConnectionActivity.workspace_id == principal.workspace_id
    )
    stmt = apply_search(stmt, params, [ConnectionActivity.event, ConnectionActivity.details])
    stmt = apply_filters(
        stmt,
        {
            ConnectionActivity.connection_id: connection_id,
            ConnectionActivity.status: status.value if status else None,
            ConnectionActivity.event: event,
        },
    )
    stmt = apply_sort(
        stmt, params, ACTIVITY_SORTABLE, default=ConnectionActivity.occurred_at, default_desc=True
    )

    rows, total = await paginate(session, stmt, params)
    if not rows:
        return [], total

    ids = {row.connection_id for row in rows}
    labels = {
        connection_id_: (name, logo_key)
        for connection_id_, name, logo_key in (
            await session.execute(
                select(Connection.id, Connection.name, Connection.logo_key).where(
                    Connection.workspace_id == principal.workspace_id,
                    Connection.id.in_(ids),
                )
            )
        ).all()
    }

    payload: list[dict[str, Any]] = []
    for row in rows:
        name, logo_key = labels.get(row.connection_id, (None, None))
        payload.append(
            {
                "id": row.id,
                "connection_id": row.connection_id,
                "connection_name": name,
                "connection_logo_key": logo_key,
                "occurred_at": row.occurred_at,
                "event": row.event,
                "status": row.status,
                "details": row.details,
            }
        )
    return payload, total


async def summarise(session: AsyncSession, principal: Principal) -> ConnectionsSummary:
    """KPI cards plus the health donut, computed entirely in SQL."""
    workspace = Connection.workspace_id == principal.workspace_id

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(Connection.id).label("total"),
                _count_where(
                    Connection.status == ConnectionStatus.CONNECTED.value
                ).label("connected"),
                _count_where(Connection.status == ConnectionStatus.WARNING.value).label(
                    "warning"
                ),
                _count_where(
                    Connection.status == ConnectionStatus.DISCONNECTED.value
                ).label("disconnected"),
                _count_where(Connection.enabled.is_(True)).label("enabled"),
                _count_where(Connection.enabled.is_(False)).label("disabled"),
                func.max(Connection.last_sync_at).label("last_sync_at"),
                func.avg(Connection.latency_ms).label("avg_latency_ms"),
                func.coalesce(func.sum(Connection.syncs_today), 0).label("syncs_today"),
                func.coalesce(func.sum(Connection.linked_agent_count), 0).label("agents"),
            ).where(workspace)
        )
    ).one()

    grouped = (
        await session.execute(
            select(Connection.health, func.count(Connection.id))
            .where(workspace)
            .group_by(Connection.health)
        )
    ).all()
    counts = {health: count for health, count in grouped}

    total = int(totals.total or 0)
    breakdown = [
        ConnectionHealthSlice(
            health=state,
            count=int(counts.get(state.value, 0)),
            percent=round(int(counts.get(state.value, 0)) / total * 100, 1) if total else 0.0,
        )
        for state in HealthState
    ]

    return ConnectionsSummary(
        total=total,
        connected=int(totals.connected or 0),
        warning=int(totals.warning or 0),
        disconnected=int(totals.disconnected or 0),
        enabled=int(totals.enabled or 0),
        disabled=int(totals.disabled or 0),
        last_sync_at=totals.last_sync_at,
        avg_latency_ms=(
            round(float(totals.avg_latency_ms), 1) if totals.avg_latency_ms is not None else None
        ),
        syncs_today=int(totals.syncs_today or 0),
        linked_agents=int(totals.agents or 0),
        health_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_connection(
    session: AsyncSession,
    principal: Principal,
    payload: ConnectionCreate,
    *,
    request: Request | None = None,
) -> Connection:
    """Register an integration. It starts Disconnected until a probe says otherwise."""
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(_scoped(principal).where(Connection.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A connection named '{payload.name}' already exists.")

    connection = Connection(
        workspace_id=principal.workspace_id,
        name=payload.name,
        kind=payload.kind,
        logo_key=payload.logo_key,
        status=ConnectionStatus.DISCONNECTED.value,
        health=HealthState.UNHEALTHY.value,
        metadata_pairs=[list(pair) for pair in payload.metadata_pairs],
        note=payload.note,
        config=dict(payload.config),
        credential_secret_id=payload.credential_secret_id,
        enabled=payload.enabled,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(connection)

    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(f"A connection named '{payload.name}' already exists.") from exc

    session.add(
        _activity_row(
            connection,
            event="Connection Added",
            status=ActivityStatus.SUCCESS,
            details=f"Registered as {connection.kind}; awaiting first test",
            occurred_at=dt.datetime.now(dt.UTC),
        )
    )
    await audit.record(
        session,
        principal=principal,
        action="connection.created",
        entity_type=ENTITY_TYPE,
        entity_id=connection.id,
        entity_label=connection.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Registered {connection.kind} connection",
        request=request,
    )
    await session.flush()
    # Server-side timestamp defaults land on the row, not on the instance;
    # refresh explicitly so the caller never triggers implicit IO while
    # serialising it.
    await session.refresh(connection)
    return connection


async def update_connection(
    session: AsyncSession,
    principal: Principal,
    connection_id: str,
    payload: ConnectionUpdate,
    *,
    request: Request | None = None,
) -> Connection:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.ADMIN)
    connection = await get_connection(session, principal, connection_id)

    if payload.expected_updated_at is not None:
        current = connection.updated_at
        expected = _as_utc(payload.expected_updated_at)
        # One second of slack: clients round-trip the timestamp through JSON.
        if current is not None and abs((_as_utc(current) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{connection.name}' was changed by someone else. Reload and try again."
            )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return connection

    if "name" in changes and changes["name"] != connection.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    Connection.name == changes["name"], Connection.id != connection.id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A connection named '{changes['name']}' already exists.")

    if "metadata_pairs" in changes and changes["metadata_pairs"] is not None:
        changes["metadata_pairs"] = [list(pair) for pair in changes["metadata_pairs"]]
    for field in ("status", "health"):
        value = changes.get(field)
        if isinstance(value, (ConnectionStatus, HealthState)):
            changes[field] = value.value

    if isinstance(changes.get("config"), dict):
        # Reads redact credential-shaped values, so a client that round-trips
        # what it was shown would overwrite real material with the marker.
        # A marker value means "unchanged": keep what is stored.
        stored = dict(connection.config or {})
        changes["config"] = {
            key: (stored.get(key) if value == REDACTED else value)
            for key, value in changes["config"].items()
        }

    for field, value in changes.items():
        setattr(connection, field, value)
    connection.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A connection named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="connection.updated",
        entity_type=ENTITY_TYPE,
        entity_id=connection.id,
        entity_label=connection.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    # ``updated_at`` is a server-side onupdate: the UPDATE expired it rather
    # than refetching it, so read it back before the caller serialises the row.
    await session.refresh(connection)
    return connection


async def delete_connection(
    session: AsyncSession,
    principal: Principal,
    connection_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove an integration. Its activity feed goes with it; the audit trail stays."""
    principal.require(Role.ADMIN)
    connection = await get_connection(session, principal, connection_id)

    if connection.linked_agent_count > 0:
        raise PreconditionFailed(
            f"'{connection.name}' is still used by {connection.linked_agent_count} agent(s). "
            "Detach them before removing it."
        )

    label = connection.name
    kind = connection.kind
    await audit.record(
        session,
        principal=principal,
        action="connection.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=connection.id,
        entity_label=label,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed {kind} connection",
        request=request,
    )
    await session.delete(connection)
    await session.flush()


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


async def test_connection(
    session: AsyncSession,
    principal: Principal,
    connection_id: str,
    *,
    request: Request | None = None,
) -> ConnectionTestOutcome:
    """Probe the configured endpoint and flip the tile to what was measured."""
    principal.require(Role.OPERATOR)
    connection = await get_connection(session, principal, connection_id)

    if not connection.enabled:
        raise PreconditionFailed(f"'{connection.name}' is disabled. Enable it before testing.")

    endpoint = _endpoint_for(connection)
    if endpoint is None:
        raise PreconditionFailed(
            f"'{connection.name}' has no endpoint configured. "
            f"Set one of {', '.join(ENDPOINT_CONFIG_KEYS)} in its config first."
        )

    async with _probe_client() as client:
        probe = await _probe(client, endpoint)

    outcome = _record_probe(session, connection, probe, principal)
    await audit.record(
        session,
        principal=principal,
        action="connection.tested",
        entity_type=ENTITY_TYPE,
        entity_id=connection.id,
        entity_label=connection.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{outcome.status.value} - {outcome.detail}",
        metadata={"latency_ms": outcome.latency_ms, "http_status": outcome.http_status},
        request=request,
    )
    await session.flush()
    return outcome


async def sync_connection(
    session: AsyncSession,
    principal: Principal,
    connection_id: str,
    *,
    request: Request | None = None,
) -> ConnectionSyncOutcome:
    """Record a completed sync: bump the counter, stamp the time, log the feed row."""
    principal.require(Role.OPERATOR)
    connection = await get_connection(session, principal, connection_id)

    if not connection.enabled:
        raise PreconditionFailed(f"'{connection.name}' is disabled. Enable it before syncing.")
    if connection.status == ConnectionStatus.DISCONNECTED.value:
        raise PreconditionFailed(
            f"'{connection.name}' is disconnected. Test it successfully before syncing."
        )

    outcome = _record_sync(session, connection, principal)
    await audit.record(
        session,
        principal=principal,
        action="connection.synced",
        entity_type=ENTITY_TYPE,
        entity_id=connection.id,
        entity_label=connection.name,
        source_screen=SOURCE_SCREEN,
        detail=outcome.detail,
        request=request,
    )
    await session.flush()
    return outcome


async def test_all(
    session: AsyncSession, principal: Principal, *, request: Request | None = None
) -> tuple[list[ConnectionTestOutcome], BulkActionSummary]:
    """Probe every eligible connection at once.

    Disabled tiles and tiles with no endpoint are skipped rather than failed —
    they are a configuration state, not an outage. The probes run concurrently
    over one client; the database is touched only after they have all landed.
    """
    principal.require(Role.OPERATOR)
    connections = (
        (await session.execute(_scoped(principal).order_by(Connection.name.asc())))
        .scalars()
        .all()
    )

    summary = BulkActionSummary(requested=len(connections))
    targets: list[tuple[Connection, str]] = []
    for connection in connections:
        endpoint = _endpoint_for(connection)
        if not connection.enabled or endpoint is None:
            summary.skipped += 1
            continue
        targets.append((connection, endpoint))

    outcomes: list[ConnectionTestOutcome] = []
    if targets:
        async with _probe_client() as client:
            probes = await asyncio.gather(*(_probe(client, url) for _, url in targets))
        for (connection, _), probe in zip(targets, probes, strict=True):
            outcome = _record_probe(session, connection, probe, principal)
            outcomes.append(outcome)
            if not outcome.reachable:
                summary.failed += 1
            elif outcome.health == HealthState.HEALTHY:
                summary.succeeded += 1
            else:
                summary.warned += 1

    await audit.record(
        session,
        principal=principal,
        action="connection.tested_all",
        entity_type=ENTITY_TYPE,
        entity_label=f"{summary.requested} connection(s)",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{summary.succeeded} healthy, {summary.warned} warning, "
            f"{summary.failed} unreachable, {summary.skipped} skipped"
        ),
        metadata=summary.model_dump(),
        request=request,
    )
    await session.flush()
    return outcomes, summary


async def sync_all(
    session: AsyncSession, principal: Principal, *, request: Request | None = None
) -> tuple[list[ConnectionSyncOutcome], BulkActionSummary]:
    """Sync every connected tile. Disconnected and disabled tiles are skipped."""
    principal.require(Role.OPERATOR)
    connections = (
        (await session.execute(_scoped(principal).order_by(Connection.name.asc())))
        .scalars()
        .all()
    )

    summary = BulkActionSummary(requested=len(connections))
    outcomes: list[ConnectionSyncOutcome] = []
    for connection in connections:
        if not connection.enabled or connection.status == ConnectionStatus.DISCONNECTED.value:
            summary.skipped += 1
            continue
        outcomes.append(_record_sync(session, connection, principal))
        summary.succeeded += 1

    await audit.record(
        session,
        principal=principal,
        action="connection.synced_all",
        entity_type=ENTITY_TYPE,
        entity_label=f"{summary.requested} connection(s)",
        source_screen=SOURCE_SCREEN,
        detail=f"{summary.succeeded} synchronised, {summary.skipped} skipped",
        metadata=summary.model_dump(),
        request=request,
    )
    await session.flush()
    return outcomes, summary

# ---------------------------------------------------------------------------
# Observed traffic
# ---------------------------------------------------------------------------

#: Spans to scan for one traffic view. A busy workspace is capped rather than
#: paged forever; the response says when the cap was hit so the totals are read
#: as a floor rather than a count.
MAX_TRAFFIC_SPANS: Final[int] = 2000
TRAFFIC_PAGE_SIZE: Final[int] = 200
DEFAULT_TRAFFIC_WINDOW_DAYS: Final[int] = 30


def _span_instant(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


async def connection_traffic(
    session: AsyncSession,
    principal: Principal,
    connection_id: str,
    *,
    window_days: int = DEFAULT_TRAFFIC_WINDOW_DAYS,
) -> ConnectionTraffic:
    """What the agents on this connection's platform actually did.

    A connection used to answer one question — is the endpoint reachable. That
    says nothing about whether anything is flowing through it. This reads the
    tool and retrieval spans of every agent whose platform matches the
    connection's kind, so the screen can show the calls being made rather than
    only the plumbing being present.

    The link is ``Platform``: an agent declares where it runs, a connection
    declares what it connects to, and the enum is shared between them. Agents on
    another platform are never read, so this inherits the same tenancy scoping
    as every other telemetry path.
    """
    connection = await get_connection(session, principal, connection_id)

    # Connection kinds are a superset of agent platforms: an agent runs *on*
    # Azure AI Foundry, but nothing runs "on" a Vector Database. For a kind with
    # no platform equivalent the honest answer is that traffic cannot be
    # attributed here at all, which is different from nothing having happened.
    attributable = connection.kind in {platform.value for platform in Platform}
    projects = (
        [
            project
            for project in await telemetry.workspace_projects(session, principal)
            if project.platform == connection.kind
        ]
        if attributable
        else []
    )
    window = telemetry.resolve_window_days(window_days)

    empty = ConnectionTraffic(
        connection_id=connection.id,
        name=connection.name,
        kind=connection.kind,
        window_days=window_days,
        agents=len(projects),
        runs=0,
        attributable=attributable,
        note=(
            None
            if attributable
            else (
                f"A '{connection.kind}' connection is not a platform agents run on, so runs "
                "cannot be attributed to it. Tools reached through it are governed on "
                "Connector & MCP Governance instead."
            )
        ),
    )
    if not projects:
        return empty

    client = get_engine_client()
    tools: dict[str, dict[str, Any]] = {}
    data: dict[str, dict[str, Any]] = {}
    scanned = 0
    truncated = False

    for project in projects:
        page = 1
        while scanned < MAX_TRAFFIC_SPANS:
            try:
                payload = await client.list_spans(
                    project_id=project.project_id,
                    page=page,
                    size=TRAFFIC_PAGE_SIZE,
                    from_time=window.start,
                    to_time=window.end,
                )
            except EngineError as exc:
                raise telemetry.telemetry_unavailable(exc) from exc
            rows = telemetry._records(payload)
            if not rows:
                break
            for row in rows:
                scanned += 1
                kind = str(row.get("type") or "").lower()
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                duration = row.get("duration")
                moment = _span_instant(row.get("start_time"))
                failed = bool(row.get("error_info"))

                if kind == "tool":
                    slot = tools.setdefault(
                        name, {"calls": 0, "errors": 0, "ms": 0.0, "timed": 0, "last": None}
                    )
                elif kind in {"retrieval", "general"} and _looks_like_data(name):
                    slot = data.setdefault(
                        name, {"calls": 0, "docs": 0, "ms": 0.0, "timed": 0, "last": None}
                    )
                else:
                    continue

                slot["calls"] += 1
                if failed:
                    slot["errors"] = slot.get("errors", 0) + 1
                if isinstance(duration, (int, float)):
                    slot["ms"] += float(duration)
                    slot["timed"] += 1
                if moment and (slot["last"] is None or moment > slot["last"]):
                    slot["last"] = moment
            if len(rows) < TRAFFIC_PAGE_SIZE:
                break
            page += 1
        if scanned >= MAX_TRAFFIC_SPANS:
            truncated = True
            break

    rollups = await telemetry.project_rollups(client, projects, window.start, window.end)

    def mean(slot: dict[str, Any]) -> float | None:
        return round(slot["ms"] / slot["timed"], 1) if slot["timed"] else None

    tool_rows = [
        ConnectionToolCall(
            name=name,
            calls=slot["calls"],
            errors=slot.get("errors", 0),
            error_rate=(
                round(slot.get("errors", 0) / slot["calls"] * 100, 1) if slot["calls"] else None
            ),
            avg_duration_ms=mean(slot),
            last_called_at=slot["last"],
        )
        for name, slot in tools.items()
    ]
    tool_rows.sort(key=lambda row: -row.calls)

    data_rows = [
        ConnectionDataFlow(
            name=name,
            operations=slot["calls"],
            documents=slot["docs"] or None,
            avg_duration_ms=mean(slot),
            last_seen_at=slot["last"],
        )
        for name, slot in data.items()
    ]
    data_rows.sort(key=lambda row: -row.operations)

    return ConnectionTraffic(
        connection_id=connection.id,
        name=connection.name,
        kind=connection.kind,
        window_days=window_days,
        agents=len(projects),
        runs=sum(rollup.runs for rollup in rollups),
        tool_calls=tool_rows,
        data_flows=data_rows,
        truncated=truncated,
        attributable=True,
    )


#: Span names that mean "this touched a knowledge source" rather than "this
#: called a tool". Matched on the name because the engine has no separate
#: retrieval span type.
_DATA_MARKERS: Final[tuple[str, ...]] = (
    "retriev",
    "search",
    "vector",
    "index",
    "lookup",
    "query",
    "fetch",
    "embed",
)


def _looks_like_data(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _DATA_MARKERS)
