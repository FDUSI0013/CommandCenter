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
import logging
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import ratelimit
from ..core.config import settings
from ..core.errors import PermissionDenied, Unauthenticated
from ..core.security import (
    SessionTokenError,
    api_secret_matches,
    decode_session_token,
    parse_api_key,
)
from ..db.base import stamp
from ..db.session import get_session, get_sessionmaker
from ..models.identity import ApiKey, Membership, Role, User, Workspace

log = logging.getLogger(__name__)

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
    #: The session token's ``iat``, carried so work that outlives its request can
    #: re-check it. Every ordinary request is authenticated once and answered in
    #: milliseconds, but a server-sent-event stream holds one Principal for up to
    #: half an hour; without this it could not tell that the password behind that
    #: token had since changed. None for an API key, which has no session.
    token_issued_at: float | None = None

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


#: An API key's ``last_used_at`` is not rewritten more often than this. The
#: console shows it to the minute at best ("2 minutes ago").
KEY_TOUCH_INTERVAL = dt.timedelta(seconds=60)


async def _note_key_used(session: AsyncSession, key: ApiKey, request: Request) -> None:
    """Record that the key was presented -- at most once a minute per key.

    This used to be two attribute writes on every authenticated request, under a
    comment saying ingest had a separate path that batched them. It never had:
    ingest resolves its caller through this same dependency. So every batch,
    every ``GET /ingest/config`` poll and every SDK prompt read was a write
    transaction ending in ``UPDATE api_keys`` -- some 17,000 a day from one
    agent's five-second poller -- and a fleet flushing in parallel with one key
    queued on that row's lock from the statement until the commit. It also moved
    ``updated_at`` each time, which is a statement that somebody edited the key.

    A key that was noted within the last minute is left alone, so an otherwise
    read-only request stays read-only. The address is not a reason to write
    sooner: a fleet behind several addresses would flip it on every request and
    put the write straight back.

    The note is committed here rather than with the request. Left to the
    request's own commit, the row lock it takes is held for as long as the
    handler runs -- an ingest batch waiting on the engine included -- and every
    other request presenting the same key in that time still reads the old
    ``last_used_at``, issues the same UPDATE and queues behind it: a fleet
    sharing one key serialised once a minute. It also rolled back with a request
    that was refused, so a key being tried against doors closed to it read
    "never used". Nothing else is pending on the session this early -- the
    caller is still being identified -- and ``expire_on_commit`` is off, so the
    rows loaded for the principal stay usable. A session-token request writes
    nothing here and is deliberately not committed: that would hand its
    connection back only to check one out again for the handler's first
    statement, two round trips on every console request for no lock released.
    """
    now = dt.datetime.now(dt.UTC)
    if key.last_used_at is not None and now - key.last_used_at < KEY_TOUCH_INTERVAL:
        return
    await stamp(
        session,
        [key],
        last_used_at=now,
        last_used_ip=request.client.host if request.client else None,
    )
    await session.commit()


#: Routes any live key may call whatever it carries: they say who the key is and
#: nothing about the workspace, and an SDK calls them to verify its credential.
KEY_SELF_ROUTES = frozenset({"/auth/session", "/auth/me", "/auth/status"})


def _scope_needed(request: Request) -> str | None:
    """The scope an API key must carry for this request; ``None`` when any key may.

    Scopes used to be checked in two places, both of them ``ingest``. Nothing
    ever asked for ``read``, so it opened nothing and its absence closed
    nothing: a key minted with ``ingest`` alone -- the least-privilege credential
    that gets baked into a customer-side agent container -- still authenticated
    as a member and could list every agent's runs, read their prompts and
    outputs, and make member-level writes. The mint dialog says otherwise
    ("ingest writes telemetry, read queries it"), and an operator is entitled to
    believe it.

    The rule is stated once, here, so that a route added later is covered
    without its author remembering to ask. Reporting -- the ingest API, the
    OpenTelemetry receiver and the SDK's feedback submission -- needs
    ``ingest``. Everything else needs ``read``, writes included: the role a key
    maps to still decides *which* writes, and the SDK's dataset and evaluation
    calls are made with the default ``ingest`` + ``read`` key, so asking for
    more than ``read`` there would break the documented set-up. ``admin``
    implies both, as it always has.
    """
    path = request.url.path
    prefix = settings.api_prefix.rstrip("/")
    relative = path[len(prefix) :] if path.startswith(f"{prefix}/") else path
    relative = relative.rstrip("/")

    if relative in KEY_SELF_ROUTES:
        return None
    if relative == "/ingest" or relative.startswith("/ingest/") or relative == "/v1/traces":
        return "ingest"
    if request.method == "POST" and relative == "/feedback":
        return "ingest"
    return "read"


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

    await _note_key_used(session, key, request)

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

    # The account, the workspace and the membership that joins them, in one
    # statement. They were three round trips in a row (four with the header
    # override), paid before any handler work by every console request -- and a
    # screen opens with six or eight of those in parallel. The header override
    # allows switching workspace within the session, but only to one the user
    # actually belongs to, which the membership join decides either way.
    wanted = (
        Workspace.slug == workspace_override
        if workspace_override
        else Workspace.id == claims["ws"]
    )
    row = (
        await session.execute(
            select(User, Workspace, Membership)
            .select_from(User)
            .outerjoin(Workspace, wanted)
            .outerjoin(
                Membership,
                and_(Membership.user_id == User.id, Membership.workspace_id == Workspace.id),
            )
            .where(User.id == claims["sub"])
        )
    ).first()
    user, workspace, membership = row if row is not None else (None, None, None)

    if user is None or not user.is_active:
        raise Unauthenticated("This account is no longer active.")
    # A token minted before the password changed is no longer proof of anything:
    # whoever held the old credential could have minted it. Both sides carry
    # sub-second precision, so the comparison is exact rather than "some time
    # that second" -- and the token the change itself re-issues is minted after
    # the stamp, which is what keeps that caller signed in.
    changed = user.credentials_changed_at
    if changed is not None and claims.get("iat", 0) < changed.timestamp():
        raise Unauthenticated(
            "Your password changed, so this session ended. Sign in again."
        )
    if workspace is None and workspace_override:
        raise PermissionDenied("Unknown workspace.")
    if workspace is None or membership is None:
        raise PermissionDenied("You do not have access to this workspace.")
    if workspace.status != "active":
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
        token_issued_at=claims.get("iat"),
    )


async def session_revoked(principal: Principal) -> bool:
    """Has this caller's session ended since it was authenticated?

    For long-lived work that holds one Principal across many minutes -- the two
    server-sent-event streams, which run for up to half an hour and fifteen
    minutes respectively. An ordinary request never needs this: it is
    authenticated and answered before anything could change.

    It re-reads the three things that can end a session while it is open:

    * the account was deactivated,
    * the password was changed out from under a token minted before it,
    * the membership that put this caller in this workspace was removed.

    The third matters more than it looks. ``remove_member`` deletes the
    membership and leaves ``is_active`` alone -- correctly, because the account
    may belong to other workspaces -- so a removed person's ordinary requests
    start answering 403 while a stream opened a minute earlier would go on
    handing them this workspace's runs. Removal is exactly the instruction
    "stop showing them our data".

    A role CHANGE is not checked. Both streams carry one workspace's own rows
    and expose nothing that a lower role could not already read, so a demotion
    mid-stream discloses nothing the moment before it did not.

    Opens its own short-lived session, because the request's was committed and
    handed back before the stream started. Answers False on any database
    trouble: a stream is not the place to decide that an unreachable database
    means the caller is an impostor. That is one recheck interval of grace, not
    an open door -- the next interval asks again.
    """
    if principal.kind != "user" or principal.user_id is None:
        return False
    try:
        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(User, Membership)
                    .select_from(User)
                    .outerjoin(
                        Membership,
                        and_(
                            Membership.user_id == User.id,
                            Membership.workspace_id == principal.workspace_id,
                        ),
                    )
                    .where(User.id == principal.user_id)
                )
            ).first()
            user, membership = row if row is not None else (None, None)
            if user is None or not user.is_active or membership is None:
                return True
            changed = user.credentials_changed_at
            return changed is not None and (principal.token_issued_at or 0) < changed.timestamp()
    except Exception:  # noqa: BLE001 -- see the docstring: never fail closed here
        log.warning("could not re-check a streaming session; leaving it open", exc_info=True)
        return False


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
            ratelimit.check(principal.api_key_id or "api-key", request.url.path)
            # After the limiter, so a key hammering a door that is closed to it
            # is still counted.
            needed = _scope_needed(request)
            if needed is not None:
                principal.require_scope(needed)
            return principal

    token = bearer or request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        # The commonest 401 by far: the cookie aged out or was cleared, so the
        # browser sends nothing. The console shows this sentence on its sign-in
        # gate, so it has to read like one -- the generic
        # "Valid credentials are required." is an API answer, not something to
        # put in front of a person who was working a moment ago.
        raise Unauthenticated("Your session has ended. Sign in to continue.")

    principal = await _principal_from_session(token, session, x_fulcrum_workspace)
    request.state.principal = principal
    ratelimit.check(principal.user_id or "session", request.url.path)
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
