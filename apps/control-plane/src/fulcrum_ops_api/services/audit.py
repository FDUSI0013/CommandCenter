"""Append-only audit trail.

Every state change a person or key makes lands here. Rows are chained: each
row's checksum covers its own canonical form plus the previous row's checksum,
so deleting or editing history breaks the chain and ``verify_chain`` will say
where. Nothing in this module ever updates or deletes a row.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..models.governance import AuditEvent

GENESIS = "0" * 64


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
) -> AuditEvent:
    """Write one audit row. Call inside the same transaction as the change it describes."""
    occurred_at = dt.datetime.now(dt.UTC)
    previous = await _previous_checksum(session, principal.workspace_id)
    canonical = _canonical(
        workspace_id=principal.workspace_id,
        occurred_at=occurred_at,
        actor=principal.actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    )

    event = AuditEvent(
        workspace_id=principal.workspace_id,
        occurred_at=occurred_at,
        actor=principal.actor,
        actor_user_id=principal.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        source_screen=source_screen,
        detail=detail,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") if request else None),
        event_metadata=metadata or {},
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


async def verify_chain(
    session: AsyncSession, workspace_id: str, limit: int | None = None
) -> dict[str, Any]:
    """Recompute the chain. Returns where it first breaks, if it does."""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace_id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)

    previous = GENESIS
    checked = 0
    for event in (await session.execute(stmt)).scalars():
        canonical = _canonical(
            workspace_id=event.workspace_id,
            occurred_at=event.occurred_at,
            actor=event.actor,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            detail=event.detail,
        )
        expected = _checksum(canonical, previous)
        if expected != event.checksum:
            return {
                "intact": False,
                "checked": checked,
                "broken_at_event_id": event.id,
                "broken_at": event.occurred_at,
            }
        previous = event.checksum
        checked += 1

    return {"intact": True, "checked": checked, "broken_at_event_id": None}
