"""Workspace, member and API-key routes.

Three resources hang off one prefix because they are one administrative
surface: the tenant itself, the people in it, and the credentials its agents
authenticate with.

The API-key routes are the ones a customer meets first — minting a key is how
an agent gets connected — so `POST /workspaces/api-keys` returns the plaintext
once, alongside copy-ready setup for each SDK, and every other route in this
module treats that value as gone.

Fixed paths are declared before their parameterised siblings; FastAPI matches
in declaration order, and `/users/summary` would otherwise be read as a user id.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...core.errors import PermissionDenied
from ...models.identity import Role
from ...models.registry import EnvironmentType
from ...schemas.identity import (
    API_KEY_SCOPES,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ApiKeyRevoke,
    ApiKeysSummary,
    ApiKeyStatus,
    ApiKeyUsage,
    MemberCreate,
    MemberRead,
    MemberRef,
    MemberRoleChange,
    MembersSummary,
    MemberStatus,
    MemberUpdate,
    WorkspaceOption,
    WorkspaceRead,
    WorkspaceUpdate,
)
from ...services import identity as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

Params = Annotated[ListParams, Depends(list_params)]

#: Column order of the member CSV, matching the member table left to right.
MEMBER_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("full_name", "Name"),
    ("email", "Email"),
    ("role", "Role"),
    ("status", "Status"),
    ("job_title", "Job Title"),
    ("team", "Team"),
    ("last_login_at", "Last Sign-In"),
    ("joined_at", "Joined"),
    ("password_set", "Password Set"),
    ("id", "User ID"),
]

#: Column order of the API-key CSV. The secret is not among them and never
#: will be — only the display hint, which is not a usable credential.
API_KEY_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("name", "Key Name"),
    ("display_hint", "Key"),
    ("status", "Status"),
    ("environment", "Environment"),
    ("scopes", "Scopes"),
    ("agent_name", "Bound Agent"),
    ("created_at", "Created"),
    ("created_by", "Created By"),
    ("last_used_at", "Last Used"),
    ("last_used_ip", "Last Used IP"),
    ("expires_at", "Expires"),
    ("revoked_at", "Revoked"),
    ("revoked_reason", "Revoked Reason"),
    ("id", "Key ID"),
]


def _csv_response(body: str, stem: str) -> StreamingResponse:
    filename = f"{stem}-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# The workspace itself
# ---------------------------------------------------------------------------


@router.get("", response_model=list[WorkspaceOption], summary="List your workspaces")
async def list_workspaces(principal: CurrentPrincipal, session: Db) -> list[WorkspaceOption]:
    """Every workspace the caller may switch into, with the current one flagged.

    An API-key caller sees exactly one entry: a key is bound to the workspace
    it was minted in and cannot address another.
    """
    return await service.list_my_workspaces(session, principal)


@router.get("/current", response_model=WorkspaceRead, summary="Current workspace")
async def get_current_workspace(principal: CurrentPrincipal, session: Db) -> WorkspaceRead:
    """The tenant this session is scoped to, with its member and key counts.

    Per-person preferences share the same JSON column as the tenant attributes
    and are stripped here, so one member's notification settings never travel
    to another member's browser.
    """
    return await service.get_current_workspace(session, principal)


@router.patch("/current", response_model=WorkspaceRead, summary="Update the workspace")
async def update_current_workspace(
    principal: CurrentPrincipal, session: Db, payload: WorkspaceUpdate, request: Request
) -> WorkspaceRead:
    """Rename the workspace or replace its tenant attributes.

    The slug is immutable: it is baked into every SDK configuration already
    deployed, and changing it would silently break running agents. Requires the
    admin role.
    """
    return await service.update_current_workspace(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Members — fixed paths first
# ---------------------------------------------------------------------------


@router.get("/users/summary", response_model=MembersSummary, summary="Member KPI summary")
async def members_summary(principal: CurrentPrincipal, session: Db) -> MembersSummary:
    """Headcount, activity and the role breakdown, as SQL aggregates.

    Every number is computed over the workspace's memberships, so the cards stay
    correct on a tenant with thousands of people.
    """
    return await service.summarise_members(session, principal)


@router.get("/users/export", summary="Export members as CSV")
async def export_members(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    role: Annotated[str | None, Query(description="Exact role, e.g. 'operator'")] = None,
    member_status: Annotated[
        MemberStatus | None, Query(alias="status", description="Active or Inactive")
    ] = None,
    team: Annotated[str | None, Query(description="Exact team name")] = None,
) -> StreamingResponse:
    """Download the filtered member list as CSV.

    Honours exactly the search, sort and filters the table is showing, so the
    file matches what the administrator sees on screen.
    """
    rows = await service.export_members(
        session, principal, params, role=role, status=member_status, team=team
    )
    body = to_csv([service.member_export_row(row) for row in rows], MEMBER_EXPORT_COLUMNS)
    return _csv_response(body, "workspace-members")


@router.get("/users/me", response_model=MemberRead, summary="Your own membership")
async def get_own_membership(principal: CurrentPrincipal, session: Db) -> MemberRead:
    """Your row in this workspace, including the role you hold here."""
    if principal.kind != "user" or principal.user_id is None:
        # Explicit rather than falling through to a 404 on a null user id.
        raise PermissionDenied("This endpoint is for signed-in users, not API keys.")
    row = await service.get_member(session, principal, principal.user_id)
    return MemberRead.model_validate(row)


@router.get(
    "/users/directory",
    response_model=list[MemberRef],
    summary="Member names for pickers",
)
async def member_directory(principal: CurrentPrincipal, session: Db) -> list[MemberRef]:
    """Names only, for assignment pickers on operator screens.

    The full member list is admin-only so an API key can never enumerate the
    tenant's staff; this endpoint carries just id, name and initials, and is
    open only to signed-in people — the alert-assignment and escalation
    pickers are operator flows, and an operator choosing an assignee needs to
    see who exists.
    """
    if principal.kind != "user":
        raise PermissionDenied("This endpoint is for signed-in users, not API keys.")
    principal.require(Role.OPERATOR)
    rows = await service.member_directory(session, principal)
    return [MemberRef.model_validate(row) for row in rows]


@router.get("/users", response_model=Page[MemberRead], summary="List members")
async def list_members(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    role: Annotated[str | None, Query(description="Exact role, e.g. 'operator'")] = None,
    member_status: Annotated[
        MemberStatus | None, Query(alias="status", description="Active or Inactive")
    ] = None,
    team: Annotated[str | None, Query(description="Exact team name")] = None,
) -> Page[MemberRead]:
    """One page of the workspace's people, ordered by name by default.

    Free-text search covers name, email, job title and team; `sort` accepts any
    column key the table shows.
    """
    rows, total = await service.list_members(
        session, principal, params, role=role, status=member_status, team=team
    )
    return Page[MemberRead].build(
        [MemberRead.model_validate(row) for row in rows], total, params.page, params.page_size
    )


@router.post(
    "/users",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
)
async def create_member(
    principal: CurrentPrincipal, session: Db, payload: MemberCreate, request: Request
) -> MemberRead:
    """Add someone to this workspace.

    An address the platform already knows is joined to the workspace rather
    than duplicated — one person, one account, many memberships — and in that
    case a supplied password is refused, because an existing credential belongs
    to its owner. Requires the admin role; only an owner may grant the owner
    role.
    """
    row = await service.create_member(session, principal, payload, request=request)
    return MemberRead.model_validate(row)


@router.get("/users/{user_id}", response_model=MemberRead, summary="Get a member")
async def get_member(principal: CurrentPrincipal, session: Db, user_id: str) -> MemberRead:
    """One member. Someone in another workspace answers 404, not 403."""
    row = await service.get_member(session, principal, user_id)
    return MemberRead.model_validate(row)


@router.patch("/users/{user_id}", response_model=MemberRead, summary="Update a member")
async def update_member(
    principal: CurrentPrincipal,
    session: Db,
    user_id: str,
    payload: MemberUpdate,
    request: Request,
) -> MemberRead:
    """Edit a member's record, their role, or their ability to sign in.

    Deactivation blocks the account platform-wide, so it is refused for the last
    owner and for yourself. Only an owner may modify another owner. Requires the
    admin role.
    """
    row = await service.update_member(session, principal, user_id, payload, request=request)
    return MemberRead.model_validate(row)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
async def remove_member(
    principal: CurrentPrincipal, session: Db, user_id: str, request: Request
) -> None:
    """Remove someone from this workspace.

    The membership goes; the account survives, because it may belong to other
    workspaces and its audit history has to stay attributable. Refused for the
    last owner and for your own membership. Requires the admin role.
    """
    await service.remove_member(session, principal, user_id, request=request)


@router.post(
    "/users/{user_id}/role",
    response_model=MemberRead,
    summary="Change a member's role",
)
async def change_member_role(
    principal: CurrentPrincipal,
    session: Db,
    user_id: str,
    payload: MemberRoleChange,
    request: Request,
) -> MemberRead:
    """Move one member to another role, recording the reason on the audit row.

    Demoting the only owner is refused, and only an owner may hand the owner
    role out. Requires the admin role.
    """
    row = await service.change_member_role(session, principal, user_id, payload, request=request)
    return MemberRead.model_validate(row)


# ---------------------------------------------------------------------------
# API keys — fixed paths first
# ---------------------------------------------------------------------------


@router.get(
    "/api-keys/summary",
    response_model=ApiKeysSummary,
    summary="API key KPI summary",
)
async def api_keys_summary(principal: CurrentPrincipal, session: Db) -> ApiKeysSummary:
    """Live, revoked, expired and expiring-soon counts, plus usage recency.

    `expiring_soon` is the card that matters operationally: it is the list of
    credentials that will take an agent down if nobody rotates them.
    """
    return await service.summarise_api_keys(session, principal)


@router.get("/api-keys/export", summary="Export API keys as CSV")
async def export_api_keys(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    key_status: Annotated[
        ApiKeyStatus | None, Query(alias="status", description="Active, Expired or Revoked")
    ] = None,
    environment: Annotated[EnvironmentType | None, Query()] = None,
    scope: Annotated[
        str | None, Query(description=f"One of {', '.join(API_KEY_SCOPES)}")
    ] = None,
    agent_id: Annotated[str | None, Query(description="Keys bound to one agent")] = None,
) -> StreamingResponse:
    """Download the filtered key inventory as CSV.

    Carries the display hint, never the secret: the file is an inventory for an
    audit, not a set of working credentials.
    """
    keys = await service.export_api_keys(
        session,
        principal,
        params,
        status=key_status,
        environment=environment.value if environment else None,
        scope=scope,
        agent_id=agent_id,
    )
    body = to_csv([service.api_key_export_row(key) for key in keys], API_KEY_EXPORT_COLUMNS)
    return _csv_response(body, "api-keys")


@router.get("/api-keys", response_model=Page[ApiKeyRead], summary="List API keys")
async def list_api_keys(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    key_status: Annotated[
        ApiKeyStatus | None, Query(alias="status", description="Active, Expired or Revoked")
    ] = None,
    environment: Annotated[EnvironmentType | None, Query()] = None,
    scope: Annotated[
        str | None, Query(description=f"One of {', '.join(API_KEY_SCOPES)}")
    ] = None,
    agent_id: Annotated[str | None, Query(description="Keys bound to one agent")] = None,
) -> Page[ApiKeyRead]:
    """One page of the workspace's keys, newest first.

    Status is derived, not stored — a key with an elapsed `expires_at` reads as
    Expired without a sweep having to touch the row — so filtering on it is a
    condition over `revoked_at` and `expires_at` rather than a column match.
    Requires the operator role.
    """
    keys, total = await service.list_api_keys(
        session,
        principal,
        params,
        status=key_status,
        environment=environment.value if environment else None,
        scope=scope,
        agent_id=agent_id,
    )
    return Page[ApiKeyRead].build(keys, total, params.page, params.page_size)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key",
)
async def create_api_key(
    principal: CurrentPrincipal, session: Db, payload: ApiKeyCreate, request: Request
) -> ApiKeyCreated:
    """Mint a key and return its plaintext exactly once.

    Only a hash, a key id and a display hint are stored, so this response is
    the only opportunity to copy the key — the `snippets` array carries it
    already pasted into the environment, Python, TypeScript and curl setups a
    customer needs to connect an agent. Binding to an agent is verified against
    this workspace. Requires the admin role.
    """
    return await service.create_api_key(session, principal, payload, request=request)


@router.get("/api-keys/{key_id}", response_model=ApiKeyRead, summary="Get an API key")
async def get_api_key(principal: CurrentPrincipal, session: Db, key_id: str) -> ApiKeyRead:
    """One key's metadata. A key in another workspace answers 404, not 403."""
    return await service.get_api_key(session, principal, key_id)


@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=ActionResult,
    summary="Revoke an API key",
)
async def revoke_api_key(
    principal: CurrentPrincipal,
    session: Db,
    key_id: str,
    payload: ApiKeyRevoke,
    request: Request,
) -> ActionResult:
    """Retire a key immediately; every request presenting it fails from now on.

    Revocation is preferred over deletion because the row keeps the audit trail
    attributable to a name rather than to an opaque id. Requires the admin role.
    """
    key = await service.revoke_api_key(
        session, principal, key_id, reason=payload.reason, request=request
    )
    return ActionResult(
        message=f"'{key.name}' revoked. Agents using it will fail authentication.",
        entity_id=key.id,
        data=key.model_dump(mode="json"),
    )


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an API key",
)
async def delete_api_key(
    principal: CurrentPrincipal, session: Db, key_id: str, request: Request
) -> None:
    """Delete a key row outright.

    Refused with 412 while the key is still live: a credential that is
    authenticating agents right now must not disappear without the decision to
    stop it being recorded first. Requires the admin role.
    """
    await service.delete_api_key(session, principal, key_id, request=request)


@router.get(
    "/api-keys/{key_id}/usage",
    response_model=ApiKeyUsage,
    summary="API key usage",
)
async def api_key_usage(
    principal: CurrentPrincipal,
    session: Db,
    key_id: str,
    window_days: Annotated[int, Query(ge=1, le=365, description="Reporting window")] = 30,
) -> ApiKeyUsage:
    """What this key has actually done, read back from the audit trail.

    Every audited operation performed with a key is attributed to it, so the
    call counts, the per-action breakdown and the ingest volume are all measured
    rather than modelled. Requires the operator role.
    """
    return await service.api_key_usage(session, principal, key_id, window_days=window_days)
