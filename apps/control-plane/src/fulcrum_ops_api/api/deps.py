"""Request-scoped dependencies: who is calling, for which workspace, with what role.

Two credential types reach this API:

* a **browser session** — signed token in the ``fo_session`` cookie or an
  ``Authorization: Bearer`` header, issued by ``/auth/login``;
* an **API key** — presented by an SDK or CI job as ``Authorization: Bearer fo_…``
  or in the ``X-Fulcrum-Api-Key`` header.

Both resolve to a :class:`Principal`, which every route handler receives.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import PermissionDenied, Unauthenticated
from ..core.security import (
    SessionTokenError,
    api_secret_matches,
    decode_session_token,
    parse_api_key,
)
from ..db.session import get_session
from ..models.identity import ApiKey, Membership, Role, User, Workspace

SESSION_COOKIE = "fo_session"
API_KEY_HEADER = "X-Fulcrum-Api-Key"
WORKSPACE_HEADER = "X-Fulcrum-Workspace"


@dataclasses.dataclass(frozen=True)
class Principal:
    """The authenticated caller, already scoped to one workspace."""

    workspace_id: str
    workspace_slug: str
    engine_workspace: str
    role: Role
    kind: str  # "user" | "api_key"
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    api_key_id: str | None = None
    api_key_agent_id: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def actor(self) -> str:
        """String written into audit rows."""
        if self.kind == "user":
            return self.email or self.user_id or "unknown"
        return f"api-key:{self.api_key_id}"

    def require(self, role: Role) -> None:
        if not self.role.satisfies(role):
            raise PermissionDenied(
                f"This action requires the {role.value} role; you have {self.role.value}."
            )

    def require_scope(self, scope: str) -> None:
        if self.kind == "api_key" and scope not in self.scopes and "admin" not in self.scopes:
            raise PermissionDenied(f"This API key lacks the '{scope}' scope.")


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def _principal_from_api_key(
    token: str, session: AsyncSession, request: Request
) -> Principal | None:
    parsed = parse_api_key(token)
    if parsed is None:
        return None
    key_id, raw_secret = parsed

    key = (
        await session.execute(select(ApiKey).where(ApiKey.key_id == key_id))
    ).scalar_one_or_none()
    if key is None or not key.is_active:
        raise Unauthenticated("API key is invalid or has been revoked.")
    if not api_secret_matches(raw_secret, key.secret_hash):
        raise Unauthenticated("API key is invalid or has been revoked.")

    workspace = await session.get(Workspace, key.workspace_id)
    if workspace is None or workspace.status != "active":
        raise Unauthenticated("The workspace for this key is not active.")

    # Cheap last-used tracking; a write per request is acceptable at our volume
    # because ingest uses a separate hot path that batches this update.
    key.last_used_at = dt.datetime.now(dt.UTC)
    key.last_used_ip = request.client.host if request.client else None

    return Principal(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        engine_workspace=workspace.engine_workspace,
        role=Role.OPERATOR if "admin" in (key.scopes or []) else Role.MEMBER,
        kind="api_key",
        api_key_id=key.id,
        api_key_agent_id=key.agent_id,
        scopes=tuple(key.scopes or ApiKey.DEFAULT_SCOPES),
        display_name=key.name,
    )


async def _principal_from_session(
    token: str, session: AsyncSession, workspace_override: str | None
) -> Principal:
    try:
        claims = decode_session_token(token)
    except SessionTokenError as exc:
        raise Unauthenticated("Your session has expired. Sign in again.") from exc

    user = await session.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise Unauthenticated("This account is no longer active.")

    workspace_id = claims["ws"]
    if workspace_override:
        # Allow switching workspace within the session, but only to one the
        # user actually belongs to.
        ws = (
            await session.execute(
                select(Workspace).where(Workspace.slug == workspace_override)
            )
        ).scalar_one_or_none()
        if ws is None:
            raise PermissionDenied("Unknown workspace.")
        workspace_id = ws.id

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise PermissionDenied("You do not have access to this workspace.")

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise Unauthenticated("The workspace is not active.")

    return Principal(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        engine_workspace=workspace.engine_workspace,
        role=Role(membership.role),
        kind="user",
        user_id=user.id,
        email=user.email,
        display_name=user.full_name,
    )


async def get_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
    x_fulcrum_api_key: Annotated[str | None, Header()] = None,
    x_fulcrum_workspace: Annotated[str | None, Header()] = None,
) -> Principal:
    api_token = x_fulcrum_api_key or ""
    bearer = _bearer(authorization)

    # An API key and a session token are both bearer-shaped; API keys are
    # recognised by their prefix so one header serves both.
    candidate = api_token or (bearer if bearer and bearer.startswith("fo_") else "")
    if candidate:
        principal = await _principal_from_api_key(candidate, session, request)
        if principal is not None:
            request.state.principal = principal
            return principal

    token = bearer or request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        raise Unauthenticated()

    principal = await _principal_from_session(token, session, x_fulcrum_workspace)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Db = Annotated[AsyncSession, Depends(get_session)]


def require_role(role: Role):
    """Route dependency factory: ``Depends(require_role(Role.ADMIN))``."""

    async def _dep(principal: CurrentPrincipal) -> Principal:
        principal.require(role)
        return principal

    return _dep


def require_scope(scope: str):
    async def _dep(principal: CurrentPrincipal) -> Principal:
        principal.require_scope(scope)
        return principal

    return _dep
