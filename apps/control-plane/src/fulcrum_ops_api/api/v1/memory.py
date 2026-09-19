"""Memory & State Management routes.

Eighteen endpoints back one screen: the store table with its four filters and
its export, the KPI row above it, the six tabs beside it (stores, sessions,
agent state, conversation state, retention policies, backups) and the six row
actions (view records, update retention, purge, back up, restore, delete).

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
audit writes and every call into the telemetry adapter live in
``services.memory``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.registry import MemoryStoreStatus, MemoryStoreType
from ...schemas.memory import (
    AgentStateRead,
    MemoryBackupRead,
    MemoryBackupRequest,
    MemoryBackupResult,
    MemoryConversationRead,
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
)
from ...services import memory as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/memory", tags=["Memory & State"])

ListArgs = Annotated[ListParams, Depends(list_params)]
TypeFilter = Annotated[
    MemoryStoreType | None, Query(alias="type", description="Store type, e.g. 'Conversation'")
]
StatusFilter = Annotated[
    MemoryStoreStatus | None, Query(alias="status", description="Active, Paused or Degraded")
]
EnvironmentFilter = Annotated[
    str | None, Query(alias="environment", description="Production, Staging or Development")
]
OwnerFilter = Annotated[str | None, Query(alias="owner", description="Owning user id")]
AgentFilter = Annotated[str | None, Query(description="Narrow to one agent's memory")]


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{store_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=MemorySummary, summary="Memory KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> MemorySummary:
    """The six KPI cards, plus the type, status and environment breakdowns.

    Store-side numbers are SQL aggregates over the whole workspace; Active
    Sessions is a live count from the telemetry store, so this endpoint answers
    503 rather than a stale number when telemetry is unreachable.
    """
    return await service.summarise(session, principal)


@router.get("/export", summary="Export memory stores as CSV")
async def export_stores(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    store_type: TypeFilter = None,
    store_status: StatusFilter = None,
    environment: EnvironmentFilter = None,
    owner: OwnerFilter = None,
) -> StreamingResponse:
    """Download the filtered store inventory, retention policy and owner included.

    Honours the same search, filters and sort as the list, so what downloads is
    what the operator is looking at.
    """
    rows = await service.export_rows(
        session,
        principal,
        params,
        store_type=store_type,
        status=store_status,
        environment=environment,
        owner_user_id=owner,
    )
    body = to_csv(rows, service.EXPORT_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="memory-stores-{stamp}.csv"'},
    )


@router.get(
    "/sessions",
    response_model=Page[MemorySessionRead],
    summary="List active sessions",
)
async def list_sessions(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    agent_id: AgentFilter = None,
    state: Annotated[
        SessionState | None, Query(description="Active, Idle or Closed")
    ] = None,
) -> Page[MemorySessionRead]:
    """The Sessions tab: conversation threads, most recent activity first.

    Threads come from the telemetry store, merged across the projects of the
    workspace's provisioned agents. Deep pages should narrow by `agent_id`.
    """
    rows, total = await service.list_sessions(
        session, principal, params, agent_id=agent_id, state=state
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.get(
    "/conversations",
    response_model=Page[MemoryConversationRead],
    summary="List conversation state",
)
async def list_conversations(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    agent_id: AgentFilter = None,
) -> Page[MemoryConversationRead]:
    """The Conversation State tab: message counts, context size and expiry.

    Expiry is the thread's last activity plus the retention window of the store
    its agent's memory policy names — the store whose purge reaches it. Both
    `retention_policy` and `expires_at` are null for an agent bound to no store.
    """
    rows, total = await service.list_conversations(
        session, principal, params, agent_id=agent_id
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.get(
    "/agent-state",
    response_model=Page[AgentStateRead],
    summary="List agent state",
)
async def list_agent_state(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    environment: EnvironmentFilter = None,
) -> Page[AgentStateRead]:
    """The Agent State tab: the store each agent checkpoints into and its sync state.

    Session counts are read live per agent; an agent with no telemetry project
    yet reports null rather than zero, because nobody has looked.
    """
    rows, total = await service.list_agent_state(
        session, principal, params, environment=environment
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.get(
    "/retention-policies",
    response_model=list[RetentionPolicyRead],
    summary="List retention policies",
)
async def list_retention_policies(
    principal: CurrentPrincipal, session: Db
) -> list[RetentionPolicyRead]:
    """The Retention Policies tab: every distinct policy and what it governs.

    Policies are derived from the stores that use them, so the tab cannot drift
    from the windows the purge actually enforces. `record_count` counts a
    policy's conversation and session stores live, and is null — not zero — when
    the telemetry store cannot be read.
    """
    return await service.list_retention_policies(session, principal)


@router.get(
    "/backups",
    response_model=Page[MemoryBackupRead],
    summary="List backups",
)
async def list_backups(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    store_id: Annotated[str | None, Query(description="Restrict to one store")] = None,
) -> Page[MemoryBackupRead]:
    """The Backups tab: the workspace's backup ledger, newest first.

    Backups are audit rows, so nothing here can be edited or removed after the
    snapshot was taken.
    """
    rows, total = await service.list_backups(session, principal, params, store_id=store_id)
    return Page.build(rows, total, params.page, params.page_size)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[MemoryStoreRead], summary="List memory stores")
async def list_stores(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    store_type: TypeFilter = None,
    store_status: StatusFilter = None,
    environment: EnvironmentFilter = None,
    owner: OwnerFilter = None,
) -> Page[MemoryStoreRead]:
    """One page of the workspace's memory stores, ordered by name by default.

    Free-text search covers the name, type, backend and retention policy; `sort`
    accepts any column key the table renders.
    """
    rows, total = await service.list_stores(
        session,
        principal,
        params,
        store_type=store_type,
        status=store_status,
        environment=environment,
        owner_user_id=owner,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=MemoryStoreRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a memory store",
)
async def create_store(
    principal: CurrentPrincipal,
    session: Db,
    payload: MemoryStoreCreate,
    request: Request,
) -> MemoryStoreRead:
    """Provision a store.

    It starts Active and empty — no counters are accepted, because a brand new
    store has not stored anything yet. Requires the operator role.
    """
    return await service.create_store(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Single store
# ---------------------------------------------------------------------------


@router.get("/{store_id}", response_model=MemoryStoreRead, summary="Get a memory store")
async def get_store(
    principal: CurrentPrincipal, session: Db, store_id: str
) -> MemoryStoreRead:
    """One store. A store in another workspace answers 404, not 403."""
    return await service.read_store(session, principal, store_id)


@router.patch(
    "/{store_id}", response_model=MemoryStoreRead, summary="Update a memory store"
)
async def update_store(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    payload: MemoryStoreUpdate,
    request: Request,
) -> MemoryStoreRead:
    """Partially update a store, including the counters its runtime reports.

    Retention is not accepted here: it moves through
    `PUT /memory/{id}/retention` so the deletion SLA has one audited path. Send
    `expected_updated_at` to make the write conditional. Requires the operator
    role.
    """
    return await service.update_store(session, principal, store_id, payload, request=request)


@router.delete(
    "/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a memory store",
)
async def delete_store(
    principal: CurrentPrincipal, session: Db, store_id: str, request: Request
) -> None:
    """Remove a store from the registry.

    Only the registry row goes; no record is deleted anywhere, and the store's
    backups stay on the ledger. Refused with 412 while any agent still names the
    store as its memory policy. Requires the admin role.
    """
    await service.delete_store(session, principal, store_id, request=request)


@router.get(
    "/{store_id}/records",
    response_model=Page[MemoryRecordRead],
    summary="List stored records",
)
async def list_records(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    params: ListArgs,
    agent_id: AgentFilter = None,
) -> Page[MemoryRecordRead]:
    """One page of the store's records, read live from the telemetry store.

    Records are the conversation threads of the agents whose memory policy names
    the store — the same scope a purge sweeps — so this is answered for
    conversation and session stores; any other type is refused with 412 naming
    the backend that does hold them. A store no agent names holds no records.
    """
    rows, total = await service.list_records(
        session, principal, store_id, params, agent_id=agent_id
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.put(
    "/{store_id}/retention",
    response_model=MemoryStoreRead,
    summary="Update the retention policy",
)
async def update_retention(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    payload: RetentionPolicyUpdate,
    request: Request,
) -> MemoryStoreRead:
    """Set the deletion SLA the store is audited against.

    The window is a whole number of days; the label is derived from it when the
    caller does not supply one, so the table and the purge always agree.
    Requires the admin role.
    """
    return await service.update_retention(
        session, principal, store_id, payload, request=request
    )


@router.post(
    "/{store_id}/purge",
    response_model=MemoryPurgeResult,
    summary="Purge expired records",
)
async def purge_store(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    payload: MemoryPurgeRequest,
    request: Request,
) -> MemoryPurgeResult:
    """Delete the conversation records past the store's retention window.

    This is a real, permanent deletion against the telemetry store, so it is
    narrow on purpose: only the agents whose memory policy names this store are
    swept, only whole conversation threads whose last activity is past the
    window are removed, and runs that belong to no thread are never touched.

    Send `dry_run` first: it needs no confirmation and answers with how many
    records would go and which agents hold them. The real call must carry
    `confirm` set to the store's exact name, or it is refused with 422.

    Refused with 412 when the store has no policy, is paused, or is named by no
    agent; with 409 while another purge of the same store is running. A run the
    telemetry store interrupts answers 200 with `partial` set — what it reports
    was deleted and audited — and `capped` means there is more: run it again.
    Requires the operator role.
    """
    return await service.purge_store(session, principal, store_id, payload, request=request)


@router.post(
    "/{store_id}/backup",
    response_model=MemoryBackupResult,
    status_code=status.HTTP_201_CREATED,
    summary="Create a backup",
)
async def backup_store(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    payload: MemoryBackupRequest,
    request: Request,
) -> MemoryBackupResult:
    """Snapshot the store's governed state and record the manifest.

    The manifest holds the retention policy and status — what a restore can put
    back — and a count of the conversation threads the store's agents held at
    that moment. The threads themselves are **not** copied: `threads_captured`
    is always false, `payload_bytes` is always null, and `notice` says so in a
    sentence. A backup is therefore no protection against a purge. The response
    carries the backup id used by restore. Requires the operator role.
    """
    return await service.backup_store(session, principal, store_id, payload, request=request)


@router.get(
    "/{store_id}/backups",
    response_model=Page[MemoryBackupRead],
    summary="List a store's backups",
)
async def list_store_backups(
    principal: CurrentPrincipal, session: Db, store_id: str, params: ListArgs
) -> Page[MemoryBackupRead]:
    """The backup history of one store, newest first."""
    rows, total = await service.list_backups(session, principal, params, store_id=store_id)
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "/{store_id}/restore",
    response_model=MemoryRestoreResult,
    summary="Restore from a backup",
)
async def restore_store(
    principal: CurrentPrincipal,
    session: Db,
    store_id: str,
    payload: MemoryRestoreRequest,
    request: Request,
) -> MemoryRestoreResult:
    """Put the store's governed state back to what one backup captured.

    That is the retention policy and the status, and `fields_restored` reports
    exactly which of them moved. Conversation threads are never restored — no
    backup holds any — so `threads_restored` is always false, `record_count` is
    what the store holds *now*, and `notice` says so in a sentence. Requires the
    admin role.
    """
    return await service.restore_store(session, principal, store_id, payload, request=request)
