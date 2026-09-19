"""Append-only audit trail.

Every state change a person or key makes lands here. Rows are chained: each
row's checksum covers its own canonical form plus the previous row's checksum,
so deleting or editing history breaks the chain and ``verify_chain`` will say
where. Nothing in this module ever updates or deletes a row.

A chain has one tail, so its writers have to take turns. ``record`` holds a
per-workspace advisory lock from before it stamps the time until the caller's
transaction commits; without it two workers read the same tail, both chain to
it, and the trail forks. History written before that lock existed *is* forked,
so ``verify_chain`` checks links rather than positions: every row must
reconcile with the parent it names, and that parent must exist. A row that
names a parent other than its immediate predecessor is counted as a fork and
reported, not called tampering.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import hashlib
import itertools
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..core.config import settings
from ..core.ttlcache import SingleFlightCache
from ..db.session import get_sessionmaker
from ..models.governance import AuditEvent

log = logging.getLogger(__name__)

GENESIS = "0" * 64

#: First key of the two-int advisory lock the writers of one workspace's chain
#: take turns on. The scheduler's tick lock is the single-bigint form, which is
#: a separate key space, so the two can never collide.
AUDIT_LOCK_NAMESPACE = 0x41554454  # "AUDT"

#: How long a writer waits for its turn before it writes anyway. The lock is
#: held until the holder commits, and a holder can be slow (a handler that
#: talks to the engine after its first audit row). Waiting for ever would park
#: every audited change in the workspace behind it, one pooled connection each;
#: giving up costs at worst one fork, which ``verify_chain`` counts and reports.
AUDIT_LOCK_WAIT_SECONDS = 3.0
AUDIT_LOCK_POLL_SECONDS = 0.025

#: Rows fetched per round trip while replaying, and how many recent checksums
#: are kept in memory to resolve a fork's parent without asking the database.
VERIFY_CHUNK_ROWS = 5000
VERIFY_WINDOW_ROWS = 20000


def _canonical(
    *,
    workspace_id: str,
    occurred_at: dt.datetime,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | None,
    detail: str | None,
) -> str:
    return json.dumps(
        {
            "workspace_id": workspace_id,
            "occurred_at": occurred_at.isoformat(),
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum(canonical: str, previous: str) -> str:
    return hashlib.sha256(f"{previous}{canonical}".encode()).hexdigest()


async def _previous_checksum(session: AsyncSession, workspace_id: str) -> str:
    stmt = (
        select(AuditEvent.checksum)
        .where(AuditEvent.workspace_id == workspace_id)
        .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() or GENESIS


def _fit(value: str | None, limit: int) -> str | None:
    """Cut a value to the width of its column.

    PostgreSQL refuses an overlong string rather than truncating it, and this
    row is written inside the transaction of the change it describes -- so one
    browser with a 300-character User-Agent got a 500 from every governed
    action it attempted, while its reads kept working. A clipped forensic
    detail is a smaller loss than the change itself.
    """
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def _state(value: Any) -> str | None:
    """A before/after value as the audit drawer shows it, or None if it is not one.

    Only plain values qualify. Several services nest ``{"from", "to"}`` under
    per-field keys; a dict is a diff, not a state, and is left in the metadata.
    """
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    return None


def _lock_key(workspace_id: str) -> int:
    """The workspace's half of the lock key: a stable signed 32-bit number.

    Derived here rather than by a database hash function so every worker, and
    every database version, agrees on it.
    """
    digest = hashlib.sha256(workspace_id.encode()).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


async def _take_turn(session: AsyncSession, workspace_id: str) -> None:
    """Serialise this workspace's chain writers until the transaction ends.

    Transaction-scoped, so there is no unlock for a cancelled request to skip,
    and re-entrant, so a handler that records several rows simply keeps its
    turn. Under READ COMMITTED the tail read that follows takes a fresh
    snapshot, so it sees the row the previous holder has just committed.

    The lock is *polled* with the try- form rather than waited on. A blocking
    wait would need ``lock_timeout`` to be bounded, and a statement that times
    out aborts the caller's whole transaction -- the audit trail would be
    failing the change it was asked to describe. Polling also cannot deadlock
    against a holder that is itself waiting on a row this caller has locked.

    SQLite has one writer at a time by construction; there is nothing to take.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    statement = select(
        func.pg_try_advisory_xact_lock(AUDIT_LOCK_NAMESPACE, _lock_key(workspace_id))
    )
    deadline = time.monotonic() + AUDIT_LOCK_WAIT_SECONDS
    while True:
        if (await session.execute(statement)).scalar():
            return
        if time.monotonic() >= deadline:
            log.warning(
                "audit chain lock for workspace %s still held after %.1fs; "
                "writing without it (a fork is reported by verify, not hidden)",
                workspace_id,
                AUDIT_LOCK_WAIT_SECONDS,
            )
            return
        await asyncio.sleep(AUDIT_LOCK_POLL_SECONDS)


async def record(
    session: AsyncSession,
    *,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    entity_label: str | None = None,
    source_screen: str | None = None,
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
    request: Request | None = None,
    prev_value: str | None = None,
    new_value: str | None = None,
) -> AuditEvent:
    """Write one audit row. Call inside the same transaction as the change it describes.

    ``prev_value`` / ``new_value`` are the before and after the audit drawer
    and the CSV show side by side. A transition that already says
    ``{"from": ..., "to": ...}`` in its metadata need not repeat itself: those
    are used when the arguments are left out.
    """
    # The turn is taken before the clock is read, not merely before the tail
    # is: a writer that stamped its time and *then* waited would chain to a row
    # dated later than itself, and the trail is replayed in time order.
    await _take_turn(session, principal.workspace_id)
    occurred_at = dt.datetime.now(dt.UTC)
    previous = await _previous_checksum(session, principal.workspace_id)

    # Four of the bounded columns are hashed. They are cut to width *before*
    # the checksum is taken, so the row that is stored is the row that was
    # hashed -- cutting afterwards would make verify report a break here.
    actor = _fit(principal.actor, 255)
    action = _fit(action, 120)
    entity_type = _fit(entity_type, 48)
    entity_id = _fit(entity_id, 64)
    canonical = _canonical(
        workspace_id=principal.workspace_id,
        occurred_at=occurred_at,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    )

    states = metadata or {}
    if prev_value is None:
        prev_value = _state(states.get("from"))
    if new_value is None:
        new_value = _state(states.get("to"))

    event = AuditEvent(
        workspace_id=principal.workspace_id,
        occurred_at=occurred_at,
        actor=actor,
        actor_user_id=principal.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=_fit(entity_label, 255),
        source_screen=_fit(source_screen, 80),
        detail=detail,
        prev_value=prev_value,
        new_value=new_value,
        ip_address=_fit(_client_ip(request), 64),
        user_agent=_fit(request.headers.get("user-agent") if request else None, 255),
        event_metadata=metadata or {},
        # Stored alongside the recomputable value so an external verifier can
        # walk the chain row by row without reconstructing the ordering first.
        previous_checksum=previous,
        checksum=_checksum(canonical, previous),
    )
    session.add(event)
    await session.flush()
    return event


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # Caddy sets X-Forwarded-For; take the left-most entry, which is the client.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


#: Where a replay stopped believing a row: its position, and enough to name it.
_Suspect = tuple[int, str, dt.datetime]

#: A row written before the link was stored carries no parent. When it does not
#: follow from its predecessor it is tried against this many rows either side.
_UNLINKED_REACH = 64


async def _absent(session: AsyncSession, workspace_id: str, checksums: list[str]) -> set[str]:
    """Which of these checksums no row in the workspace carries."""
    absent = set(checksums)
    for start in range(0, len(checksums), 500):
        found = await session.execute(
            select(AuditEvent.checksum).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.checksum.in_(checksums[start : start + 500]),
            )
        )
        absent.difference_update(found.scalars().all())
    return absent


async def verify_chain(
    session: AsyncSession, workspace_id: str, limit: int | None = None
) -> dict[str, Any]:
    """Replay the chain. Returns where it first breaks, if it does.

    A row is good when its checksum follows from its own content plus the
    parent it names, and that parent is a row of this workspace. Requiring the
    parent to be the row *immediately before* would be stricter than the writer
    ever was: before ``record`` serialised its callers, two workers could chain
    to the same tail, and that history cannot be rewritten -- it is append-only.
    Such rows are counted as ``forks``. What the chain exists to catch still
    shows: an edited row no longer follows from its parent, and a deleted row
    leaves its children naming a parent that is not there.

    Only the hashed columns are read, a few thousand rows at a time, so a long
    trail neither loads whole ORM rows nor holds the event loop for the length
    of the replay: every chunk fetch is an await.
    """
    stmt = (
        select(
            AuditEvent.id,
            AuditEvent.occurred_at,
            AuditEvent.actor,
            AuditEvent.action,
            AuditEvent.entity_type,
            AuditEvent.entity_id,
            AuditEvent.detail,
            AuditEvent.previous_checksum,
            AuditEvent.checksum,
        )
        .where(AuditEvent.workspace_id == workspace_id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)

    previous = GENESIS
    position = 0
    forks = 0
    #: Checksums already replayed, newest last; bounded, the database backs it.
    recent: OrderedDict[str, None] = OrderedDict()
    #: Parents some row has already chained from. A second row that chains from
    #: the same one is the fork; the row that carries on from either branch is not.
    claimed: OrderedDict[str, None] = OrderedDict()
    #: Parents a row named that have not been replayed yet, and who named them
    #: first. A writer that stamped its time before it read the tail names a
    #: parent dated after itself, legitimately.
    named: dict[str, _Suspect] = {}
    #: Unlinked rows still looking for a parent among the rows after them.
    adrift: list[tuple[str, str, _Suspect, int]] = []
    suspects: list[_Suspect] = []

    def claim(parent: str) -> int:
        if parent in claimed:
            return 1
        claimed[parent] = None
        if len(claimed) > VERIFY_WINDOW_ROWS:
            claimed.popitem(last=False)
        return 0

    result = await session.stream(stmt.execution_options(yield_per=VERIFY_CHUNK_ROWS))
    async for chunk in result.partitions(VERIFY_CHUNK_ROWS):
        for row in chunk:
            canonical = _canonical(
                workspace_id=workspace_id,
                occurred_at=row.occurred_at,
                actor=row.actor,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                detail=row.detail,
            )
            here: _Suspect = (position, row.id, row.occurred_at)
            parent = row.previous_checksum

            if parent is None:
                # Written before the link was stored, so the parent has to be
                # found: the row before it, else one of the rows around it.
                nearby = itertools.chain(
                    (previous,), itertools.islice(reversed(recent), _UNLINKED_REACH)
                )
                parent = next(
                    (c for c in nearby if _checksum(canonical, c) == row.checksum), None
                )
                if parent is None:
                    adrift.append((canonical, row.checksum, here, _UNLINKED_REACH))
                else:
                    forks += claim(parent)
            elif _checksum(canonical, parent) != row.checksum:
                suspects.append(here)
                break
            else:
                forks += claim(parent)
                if parent not in (previous, GENESIS) and parent not in recent:
                    named.setdefault(parent, here)

            named.pop(row.checksum, None)
            still_adrift = []
            for content, checksum, suspect, reach in adrift:
                if _checksum(content, row.checksum) == checksum:
                    forks += claim(row.checksum)
                elif reach > 1:
                    still_adrift.append((content, checksum, suspect, reach - 1))
                else:
                    suspects.append(suspect)
            adrift = still_adrift

            recent[row.checksum] = None
            if len(recent) > VERIFY_WINDOW_ROWS:
                recent.popitem(last=False)
            previous = row.checksum
            position += 1
            if suspects:
                break
        if suspects:
            break
    await result.close()

    # Whatever is still looking for a parent has run out of rows to find it in.
    suspects.extend(suspect for _content, _sum, suspect, _reach in adrift)
    if named:
        # Older than the window: ask the database, which is rare enough to afford.
        absent = await _absent(session, workspace_id, list(named))
        suspects.extend(first for parent, first in named.items() if parent in absent)

    outcome: dict[str, Any] = {
        "intact": not suspects,
        "checked": position,
        "broken_at_event_id": None,
        "broken_at": None,
    }
    if suspects:
        at, event_id, occurred_at = min(suspects, key=lambda suspect: suspect[0])
        outcome.update(checked=at, broken_at_event_id=event_id, broken_at=occurred_at)
    if forks:
        # Present only when there are any, so an unforked trail reads as before.
        outcome["forks"] = forks
    return outcome


#: One replay per workspace per ``audit_verify_cache_seconds``, shared by
#: everyone who asks. The console asks on every visit to the Audit Trail tab
#: (and used to ask twice), any signed-in caller or key may, and each answer
#: costs a read of every audit row the workspace has.
_verified: SingleFlightCache[dict[str, Any]] = SingleFlightCache(
    ttl=lambda: settings.audit_verify_cache_seconds, max_entries=64
)


async def verify_chain_shared(workspace_id: str) -> dict[str, Any]:
    """``verify_chain``, replayed at most once per interval however many ask.

    The replay runs on a session of its own rather than the asking request's.
    Its answer belongs to every caller waiting on it, so it must not die with
    whichever request happened to ask first -- a closed tab takes its request's
    session with it.
    """

    async def replay() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            return await verify_chain(session, workspace_id)

    return await _verified.get(workspace_id, replay)
