"""Connector and MCP governance.

Everything an agent is allowed to call is registered here, and the only
interesting state transition is the block: a blocked connector keeps its row so
the grants, the history and the reason survive, and every agent holding a grant
fails closed against it. Blocks are therefore never a plain field edit - they go
through :func:`block_connector`, which records who, when and why on the row and
writes the matching audit entry in the same transaction.

Two things are deliberately computed in SQL rather than in Python: the
``used_by_agents`` join that fills the "Used By Agents" column for a whole page
at once, and the KPI counts, which are filtered aggregates over the workspace
rather than a scan of loaded rows.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import ipaddress
import socket
import time
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..models.identity import Role
from ..models.registry import (
    AccessLevel,
    Agent,
    AgentConnector,
    Connector,
    ConnectorStatus,
    ConnectorType,
    DataClassification,
    RiskLevel,
)
from ..schemas.connectors import (
    ConnectorAgentRef,
    ConnectorBlockRequest,
    ConnectorCreate,
    ConnectorGrantRequest,
    ConnectorRead,
    ConnectorSummary,
    ConnectorTestResult,
    ConnectorUpdate,
    TestStatus,
)
from . import audit

#: Written into every audit row this module produces.
SOURCE_SCREEN = "Connector & MCP Governance"
ENTITY_TYPE = "connector"

#: An export is a report, not a bulk data channel; beyond this the caller should
#: narrow the filters.
MAX_EXPORT_ROWS = 5000

#: A reachability probe is a UI action behind a spinner - it fails fast.
PROBE_TIMEOUT_SECONDS = 5.0
#: ...and that is a timeout per phase (connect, then the status line), started
#: again on every redirect. The probe as a whole gets one deadline as well, so a
#: slow chain of hops cannot outlive the console's request while the handler
#: sits on a database connection.
PROBE_DEADLINE_SECONDS = 10.0
#: Redirects are followed by hand so that each hop is vetted like the first; a
#: real endpoint is an http->https bounce and perhaps a trailing slash away.
PROBE_MAX_REDIRECTS = 3
PROBE_USER_AGENT = "FD-AI-Command-Center/1.0 (connector reachability probe)"

#: Two clocks are never perfectly aligned and clients round timestamps when they
#: echo them back, so an optimistic check tolerates a second of drift.
CONCURRENCY_TOLERANCE_SECONDS = 1.0

#: Columns free-text search covers, in the order the console renders them.
SEARCH_COLUMNS = (
    Connector.name,
    Connector.provider,
    Connector.connector_type,
    Connector.endpoint_url,
)

#: Body fields an update may write, mapped to their column on the model.
UPDATABLE_FIELDS: dict[str, str] = {
    "name": "name",
    "connector_type": "connector_type",
    "provider": "provider",
    "risk_level": "risk_level",
    "status": "status",
    "access": "access",
    "data_classification": "data_classification",
    "scopes": "scopes",
    "endpoint_url": "endpoint_url",
    "auth_mode": "auth_mode",
    "owner_user_id": "owner_user_id",
}

#: Fields the model stores NOT NULL: an explicit null in the body is a mistake,
#: not an instruction to clear them.
NON_NULLABLE_FIELDS = frozenset(
    {"name", "connector_type", "risk_level", "status", "access", "data_classification", "scopes"}
)

#: CSV layout, mirroring the on-screen table and then the governance detail the
#: table has no room for.
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "Connector / Tool"),
    ("connector_type", "Type"),
    ("provider", "Provider / System"),
    ("used_by_agents", "Used By Agents"),
    ("agents", "Agents"),
    ("risk_level", "Risk Level"),
    ("status", "Status"),
    ("access", "Access"),
    ("data_classification", "Data Classification"),
    ("auth_mode", "Authentication"),
    ("endpoint_url", "Endpoint"),
    ("scopes", "Scopes"),
    ("last_used_at", "Last Used"),
    ("blocked_at", "Blocked At"),
    ("blocked_by", "Blocked By"),
    ("blocked_reason", "Block Reason"),
    ("owner_user_id", "Owner"),
    ("created_at", "Registered"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Enum members are stored as their string value, never as the member."""
    return value.value if isinstance(value, enum.Enum) else value


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _enum_value(raw: str | None, choices: type[enum.Enum], field: str) -> str | None:
    """Resolve a dropdown value case-insensitively, or say what is accepted."""
    if raw is None or not raw.strip():
        return None
    needle = raw.strip().lower()
    for member in choices:
        if str(member.value).lower() == needle:
            return str(member.value)
    raise ValidationFailed(
        f"'{raw}' is not a recognised {field}.",
        details={field: [str(member.value) for member in choices]},
    )


def _used_by_count(workspace_id: str) -> ColumnElement[int]:
    """Correlated count of grants, so the list can be sorted by "Used By Agents"."""
    return (
        select(func.count(AgentConnector.id))
        .where(
            AgentConnector.connector_id == Connector.id,
            AgentConnector.workspace_id == workspace_id,
        )
        .correlate(Connector)
        .scalar_subquery()
        .label("used_by_agents")
    )


def _sortable(workspace_id: str) -> dict[str, Any]:
    """Sort keys: the console sends its column keys, SDKs send field names."""
    used_by = _used_by_count(workspace_id)
    return {
        "name": Connector.name,
        "type": Connector.connector_type,
        "connector_type": Connector.connector_type,
        "provider": Connector.provider,
        "usedBy": used_by,
        "used_by_agents": used_by,
        "risk": Connector.risk_level,
        "risk_level": Connector.risk_level,
        "status": Connector.status,
        "access": Connector.access,
        "data_classification": Connector.data_classification,
        "lastUsed": Connector.last_used_at,
        "last_used_at": Connector.last_used_at,
        "created_at": Connector.created_at,
        "updated_at": Connector.updated_at,
    }


def _apply_access_filter(stmt: Select, raw: str | None) -> Select:
    """Resolve the console's Access dropdown against the right column.

    The dropdown asks a tenancy question - Internal or External - which this
    model answers from ``data_classification``; anything not classified Internal
    carries data past the tenant boundary. SDK callers ask the other question
    with the same parameter, "what may a grant do", which is ``access``. Both
    vocabularies are accepted because both are unambiguous.
    """
    if raw is None or not raw.strip():
        return stmt
    needle = raw.strip().lower()

    for level in AccessLevel:
        if level.value.lower() == needle:
            return stmt.where(Connector.access == level.value)

    if needle == DataClassification.EXTERNAL.value.lower():
        return stmt.where(Connector.data_classification != DataClassification.INTERNAL.value)

    for classification in DataClassification:
        if classification.value.lower() == needle:
            return stmt.where(Connector.data_classification == classification.value)

    raise ValidationFailed(
        f"'{raw}' is not a recognised access filter.",
        details={
            "access": [level.value for level in AccessLevel]
            + [item.value for item in DataClassification]
        },
    )


def _filtered_statement(
    principal: Principal,
    params: ListParams,
    *,
    connector_type: str | None,
    status: str | None,
    risk: str | None,
    access: str | None,
) -> Select:
    """The one query the list, the export and the tab bar all run."""
    stmt = select(Connector).where(Connector.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            Connector.connector_type: _enum_value(connector_type, ConnectorType, "type"),
            Connector.status: _enum_value(status, ConnectorStatus, "status"),
            Connector.risk_level: _enum_value(risk, RiskLevel, "risk"),
        },
    )
    stmt = _apply_access_filter(stmt, access)
    stmt = apply_search(stmt, params, SEARCH_COLUMNS)
    return apply_sort(
        stmt, params, _sortable(principal.workspace_id), Connector.name, default_desc=False
    )


async def _agents_by_connector(
    session: AsyncSession, workspace_id: str, connector_ids: Sequence[str]
) -> dict[str, list[ConnectorAgentRef]]:
    """One join for the whole page: connector id -> the agents granted it."""
    usage: dict[str, list[ConnectorAgentRef]] = {}
    if not connector_ids:
        return usage

    stmt = (
        select(AgentConnector.connector_id, Agent.id, Agent.name)
        .join(Agent, Agent.id == AgentConnector.agent_id)
        .where(
            AgentConnector.connector_id.in_(list(connector_ids)),
            AgentConnector.workspace_id == workspace_id,
        )
        .order_by(Agent.name.asc())
    )
    for connector_id, agent_id, agent_name in await session.execute(stmt):
        usage.setdefault(connector_id, []).append(
            ConnectorAgentRef(id=agent_id, name=agent_name)
        )
    return usage


async def _load(session: AsyncSession, principal: Principal, connector_id: str) -> Connector:
    """Fetch one connector inside the caller's workspace.

    A row in another workspace is reported as missing rather than forbidden: a
    403 would confirm that the id exists somewhere.
    """
    stmt = select(Connector).where(
        Connector.id == connector_id,
        Connector.workspace_id == principal.workspace_id,
    )
    connector = (await session.execute(stmt)).scalar_one_or_none()
    if connector is None:
        raise NotFound("That connector does not exist in this workspace.")
    return connector


async def _read(
    session: AsyncSession, principal: Principal, connector: Connector
) -> ConnectorRead:
    usage = await _agents_by_connector(session, principal.workspace_id, [connector.id])
    return ConnectorRead.from_model(connector, usage.get(connector.id, ()))


async def _assert_name_available(
    session: AsyncSession, principal: Principal, name: str, *, exclude_id: str | None = None
) -> None:
    """Names are the handle people use in policies and tickets, so they are unique.

    Matched case-insensitively, which is stricter than the database constraint:
    two connectors differing only in capitalisation are a governance hazard.
    """
    stmt = select(Connector.id).where(
        Connector.workspace_id == principal.workspace_id,
        func.lower(Connector.name) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Connector.id != exclude_id)
    if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise Conflict(f"A connector named '{name}' already exists in this workspace.")


def _export_row(item: ConnectorRead) -> dict[str, Any]:
    return {
        "name": item.name,
        "connector_type": item.connector_type.value,
        "provider": item.provider,
        "used_by_agents": item.used_by_agents,
        "agents": [agent.name for agent in item.agents],
        "risk_level": item.risk_level.value,
        "status": item.status.value,
        "access": item.access.value,
        "data_classification": item.data_classification.value,
        "auth_mode": item.auth_mode,
        "endpoint_url": item.endpoint_url,
        "scopes": item.scopes,
        "last_used_at": _iso(item.last_used_at),
        "blocked_at": _iso(item.blocked_at),
        "blocked_by": item.blocked_by,
        "blocked_reason": item.blocked_reason,
        "owner_user_id": item.owner_user_id,
        "created_at": _iso(item.created_at),
    }


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def list_connectors(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    connector_type: str | None = None,
    status: str | None = None,
    risk: str | None = None,
    access: str | None = None,
) -> tuple[list[ConnectorRead], int]:
    """One page of connectors with their agent grants, plus the unpaged total."""
    principal.require(Role.VIEWER)
    stmt = _filtered_statement(
        principal, params, connector_type=connector_type, status=status, risk=risk, access=access
    )
    rows, total = await paginate(session, stmt, params)
    usage = await _agents_by_connector(
        session, principal.workspace_id, [connector.id for connector in rows]
    )
    items = [
        ConnectorRead.from_model(connector, usage.get(connector.id, ())) for connector in rows
    ]
    return items, total


async def get_connector(
    session: AsyncSession, principal: Principal, connector_id: str
) -> ConnectorRead:
    """One connector, with the agents that hold a grant on it."""
    principal.require(Role.VIEWER)
    connector = await _load(session, principal, connector_id)
    return await _read(session, principal, connector)


async def summarize(session: AsyncSession, principal: Principal) -> ConnectorSummary:
    """The five KPI cards and the tab-bar counts, as one aggregate query.

    External means "not classified Internal": public, external, confidential and
    restricted connectors all move data past the tenant boundary and belong on
    that card.
    """
    principal.require(Role.VIEWER)
    external = Connector.data_classification != DataClassification.INTERNAL.value

    stmt = select(
        func.count(Connector.id),
        func.count(case((Connector.status == ConnectorStatus.ACTIVE.value, 1))),
        func.count(case((external, 1))),
        func.count(case((Connector.risk_level == RiskLevel.HIGH.value, 1))),
        func.count(case((Connector.status == ConnectorStatus.BLOCKED.value, 1))),
        func.count(case((Connector.connector_type == ConnectorType.MCP_SERVER.value, 1))),
        func.count(case((Connector.connector_type == ConnectorType.TOOL.value, 1))),
        func.count(case((Connector.connector_type == ConnectorType.DATA_SOURCE.value, 1))),
    ).where(Connector.workspace_id == principal.workspace_id)

    total, active, external_count, high_risk, blocked, mcp, tools, data_sources = (
        await session.execute(stmt)
    ).one()

    def percent(part: int) -> float:
        return round(part / total * 100, 1) if total else 0.0

    return ConnectorSummary(
        total=total,
        active=active,
        external=external_count,
        high_risk=high_risk,
        blocked=blocked,
        mcp_servers=mcp,
        tools=tools,
        data_sources=data_sources,
        active_percent=percent(active),
        external_percent=percent(external_count),
    )


async def export_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    connector_type: str | None = None,
    status: str | None = None,
    risk: str | None = None,
    access: str | None = None,
) -> list[dict[str, Any]]:
    """Every row matching the current filters, flattened for CSV."""
    principal.require(Role.VIEWER)
    stmt = _filtered_statement(
        principal, params, connector_type=connector_type, status=status, risk=risk, access=access
    ).limit(MAX_EXPORT_ROWS)
    connectors = (await session.execute(stmt)).scalars().all()
    usage = await _agents_by_connector(
        session, principal.workspace_id, [connector.id for connector in connectors]
    )
    return [
        _export_row(ConnectorRead.from_model(connector, usage.get(connector.id, ())))
        for connector in connectors
    ]


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def create_connector(
    session: AsyncSession,
    principal: Principal,
    payload: ConnectorCreate,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Register a connector. It starts with no grants, so no agent can call it yet."""
    principal.require(Role.OPERATOR)
    await _assert_name_available(session, principal, payload.name)

    connector = Connector(
        workspace_id=principal.workspace_id,
        name=payload.name,
        connector_type=payload.connector_type.value,
        provider=payload.provider,
        risk_level=payload.risk_level.value,
        status=payload.status.value,
        access=payload.access.value,
        data_classification=payload.data_classification.value,
        scopes=list(payload.scopes),
        endpoint_url=payload.endpoint_url,
        auth_mode=payload.auth_mode,
        owner_user_id=payload.owner_user_id or principal.user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(connector)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.created",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Registered {connector.connector_type} '{connector.name}'.",
        metadata={
            "connector_type": connector.connector_type,
            "provider": connector.provider,
            "risk_level": connector.risk_level,
            "status": connector.status,
            "access": connector.access,
            "data_classification": connector.data_classification,
        },
        request=request,
    )
    return ConnectorRead.from_model(connector)


async def update_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    payload: ConnectorUpdate,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Apply the supplied fields. A body that changes nothing writes nothing."""
    principal.require(Role.OPERATOR)
    connector = await _load(session, principal, connector_id)

    supplied = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    _assert_unchanged_since(connector, payload.expected_updated_at)

    if supplied.get("status") is not None:
        _assert_status_transition(connector, _plain(supplied["status"]))
    if "name" in supplied and supplied["name"] is not None:
        await _assert_name_available(
            session, principal, supplied["name"], exclude_id=connector.id
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, column in UPDATABLE_FIELDS.items():
        if field not in supplied:
            continue
        value = _plain(supplied[field])
        if value is None and field in NON_NULLABLE_FIELDS:
            raise ValidationFailed(f"'{field}' cannot be cleared.", details={"field": field})
        current = getattr(connector, column)
        if value == current:
            continue
        setattr(connector, column, value)
        before[field] = current
        after[field] = value

    if not after:
        return await _read(session, principal, connector)

    connector.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.updated",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail="Changed " + ", ".join(sorted(after)) + ".",
        metadata={"before": before, "after": after},
        request=request,
    )
    return await _read(session, principal, connector)


def _assert_unchanged_since(connector: Connector, expected: dt.datetime | None) -> None:
    """Optimistic concurrency: refuse an edit built on a stale copy of the row."""
    if expected is None:
        return
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=dt.UTC)
    drift = abs((connector.updated_at - expected).total_seconds())
    if drift > CONCURRENCY_TOLERANCE_SECONDS:
        raise Conflict(
            "This connector was changed by someone else since you loaded it. "
            "Reload it and re-apply your edit.",
            details={"updated_at": connector.updated_at.isoformat()},
        )


def _assert_status_transition(connector: Connector, new_status: str) -> None:
    """Blocking and unblocking are their own endpoints, never a status edit."""
    if new_status == connector.status:
        return
    if new_status == ConnectorStatus.BLOCKED.value:
        raise ValidationFailed(
            "Use POST /connectors/{id}/block so the block is recorded with a reason.",
            details={"field": "status"},
        )
    if connector.status == ConnectorStatus.BLOCKED.value:
        raise ValidationFailed(
            "This connector is blocked. Use POST /connectors/{id}/unblock to restore it.",
            details={"field": "status"},
        )


async def delete_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    *,
    request: Request | None = None,
) -> str:
    """Remove a connector that nothing depends on. Returns the deleted name.

    A connector still granted to an agent is refused rather than cascaded away:
    deleting it would silently strip that agent's tool without anyone approving
    the change. Blocking is the reversible way to stop it being called.
    """
    principal.require(Role.ADMIN)
    connector = await _load(session, principal, connector_id)

    grants = (
        await session.execute(
            select(func.count(AgentConnector.id)).where(
                AgentConnector.connector_id == connector.id,
                AgentConnector.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one()
    if grants:
        raise PreconditionFailed(
            f"'{connector.name}' is still granted to {grants} agent(s). "
            "Revoke those grants first, or block the connector instead.",
            details={"used_by_agents": grants},
        )

    name = connector.name
    await audit.record(
        session,
        principal=principal,
        action="connector.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=name,
        source_screen=SOURCE_SCREEN,
        detail=f"Deleted {connector.connector_type} '{name}'.",
        metadata={
            "connector_type": connector.connector_type,
            "provider": connector.provider,
            "risk_level": connector.risk_level,
            "status": connector.status,
        },
        request=request,
    )
    await session.delete(connector)
    await session.flush()
    return name


async def block_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    payload: ConnectorBlockRequest,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Block a connector for every agent, recording who, when and why."""
    principal.require(Role.ADMIN)
    connector = await _load(session, principal, connector_id)
    if connector.status == ConnectorStatus.BLOCKED.value:
        raise Conflict(
            f"'{connector.name}' is already blocked.",
            details={"blocked_by": connector.blocked_by, "blocked_at": _iso(connector.blocked_at)},
        )

    previous_status = connector.status
    blocked_at = dt.datetime.now(dt.UTC)
    connector.status = ConnectorStatus.BLOCKED.value
    connector.blocked_at = blocked_at
    connector.blocked_by = principal.actor
    connector.blocked_reason = payload.reason
    connector.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.blocked",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=payload.reason,
        metadata={
            "previous_status": previous_status,
            "new_status": connector.status,
            "blocked_at": blocked_at.isoformat(),
            "blocked_by": connector.blocked_by,
        },
        request=request,
    )
    return await _read(session, principal, connector)


async def unblock_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Lift a block and return the connector to Active.

    The pre-block status is not restored - only the block itself was recorded -
    so the connector comes back Active and the audit trail keeps the reason it
    was stopped.
    """
    principal.require(Role.ADMIN)
    connector = await _load(session, principal, connector_id)
    if connector.status != ConnectorStatus.BLOCKED.value:
        raise Conflict(f"'{connector.name}' is not blocked.")

    reason = connector.blocked_reason
    blocked_at = connector.blocked_at
    blocked_by = connector.blocked_by

    connector.status = ConnectorStatus.ACTIVE.value
    connector.blocked_at = None
    connector.blocked_by = None
    connector.blocked_reason = None
    connector.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.unblocked",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Block lifted. Original reason: {reason}" if reason else "Block lifted.",
        metadata={
            "previous_status": ConnectorStatus.BLOCKED.value,
            "new_status": connector.status,
            "blocked_at": _iso(blocked_at),
            "blocked_by": blocked_by,
            "blocked_reason": reason,
        },
        request=request,
    )
    return await _read(session, principal, connector)


async def grant_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    payload: ConnectorGrantRequest,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Grant this connector to one agent.

    The grant is the edge governance acts on: connector-scoped policies reach
    the agent through it, and a block on the connector fails the agent closed
    at ingest. A blocked connector cannot take new grants - unblock it first,
    so the reach it regains is a deliberate decision.
    """
    principal.require(Role.OPERATOR)
    connector = await _load(session, principal, connector_id)
    if connector.status == ConnectorStatus.BLOCKED.value:
        raise PreconditionFailed(
            f"'{connector.name}' is blocked. Unblock it before granting it to an agent.",
            details={"blocked_reason": connector.blocked_reason},
        )

    agent = (
        await session.execute(
            select(Agent).where(
                Agent.workspace_id == principal.workspace_id, Agent.id == payload.agent_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound("No agent with that id exists in this workspace.")

    existing = (
        await session.execute(
            select(AgentConnector).where(
                AgentConnector.workspace_id == principal.workspace_id,
                AgentConnector.connector_id == connector.id,
                AgentConnector.agent_id == agent.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(f"'{connector.name}' is already granted to '{agent.name}'.")

    session.add(
        AgentConnector(
            workspace_id=principal.workspace_id,
            agent_id=agent.id,
            connector_id=connector.id,
            granted_at=dt.datetime.now(dt.UTC),
            granted_by=principal.actor,
        )
    )
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.granted",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Granted '{connector.name}' to agent '{agent.name}'.",
        metadata={"agent_id": agent.id, "agent_name": agent.name},
        request=request,
    )
    return await _read(session, principal, connector)


async def revoke_grant(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    agent_id: str,
    *,
    request: Request | None = None,
) -> ConnectorRead:
    """Take a connector away from one agent."""
    principal.require(Role.OPERATOR)
    connector = await _load(session, principal, connector_id)

    grant = (
        await session.execute(
            select(AgentConnector).where(
                AgentConnector.workspace_id == principal.workspace_id,
                AgentConnector.connector_id == connector.id,
                AgentConnector.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise NotFound(f"'{connector.name}' is not granted to that agent.")

    agent_name = (
        await session.execute(select(Agent.name).where(Agent.id == agent_id))
    ).scalar_one_or_none()

    await session.delete(grant)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="connector.grant_revoked",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Revoked '{connector.name}' from agent '{agent_name or agent_id}'.",
        metadata={"agent_id": agent_id, "agent_name": agent_name},
        request=request,
    )
    return await _read(session, principal, connector)


async def test_connector(
    session: AsyncSession,
    principal: Principal,
    connector_id: str,
    *,
    request: Request | None = None,
) -> ConnectorTestResult:
    """Probe the registered endpoint and report what came back.

    The probe carries no credentials - this service holds none for the connector
    - so it answers reachability only: a 401 proves the endpoint is up and
    guarding itself, which is a healthy answer to this question. A blocked
    connector is not probed at all; blocked means blocked. Neither is one whose
    endpoint is on a private network (see :func:`probe_addresses`): that is
    reported as a warning that says so, never as unreachable, because nothing
    was measured. Every outcome, including those, is audited.
    """
    principal.require(Role.MEMBER)
    connector = await _load(session, principal, connector_id)
    checked_at = dt.datetime.now(dt.UTC)

    if connector.status == ConnectorStatus.BLOCKED.value:
        result = ConnectorTestResult(
            connector_id=connector.id,
            name=connector.name,
            ok=False,
            status="Blocked",
            message=f"'{connector.name}' is blocked, so agents calling it fail closed.",
            endpoint_url=connector.endpoint_url,
            checked_at=checked_at,
        )
    else:
        if not connector.endpoint_url:
            raise PreconditionFailed(
                f"'{connector.name}' has no endpoint URL registered, so it cannot be probed."
            )
        result = await _probe(connector, checked_at)

    await audit.record(
        session,
        principal=principal,
        action="connector.tested",
        entity_type=ENTITY_TYPE,
        entity_id=connector.id,
        entity_label=connector.name,
        source_screen=SOURCE_SCREEN,
        detail=result.message,
        metadata={
            "ok": result.ok,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "http_status": result.http_status,
        },
        request=request,
    )
    return result


class ProbeStopped(Exception):
    """The probe ended without an answer from the endpoint, and this is why.

    Carries the verdict to report, because the two reasons are different
    answers: a host that does not resolve is unreachable, while an address this
    process refuses to call has not been found to be anything at all.
    """

    def __init__(self, status: TestStatus, message: str) -> None:
        super().__init__(message)
        self.status: TestStatus = status


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``address`` is on the public internet and nowhere else.

    Everything else is somewhere a URL typed into a form must not be able to
    send this process: loopback, the RFC 1918 and unique-local ranges the
    container network hands out, carrier-grade NAT space, and link-local, which
    is where the instance metadata endpoint (169.254.169.254) lives.
    """
    # An IPv4 address carried inside an IPv6 one is routed as the IPv4 address,
    # so it is judged as one: ::ffff:10.0.0.5 is 10.0.0.5 by another spelling.
    if isinstance(address, ipaddress.IPv6Address):
        for carried in (address.ipv4_mapped, address.sixtofour):
            if carried is not None:
                return is_public_address(carried)
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def probe_addresses(url: httpx.URL, *, redirected: bool = False) -> list[str]:
    """The literal addresses a probe of ``url`` may connect to.

    The probe is a request this process makes on a member's say-so to a URL an
    operator typed, and the server sits on the same network as the
    telemetry engine, the analytics store and the blob store - none of them
    published, some of them unauthenticated *because* they are unpublished. So
    the host is resolved here, once, and refused if **any** answer is not a
    public address (:class:`ProbeStopped`). The caller then connects to the
    addresses returned rather than to the name: handing the name back to the
    HTTP library would resolve it a second time, and a DNS server that answers
    "public" the first time and "10.0.0.5" the second walks straight through a
    check made on the first answer.

    Registering a private endpoint stays legal - an internal MCP server is a
    legitimate thing to govern - it is only the call from here that is withheld,
    unless ``connector_probe_allow_private`` says this deployment wants it.
    """
    if url.scheme not in ("http", "https"):
        raise httpx.UnsupportedProtocol(f"'{url.scheme}://' cannot be probed")
    # The ASCII form: what DNS is asked, and what goes in Host and SNI later.
    host = url.raw_host.decode("ascii")
    if not host:
        raise httpx.InvalidURL("the URL has no host")

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            found = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except (OSError, UnicodeError):
            raise ProbeStopped("Unreachable", f"The host name '{host}' does not resolve.") from None
        # sockaddr[0] may carry a zone ("fe80::1%eth0"); the address is the part before it.
        addresses = list(
            dict.fromkeys(ipaddress.ip_address(str(info[4][0]).partition("%")[0]) for info in found)
        )

    if not settings.connector_probe_allow_private and not all(map(is_public_address, addresses)):
        where = f"The endpoint redirects to '{host}', which" if redirected else f"'{host}'"
        raise ProbeStopped(
            "Warning",
            f"{where} is a private or internal address, so it was not probed: the control "
            "plane does not call into its own network on a connector's behalf.",
        )
    return [str(address) for address in addresses]


async def _status_line(
    client: httpx.AsyncClient, url: httpx.URL, addresses: Sequence[str]
) -> tuple[int, str | None]:
    """One GET to the vetted addresses; the status, and where it redirects if it does.

    The connection goes to the address, while the Host header and the TLS server
    name stay the registered name - so virtual hosting works and the certificate
    is still checked against the name, not the number.
    """
    headers = {"User-Agent": PROBE_USER_AGENT, "Host": url.netloc.decode("ascii")}
    extensions = {"sni_hostname": url.raw_host.decode("ascii")}
    failure: httpx.TransportError | None = None
    # A name with an address this host has no route to (AAAA from a v4-only
    # network) must not read as down while another of its addresses answers.
    for address in addresses:
        try:
            async with client.stream(
                "GET", url.copy_with(host=address), headers=headers, extensions=extensions
            ) as response:
                location = response.headers.get("location")
                return response.status_code, location if response.has_redirect_location else None
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            failure = exc
    raise failure or httpx.ConnectError("the host has no address")


async def _final_status(client: httpx.AsyncClient, endpoint: str) -> int:
    """Follow the endpoint to its answer, vetting every hop like the first.

    Redirects are followed by hand. Left to the HTTP library, a public URL that
    answers ``302 Location: http://169.254.169.254/...`` would be followed into
    exactly the place :func:`probe_addresses` exists to keep the probe out of.
    """
    url = httpx.URL(endpoint)
    for hop in range(PROBE_MAX_REDIRECTS + 1):
        addresses = await probe_addresses(url, redirected=hop > 0)
        http_status, location = await _status_line(client, url, addresses)
        if location is None:
            return http_status
        try:
            url = url.join(location)
        except httpx.InvalidURL:
            raise ProbeStopped(
                "Unreachable", f"The endpoint answered {http_status} with a redirect to nowhere."
            ) from None
    raise ProbeStopped("Unreachable", f"Redirected more than {PROBE_MAX_REDIRECTS} times.")


async def _probe(connector: Connector, checked_at: dt.datetime) -> ConnectorTestResult:
    """Open the endpoint, read the status line, close it without reading a body."""
    endpoint = connector.endpoint_url or ""

    def no_answer(message: str, status: TestStatus = "Unreachable") -> ConnectorTestResult:
        return ConnectorTestResult(
            connector_id=connector.id,
            name=connector.name,
            ok=False,
            status=status,
            message=message,
            endpoint_url=endpoint,
            checked_at=checked_at,
        )

    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            # The clock starts once there is a client: building one loads the
            # trust store, which is neither the endpoint's latency nor its fault.
            started = time.perf_counter()
            async with asyncio.timeout(PROBE_DEADLINE_SECONDS):
                http_status = await _final_status(client, endpoint)
    except ProbeStopped as stopped:
        return no_answer(str(stopped), stopped.status)
    except httpx.InvalidURL:
        # Not an httpx.HTTPError, so the arm below never saw it and a malformed
        # endpoint was an opaque 500 on every test, with no audit row. Rows
        # registered before the schema started parsing the URL are still in the
        # table, so this has to be an answer rather than an error.
        return no_answer("The registered endpoint is not a valid URL.")
    except httpx.TimeoutException:
        return no_answer(f"No response within {PROBE_TIMEOUT_SECONDS:.0f}s.")
    except TimeoutError:
        # The overall deadline, which is the builtin and not an httpx exception.
        return no_answer(f"No answer within {PROBE_DEADLINE_SECONDS:.0f}s.")
    except httpx.HTTPError as exc:
        return no_answer(f"The endpoint could not be reached ({type(exc).__name__}).")

    latency_ms = int((time.perf_counter() - started) * 1000)
    if http_status >= 500:
        status: TestStatus = "Unreachable"
        ok = False
        message = f"The endpoint answered {http_status} in {latency_ms}ms."
    elif http_status >= 400:
        status = "Warning"
        ok = True
        message = (
            f"Reachable in {latency_ms}ms; answered {http_status} to an "
            "unauthenticated probe."
        )
    else:
        status = "Healthy"
        ok = True
        message = f"Responded {http_status} in {latency_ms}ms."

    return ConnectorTestResult(
        connector_id=connector.id,
        name=connector.name,
        ok=ok,
        status=status,
        message=message,
        latency_ms=latency_ms,
        http_status=http_status,
        endpoint_url=endpoint,
        checked_at=checked_at,
    )
