"""Identity, session and API-key business logic.

Everything that decides *who is calling* lives here: password verification and
lockout, session issuance, workspace switching, membership management, and the
minting and retirement of the API keys an agent authenticates with.

Four invariants hold in every function below.

* **Enumeration is impossible.** A sign-in failure returns one message and one
  code whatever went wrong — unknown address, wrong password, deactivated
  account, no membership — and the unknown-address path still pays the cost of
  a password verification so the timing does not answer the question either.
* **The lockout counter survives a rejected request.** The failure path commits
  before raising, because the session dependency rolls back on an exception and
  a counter that rolls back is not a counter.
* **Every statement filters on the caller's workspace.** A member or key in
  another tenant is indistinguishable from one that never existed.
* **A secret is written once and read never.** Passwords are stored as argon2
  hashes, key secrets as SHA-256 digests, and no read path in this module can
  return either.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import shlex
import time
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, Text, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    Unauthenticated,
    ValidationFailed,
)
from ..core.security import (
    MintedApiKey,
    api_secret_matches,
    hash_api_secret,
    hash_password,
    issue_session_token,
    mint_api_key,
    needs_rehash,
    off_loop,
    parse_api_key,
    verify_password,
)
from ..models.governance import AuditEvent
from ..models.identity import ApiKey, Membership, Role, User, Workspace
from ..models.licensing import (
    Entitlement,
    EntitlementValueType,
    LicenseStatus,
    SeatAssignment,
    TenantLicense,
)
from ..models.registry import Agent
from ..schemas.identity import (
    EXPIRING_SOON_DAYS,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ApiKeySnippet,
    ApiKeysSummary,
    ApiKeyStatus,
    ApiKeyUsage,
    ApiKeyUsageAction,
    ApiKeyUsageDay,
    EnvironmentCount,
    MemberCreate,
    MemberRoleChange,
    MembersSummary,
    MemberStatus,
    MemberUpdate,
    PreferencesUpdate,
    ProfileUpdate,
    RoleCount,
    SessionApiKey,
    SessionEnvelope,
    SessionUser,
    SessionWorkspace,
    UserPreferences,
    UserProfile,
    WorkspaceOption,
    WorkspaceRead,
    WorkspaceUpdate,
)
from . import audit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_SCREEN_SIGN_IN: Final[str] = "Sign In"
SOURCE_SCREEN_ACCOUNT: Final[str] = "Account Menu"
SOURCE_SCREEN_MEMBERS: Final[str] = "Workspace Settings"
SOURCE_SCREEN_KEYS: Final[str] = "API Access Tokens"

ENTITY_USER: Final[str] = "user"
ENTITY_MEMBERSHIP: Final[str] = "membership"
ENTITY_API_KEY: Final[str] = "api_key"
ENTITY_WORKSPACE: Final[str] = "workspace"

#: Consecutive failures before the account is locked, and for how long. Five is
#: forgiving enough for a mistyped password and short enough that an attacker
#: gets roughly twenty guesses an hour.
MAX_FAILED_LOGINS: Final[int] = 5
LOCKOUT_MINUTES: Final[int] = 15

#: One message, one code, for every possible sign-in failure.
SIGN_IN_FAILED: Final[str] = "Email or password is incorrect."

#: Exports stream the filtered table, not the whole tenant.
EXPORT_LIMIT: Final[int] = 5_000

#: Window the key-usage panel reports over, and the cap on how many audit rows
#: it reads to build the daily series.
USAGE_WINDOW_DAYS: Final[int] = 30
USAGE_DETAIL_LIMIT: Final[int] = 5_000

#: Licenses whose entitlements are in force.
LIVE_LICENSE_STATUSES: Final[tuple[str, ...]] = (
    LicenseStatus.ACTIVE.value,
    LicenseStatus.TRIAL.value,
    LicenseStatus.EXPIRING_SOON.value,
)

#: Where per-person preferences live inside ``Workspace.settings``. The column
#: is the only JSON bag on either table, so preferences are namespaced under
#: one reserved key and stripped from every workspace read and write.
PREFERENCES_KEY: Final[str] = "member_preferences"

MEMBER_SORTABLE: Final[dict[str, Any]] = {
    "full_name": User.full_name,
    "email": User.email,
    "job_title": User.job_title,
    "team": User.team,
    "role": Membership.role,
    "status": User.is_active,
    "last_login_at": User.last_login_at,
    "joined_at": Membership.created_at,
}

API_KEY_SORTABLE: Final[dict[str, Any]] = {
    "name": ApiKey.name,
    "environment": ApiKey.environment,
    "created_at": ApiKey.created_at,
    "created_by": ApiKey.created_by_user_id,
    "last_used_at": ApiKey.last_used_at,
    "expires_at": ApiKey.expires_at,
    "revoked_at": ApiKey.revoked_at,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Treat a naive instant as UTC; SQLite hands some drivers tz-less values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


@functools.lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """A real argon2 hash of a throwaway value.

    Verified against when the address is unknown so that path costs the same as
    a genuine rejection. Built lazily: hashing at import time would slow every
    process that merely imports this module.
    """
    return hash_password(f"decoy-{settings.secret_key}-{settings.service_name}")


def _verify_against_decoy(password: str) -> bool:
    """The unknown-address verification, as one unit of work for :func:`off_loop`.

    Building the decoy is itself a hash, so it belongs on the worker thread too.
    """
    return verify_password(password, _decoy_hash())


def _parse_role(value: str) -> Role:
    try:
        return Role(value.strip().lower())
    except ValueError as exc:
        raise ValidationFailed(
            f"'{value}' is not a role.",
            details={"roles": [role.value for role in Role]},
        ) from exc


def _mint_usable_key() -> MintedApiKey:
    """Mint a key and prove the request path can read it back.

    The token is ``fo_<env>_<id>_<secret>`` and is split on ``_``, so the
    encoding of the two fields has to be delimiter-free — it is (base32), but
    that is an invariant two modules share rather than one enforces. This asserts
    it at the only moment it can still be corrected cheaply: before the hash is
    stored and the plaintext is handed to a customer who will wire it into a
    deployment. A failure here means the key format and the parser have drifted
    apart, which is a defect to fix, not a condition to retry.
    """
    minted = mint_api_key()
    parsed = parse_api_key(minted.token)
    if parsed is not None:
        key_id, raw_secret = parsed
        if key_id == minted.key_id and api_secret_matches(raw_secret, minted.secret_hash):
            return minted
    raise PreconditionFailed(
        "Could not mint a usable API key. The key format and the key parser disagree."
    )


def _key_status(key: ApiKey) -> ApiKeyStatus:
    if key.revoked_at is not None:
        return ApiKeyStatus.REVOKED
    expires_at = _as_utc(key.expires_at)
    if expires_at is not None and expires_at < _now():
        return ApiKeyStatus.EXPIRED
    return ApiKeyStatus.ACTIVE


def _expires_in_days(key: ApiKey) -> int | None:
    expires_at = _as_utc(key.expires_at)
    if expires_at is None:
        return None
    return (expires_at - _now()).days


# ---------------------------------------------------------------------------
# Preferences, stored per person per workspace
# ---------------------------------------------------------------------------


def _read_preferences(workspace: Workspace, user_id: str | None) -> UserPreferences:
    """Load one person's preferences, falling back to the documented defaults."""
    if user_id is None:
        return UserPreferences()
    stored = (workspace.settings or {}).get(PREFERENCES_KEY, {})
    raw = stored.get(user_id) if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        return UserPreferences()
    # A preference document written by an older release may be missing fields
    # this one knows about; validating with defaults fills them rather than
    # failing the sign-in that asked for them.
    return UserPreferences.model_validate(raw)


def _write_preferences(workspace: Workspace, user_id: str, preferences: UserPreferences) -> None:
    """Persist one person's preferences without touching anyone else's.

    ``settings`` is a plain JSON column, so the whole dict is reassigned:
    mutating it in place leaves SQLAlchemy with nothing to flush.
    """
    settings_bag = dict(workspace.settings or {})
    existing = settings_bag.get(PREFERENCES_KEY)
    people = dict(existing) if isinstance(existing, dict) else {}
    people[user_id] = preferences.model_dump(mode="json")
    settings_bag[PREFERENCES_KEY] = people
    workspace.settings = settings_bag


def _public_settings(workspace: Workspace) -> dict[str, Any]:
    """Tenant attributes with the per-person preference bag removed."""
    return {k: v for k, v in (workspace.settings or {}).items() if k != PREFERENCES_KEY}


# ---------------------------------------------------------------------------
# Session envelope
# ---------------------------------------------------------------------------


async def _entitlements(session: AsyncSession, workspace_id: str) -> dict[str, Any]:
    """Resolve the workspace's in-force entitlements to a flat lookup.

    The console gates features on these, so an unlicensed workspace simply gets
    an empty map rather than an invented allowance.
    """
    license_id = (
        await session.execute(
            select(TenantLicense.id)
            .where(
                TenantLicense.tenant_workspace_id == workspace_id,
                TenantLicense.status.in_(LIVE_LICENSE_STATUSES),
            )
            .order_by(TenantLicense.starts_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if license_id is None:
        return {}

    rows = (
        await session.execute(
            select(
                Entitlement.key,
                Entitlement.value_type,
                Entitlement.bool_value,
                Entitlement.int_value,
                Entitlement.string_value,
            ).where(Entitlement.license_id == license_id)
        )
    ).all()

    resolved: dict[str, Any] = {}
    for key, value_type, bool_value, int_value, string_value in rows:
        if value_type == EntitlementValueType.INT.value:
            resolved[key] = int_value
        elif value_type == EntitlementValueType.STRING.value:
            resolved[key] = string_value
        else:
            resolved[key] = bool_value
    return resolved


async def list_workspace_options(
    session: AsyncSession, user_id: str, *, current_workspace_id: str | None = None
) -> list[WorkspaceOption]:
    """Every workspace this person may switch into, ordered by name."""
    rows = (
        await session.execute(
            select(Workspace.id, Workspace.name, Workspace.slug, Membership.role)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user_id, Workspace.status == "active")
            .order_by(Workspace.name.asc())
        )
    ).all()
    return [
        WorkspaceOption(
            id=workspace_id,
            name=name,
            slug=slug,
            role=role,
            is_current=workspace_id == current_workspace_id,
        )
        for workspace_id, name, slug, role in rows
    ]


async def build_envelope(
    session: AsyncSession,
    *,
    workspace: Workspace,
    role: Role,
    user: User | None = None,
    key: ApiKey | None = None,
    issued_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
) -> SessionEnvelope:
    """Assemble what the console shell renders after a successful sign-in."""
    return SessionEnvelope(
        user=(
            SessionUser(
                id=user.id,
                email=user.email,
                full_name=user.full_name,
                initials=user.initials,
                job_title=user.job_title,
                team=user.team,
                is_active=user.is_active,
                last_login_at=_as_utc(user.last_login_at),
            )
            if user is not None
            else None
        ),
        api_key=(
            SessionApiKey(
                id=key.id,
                name=key.name,
                display_hint=key.display_hint,
                scopes=list(key.scopes or []),
                environment=key.environment,
                agent_id=key.agent_id,
            )
            if key is not None
            else None
        ),
        workspace=SessionWorkspace(
            id=workspace.id, name=workspace.name, slug=workspace.slug, status=workspace.status
        ),
        role=role.value,
        workspaces=(
            await list_workspace_options(session, user.id, current_workspace_id=workspace.id)
            if user is not None
            else []
        ),
        entitlements=await _entitlements(session, workspace.id),
        preferences=_read_preferences(workspace, user.id if user else None),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def issue_session(*, user: User, workspace: Workspace, role: Role) -> tuple[str, dt.datetime]:
    """Mint a session token for one user in one workspace, with its expiry."""
    token = issue_session_token(user_id=user.id, workspace_id=workspace.id, role=role.value)
    expires_at = _now() + dt.timedelta(minutes=settings.session_ttl_minutes)
    return token, expires_at


def _principal_for(user: User, workspace: Workspace, role: Role) -> Principal:
    """The principal a just-authenticated user acts as, for the audit row."""
    return Principal(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        engine_workspace=workspace.engine_workspace,
        role=role,
        kind="user",
        user_id=user.id,
        email=user.email,
        display_name=user.full_name,
    )


# ---------------------------------------------------------------------------
# Sign in / sign out
# ---------------------------------------------------------------------------


async def _primary_membership(
    session: AsyncSession, user_id: str
) -> tuple[Membership, Workspace] | None:
    """The workspace a session lands in by default: the oldest active one."""
    row = (
        await session.execute(
            select(Membership, Workspace)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(Membership.user_id == user_id, Workspace.status == "active")
            .order_by(Membership.created_at.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


#: Sign-in attempts inside the current minute: ``key -> (window start, count)``.
#: Process memory, per worker, exactly as :mod:`core.ratelimit` keeps its own --
#: and for the reason given there. That limiter cannot do this job: it is keyed
#: by the credential, so it only ever runs *after* authentication has succeeded,
#: and sign-in is the one route where the expensive part comes before.
_sign_in_windows: dict[str, tuple[int, int]] = {}
_SIGN_IN_PRUNE_ABOVE: Final[int] = 10_000


def _throttle_sign_in(email: str, request: Request | None) -> None:
    """Count one sign-in attempt; refuse it, unverified, when the minute is full.

    Every attempt costs an argon2 verification -- for an unknown address too,
    which is what keeps the route from enumerating accounts -- and nothing
    bounded how many could be asked for. The lockout does not: it counts
    failures against an account that exists, so a loop over made-up addresses
    never trips it. A misconfigured client or a credential-stuffing run could
    keep every worker hashing, with no credential at all.

    Two windows, both checked before either is counted. The one per account
    is what a person mistyping meets, and sits above the lockout's five so that
    it never pre-empts the more useful message. The one per address is the one a
    spray across many accounts meets; it is an order taller because an office
    signs in from one address. The answer is the same 429 whether or not the
    account exists.
    """
    if not settings.rate_limit_enabled:
        return
    now = int(time.time())
    minute = now - (now % 60)
    buckets = [(f"account:{email.strip().lower()}", settings.rate_limit_login_per_minute)]
    if request is not None and request.client is not None:
        buckets.append(
            (f"address:{request.client.host}", settings.rate_limit_login_per_address_per_minute)
        )

    counts: dict[str, int] = {}
    for key, limit in buckets:
        if limit <= 0:
            continue
        window_start, count = _sign_in_windows.get(key, (minute, 0))
        if window_start != minute:
            count = 0
        if count >= limit:
            raise RateLimited(
                retry_after_seconds=max(1, minute + 60 - now),
                message="Too many sign-in attempts. Wait a minute and try again.",
                details={"limit_per_minute": limit, "bucket": "sign_in"},
            )
        counts[key] = count + 1

    if len(_sign_in_windows) > _SIGN_IN_PRUNE_ABOVE:
        for stale_key, (start, _) in list(_sign_in_windows.items()):
            if start != minute:
                _sign_in_windows.pop(stale_key, None)
    for key, count in counts.items():
        _sign_in_windows[key] = (minute, count)


def reset_sign_in_windows() -> None:
    """Forget every sign-in window. Tests only."""
    _sign_in_windows.clear()


async def _reject_sign_in(
    session: AsyncSession,
    *,
    user: User | None = None,
    reason: str,
    request: Request | None = None,
) -> Unauthenticated:
    """Record the failure durably, then hand back the error to raise.

    Committing here is deliberate: the caller raises immediately afterwards and
    the request-scoped session rolls back on the way out, which would otherwise
    discard the very counter the lockout depends on.
    """
    if user is not None:
        user.failed_login_count = (user.failed_login_count or 0) + 1
        locked = user.failed_login_count >= MAX_FAILED_LOGINS
        if locked:
            user.locked_until = _now() + dt.timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0

        primary = await _primary_membership(session, user.id)
        if primary is not None:
            membership, workspace = primary
            await audit.record(
                session,
                principal=_principal_for(user, workspace, Role(membership.role)),
                action="auth.login_locked" if locked else "auth.login_failed",
                entity_type=ENTITY_USER,
                entity_id=user.id,
                entity_label=user.email,
                source_screen=SOURCE_SCREEN_SIGN_IN,
                detail=(
                    f"Account locked for {LOCKOUT_MINUTES} minutes after "
                    f"{MAX_FAILED_LOGINS} failed attempts"
                    if locked
                    else f"Sign-in rejected: {reason}"
                ),
                metadata={"reason": reason},
                request=request,
            )
    await session.commit()
    return Unauthenticated(SIGN_IN_FAILED)


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    request: Request | None = None,
) -> tuple[str, dt.datetime, SessionEnvelope]:
    """Verify credentials and open a session, or fail without saying why.

    On success the failure counter and any lock are cleared, ``last_login_at``
    is stamped, and a password stored under superseded hash parameters is
    upgraded in place — the plaintext is available exactly here and nowhere
    else, so this is the only moment a rehash is possible.
    """
    _throttle_sign_in(email, request)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if user is None or not user.password_hash:
        # Pay the verification cost anyway: a fast "no" for unknown addresses
        # would enumerate the tenant by stopwatch.
        await off_loop(_verify_against_decoy, password)
        raise await _reject_sign_in(session, reason="unknown or password-less account")

    password_ok = await off_loop(verify_password, password, user.password_hash)

    locked_until = _as_utc(user.locked_until)
    if locked_until is not None and locked_until > _now():
        if not password_ok:
            raise await _reject_sign_in(session, user=user, reason="locked", request=request)
        # The caller already holds the password, so naming the lock reveals
        # nothing they could not confirm by waiting it out.
        minutes = max(1, int((locked_until - _now()).total_seconds() // 60) + 1)
        await session.commit()
        raise Unauthenticated(
            "This account is locked after repeated failed sign-ins. "
            f"Try again in {minutes} minute(s).",
            code="account_locked",
        )

    if not password_ok:
        raise await _reject_sign_in(session, user=user, reason="bad password", request=request)
    if not user.is_active:
        raise await _reject_sign_in(session, user=user, reason="inactive account", request=request)

    primary = await _primary_membership(session, user.id)
    if primary is None:
        raise await _reject_sign_in(session, user=user, reason="no workspace", request=request)
    membership, workspace = primary
    role = Role(membership.role)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()
    if needs_rehash(user.password_hash):
        user.password_hash = await off_loop(hash_password, password)

    token, expires_at = issue_session(user=user, workspace=workspace, role=role)
    await audit.record(
        session,
        principal=_principal_for(user, workspace, role),
        action="auth.login",
        entity_type=ENTITY_USER,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_SIGN_IN,
        detail=f"Signed in to {workspace.name} as {role.value}",
        request=request,
    )
    await session.flush()

    envelope = await build_envelope(
        session,
        workspace=workspace,
        role=role,
        user=user,
        issued_at=_now(),
        expires_at=expires_at,
    )
    return token, expires_at, envelope


async def logout(
    session: AsyncSession, principal: Principal | None, *, request: Request | None = None
) -> None:
    """Record the sign-out. The cookie is cleared by the route either way."""
    if principal is None or principal.kind != "user":
        return
    await audit.record(
        session,
        principal=principal,
        action="auth.logout",
        entity_type=ENTITY_USER,
        entity_id=principal.user_id,
        entity_label=principal.email,
        source_screen=SOURCE_SCREEN_ACCOUNT,
        detail="Signed out",
        request=request,
    )
    await session.flush()


async def current_session(session: AsyncSession, principal: Principal) -> SessionEnvelope:
    """The envelope for whoever is calling right now."""
    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise Unauthenticated("The workspace is no longer available.")

    if principal.kind == "api_key":
        key = await session.get(ApiKey, principal.api_key_id) if principal.api_key_id else None
        if key is None:
            raise Unauthenticated("This API key is no longer valid.")
        return await build_envelope(session, workspace=workspace, role=principal.role, key=key)

    user = await session.get(User, principal.user_id) if principal.user_id else None
    if user is None:
        raise Unauthenticated("This account is no longer available.")
    return await build_envelope(session, workspace=workspace, role=principal.role, user=user)


async def switch_workspace(
    session: AsyncSession,
    principal: Principal,
    target: str,
    *,
    request: Request | None = None,
) -> tuple[str, dt.datetime, SessionEnvelope]:
    """Re-issue the session against another workspace the user belongs to.

    A slug or an id is accepted because the switcher sends whichever it holds.
    A workspace the user is not a member of answers 404: the switcher must not
    become a directory of tenants.
    """
    if principal.kind != "user" or principal.user_id is None:
        raise PermissionDenied("Only a signed-in user can switch workspace.")

    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise Unauthenticated("This account is no longer active.")

    row = (
        await session.execute(
            select(Workspace, Membership)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(
                Membership.user_id == user.id,
                or_(Workspace.slug == target, Workspace.id == target),
            )
        )
    ).first()
    if row is None:
        raise NotFound(f"You are not a member of workspace '{target}'.")
    workspace, membership = row
    if workspace.status != "active":
        raise PreconditionFailed(f"Workspace '{workspace.name}' is not active.")

    role = Role(membership.role)
    token, expires_at = issue_session(user=user, workspace=workspace, role=role)
    await audit.record(
        session,
        principal=_principal_for(user, workspace, role),
        action="auth.workspace_switched",
        entity_type=ENTITY_WORKSPACE,
        entity_id=workspace.id,
        entity_label=workspace.name,
        source_screen=SOURCE_SCREEN_ACCOUNT,
        detail=f"Session moved from {principal.workspace_slug} to {workspace.slug}",
        request=request,
    )
    await session.flush()

    envelope = await build_envelope(
        session,
        workspace=workspace,
        role=role,
        user=user,
        issued_at=_now(),
        expires_at=expires_at,
    )
    return token, expires_at, envelope


async def change_password(
    session: AsyncSession,
    principal: Principal,
    *,
    current_password: str,
    new_password: str,
    request: Request | None = None,
) -> tuple[str, dt.datetime]:
    """Rotate your own password and re-issue the session token.

    The current password is always required, so a stolen session cookie cannot
    be upgraded into permanent account control.
    """
    if principal.kind != "user" or principal.user_id is None:
        raise PermissionDenied("An API key cannot change a password.")

    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise Unauthenticated("This account is no longer active.")
    if not user.password_hash or not await off_loop(
        verify_password, current_password, user.password_hash
    ):
        raise ValidationFailed(
            "Your current password is incorrect.",
            details={"fields": [{"field": "current_password", "message": "incorrect"}]},
        )
    if await off_loop(verify_password, new_password, user.password_hash):
        raise ValidationFailed("The new password must differ from the current one.")

    local_part = user.email.split("@", 1)[0]
    if local_part and local_part.lower() in new_password.lower():
        raise ValidationFailed("The password must not contain your email address.")

    user.password_hash = await off_loop(hash_password, new_password)
    user.failed_login_count = 0
    user.locked_until = None

    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise Unauthenticated("The workspace is no longer available.")

    await audit.record(
        session,
        principal=principal,
        action="auth.password_changed",
        entity_type=ENTITY_USER,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_ACCOUNT,
        detail="Password rotated by the account holder",
        request=request,
    )
    await session.flush()
    return issue_session(user=user, workspace=workspace, role=principal.role)


# ---------------------------------------------------------------------------
# Profile and preferences
# ---------------------------------------------------------------------------


async def _self(session: AsyncSession, principal: Principal) -> tuple[User, Workspace]:
    if principal.kind != "user" or principal.user_id is None:
        raise PermissionDenied("This endpoint is for signed-in users, not API keys.")
    user = await session.get(User, principal.user_id)
    workspace = await session.get(Workspace, principal.workspace_id)
    if user is None or workspace is None:
        raise Unauthenticated("This session no longer resolves to an account.")
    return user, workspace


def _profile(user: User, workspace: Workspace, role: Role) -> UserProfile:
    return UserProfile(
        user=SessionUser(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            initials=user.initials,
            job_title=user.job_title,
            team=user.team,
            is_active=user.is_active,
            last_login_at=_as_utc(user.last_login_at),
        ),
        role=role.value,
        workspace=SessionWorkspace(
            id=workspace.id, name=workspace.name, slug=workspace.slug, status=workspace.status
        ),
        preferences=_read_preferences(workspace, user.id),
    )


async def get_profile(session: AsyncSession, principal: Principal) -> UserProfile:
    """Your own record, as the Profile & Preferences panel opens it."""
    user, workspace = await _self(session, principal)
    return _profile(user, workspace, principal.role)


async def update_profile(
    session: AsyncSession,
    principal: Principal,
    payload: ProfileUpdate,
    *,
    request: Request | None = None,
) -> UserProfile:
    """Edit your own display fields. Role and email are not self-service."""
    user, workspace = await _self(session, principal)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _profile(user, workspace, principal.role)

    for field, value in changes.items():
        setattr(user, field, value)

    await audit.record(
        session,
        principal=principal,
        action="user.profile_updated",
        entity_type=ENTITY_USER,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_ACCOUNT,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    return _profile(user, workspace, principal.role)


async def get_preferences(session: AsyncSession, principal: Principal) -> UserPreferences:
    """Your notification and console preferences for this workspace."""
    user, workspace = await _self(session, principal)
    return _read_preferences(workspace, user.id)


async def update_preferences(
    session: AsyncSession,
    principal: Principal,
    payload: PreferencesUpdate,
    *,
    request: Request | None = None,
) -> UserPreferences:
    """Merge a preference change section by section.

    Sending only ``notifications`` leaves the console preferences untouched,
    which is what the two separate panels in the account menu need.
    """
    user, workspace = await _self(session, principal)
    current = _read_preferences(workspace, user.id)
    sections = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not sections:
        return current

    merged = current.model_copy(update=sections)
    _write_preferences(workspace, user.id, merged)

    await audit.record(
        session,
        principal=principal,
        action="user.preferences_updated",
        entity_type=ENTITY_USER,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_ACCOUNT,
        detail="Updated " + ", ".join(sorted(sections)),
        metadata={"sections": sorted(sections)},
        request=request,
    )
    await session.flush()
    return merged


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


#: The half of a member edit that lands on the *account* rather than on the
#: membership. An account is platform-wide — one person, many memberships — so
#: these are this workspace's to change only while the account is in no other.
ACCOUNT_FIELDS: Final[tuple[str, ...]] = (
    "full_name",
    "job_title",
    "team",
    "avatar_initials",
    "is_active",
)

#: Account columns that cannot hold NULL. A PATCH that sends one as ``null``
#: means "no change", not "blank it" — written through, it failed the insert
#: constraint and answered 500.
_ACCOUNT_FIELDS_REQUIRED: Final[frozenset[str]] = frozenset({"full_name", "is_active"})


async def _shared_accounts(
    session: AsyncSession, workspace_id: str, user_ids: set[str]
) -> set[str]:
    """Which of these accounts also belong to a workspace other than this one.

    Every membership counts, including one in a suspended workspace: it can be
    reactivated, and the person's role there comes back with it.
    """
    if not user_ids:
        return set()
    rows = await session.execute(
        select(Membership.user_id)
        .where(Membership.user_id.in_(user_ids), Membership.workspace_id != workspace_id)
        .distinct()
    )
    return set(rows.scalars().all())


def _member_row(
    membership: Membership, user: User, principal: Principal, *, shared: bool = False
) -> dict[str, Any]:
    return {
        "id": user.id,
        "membership_id": membership.id,
        "email": user.email,
        "full_name": user.full_name,
        "initials": user.initials,
        "job_title": user.job_title,
        "team": user.team,
        "role": membership.role,
        "status": MemberStatus.ACTIVE if user.is_active else MemberStatus.INACTIVE,
        "last_login_at": _as_utc(user.last_login_at),
        "joined_at": _as_utc(membership.created_at),
        "password_set": bool(user.password_hash),
        "is_current_user": user.id == principal.user_id,
        "shared_account": shared,
    }


async def _member_read(
    session: AsyncSession, membership: Membership, user: User, principal: Principal
) -> dict[str, Any]:
    """One member's row, including whether the account is shared with another tenant."""
    shared = await _shared_accounts(session, principal.workspace_id, {user.id})
    return _member_row(membership, user, principal, shared=user.id in shared)


def _members_stmt(
    principal: Principal,
    params: ListParams,
    *,
    role: str | None,
    status: MemberStatus | None,
    team: str | None,
) -> Select:
    """The member table's query: one workspace, joined to the person."""
    stmt = (
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == principal.workspace_id)
    )
    stmt = apply_search(stmt, params, [User.full_name, User.email, User.job_title, User.team])
    stmt = apply_filters(
        stmt,
        {
            Membership.role: role,
            User.team: team,
            User.is_active: None if status is None else status is MemberStatus.ACTIVE,
        },
    )
    return apply_sort(
        stmt, params, MEMBER_SORTABLE, default=User.full_name, default_desc=False
    )


async def _attach_users(
    session: AsyncSession, memberships: Sequence[Membership], principal: Principal
) -> list[dict[str, Any]]:
    """Resolve the people for one page of memberships in a single statement."""
    if not memberships:
        return []
    users = {
        user.id: user
        for user in (
            await session.execute(
                select(User).where(User.id.in_({m.user_id for m in memberships}))
            )
        )
        .scalars()
        .all()
    }
    shared = await _shared_accounts(session, principal.workspace_id, set(users))
    rows: list[dict[str, Any]] = []
    for membership in memberships:
        user = users.get(membership.user_id)
        if user is not None:
            rows.append(_member_row(membership, user, principal, shared=user.id in shared))
    return rows


async def list_members(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    role: str | None = None,
    status: MemberStatus | None = None,
    team: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the workspace's members. Requires the admin role.

    The roster is who-has-access-to-what, so reading it is an administrative
    act: an ingest key must not be able to enumerate the tenant's staff.
    """
    principal.require(Role.ADMIN)
    if role is not None:
        role = _parse_role(role).value
    stmt = _members_stmt(principal, params, role=role, status=status, team=team)
    memberships, total = await paginate(session, stmt, params)
    return await _attach_users(session, memberships, principal), total


async def member_directory(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    """Active members' names only, for the assignment and escalation pickers.

    Deliberately thinner than :func:`list_members`: no emails, roles, teams or
    login history — nothing worth mining — so it can open to approvers and
    operators without weakening the admin gate on the roster itself. The caller
    enforces the signed-in-person and approver checks.
    """
    stmt = (
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.workspace_id == principal.workspace_id,
            User.is_active.is_(True),
        )
        .order_by(User.full_name.asc())
    )
    return [
        {"id": user.id, "full_name": user.full_name, "initials": user.initials}
        for user in (await session.execute(stmt)).scalars()
    ]


async def export_members(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    role: str | None = None,
    status: MemberStatus | None = None,
    team: str | None = None,
) -> list[dict[str, Any]]:
    """Every member matching the table's filters, capped at :data:`EXPORT_LIMIT`.

    Requires the admin role, for the same reason the list does.
    """
    principal.require(Role.ADMIN)
    if role is not None:
        role = _parse_role(role).value
    stmt = _members_stmt(principal, params, role=role, status=status, team=team).limit(
        EXPORT_LIMIT
    )
    memberships = (await session.execute(stmt)).scalars().all()
    return await _attach_users(session, memberships, principal)


async def _membership_for(
    session: AsyncSession, principal: Principal, user_id: str
) -> tuple[Membership, User]:
    row = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.workspace_id == principal.workspace_id,
                Membership.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        raise NotFound(f"'{user_id}' is not a member of this workspace.")
    return row[0], row[1]


async def get_member(session: AsyncSession, principal: Principal, user_id: str) -> dict[str, Any]:
    """One member. Someone in another workspace answers 404, not 403.

    Reading your own row is always allowed — that is what the sidebar user card
    asks for; reading anyone else's requires the admin role.
    """
    if user_id != principal.user_id:
        principal.require(Role.ADMIN)
    membership, user = await _membership_for(session, principal, user_id)
    return await _member_read(session, membership, user, principal)


async def _count_role(session: AsyncSession, workspace_id: str, role: Role) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Membership.id)).where(
                    Membership.workspace_id == workspace_id, Membership.role == role.value
                )
            )
        ).scalar_one()
    )


def _assert_may_manage(principal: Principal, target_role: Role, new_role: Role | None) -> None:
    """Only an owner may touch an owner, or hand the owner role out.

    Without this an admin could promote themselves and take the workspace, so
    the check is on both ends: the member being edited and the role being set.
    """
    if principal.role is Role.OWNER:
        return
    if target_role is Role.OWNER:
        raise PermissionDenied("Only an owner can modify another owner.")
    if new_role is Role.OWNER:
        raise PermissionDenied("Only an owner can grant the owner role.")


async def _assert_not_last_owner(
    session: AsyncSession, principal: Principal, membership: Membership, *, action: str
) -> None:
    if membership.role != Role.OWNER.value:
        return
    if await _count_role(session, principal.workspace_id, Role.OWNER) > 1:
        return
    raise PreconditionFailed(
        f"Cannot {action} the only owner of this workspace. Promote another owner first."
    )


async def _assert_account_is_ours(
    session: AsyncSession, principal: Principal, user: User, fields: Sequence[str]
) -> None:
    """Refuse an account-level edit of an account this workspace does not solely hold.

    Accounts are platform-wide and :func:`create_member` joins an existing one to
    a workspace without asking its holder. So "is a member here" says nothing
    about whose the credential is: an admin of any workspace could add the owner
    of another by email, set that account's password from the member table, and
    sign in as them — landing, by :func:`_primary_membership`, in the *other*
    tenant with the role held there. Deactivating did the same damage in one
    request. The rule that closes it is the one ``create_member`` already states
    for a password — an existing credential belongs to its owner — applied to
    every write that reaches the account: they are an admin's to make only while
    this workspace is the account's only one.

    Whoever created the account is deliberately not a way in. Nothing records
    it, and it would not be safe if something did: the takeover works just as
    well on an account one tenant created and another later made an owner.

    Your own password is never set from here either. The account menu asks for
    the current one, so that a borrowed session cannot be turned into the
    account; the member table would be the way round it.
    """
    if "password" in fields and user.id == principal.user_id:
        raise PermissionDenied(
            "Change your own password from the account menu, which asks for the current one.",
            details={"fields": ["password"], "reason": "own_account"},
        )
    if not await _shared_accounts(session, principal.workspace_id, {user.id}):
        return

    if "password" in fields:
        message = (
            f"{user.email} also belongs to another workspace, so their password is theirs "
            "to change (account menu, Change Password) and not this workspace's to set."
        )
    elif "is_active" in fields:
        message = (
            f"{user.email} also belongs to another workspace, and deactivating the account "
            "would lock them out of all of them. To end their access here, remove them from "
            "this workspace instead."
        )
    else:
        message = (
            f"{user.email} also belongs to another workspace, so their profile is theirs to "
            "edit and not this workspace's. Their role here can still be changed."
        )
    raise PermissionDenied(
        message, details={"fields": sorted(fields), "reason": "shared_account"}
    )


async def _release_seats(
    session: AsyncSession,
    principal: Principal,
    user: User,
    *,
    why: str,
    request: Request | None = None,
) -> int:
    """Release the licensed seats a departing member holds here; returns how many.

    A seat is given to a *member* -- assigning one refuses anybody else, and an
    inactive account too -- but nothing took it back when the membership went or
    the account was switched off. The Seats tab went on showing the person
    Active, "Seats Assigned" stayed at 10 of 10, and their replacement was
    answered 402. The way out was to know to press Reassign on the row of
    somebody who no longer worked there.

    The licence row is taken ``FOR UPDATE`` first, as every seat change in
    :mod:`services.licensing` does, so an assignment running alongside counts
    this release rather than racing it. The counter is recomputed from the rows,
    never decremented: it is a denormalised copy, and a copy that has drifted is
    put right by a recount and made worse by arithmetic.
    """
    licences = (
        (
            await session.execute(
                select(TenantLicense)
                .where(
                    TenantLicense.tenant_workspace_id == principal.workspace_id,
                    TenantLicense.id.in_(
                        select(SeatAssignment.license_id).where(
                            SeatAssignment.user_id == user.id,
                            SeatAssignment.released_at.is_(None),
                        )
                    ),
                )
                .order_by(TenantLicense.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    released = 0
    now = _now()
    for licence in licences:
        seats = (
            (
                await session.execute(
                    select(SeatAssignment).where(
                        SeatAssignment.license_id == licence.id,
                        SeatAssignment.user_id == user.id,
                        SeatAssignment.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for seat in seats:
            seat.released_at = now
        await session.flush()
        licence.seats_assigned = int(
            (
                await session.execute(
                    select(func.count(SeatAssignment.id)).where(
                        SeatAssignment.license_id == licence.id,
                        SeatAssignment.released_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        licence.updated_by = principal.actor
        released += len(seats)
        await audit.record(
            session,
            principal=principal,
            action="licensing.seat.released",
            entity_type="seat_assignment",
            entity_id=seats[0].id if seats else licence.id,
            entity_label=user.email,
            source_screen=SOURCE_SCREEN_MEMBERS,
            detail=f"Seat released: {user.email} {why}.",
            metadata={
                "license_id": licence.id,
                "user_id": user.id,
                "seats_assigned": licence.seats_assigned,
            },
            request=request,
        )
    return released


async def create_member(
    session: AsyncSession,
    principal: Principal,
    payload: MemberCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add someone to the workspace, creating the account if it is new.

    An address the platform already knows is joined rather than duplicated, and
    in that case the supplied password is refused — an existing credential
    belongs to its owner. Requires the admin role.
    """
    principal.require(Role.ADMIN)
    role = _parse_role(payload.role)
    _assert_may_manage(principal, Role.MEMBER, role)

    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    if user is None:
        user = User(
            email=payload.email,
            full_name=payload.full_name,
            job_title=payload.job_title,
            team=payload.team,
            password_hash=(
                await off_loop(hash_password, payload.password) if payload.password else None
            ),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        created_account = True
    else:
        if payload.password is not None:
            raise Conflict(
                f"{payload.email} already has an account. "
                "Add them without a password; they keep the one they have."
            )
        existing = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == principal.workspace_id,
                    Membership.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(f"{payload.email} is already a member of this workspace.")
        created_account = False

    membership = Membership(
        workspace_id=principal.workspace_id, user_id=user.id, role=role.value
    )
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError as exc:  # raced with another admin adding the same person
        await session.rollback()
        raise Conflict(f"{payload.email} is already a member of this workspace.") from exc

    await audit.record(
        session,
        principal=principal,
        action="member.added",
        entity_type=ENTITY_MEMBERSHIP,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_MEMBERS,
        detail=(
            f"{'Created account and added' if created_account else 'Added existing account'} "
            f"as {role.value}"
        ),
        metadata={"role": role.value, "new_account": created_account},
        request=request,
    )
    await session.flush()
    await session.refresh(membership)
    return await _member_read(session, membership, user, principal)


async def update_member(
    session: AsyncSession,
    principal: Principal,
    user_id: str,
    payload: MemberUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Edit a member's record, their role, or their ability to sign in.

    The role belongs to the membership and is this workspace's to set. The
    name, the active flag and the password belong to the *account*, which is
    platform-wide, so they are this workspace's to change only while the account
    is in no other workspace (:func:`_assert_account_is_ours`). Deactivating
    blocks sign-in everywhere, so the last owner cannot be deactivated and
    nobody can deactivate themselves out of the console. Requires the admin
    role.
    """
    principal.require(Role.ADMIN)
    membership, user = await _membership_for(session, principal, user_id)

    changes = payload.model_dump(exclude_unset=True)
    for field in _ACCOUNT_FIELDS_REQUIRED:
        if field in changes and changes[field] is None:
            del changes[field]
    if not changes:
        return await _member_read(session, membership, user, principal)

    new_role = _parse_role(changes["role"]) if changes.get("role") is not None else None
    _assert_may_manage(principal, Role(membership.role), new_role)

    # Only what would actually move is judged: a form that sends the whole
    # record back to change a role has not asked to edit the account.
    account_changes = [
        field
        for field in ACCOUNT_FIELDS
        if field in changes and changes[field] != getattr(user, field)
    ]
    if changes.get("password"):
        account_changes.append("password")
    if account_changes:
        await _assert_account_is_ours(session, principal, user, account_changes)

    if new_role is not None and new_role.value != membership.role:
        await _assert_not_last_owner(session, principal, membership, action="demote")
        membership.role = new_role.value

    if changes.get("is_active") is False:
        if user.id == principal.user_id:
            raise PreconditionFailed("You cannot deactivate your own account.")
        await _assert_not_last_owner(session, principal, membership, action="deactivate")
        if user.is_active:
            # An account that cannot sign in is not using its seat, and could not
            # be given one; reactivating it does not take the seat back.
            await _release_seats(
                session, principal, user, why="was deactivated", request=request
            )

    for field in ACCOUNT_FIELDS:
        if field in changes:
            setattr(user, field, changes[field])

    if changes.get("password"):
        # An admin setting a member's credential: the console's "Set Password"
        # action for accounts created without one, or a reset. Clearing the
        # lockout alongside, or the fresh password would bounce off it.
        user.password_hash = await off_loop(hash_password, changes["password"])
        user.failed_login_count = 0
        user.locked_until = None

    await audit.record(
        session,
        principal=principal,
        action="member.updated",
        entity_type=ENTITY_MEMBERSHIP,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_MEMBERS,
        # The audit trail records that a password changed, never anything
        # about the password itself.
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes), "role": membership.role},
        request=request,
    )
    await session.flush()
    return await _member_read(session, membership, user, principal)


async def change_member_role(
    session: AsyncSession,
    principal: Principal,
    user_id: str,
    payload: MemberRoleChange,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Move one member to another role. Requires the admin role."""
    principal.require(Role.ADMIN)
    membership, user = await _membership_for(session, principal, user_id)

    new_role = _parse_role(payload.role)
    previous = Role(membership.role)
    if new_role is previous:
        return await _member_read(session, membership, user, principal)

    _assert_may_manage(principal, previous, new_role)
    await _assert_not_last_owner(session, principal, membership, action="demote")
    membership.role = new_role.value

    await audit.record(
        session,
        principal=principal,
        action="member.role_changed",
        entity_type=ENTITY_MEMBERSHIP,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_MEMBERS,
        detail=f"Role changed from {previous.value} to {new_role.value}"
        + (f" - {payload.reason}" if payload.reason else ""),
        metadata={"from": previous.value, "to": new_role.value, "reason": payload.reason},
        request=request,
    )
    await session.flush()
    return await _member_read(session, membership, user, principal)


async def remove_member(
    session: AsyncSession,
    principal: Principal,
    user_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove someone from this workspace.

    The membership goes; the account stays, because it may belong to other
    workspaces and its audit history must remain attributable. Any licensed seat
    they held on this workspace's licences is released in the same transaction
    (:func:`_release_seats`). Requires the admin role.
    """
    principal.require(Role.ADMIN)
    membership, user = await _membership_for(session, principal, user_id)

    if user.id == principal.user_id:
        raise PreconditionFailed(
            "You cannot remove your own membership. Ask another admin to do it."
        )
    _assert_may_manage(principal, Role(membership.role), None)
    await _assert_not_last_owner(session, principal, membership, action="remove")

    await _release_seats(
        session, principal, user, why="was removed from the workspace", request=request
    )
    await audit.record(
        session,
        principal=principal,
        action="member.removed",
        entity_type=ENTITY_MEMBERSHIP,
        entity_id=user.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN_MEMBERS,
        detail=f"Removed from the workspace (was {membership.role})",
        metadata={"role": membership.role},
        request=request,
    )
    await session.delete(membership)
    await session.flush()


async def summarise_members(session: AsyncSession, principal: Principal) -> MembersSummary:
    """Member KPI cards, computed with SQL aggregates. Requires the admin role."""
    principal.require(Role.ADMIN)
    now = _now()
    cutoff = now - dt.timedelta(days=30)
    scope = Membership.workspace_id == principal.workspace_id

    totals = (
        await session.execute(
            select(
                func.count(Membership.id).label("total"),
                func.coalesce(
                    func.sum(case((User.is_active.is_(True), 1), else_=0)), 0
                ).label("active"),
                func.count(User.last_login_at).label("ever_signed_in"),
                func.max(Membership.created_at).label("last_joined_at"),
            )
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(scope)
        )
    ).one()

    signed_in_30d = int(
        (
            await session.execute(
                select(func.count(Membership.id))
                .select_from(Membership)
                .join(User, User.id == Membership.user_id)
                .where(scope, User.last_login_at.is_not(None), User.last_login_at >= cutoff)
            )
        ).scalar_one()
    )

    added_30d = int(
        (
            await session.execute(
                select(func.count(Membership.id)).where(scope, Membership.created_at >= cutoff)
            )
        ).scalar_one()
    )

    grouped = (
        await session.execute(
            select(Membership.role, func.count(Membership.id)).where(scope).group_by(
                Membership.role
            )
        )
    ).all()
    counts = {role: int(count) for role, count in grouped}

    total = int(totals.total or 0)
    active = int(totals.active or 0)
    by_role = [
        RoleCount(
            role=role.value,
            count=counts.get(role.value, 0),
            percent=round(counts.get(role.value, 0) / total * 100, 1) if total else 0.0,
        )
        for role in Role
    ]

    return MembersSummary(
        total=total,
        active=active,
        inactive=total - active,
        owners=counts.get(Role.OWNER.value, 0),
        admins=counts.get(Role.ADMIN.value, 0),
        privileged=(
            counts.get(Role.OWNER.value, 0)
            + counts.get(Role.ADMIN.value, 0)
            + counts.get(Role.OPERATOR.value, 0)
        ),
        never_signed_in=total - int(totals.ever_signed_in or 0),
        signed_in_last_30d=signed_in_30d,
        added_last_30d=added_30d,
        last_joined_at=_as_utc(totals.last_joined_at),
        by_role=by_role,
    )


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def _keys_stmt(
    principal: Principal,
    params: ListParams,
    *,
    status: ApiKeyStatus | None,
    environment: str | None,
    scope: str | None,
    agent_id: str | None,
) -> Select:
    """The key table's query, including the derived status filter."""
    now = _now()
    stmt = select(ApiKey).where(ApiKey.workspace_id == principal.workspace_id)
    stmt = apply_search(stmt, params, [ApiKey.name, ApiKey.display_hint, ApiKey.environment])
    stmt = apply_filters(
        stmt, {ApiKey.environment: environment, ApiKey.agent_id: agent_id}
    )

    if status is ApiKeyStatus.REVOKED:
        stmt = stmt.where(ApiKey.revoked_at.is_not(None))
    elif status is ApiKeyStatus.EXPIRED:
        stmt = stmt.where(ApiKey.revoked_at.is_(None), ApiKey.expires_at < now)
    elif status is ApiKeyStatus.ACTIVE:
        stmt = stmt.where(
            ApiKey.revoked_at.is_(None),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at >= now),
        )

    if scope:
        # ``scopes`` is a JSON array on both dialects, so the portable filter is
        # a substring match on the serialised form, quoted so a scope name that
        # merely starts the same way cannot match.
        stmt = stmt.where(func.cast(ApiKey.scopes, Text).like(f'%"{scope}"%'))

    return apply_sort(stmt, params, API_KEY_SORTABLE, default=ApiKey.created_at, default_desc=True)


async def _agent_names(
    session: AsyncSession, principal: Principal, keys: Sequence[ApiKey]
) -> dict[str, str]:
    ids = {key.agent_id for key in keys if key.agent_id}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == principal.workspace_id, Agent.id.in_(ids)
            )
        )
    ).all()
    return dict(rows)


def _key_read(key: ApiKey, agent_name: str | None) -> ApiKeyRead:
    return ApiKeyRead(
        id=key.id,
        name=key.name,
        display_hint=key.display_hint,
        scopes=list(key.scopes or []),
        environment=key.environment,
        agent_id=key.agent_id,
        agent_name=agent_name,
        status=_key_status(key),
        created_at=_as_utc(key.created_at) or _now(),
        created_by=key.created_by_user_id,
        last_used_at=_as_utc(key.last_used_at),
        last_used_ip=key.last_used_ip,
        expires_at=_as_utc(key.expires_at),
        expires_in_days=_expires_in_days(key),
        revoked_at=_as_utc(key.revoked_at),
        revoked_reason=key.revoked_reason,
    )


async def list_api_keys(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: ApiKeyStatus | None = None,
    environment: str | None = None,
    scope: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[ApiKeyRead], int]:
    """One page of the workspace's keys. The secret is not among the columns."""
    principal.require(Role.OPERATOR)
    stmt = _keys_stmt(
        principal, params, status=status, environment=environment, scope=scope, agent_id=agent_id
    )
    keys, total = await paginate(session, stmt, params)
    names = await _agent_names(session, principal, keys)
    return [_key_read(key, names.get(key.agent_id or "")) for key in keys], total


async def export_api_keys(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: ApiKeyStatus | None = None,
    environment: str | None = None,
    scope: str | None = None,
    agent_id: str | None = None,
) -> list[ApiKeyRead]:
    """Every key matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    principal.require(Role.OPERATOR)
    stmt = _keys_stmt(
        principal, params, status=status, environment=environment, scope=scope, agent_id=agent_id
    ).limit(EXPORT_LIMIT)
    keys = (await session.execute(stmt)).scalars().all()
    names = await _agent_names(session, principal, keys)
    return [_key_read(key, names.get(key.agent_id or "")) for key in keys]


async def get_api_key(session: AsyncSession, principal: Principal, key_id: str) -> ApiKeyRead:
    """One key's metadata. A key in another workspace answers 404."""
    principal.require(Role.OPERATOR)
    key = await _load_key(session, principal, key_id)
    names = await _agent_names(session, principal, [key])
    return _key_read(key, names.get(key.agent_id or ""))


async def _load_key(session: AsyncSession, principal: Principal, key_id: str) -> ApiKey:
    key = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.workspace_id == principal.workspace_id, ApiKey.id == key_id
            )
        )
    ).scalar_one_or_none()
    if key is None:
        raise NotFound(f"API key '{key_id}' does not exist.")
    return key


def _snippets(
    minted: MintedApiKey,
    workspace: Workspace,
    *,
    agent_name: str | None = None,
    environment: str | None = None,
    may_register: bool = False,
) -> list[ApiKeySnippet]:
    """Copy-ready setup for each way a customer connects an agent.

    The plaintext key is interpolated here because this is the one response
    that carries it; every other read shows the display hint instead.

    "Copy-ready" is a promise about the first minute: pasted as it stands, the
    code has to import, and what it reports has to be accepted. So the snippets
    are written against the SDKs' real signatures (``tests/test_audit_identity``
    executes the Python one against the SDK in this repository), and they name
    the agent this key can actually report for. A key bound to an agent is
    refused for any other name; an unbound key without the ``admin`` scope is
    refused for a name the Agent Registry does not hold, so the placeholder
    says so instead of inventing a name that would be rejected on every batch.
    """
    base_url = f"{settings.public_base_url.rstrip('/')}{settings.api_prefix}"
    token = minted.token

    if agent_name:
        agent, agent_note = agent_name, "the agent this key is bound to"
    elif may_register:
        agent, agent_note = "my-agent", "any name: this key registers it on first report"
    else:
        agent, agent_note = (
            "YOUR-AGENT-NAME",
            "must match an agent in the Agent Registry: this key cannot register one",
        )
    # A JSON string literal is a valid literal in both languages, whatever
    # quotes or backslashes the agent's display name holds.
    agent_literal = json.dumps(agent)
    environment_literal = json.dumps(environment) if environment else None

    exports = [
        f"export FULCRUM_OPS_API_KEY={token}",
        f"export FULCRUM_OPS_BASE_URL={base_url}",
        f"export FULCRUM_OPS_WORKSPACE={workspace.slug}",
    ]
    if agent_name:
        exports.append(f"export FULCRUM_OPS_AGENT={shlex.quote(agent_name)}")
    if environment:
        exports.append(f"export FULCRUM_OPS_ENVIRONMENT={shlex.quote(environment)}")

    return [
        ApiKeySnippet(language="bash", label="Environment", code="\n".join(exports)),
        ApiKeySnippet(
            language="python",
            label="Python SDK",
            code=(
                "from fulcrum_ops import FulcrumOps, trace\n\n"
                "client = FulcrumOps(\n"
                f'    api_key="{token}",\n'
                f'    base_url="{base_url}",\n'
                f"    agent={agent_literal},  # {agent_note}\n"
                + (f"    environment={environment_literal},\n" if environment_literal else "")
                + ")\n\n\n"
                "@trace\n"
                "def handle(question: str) -> str:\n"
                "    # Your agent's work. The arguments and the return value are the run.\n"
                '    return f"You asked: {question}"'
            ),
        ),
        ApiKeySnippet(
            language="typescript",
            label="TypeScript SDK",
            code=(
                'import { FulcrumOps } from "@fulcrum-ops/sdk";\n\n'
                "const client = new FulcrumOps({\n"
                f'  apiKey: "{token}",\n'
                f'  baseUrl: "{base_url}",\n'
                f"  agent: {agent_literal}, // {agent_note}\n"
                + (f"  environment: {environment_literal},\n" if environment_literal else "")
                + "});\n\n"
                "export async function handle(question: string): Promise<string> {\n"
                '  return client.trace({ name: "handle", input: { question } }, async () => {\n'
                "    // Your agent's work. What it returns is recorded as the run's output.\n"
                "    return `You asked: ${question}`;\n"
                "  });\n"
                "}"
            ),
        ),
        ApiKeySnippet(
            language="bash",
            label="curl",
            code=(
                f'curl -sS "{base_url}/auth/session" \\\n'
                f'  -H "Authorization: Bearer {token}"'
            ),
        ),
    ]


async def create_api_key(
    session: AsyncSession,
    principal: Principal,
    payload: ApiKeyCreate,
    *,
    request: Request | None = None,
) -> ApiKeyCreated:
    """Mint a key and return its plaintext exactly once.

    Only the key id, a SHA-256 digest of the secret and a display hint are
    stored, so this response is the only opportunity to copy the key. Binding
    to an agent is verified against this workspace, so a key cannot be pointed
    at another tenant's agent. Requires the admin role.
    """
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(
            select(ApiKey.id).where(
                ApiKey.workspace_id == principal.workspace_id,
                ApiKey.name == payload.name,
                ApiKey.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"An active key named '{payload.name}' already exists.")

    agent_name: str | None = None
    if payload.agent_id:
        agent_name = (
            await session.execute(
                select(Agent.name).where(
                    Agent.workspace_id == principal.workspace_id, Agent.id == payload.agent_id
                )
            )
        ).scalar_one_or_none()
        if agent_name is None:
            raise NotFound(f"Agent '{payload.agent_id}' does not exist in this workspace.")

    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise NotFound("This workspace no longer exists.")

    minted = _mint_usable_key()
    key = ApiKey(
        workspace_id=principal.workspace_id,
        name=payload.name,
        key_id=minted.key_id,
        secret_hash=minted.secret_hash,
        display_hint=minted.display_hint,
        agent_id=payload.agent_id,
        scopes=list(payload.scopes),
        environment=payload.environment.value if payload.environment else None,
        created_by_user_id=principal.user_id,
        expires_at=(
            _now() + dt.timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    session.add(key)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="api_key.created",
        entity_type=ENTITY_API_KEY,
        entity_id=key.id,
        entity_label=key.name,
        source_screen=SOURCE_SCREEN_KEYS,
        detail=(
            f"Minted with scopes {', '.join(key.scopes)}"
            + (f" bound to agent {agent_name}" if agent_name else "")
            + (f", expires {key.expires_at:%Y-%m-%d}" if key.expires_at else ", no expiry")
        ),
        metadata={
            "scopes": list(key.scopes),
            "environment": key.environment,
            "agent_id": key.agent_id,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(key)

    read = _key_read(key, agent_name)
    return ApiKeyCreated(
        **read.model_dump(),
        token=minted.token,
        snippets=_snippets(
            minted,
            workspace,
            agent_name=agent_name,
            environment=key.environment,
            # The test ingest applies before it registers a name it has never
            # seen; ``admin`` is the one such scope this route can grant.
            may_register="admin" in key.scopes,
        ),
    )


async def revoke_api_key(
    session: AsyncSession,
    principal: Principal,
    key_id: str,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> ApiKeyRead:
    """Retire a key immediately. Requires the admin role.

    Revocation is preferred over deletion: the row keeps the audit trail
    attributable to a name rather than to an opaque id.
    """
    principal.require(Role.ADMIN)
    key = await _load_key(session, principal, key_id)
    if key.revoked_at is not None:
        raise Conflict(f"'{key.name}' was already revoked.")

    key.revoked_at = _now()
    key.revoked_reason = reason

    await audit.record(
        session,
        principal=principal,
        action="api_key.revoked",
        entity_type=ENTITY_API_KEY,
        entity_id=key.id,
        entity_label=key.name,
        source_screen=SOURCE_SCREEN_KEYS,
        detail=f"Revoked{f' - {reason}' if reason else ''}",
        metadata={"reason": reason, "last_used_at": (
            key.last_used_at.isoformat() if key.last_used_at else None
        )},
        request=request,
    )
    await session.flush()
    names = await _agent_names(session, principal, [key])
    return _key_read(key, names.get(key.agent_id or ""))


async def delete_api_key(
    session: AsyncSession,
    principal: Principal,
    key_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Delete a key row outright.

    Refused while the key is still live: revoke it first, so a key that is
    still authenticating agents cannot vanish without a decision being logged.
    Requires the admin role.
    """
    principal.require(Role.ADMIN)
    key = await _load_key(session, principal, key_id)

    if _key_status(key) is ApiKeyStatus.ACTIVE:
        raise PreconditionFailed(
            f"'{key.name}' is still active. Revoke it before deleting it."
        )

    await audit.record(
        session,
        principal=principal,
        action="api_key.deleted",
        entity_type=ENTITY_API_KEY,
        entity_id=key.id,
        entity_label=key.name,
        source_screen=SOURCE_SCREEN_KEYS,
        detail=f"Deleted {_key_status(key).value.lower()} key",
        request=request,
    )
    await session.delete(key)
    await session.flush()


async def api_key_usage(
    session: AsyncSession,
    principal: Principal,
    key_id: str,
    *,
    window_days: int = USAGE_WINDOW_DAYS,
) -> ApiKeyUsage:
    """What this key has done, read back from the audit rows it produced.

    Every audited operation performed with a key is attributed to
    ``api-key:<id>``, so the trail is the record of those. Totals come from SQL
    aggregates; the daily series is folded from at most
    :data:`USAGE_DETAIL_LIMIT` rows inside the window.

    The trail is not a record of *traffic*, and this used to answer as though it
    were. A batch that is accepted whole writes no audit row -- one per batch
    would be tens of thousands a day per agent -- so a key that had reported all
    day came back with zero ingest calls and zero records, beside a last-used
    time of two minutes ago; and the record and byte totals were summed from
    metadata fields that ingest has never written. Ingest volume is not metered
    per key anywhere, so it is reported as unmeasured (``None``, a dash in the
    console) instead of as nought. The audited ingest rows that do exist -- a
    blocked batch, a guardrail hit, recorded governance events -- are in
    ``by_action`` under their own names, which is what they are.
    """
    principal.require(Role.OPERATOR)
    key = await _load_key(session, principal, key_id)

    # Deps writes ``api-key:<row id>`` as the actor on every audited operation a
    # key performs, so the trail is the usage record — nothing is inferred.
    actor = f"api-key:{key.id}"
    scope = (
        AuditEvent.workspace_id == principal.workspace_id,
        AuditEvent.actor == actor,
    )
    cutoff = _now() - dt.timedelta(days=window_days)

    totals = (
        await session.execute(
            select(
                func.count(AuditEvent.id).label("total"),
                func.min(AuditEvent.occurred_at).label("first_seen"),
                func.max(AuditEvent.occurred_at).label("last_seen"),
            ).where(*scope)
        )
    ).one()

    by_action = [
        ApiKeyUsageAction(action=action, count=int(count))
        for action, count in (
            await session.execute(
                select(AuditEvent.action, func.count(AuditEvent.id))
                .where(*scope, AuditEvent.occurred_at >= cutoff)
                .group_by(AuditEvent.action)
                .order_by(func.count(AuditEvent.id).desc())
            )
        ).all()
    ]

    detail_rows = (
        await session.execute(
            select(AuditEvent.occurred_at)
            .where(*scope, AuditEvent.occurred_at >= cutoff)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(USAGE_DETAIL_LIMIT)
        )
    ).all()

    daily: dict[dt.date, int] = {}
    for (occurred_at,) in detail_rows:
        day = (_as_utc(occurred_at) or _now()).date()
        daily[day] = daily.get(day, 0) + 1

    return ApiKeyUsage(
        key_id=key.id,
        name=key.name,
        status=_key_status(key),
        last_used_at=_as_utc(key.last_used_at) or _as_utc(totals.last_seen),
        last_used_ip=key.last_used_ip,
        first_seen_at=_as_utc(totals.first_seen),
        total_calls=int(totals.total or 0),
        calls_in_window=sum(daily.values()),
        ingest_calls=None,
        ingest_records=None,
        ingest_bytes=None,
        window_days=window_days,
        by_action=by_action,
        daily=[ApiKeyUsageDay(date=day, calls=count) for day, count in sorted(daily.items())],
    )


async def summarise_api_keys(session: AsyncSession, principal: Principal) -> ApiKeysSummary:
    """Key KPI cards, computed with SQL aggregates over the workspace."""
    principal.require(Role.OPERATOR)
    now = _now()
    soon = now + dt.timedelta(days=EXPIRING_SOON_DAYS)
    week_ago = now - dt.timedelta(days=7)
    scope = ApiKey.workspace_id == principal.workspace_id

    live = (
        ApiKey.revoked_at.is_(None),
        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at >= now),
    )

    async def _count(*conditions: Any) -> int:
        return int(
            (
                await session.execute(select(func.count(ApiKey.id)).where(scope, *conditions))
            ).scalar_one()
        )

    total = await _count()
    active = await _count(*live)
    revoked = await _count(ApiKey.revoked_at.is_not(None))
    expired = await _count(ApiKey.revoked_at.is_(None), ApiKey.expires_at < now)
    expiring_soon = await _count(
        ApiKey.revoked_at.is_(None), ApiKey.expires_at >= now, ApiKey.expires_at <= soon
    )
    never_used = await _count(ApiKey.last_used_at.is_(None))
    used_last_7d = await _count(ApiKey.last_used_at >= week_ago)
    agent_bound = await _count(ApiKey.agent_id.is_not(None))

    last_used_at = (
        await session.execute(select(func.max(ApiKey.last_used_at)).where(scope))
    ).scalar_one_or_none()

    grouped = (
        await session.execute(
            select(ApiKey.environment, func.count(ApiKey.id)).where(scope).group_by(
                ApiKey.environment
            )
        )
    ).all()

    return ApiKeysSummary(
        total=total,
        active=active,
        revoked=revoked,
        expired=expired,
        expiring_soon=expiring_soon,
        never_used=never_used,
        used_last_7d=used_last_7d,
        agent_bound=agent_bound,
        last_used_at=_as_utc(last_used_at),
        by_environment=[
            EnvironmentCount(environment=environment or "Unassigned", count=int(count))
            for environment, count in sorted(grouped, key=lambda row: row[0] or "")
        ],
    )


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


async def _workspace_read(
    session: AsyncSession, principal: Principal, workspace: Workspace
) -> WorkspaceRead:
    member_count = int(
        (
            await session.execute(
                select(func.count(Membership.id)).where(
                    Membership.workspace_id == workspace.id
                )
            )
        ).scalar_one()
    )
    key_counts = (
        await session.execute(
            select(
                func.count(ApiKey.id).label("total"),
                func.coalesce(
                    func.sum(case((ApiKey.revoked_at.is_(None), 1), else_=0)), 0
                ).label("live"),
            ).where(ApiKey.workspace_id == workspace.id)
        )
    ).one()

    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        status=workspace.status,
        settings=_public_settings(workspace),
        role=principal.role.value,
        member_count=member_count,
        api_key_count=int(key_counts.total or 0),
        active_api_key_count=int(key_counts.live or 0),
        created_at=_as_utc(workspace.created_at) or _now(),
        updated_at=_as_utc(workspace.updated_at) or _now(),
    )


async def get_current_workspace(session: AsyncSession, principal: Principal) -> WorkspaceRead:
    """The workspace this session is scoped to, with its headline counts."""
    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise NotFound("This workspace no longer exists.")
    return await _workspace_read(session, principal, workspace)


async def update_current_workspace(
    session: AsyncSession,
    principal: Principal,
    payload: WorkspaceUpdate,
    *,
    request: Request | None = None,
) -> WorkspaceRead:
    """Rename the workspace or replace its tenant attributes.

    The per-person preference bag is carried across untouched: it lives in the
    same JSON column but is nobody's tenant attribute. The slug is immutable —
    it is baked into every SDK configuration already deployed. Requires the
    admin role.
    """
    principal.require(Role.ADMIN)
    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise NotFound("This workspace no longer exists.")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return await _workspace_read(session, principal, workspace)

    if "name" in changes and changes["name"] is not None:
        workspace.name = changes["name"]
    if "settings" in changes and changes["settings"] is not None:
        preserved = (workspace.settings or {}).get(PREFERENCES_KEY)
        merged = {k: v for k, v in changes["settings"].items() if k != PREFERENCES_KEY}
        if preserved is not None:
            merged[PREFERENCES_KEY] = preserved
        workspace.settings = merged
    workspace.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="workspace.updated",
        entity_type=ENTITY_WORKSPACE,
        entity_id=workspace.id,
        entity_label=workspace.name,
        source_screen=SOURCE_SCREEN_MEMBERS,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    await session.refresh(workspace)
    return await _workspace_read(session, principal, workspace)


async def list_my_workspaces(
    session: AsyncSession, principal: Principal
) -> list[WorkspaceOption]:
    """Every workspace the caller may switch to; the switcher's data source."""
    if principal.kind != "user" or principal.user_id is None:
        # A key is bound to one workspace by construction, so the only entry it
        # can ever offer is the one it already authenticates against.
        workspace = await session.get(Workspace, principal.workspace_id)
        if workspace is None:
            return []
        return [
            WorkspaceOption(
                id=workspace.id,
                name=workspace.name,
                slug=workspace.slug,
                role=principal.role.value,
                is_current=True,
            )
        ]
    return await list_workspace_options(
        session, principal.user_id, current_workspace_id=principal.workspace_id
    )


# ---------------------------------------------------------------------------
# Provisioning, used by the CLI rather than by a route
# ---------------------------------------------------------------------------


async def provision_workspace(
    session: AsyncSession, *, name: str, slug: str, engine_workspace: str | None = None
) -> Workspace:
    """Create a tenant. Raises :class:`Conflict` if the slug is taken."""
    existing = (
        await session.execute(select(Workspace).where(Workspace.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(f"A workspace with slug '{slug}' already exists.")

    workspace = Workspace(
        name=name,
        slug=slug,
        engine_workspace=engine_workspace or slug,
        status="active",
        settings={},
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def provision_user(
    session: AsyncSession,
    *,
    workspace: Workspace,
    email: str,
    full_name: str,
    password: str | None,
    role: Role,
    job_title: str | None = None,
    team: str | None = None,
) -> tuple[User, Membership]:
    """Create or join an account to a workspace, without a request context."""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            full_name=full_name,
            job_title=job_title,
            team=team,
            password_hash=await off_loop(hash_password, password) if password else None,
            is_active=True,
        )
        session.add(user)
        await session.flush()

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.workspace_id == workspace.id, Membership.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if membership is not None:
        raise Conflict(f"{email} is already a member of {workspace.slug}.")

    membership = Membership(workspace_id=workspace.id, user_id=user.id, role=role.value)
    session.add(membership)
    await session.flush()
    return user, membership


async def provision_api_key(
    session: AsyncSession,
    *,
    workspace: Workspace,
    name: str,
    scopes: Sequence[str],
    environment: str | None = None,
    created_by_user_id: str | None = None,
    expires_in_days: int | None = None,
) -> tuple[ApiKey, MintedApiKey]:
    """Mint a key outside a request. The caller must show the token once."""
    minted = _mint_usable_key()
    key = ApiKey(
        workspace_id=workspace.id,
        name=name,
        key_id=minted.key_id,
        secret_hash=minted.secret_hash,
        display_hint=minted.display_hint,
        scopes=list(scopes),
        environment=environment,
        created_by_user_id=created_by_user_id,
        expires_at=(
            _now() + dt.timedelta(days=expires_in_days) if expires_in_days else None
        ),
    )
    session.add(key)
    await session.flush()
    return key, minted


def verify_api_secret_digest(raw_secret: str) -> str:
    """Expose the digest helper so provisioning tools never hash by hand."""
    return hash_api_secret(raw_secret)


def member_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten a member row into the shape :func:`api.common.to_csv` expects."""
    status = row["status"]
    return {**row, "status": status.value if isinstance(status, MemberStatus) else status}


def api_key_export_row(key: ApiKeyRead) -> dict[str, Any]:
    """Flatten a key row for CSV, joining the scope list into one cell."""
    return {
        **key.model_dump(mode="json"),
        "status": key.status.value,
        "scopes": ", ".join(key.scopes),
    }
