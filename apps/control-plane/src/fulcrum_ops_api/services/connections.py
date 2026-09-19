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
import ipaddress
import re
import socket
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx
from fastapi import Request
from sqlalchemy import Select, case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed
from ..core.ttlcache import SingleFlightCache
from ..db.base import stamp
from ..engine import EngineError, get_engine_client
from ..models.identity import Role
from ..models.operations import AlertSeverity
from ..models.registry import (
    Agent,
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
    is_link_local_host,
)
from . import alerts, audit
from . import metrics as telemetry

SOURCE_SCREEN: Final[str] = "Hosting & Deployment"
ENTITY_TYPE: Final[str] = "connection"

#: A probe is a health check, not a workload: it must fail fast enough that
#: "Test All Connections" over a few dozen tiles still feels instant.
#: Bounds the whole probe -- connect, redirects and the wait for the status line
#: together -- not just each read. Not ``Final``: tests shorten it.
PROBE_TIMEOUT_SECONDS: float = 5.0
PROBE_CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0
PROBE_MAX_REDIRECTS: Final[int] = 3
PROBE_USER_AGENT: Final[str] = "fulcrum-ops-control-plane/1.0 (connection-probe)"

#: Exactly the diagnostics the probe used to write over the operator's note.
#: A note that is nothing but one of these was never the operator's, so the
#: next probe clears it; anything else is theirs and is left alone.
_PROBE_WRITTEN_NOTE: Final[re.Pattern[str]] = re.compile(
    r"HTTP \d{3} in \d+ms|No response within \d+s|Endpoint unreachable \(\w+\)"
)

#: Feed events whose details are what a probe saw. The newest of them on a tile
#: is that tile's standing diagnostic (``status_detail``).
_PROBE_EVENTS: Final[tuple[str, ...]] = (
    "Connection Test",
    "Connection Failed",
    "Connection Restored",
    "Sync Completed",
    "Sync Failed",
)

#: Agents registered on a connection's platform. There is no agent-to-connection
#: foreign key -- ``Agent.platform == Connection.kind`` *is* the link, the same one
#: the traffic view reads through -- so the number is counted where it is asked
#: for. The ``linked_agent_count`` column was meant to hold it and nothing ever
#: wrote it: every tile said "Agents Using 0" and the delete guard never fired.
_LINKED_AGENTS: Final[Any] = (
    select(func.count(Agent.id))
    .where(Agent.workspace_id == Connection.workspace_id, Agent.platform == Connection.kind)
    .correlate(Connection)
    .scalar_subquery()
)

SORTABLE: Final[dict[str, Any]] = {
    "name": Connection.name,
    "kind": Connection.kind,
    "status": Connection.status,
    "health": Connection.health,
    "last_sync_at": Connection.last_sync_at,
    "latency_ms": Connection.latency_ms,
    "linked_agent_count": _LINKED_AGENTS,
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


class _RefusedTarget(Exception):
    """The probe was pointed, directly or by a redirect, somewhere it will not go."""


def _is_address_literal(host: str | None) -> bool:
    """True when ``host`` is already a number, so there is nothing to look up."""
    try:
        ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return True


async def _resolve(host: str, port: int) -> list[str]:
    """The addresses ``host`` answers with, or none when it does not resolve.

    Its own function because it is the one thing here a test cannot arrange:
    no name that can be relied on to answer with a link-local address exists to
    point a test at, and asking the real DNS for one would be a call out of the
    suite. A name that does not resolve is not this check's business -- the
    transport is about to fail on it and will say so in the operator's words.
    """
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    # sockaddr[0] may carry a zone ("fe80::1%eth0"); the address is the part before it.
    return [str(info[4][0]).partition("%")[0] for info in found]


async def _refuse_link_local(request: httpx.Request) -> None:
    # Runs for every hop, so an outside URL that redirects to the instance
    # metadata address is refused just as the address itself is on write.
    host = request.url.host
    if is_link_local_host(host):
        raise _RefusedTarget(host)
    if _is_address_literal(host):
        return
    # A *name* is the same address with a DNS record in front of it. The check
    # above only parses, so "metadata.google.internal" -- or any name an
    # operator can point at 169.254.169.254 -- walked straight through the one
    # thing this probe refuses. Only the metadata range is looked for: a name
    # for a host on the same network stays an ordinary thing to connect, as a
    # private address literal is.
    #
    # This closes the name. It does not close a resolver that answers with a
    # public address here and the metadata address on the second lookup the
    # transport makes: pinning the connection to the address vetted here is
    # what would, and that costs the redirect and virtual-host handling this
    # probe gets from the library. Deliberately not paid for yet.
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    # The ASCII form: what the transport is about to ask DNS for. Asking for the
    # unicode one would be asking about a different name.
    asked = request.url.raw_host.decode("ascii")
    if any(is_link_local_host(found) for found in await _resolve(asked, port)):
        raise _RefusedTarget(host)


def _probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        event_hooks={"request": [_refuse_link_local]},
        timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=PROBE_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=True,
        # A health endpoint that bounces more than a few times is misconfigured,
        # and the library's default of twenty hops is twenty sets of timeouts.
        max_redirects=PROBE_MAX_REDIRECTS,
        # An MCP server answers 406 to a caller that will not take an event
        # stream, which would read as a Warning on a healthy tile.
        headers={
            "accept": "application/json, text/event-stream, */*",
            "user-agent": PROBE_USER_AGENT,
        },
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
    """GET the endpoint once and time it. Never raises.

    The answer is the status line: the probe is over the moment the headers
    arrive, and the body is never read. Reading it is what made a healthy
    endpoint that *streams* -- an MCP server's event stream, a large download --
    either time out between keep-alives and show as Disconnected, or never
    finish at all, because the library's timeouts bound each read rather than
    the request. The deadline around the whole exchange is what bounds it:
    redirects, a trickle of header bytes and all.
    """
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with client.stream("GET", url) as response:
                status_code = response.status_code
                latency_ms = int(round((time.perf_counter() - started) * 1000))
    # The builtin is the deadline above; the library's own timeouts are not
    # subclasses of it.
    except (TimeoutError, httpx.TimeoutException):
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail=f"No response within {PROBE_TIMEOUT_SECONDS:.0f}s",
        )
    except _RefusedTarget:
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail="Endpoint is, or redirects to, a link-local (instance metadata) address",
        )
    except httpx.TooManyRedirects:
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail=f"Endpoint redirected more than {PROBE_MAX_REDIRECTS} times",
        )
    except httpx.InvalidURL:
        # Not an ``httpx.HTTPError``, so no arm here saw it: a malformed endpoint
        # was an opaque 500 on Test, and inside a gather it took Test All and
        # Sync All down for every other tile too. Rows saved before the schema
        # began parsing the URL the way the probe does are still in the table,
        # so this has to be an answer rather than an error.
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail="Endpoint is not a valid URL",
        )
    except httpx.TransportError as exc:
        return ProbeResult(
            reachable=False,
            http_status=None,
            latency_ms=None,
            detail=f"Endpoint unreachable ({type(exc).__name__})",
        )

    return ProbeResult(
        reachable=True,
        http_status=status_code,
        latency_ms=latency_ms,
        detail=f"HTTP {status_code} in {latency_ms}ms",
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


#: What a tile *entering* each status is worth raising. Connected is absent:
#: coming back up is good news, and the alert it answers is closed by whoever
#: triages it, not silently by the next probe.
_ALERT_SEVERITY: Final[dict[ConnectionStatus, AlertSeverity]] = {
    ConnectionStatus.DISCONNECTED: AlertSeverity.CRITICAL,
    ConnectionStatus.WARNING: AlertSeverity.MEDIUM,
}


async def _alert_on_status_change(
    session: AsyncSession,
    connection: Connection,
    *,
    was: str,
    status: ConnectionStatus,
    detail: str,
) -> None:
    """Raise the alert for a tile that has just *entered* Disconnected or Warning.

    Nobody sits watching the Connection Center. An integration falls over
    between two looks at it and the only trace is a feed row under a tile that
    nobody has open, so the screen that noticed says so on the alert board, the
    way Deployments and Quota do.

    Only the transition is news. A tile that was already down and is tested
    again has not changed: the dedupe key would collapse that raise while the
    alert is live, but not once a triager has resolved it, and an alert that
    reopens itself on every Test All is how an alert board stops being read.
    """
    severity = _ALERT_SEVERITY.get(status)
    if severity is None or was == status.value:
        return
    down = status is ConnectionStatus.DISCONNECTED
    await alerts.raise_alert(
        session,
        workspace_id=connection.workspace_id,
        title=(
            f"Connection down: {connection.name}"
            if down
            else f"Connection degraded: {connection.name}"
        ),
        description=(
            f"The {connection.kind} integration '{connection.name}' went from {was} to "
            f"{status.value}: {detail}."
        ),
        source=SOURCE_SCREEN,
        severity=severity,
        dedupe_key=f"connection:{connection.id}:{status.value}",
        source_entity_type=ENTITY_TYPE,
        source_entity_id=connection.id,
        metadata={"kind": connection.kind, "from": was, "to": status.value, "detail": detail},
    )


#: The three vocabularies one test result is rendered in.
_Verdict = tuple[ConnectionStatus, HealthState, ActivityStatus]

#: Kinds agents run *on*. A tile of one of these may honestly have no endpoint:
#: SDK agents push their telemetry, so there is no URL to call.
_PLATFORM_KINDS: Final[frozenset[str]] = frozenset(platform.value for platform in Platform)


async def _platform_agents(
    session: AsyncSession, principal: Principal, kinds: set[str]
) -> dict[str, tuple[int, int]]:
    """(registered, reporting) agents per platform, in one grouped statement.

    "Reporting" is having an engine project, the place an agent's telemetry
    lands: an agent without one cannot be sending anything, and it is the same
    test the traffic view applies before it reads a platform's spans. COUNT over
    the column skips the NULLs.
    """
    if not kinds:
        return {}
    return {
        platform: (int(registered), int(reporting))
        for platform, registered, reporting in (
            await session.execute(
                select(
                    Agent.platform,
                    func.count(Agent.id),
                    func.count(Agent.engine_project_id),
                )
                .where(Agent.workspace_id == principal.workspace_id, Agent.platform.in_(kinds))
                .group_by(Agent.platform)
            )
        ).all()
    }


def _registry_reading(kind: str, registered: int, reporting: int) -> tuple[ProbeResult, _Verdict]:
    """The test result of a platform tile that has no endpoint to call.

    Such a tile used to be a dead end: Test answered 412, so it stayed at the
    Disconnected it was created with, so Sync answered 412 as well -- red in the
    KPI cards while its agents' runs were listed under Tool Calls & Data. What
    can be known about it is in the registry, so that is where it is read from.
    Nothing was timed, so there is no latency: unmeasured, not zero.
    """
    if reporting:
        waiting = registered - reporting
        detail = f"{reporting} agent(s) reporting via SDK ingest" + (
            f"; {waiting} more registered with no telemetry project yet" if waiting else ""
        )
        verdict = (ConnectionStatus.CONNECTED, HealthState.HEALTHY, ActivityStatus.SUCCESS)
    elif registered:
        detail = (
            f"0 agent(s) reporting via SDK ingest; {registered} registered on {kind} "
            "have no telemetry project yet"
        )
        verdict = (ConnectionStatus.WARNING, HealthState.WARNING, ActivityStatus.WARNING)
    else:
        detail = f"0 agent(s) reporting via SDK ingest; no agent is registered on {kind}"
        # Nothing was tried and failed, so the feed row is a warning, not an error.
        verdict = (ConnectionStatus.DISCONNECTED, HealthState.UNHEALTHY, ActivityStatus.WARNING)
    return (
        ProbeResult(reachable=registered > 0, http_status=None, latency_ms=None, detail=detail),
        verdict,
    )


async def _record_probe(
    session: AsyncSession,
    connection: Connection,
    probe: ProbeResult,
    principal: Principal,
    *,
    verdict: _Verdict | None = None,
) -> ConnectionTestOutcome:
    """Apply a probe to the tile and append the activity row. No audit here.

    ``verdict`` is given when the result was read from the registry rather than
    off the network (:func:`_registry_reading`); otherwise the probe decides it.

    Testing deliberately does not touch ``last_sync_at``: a reachability check
    moved no data, and the Last Sync column has to keep meaning what it says.

    Nor does it touch ``note``. That column is the operator's free text -- the
    NOTE box in Configure, a search column on the grid -- and the probe used to
    write its diagnostic there: a healthy test erased "rotate the key before
    October" and a failing one replaced it with "HTTP 503 in 120ms". What the
    probe saw is on the activity row below, and a tile reads it back from there
    as ``status_detail``. The one exception is a note the old probe itself left
    behind, which is cleared.
    """
    was = connection.status
    was_connected = was == ConnectionStatus.CONNECTED.value
    status, health, activity_status = verdict or _verdict(probe)

    connection.status = status.value
    connection.health = health.value
    connection.latency_ms = probe.latency_ms
    if connection.note and _PROBE_WRITTEN_NOTE.fullmatch(connection.note.strip()):
        connection.note = None
    connection.updated_by = principal.actor

    # A platform nobody has registered an agent on was not called and did not
    # fail to answer: read from the registry, that is a test result like any other.
    if not probe.reachable and verdict is None:
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
    await _alert_on_status_change(
        session, connection, was=was, status=status, detail=probe.detail
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


def _start_of_today(now: dt.datetime) -> dt.datetime:
    """Midnight UTC: where "syncs today" starts counting from."""
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _record_sync(
    session: AsyncSession, connection: Connection, probe: ProbeResult | None
) -> ConnectionSyncOutcome:
    """Record what one sync found, on the tile and in the feed.

    A sync used to bump a counter and report success having looked at nothing.
    It now reconciles the two things this service can actually observe about an
    integration -- how many agents run on its platform, and whether its endpoint
    still answers (``probe``; None when no endpoint is configured) -- and the
    feed row carries those numbers. An endpoint that does not answer means no
    sync happened: the tile goes Disconnected, as a failed test would make it,
    and neither Last Sync nor the counter moves.
    """
    now = dt.datetime.now(dt.UTC)
    agents = connection.linked_agent_count
    counted = f"{agents} agent(s) on this platform"
    was = connection.status

    if probe is None:
        activity_status = ActivityStatus.SUCCESS
        detail = f"{counted}. No endpoint configured; recorded agent count only"
    else:
        status, health, activity_status = _verdict(probe)
        # What the probe measured is an observation about the tile, like the
        # counter below, and the latency differs on nearly every sync. Set on
        # the instance it would ride an ordinary UPDATE, ``onupdate`` would
        # fire, and Configure left open across a Sync Now would answer 409.
        await stamp(
            session,
            [connection],
            status=status.value,
            health=health.value,
            latency_ms=probe.latency_ms,
        )
        detail = f"{counted}, {probe.detail}"
        # A sync is the other way a tile changes status, and it is the one that
        # runs without anybody having asked to see the tile.
        await _alert_on_status_change(
            session, connection, was=was, status=status, detail=probe.detail
        )

    synced = probe is None or probe.reachable
    syncs_today = connection.syncs_today
    if synced:
        # Rollover and increment in one statement. Read-modify-write in Python
        # lost a sync whenever two workers synced the same tile together, and a
        # rollover decided outside the statement races just the same. This is
        # bookkeeping, not an edit, so ``updated_at`` -- the token Configure
        # sends back -- is named, to itself, which keeps ``onupdate`` from firing.
        syncs_today = (
            await session.execute(
                update(Connection)
                .where(Connection.id == connection.id)
                .values(
                    syncs_today=case(
                        (
                            Connection.last_sync_at >= _start_of_today(now),
                            Connection.syncs_today + 1,
                        ),
                        else_=1,
                    ),
                    last_sync_at=now,
                    updated_at=Connection.updated_at,
                )
                .returning(Connection.syncs_today)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one()
        set_committed_value(connection, "syncs_today", syncs_today)
        set_committed_value(connection, "last_sync_at", now)

    session.add(
        _activity_row(
            connection,
            event="Sync Completed" if synced else "Sync Failed",
            status=activity_status,
            details=detail,
            occurred_at=now,
        )
    )

    return ConnectionSyncOutcome(
        connection_id=connection.id,
        name=connection.name,
        synced=synced,
        status=activity_status,
        synced_at=now if synced else None,
        syncs_today=syncs_today,
        linked_agent_count=agents,
        reachable=None if probe is None else probe.reachable,
        http_status=None if probe is None else probe.http_status,
        latency_ms=None if probe is None else probe.latency_ms,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(Connection).where(Connection.workspace_id == principal.workspace_id)


async def _attach_linked_agents(
    session: AsyncSession, principal: Principal, connections: Sequence[Connection]
) -> None:
    """Put the derived agent count on rows that are about to be read or guarded.

    One grouped statement for the whole page. The value is set as *committed*
    state, so serialising a tile never queues an UPDATE of the unused column --
    a GET must not write. A refresh reloads the stored zero, so callers that
    refresh attach again afterwards.
    """
    kinds = {connection.kind for connection in connections}
    if not kinds:
        return
    counts = {
        platform: int(count)
        for platform, count in (
            await session.execute(
                select(Agent.platform, func.count(Agent.id))
                .where(Agent.workspace_id == principal.workspace_id, Agent.platform.in_(kinds))
                .group_by(Agent.platform)
            )
        ).all()
    }
    for connection in connections:
        set_committed_value(connection, "linked_agent_count", counts.get(connection.kind, 0))


async def _attach_status_detail(
    session: AsyncSession, principal: Principal, connections: Sequence[Connection]
) -> None:
    """Put what the last probe saw on rows about to be read, unless it was clean.

    The probe used to leave this in ``note``, over whatever the operator had
    written there. It has no column of its own, and the feed already holds it:
    the newest probe-bearing row of each tile, found through the
    (connection, occurred_at) index in one statement for the whole page. It is a
    plain attribute on the instance, never persistent state, so it cannot be
    written back.
    """
    ids = [connection.id for connection in connections]
    if not ids:
        return
    probed = (
        ConnectionActivity.workspace_id == principal.workspace_id,
        ConnectionActivity.connection_id.in_(ids),
        ConnectionActivity.event.in_(_PROBE_EVENTS),
    )
    newest = (
        select(
            ConnectionActivity.connection_id.label("connection_id"),
            func.max(ConnectionActivity.occurred_at).label("occurred_at"),
        )
        .where(*probed)
        .group_by(ConnectionActivity.connection_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ConnectionActivity.connection_id,
                ConnectionActivity.status,
                ConnectionActivity.details,
            )
            .join(
                newest,
                (ConnectionActivity.connection_id == newest.c.connection_id)
                & (ConnectionActivity.occurred_at == newest.c.occurred_at),
            )
            .where(*probed)
        )
    ).all()
    seen = {
        connection_id: details
        for connection_id, status, details in rows
        if status != ActivityStatus.SUCCESS.value
    }
    for connection in connections:
        connection.status_detail = seen.get(connection.id)


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
    rows, total = await paginate(session, stmt, params)
    await _attach_linked_agents(session, principal, rows)
    await _attach_status_detail(session, principal, rows)
    return rows, total


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
    await _attach_linked_agents(session, principal, [connection])
    await _attach_status_detail(session, principal, [connection])
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
                # The counter is only reset by the next sync, so on a day nobody
                # has synced it still holds the last day's total. A tile counts
                # towards "today" only if it was last synced today.
                func.coalesce(
                    func.sum(
                        case(
                            (
                                Connection.last_sync_at
                                >= _start_of_today(dt.datetime.now(dt.UTC)),
                                Connection.syncs_today,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("syncs_today"),
            ).where(workspace)
        )
    ).one()

    # Agents, not tile-counts added up: two connections of one kind share their
    # agents, and a sum would count each of those agents twice.
    linked_agents = (
        await session.execute(
            select(func.count(Agent.id)).where(
                Agent.workspace_id == principal.workspace_id,
                Agent.platform.in_(select(Connection.kind).where(workspace)),
            )
        )
    ).scalar_one()

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
        linked_agents=int(linked_agents or 0),
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
    await _attach_linked_agents(session, principal, [connection])
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
    await _attach_linked_agents(session, principal, [connection])
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

    # Agents are linked to a *kind*, not to a tile, so what must not happen is
    # the last tile of a kind going while agents still run on that platform --
    # their traffic would have nowhere to be read. A duplicate can always go.
    # (The old wording said "detach them", and there has never been a detach.)
    if connection.linked_agent_count > 0:
        siblings = (
            await session.execute(
                select(func.count(Connection.id)).where(
                    Connection.workspace_id == principal.workspace_id,
                    Connection.kind == connection.kind,
                    Connection.id != connection.id,
                )
            )
        ).scalar_one()
        if not siblings:
            raise PreconditionFailed(
                f"{connection.linked_agent_count} agent(s) still run on {connection.kind}. "
                f"Move or retire them, or keep one {connection.kind} connection."
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
    """Probe the configured endpoint and flip the tile to what was measured.

    An agent platform saved without an endpoint has nothing to probe and is read
    from the registry instead (:func:`_registry_reading`); any other kind
    without one is refused.
    """
    principal.require(Role.OPERATOR)
    connection = await get_connection(session, principal, connection_id)

    if not connection.enabled:
        raise PreconditionFailed(f"'{connection.name}' is disabled. Enable it before testing.")

    endpoint = _endpoint_for(connection)
    if endpoint is None and connection.kind not in _PLATFORM_KINDS:
        # Any other kind is only ever validated by calling it.
        raise PreconditionFailed(
            f"'{connection.name}' has no endpoint configured. "
            f"Set one of {', '.join(ENDPOINT_CONFIG_KEYS)} in its config first."
        )

    if endpoint is None:
        counted = await _platform_agents(session, principal, {connection.kind})
        probe, verdict = _registry_reading(connection.kind, *counted.get(connection.kind, (0, 0)))
        outcome = await _record_probe(session, connection, probe, principal, verdict=verdict)
    else:
        # The probe is a network wait of up to PROBE_TIMEOUT_SECONDS. End the read
        # transaction first so the pooled connection is not held, idle, across it;
        # the loaded row stays usable and the writes below open a new transaction.
        await session.commit()
        async with _probe_client() as client:
            probe = await _probe(client, endpoint)
        outcome = await _record_probe(session, connection, probe, principal)

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
    """Reconcile one tile: count its platform's agents, re-probe it, record both."""
    principal.require(Role.OPERATOR)
    connection = await get_connection(session, principal, connection_id)

    if not connection.enabled:
        raise PreconditionFailed(f"'{connection.name}' is disabled. Enable it before syncing.")
    if connection.status == ConnectionStatus.DISCONNECTED.value:
        raise PreconditionFailed(
            f"'{connection.name}' is disconnected. Test it successfully before syncing."
        )

    probe: ProbeResult | None = None
    endpoint = _endpoint_for(connection)
    if endpoint is not None:
        # As in test_connection: no pooled connection is held across the probe.
        await session.commit()
        async with _probe_client() as client:
            probe = await _probe(client, endpoint)

    outcome = await _record_sync(session, connection, probe)
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
    The exception is an agent platform with no endpoint, which is not skipped:
    its result is read from the registry (:func:`_registry_reading`).
    """
    principal.require(Role.OPERATOR)
    connections = (
        (await session.execute(_scoped(principal).order_by(Connection.name.asc())))
        .scalars()
        .all()
    )

    summary = BulkActionSummary(requested=len(connections))
    targets: list[tuple[Connection, str]] = []
    pushed: list[Connection] = []
    for connection in connections:
        endpoint = _endpoint_for(connection)
        if connection.enabled and endpoint is None and connection.kind in _PLATFORM_KINDS:
            # Read from the registry, as test_connection reads it: not skipped.
            pushed.append(connection)
            continue
        if not connection.enabled or endpoint is None:
            summary.skipped += 1
            continue
        targets.append((connection, endpoint))

    # One grouped count for all of them, and taken before the probes: it belongs
    # to the read transaction that is ended below.
    counted = await _platform_agents(session, principal, {tile.kind for tile in pushed})

    recorded: dict[str, ConnectionTestOutcome] = {}
    if targets:
        # As in test_connection: no pooled connection is held across the probes.
        await session.commit()
        async with _probe_client() as client:
            probes = await asyncio.gather(*(_probe(client, url) for _, url in targets))
        for (connection, _), probe in zip(targets, probes, strict=True):
            recorded[connection.id] = await _record_probe(
                session, connection, probe, principal
            )
    for connection in pushed:
        probe, verdict = _registry_reading(connection.kind, *counted.get(connection.kind, (0, 0)))
        recorded[connection.id] = await _record_probe(
            session, connection, probe, principal, verdict=verdict
        )

    # In the order the tiles were read, whichever way each was tested.
    outcomes = [recorded[tile.id] for tile in connections if tile.id in recorded]
    for outcome in outcomes:
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
    """Sync every connected tile. Disconnected and disabled tiles are skipped.

    The endpoints are probed together over one client, exactly as ``test_all``
    probes them: one after another would be up to a probe timeout per tile.
    """
    principal.require(Role.OPERATOR)
    connections = (
        (await session.execute(_scoped(principal).order_by(Connection.name.asc())))
        .scalars()
        .all()
    )
    await _attach_linked_agents(session, principal, connections)

    summary = BulkActionSummary(requested=len(connections))
    targets: list[Connection] = []
    for connection in connections:
        if not connection.enabled or connection.status == ConnectionStatus.DISCONNECTED.value:
            summary.skipped += 1
            continue
        targets.append(connection)

    probes: dict[str, ProbeResult] = {}
    probed = [
        (connection, endpoint)
        for connection in targets
        if (endpoint := _endpoint_for(connection)) is not None
    ]
    if probed:
        await session.commit()  # no pooled connection is held across the probes
        async with _probe_client() as client:
            results = await asyncio.gather(*(_probe(client, url) for _, url in probed))
        probes = {
            connection.id: result
            for (connection, _), result in zip(probed, results, strict=True)
        }

    outcomes: list[ConnectionSyncOutcome] = []
    for connection in targets:
        outcome = await _record_sync(session, connection, probes.get(connection.id))
        outcomes.append(outcome)
        if not outcome.synced:
            summary.failed += 1
        elif outcome.status is ActivityStatus.SUCCESS:
            summary.succeeded += 1
        else:
            summary.warned += 1

    await audit.record(
        session,
        principal=principal,
        action="connection.synced_all",
        entity_type=ENTITY_TYPE,
        entity_label=f"{summary.requested} connection(s)",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{summary.succeeded} synchronised, {summary.warned} with a warning, "
            f"{summary.failed} unreachable, {summary.skipped} skipped"
        ),
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
#: as a floor rather than a count. The cap is shared out between the platform's
#: agents -- never less than one page each -- because projects are read in name
#: order and one shared counter let the alphabetically first agent spend it all.
MAX_TRAFFIC_SPANS: Final[int] = 2000
TRAFFIC_PAGE_SIZE: Final[int] = 200
DEFAULT_TRAFFIC_WINDOW_DAYS: Final[int] = 30
#: Engine reads one traffic view keeps in flight at once.
TRAFFIC_CONCURRENCY: Final[int] = 4
#: The console stops listening at 30 s. A scan that has not finished well inside
#: that answers with what it has, marked as a floor, rather than running on
#: against the store for a caller who has gone. Not ``Final``: tests shorten it.
TRAFFIC_DEADLINE_SECONDS: float = 20.0
#: How long one platform's folded traffic is served before it is scanned again.
#: The modal re-asks on every window change and every reopen; the answer moves
#: slowly. A scan the deadline cut short is held like any other: asking a store
#: that is already slow again at once only lengthens its queue. Zero switches
#: the memory off.
TRAFFIC_CACHE_SECONDS: float = 60.0

#: The engine's span types this view reads. There is no retrieval type, so data
#: access is recognised by name among the general spans -- which is why that pass
#: cannot be narrowed further in the store and is given the smaller budget.
_TOOL_SPANS: Final[str] = "tool"
_GENERAL_SPANS: Final[str] = "general"


@dataclasses.dataclass(frozen=True)
class _TrafficScan:
    """One platform's spans folded for one window -- everything but the tile's name."""

    runs: int | None
    tool_calls: tuple[ConnectionToolCall, ...]
    data_flows: tuple[ConnectionDataFlow, ...]
    truncated: bool
    note: str | None


#: key: (workspace, kind, window days, the projects scanned). The aggregates are
#: remembered, not the response: two tiles of one kind differ only in id and
#: name, and an agent added or retired changes the key rather than going stale.
_traffic_scans: SingleFlightCache[_TrafficScan] = SingleFlightCache(
    ttl=lambda: TRAFFIC_CACHE_SECONDS, max_entries=64
)


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

    # Everything this request needs from the database it has now read. The scan
    # below waits on the telemetry store for seconds at a time; ending the read
    # transaction first hands the pooled connection back instead of holding it,
    # idle, for the length of the scan.
    await session.commit()

    key = (
        principal.workspace_id,
        connection.kind,
        window_days,
        tuple(project.project_id for project in projects),
    )
    scan = await _traffic_scans.get(key, lambda: _scan_traffic(projects, window_days))

    return ConnectionTraffic(
        connection_id=connection.id,
        name=connection.name,
        kind=connection.kind,
        window_days=window_days,
        agents=len(projects),
        runs=scan.runs,
        tool_calls=list(scan.tool_calls),
        data_flows=list(scan.data_flows),
        truncated=scan.truncated,
        attributable=True,
        note=scan.note,
    )


async def _scan_traffic(
    projects: Sequence[telemetry.AgentProject], window_days: int
) -> _TrafficScan:
    """Fold the tool and data spans of one platform's agents over one window.

    Four things keep this inside the time a person will wait for a modal:

    * the store does the filtering -- each read asks for one span type, with
      payloads truncated and attachments stripped, because only a span's name,
      duration, start and error are ever read here. Unfiltered, every llm span
      came back with its full prompt and completion and was dropped in Python;
    * every agent gets its own share of the cap and the agents are read side by
      side, a few at a time, instead of one after another;
    * the run count is one statistics read, taken alongside the scan. The full
      rollup also asks for token totals, one call per agent, which nothing here
      shows;
    * the whole scan has a deadline. Past it the answer is whatever has been
      folded so far, marked as a floor -- and a run count that did not arrive is
      reported as unknown, never as zero.
    """
    client = get_engine_client()
    window = telemetry.resolve_window_days(window_days)
    budget = max(TRAFFIC_PAGE_SIZE, MAX_TRAFFIC_SPANS // len(projects))
    gate = asyncio.Semaphore(TRAFFIC_CONCURRENCY)
    tools: dict[str, dict[str, Any]] = {}
    data: dict[str, dict[str, Any]] = {}

    def fold(row: dict[str, Any]) -> None:
        # The type is checked again here although the store was asked for it: a
        # store that ignored the filter must cost time, not correctness.
        kind = str(row.get("type") or "").lower()
        name = str(row.get("name") or "").strip()
        if not name:
            return
        if kind == _TOOL_SPANS:
            slot = tools.setdefault(
                name, {"calls": 0, "errors": 0, "ms": 0.0, "timed": 0, "last": None}
            )
        elif kind in {"retrieval", _GENERAL_SPANS} and _looks_like_data(name):
            slot = data.setdefault(
                name, {"calls": 0, "docs": 0, "ms": 0.0, "timed": 0, "last": None}
            )
        else:
            return

        duration = row.get("duration")
        moment = _span_instant(row.get("start_time"))
        slot["calls"] += 1
        if row.get("error_info"):
            slot["errors"] = slot.get("errors", 0) + 1
        if isinstance(duration, (int, float)):
            slot["ms"] += float(duration)
            slot["timed"] += 1
        if moment and (slot["last"] is None or moment > slot["last"]):
            slot["last"] = moment

    async def read(project: telemetry.AgentProject, span_type: str, limit: int) -> bool:
        """Fold one agent's spans of one type. True when some were left unread."""
        scanned = 0
        page = 1
        while True:
            async with gate:
                payload = await client.list_spans(
                    project_id=project.project_id,
                    span_type=span_type,
                    page=page,
                    size=TRAFFIC_PAGE_SIZE,
                    truncate=True,
                    strip_attachments=True,
                    from_time=window.start,
                    to_time=window.end,
                )
            rows = telemetry._records(payload)
            for row in rows:
                fold(row)
            scanned += len(rows)
            if len(rows) < TRAFFIC_PAGE_SIZE:
                return False
            total = payload.get("total") if isinstance(payload, dict) else None
            if isinstance(total, int) and not isinstance(total, bool) and scanned >= total:
                return False  # a full last page that was also the last of them
            if scanned >= limit:
                return True
            page += 1

    async def count_runs() -> int:
        stats = await telemetry._stats_by_project(
            client, {project.project_id for project in projects}, window.start, window.end
        )
        return sum(
            int(
                telemetry._measure(
                    stats.get(project.project_id, {}),
                    "trace_count",
                    "traces",
                    "total_traces",
                    "run_count",
                )
                or 0
            )
            for project in projects
        )

    counting = asyncio.ensure_future(count_runs())
    reads = [
        asyncio.ensure_future(read(project, span_type, limit))
        for project in projects
        for span_type, limit in (
            (_TOOL_SPANS, budget),
            (_GENERAL_SPANS, max(TRAFFIC_PAGE_SIZE, budget // 2)),
        )
    ]
    out_of_time = False
    try:
        async with asyncio.timeout(TRAFFIC_DEADLINE_SECONDS):
            await asyncio.gather(counting, *reads)
    except TimeoutError:
        out_of_time = True
    except EngineError as exc:
        raise telemetry.telemetry_unavailable(exc) from exc
    finally:
        # Whatever is still queued behind the gate stops here. An exchange
        # already on the wire is shielded by the client and ends on its own.
        for job in (counting, *reads):
            job.cancel()
        await asyncio.gather(counting, *reads, return_exceptions=True)

    def landed(job: asyncio.Future[Any]) -> bool:
        return job.done() and not job.cancelled() and job.exception() is None

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

    return _TrafficScan(
        runs=counting.result() if landed(counting) else None,
        tool_calls=tuple(tool_rows),
        data_flows=tuple(data_rows),
        truncated=out_of_time or any(job.result() for job in reads if landed(job)),
        note=(
            "The telemetry store was slow to answer, so only part of this window was "
            "read. These totals are a floor."
            if out_of_time
            else None
        ),
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
