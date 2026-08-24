"""Secrets & Credentials business logic.

The vault is the one place in this system where a read can be a security
incident, so the rules here are stricter than anywhere else:

* material enters on create and on rotate only, and is encrypted before it is
  attached to a row (``core.security.encrypt_secret``); the plaintext is never
  written to a log line, an audit detail or a CSV cell;
* material leaves through :func:`reveal_secret` only. That path demands the
  admin role and a written justification, refuses disabled and revoked
  credentials, and writes two independent records: a ``SecretAccessLog`` row
  (the operator-facing history the console shows) and an audit row (the
  tamper-evident chain a compliance reviewer replays);
* a *refused* access is evidence too. Denials are committed on their own before
  the error unwinds the request, because "who tried and was stopped" is exactly
  what the failed-attempts counter on the inspector is for.

Query paths return assembled ``SecretRead`` rows rather than ORM objects: the
vault table renders the owner's display name, which lives on ``users``, and
``has_material`` must be derived where ``ciphertext`` is in scope and nowhere
else. Everything else follows the house shape — every statement filters on
``principal.workspace_id``, so another tenant's row is indistinguishable from a
row that does not exist.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence
from secrets import token_urlsafe
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, and_, case, func, not_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import (
    ListParams,
    apply_filters,
    apply_search,
    apply_sort,
    paginate,
    to_csv,
)
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed
from ..core.security import decrypt_secret, encrypt_secret, mask_secret
from ..models.governance import (
    Secret,
    SecretAccessAction,
    SecretAccessLog,
    SecretStatus,
)
from ..models.identity import Role, User
from ..schemas.secrets import (
    EXPIRING_WINDOW_DAYS,
    PRIVILEGED_ACCESS_WINDOW_DAYS,
    AccessLogFilters,
    RotationState,
    SecretCreate,
    SecretDisableRequest,
    SecretFilters,
    SecretRead,
    SecretRevealRequest,
    SecretRevealResult,
    SecretRotateRequest,
    SecretRotateResult,
    SecretsSummary,
    SecretTypeBreakdown,
    SecretUpdate,
)
from . import audit

SOURCE_SCREEN: Final[str] = "Secrets & Credentials"
ENTITY_TYPE: Final[str] = "secret"

#: Length of a control-plane-minted credential, in random bytes before
#: base64url encoding. 36 bytes is 288 bits — comfortably beyond guessing.
GENERATED_VALUE_BYTES: Final[int] = 36

#: An export is a report, not a bulk download of the vault. Anything larger is
#: a query that should have been filtered.
EXPORT_MAX_ROWS: Final[int] = 5000

#: Optimistic-concurrency comparisons allow a second of slack: some drivers
#: round ``updated_at`` on the way out, and a rounding artefact is not a clash.
CONFLICT_TOLERANCE_SECONDS: Final[float] = 1.0

#: States in which a credential is inert: it may not be revealed or rotated.
INERT_STATUSES: Final[tuple[str, ...]] = (
    SecretStatus.DISABLED.value,
    SecretStatus.REVOKED.value,
)

SEARCH_COLUMNS: Final[tuple[Any, ...]] = (
    Secret.name,
    Secret.secret_type,
    Secret.vault,
    Secret.environment,
    Secret.vault_reference,
    User.full_name,
    User.email,
)

#: Keys accepted by ``?sort=``. Both the console's column keys and the column
#: names are listed, so either spelling sorts.
SORTABLE: Final[dict[str, Any]] = {
    "name": Secret.name,
    "type": Secret.secret_type,
    "secret_type": Secret.secret_type,
    "vault": Secret.vault,
    "env": Secret.environment,
    "environment": Secret.environment,
    "status": Secret.status,
    "rotation": Secret.next_rotation_at,
    "next_rotation_at": Secret.next_rotation_at,
    "lastAccessed": Secret.last_accessed_at,
    "last_accessed_at": Secret.last_accessed_at,
    "lastRotated": Secret.last_rotated_at,
    "last_rotated_at": Secret.last_rotated_at,
    "expires_at": Secret.expires_at,
    "owner": User.full_name,
    "risk": Secret.risk,
    "privileged": Secret.privileged,
    "created_at": Secret.created_at,
    "updated_at": Secret.updated_at,
}

ACCESS_LOG_SORTABLE: Final[dict[str, Any]] = {
    "occurred_at": SecretAccessLog.occurred_at,
    "time": SecretAccessLog.occurred_at,
    "actor": SecretAccessLog.actor,
    "action": SecretAccessLog.action,
    "success": SecretAccessLog.success,
}

#: Metadata only. Nothing that resembles material — not even the masked hint —
#: travels in a file that leaves the building.
EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "Secret Name"),
    ("secret_type", "Type"),
    ("vault", "Vault / Store"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("rotation_label", "Rotation"),
    ("next_rotation_at", "Next Rotation"),
    ("last_rotated_at", "Last Rotated"),
    ("expires_at", "Expires"),
    ("last_accessed_at", "Last Accessed"),
    ("owner_name", "Owner"),
    ("owner_email", "Owner Email"),
    ("risk", "Risk"),
    ("privileged", "Privileged"),
    ("compliance", "Compliance"),
    ("vault_reference", "Vault Reference"),
    ("created_at", "Created"),
)

_DATE_KEYS: Final[tuple[str, ...]] = (
    "next_rotation_at",
    "last_rotated_at",
    "expires_at",
    "last_accessed_at",
    "created_at",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _client_ip(request: Request | None) -> str | None:
    """Left-most X-Forwarded-For entry, which is the real client behind Caddy."""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _value(item: Any) -> Any:
    """Unwrap an enum for the query layer; string columns store the label."""
    return item.value if isinstance(item, enum.Enum) else item


def _values(items: Sequence[Any] | None) -> list[Any] | None:
    return [_value(item) for item in items] if items else None


def _scoped(principal: Principal) -> Select:
    return select(Secret).where(Secret.workspace_id == principal.workspace_id)


def _listing_query(
    principal: Principal, params: ListParams, filters: SecretFilters
) -> Select:
    """The vault table's query: workspace scope, dropdowns, search, sort.

    The owner join is always present because the table both searches and sorts
    on the owner's display name, which lives on ``users``.
    """
    stmt = _scoped(principal).outerjoin(User, User.id == Secret.owner_user_id)
    stmt = apply_filters(
        stmt,
        {
            Secret.secret_type: _values(filters.secret_type),
            Secret.environment: list(filters.environment) if filters.environment else None,
            Secret.status: _values(filters.status),
            Secret.vault: list(filters.vault) if filters.vault else None,
            Secret.risk: _value(filters.risk),
            Secret.owner_user_id: filters.owner_user_id,
            Secret.privileged: filters.privileged,
        },
    )
    stmt = _apply_rotation_state(stmt, filters.rotation_state)
    if filters.expiring_within_days is not None:
        horizon = _now() + dt.timedelta(days=filters.expiring_within_days)
        stmt = stmt.where(Secret.expires_at.is_not(None), Secret.expires_at <= horizon)
    stmt = apply_search(stmt, params, SEARCH_COLUMNS)
    stmt = apply_sort(stmt, params, SORTABLE, Secret.created_at)
    # Deterministic tie-break: without it, two rows sharing a sort value can
    # swap places between pages and the console shows one of them twice.
    return stmt.order_by(Secret.id.desc())


def _apply_rotation_state(stmt: Select, state: RotationState | None) -> Select:
    if state is None:
        return stmt
    now = _now()
    if state is RotationState.OVERDUE:
        return stmt.where(Secret.next_rotation_at.is_not(None), Secret.next_rotation_at < now)
    if state is RotationState.DUE_SOON:
        return stmt.where(
            Secret.next_rotation_at.is_not(None),
            Secret.next_rotation_at >= now,
            Secret.next_rotation_at <= now + dt.timedelta(days=EXPIRING_WINDOW_DAYS),
        )
    if state is RotationState.SCHEDULED:
        return stmt.where(
            Secret.next_rotation_at.is_not(None),
            Secret.next_rotation_at > now + dt.timedelta(days=EXPIRING_WINDOW_DAYS),
        )
    return stmt.where(Secret.next_rotation_at.is_(None))


async def _owner_map(
    session: AsyncSession, user_ids: set[str]
) -> dict[str, tuple[str | None, str | None]]:
    """One extra statement per page resolves every owner; never one per row."""
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.email).where(User.id.in_(user_ids))
        )
    ).all()
    return {row.id: (row.full_name, row.email) for row in rows}


async def _reads(session: AsyncSession, rows: Sequence[Secret]) -> list[SecretRead]:
    owners = await _owner_map(session, {r.owner_user_id for r in rows if r.owner_user_id})
    reads: list[SecretRead] = []
    for row in rows:
        owner_name, owner_email = owners.get(row.owner_user_id or "", (None, None))
        reads.append(
            SecretRead.from_model(row, owner_name=owner_name, owner_email=owner_email)
        )
    return reads


async def _read(session: AsyncSession, secret: Secret) -> SecretRead:
    return (await _reads(session, [secret]))[0]


async def _get(session: AsyncSession, principal: Principal, secret_id: str) -> Secret:
    """Fetch one row inside the caller's workspace, or 404.

    A credential belonging to another tenant answers 404 rather than 403: a 403
    would confirm that the id exists, which is itself a disclosure.
    """
    secret = (
        await session.execute(_scoped(principal).where(Secret.id == secret_id))
    ).scalar_one_or_none()
    if secret is None:
        raise NotFound("That secret does not exist.")
    return secret


async def _assert_name_free(
    session: AsyncSession, principal: Principal, name: str, *, exclude_id: str | None = None
) -> None:
    stmt = _scoped(principal).where(func.lower(Secret.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(Secret.id != exclude_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise Conflict(f"A secret named '{name}' already exists in this workspace.")


def _access_row(
    secret: Secret,
    principal: Principal,
    action: SecretAccessAction,
    *,
    request: Request | None = None,
    justification: str | None = None,
    success: bool = True,
) -> SecretAccessLog:
    return SecretAccessLog(
        workspace_id=secret.workspace_id,
        secret_id=secret.id,
        actor=principal.actor,
        actor_user_id=principal.user_id,
        action=action.value,
        occurred_at=_now(),
        ip_address=_client_ip(request),
        justification=justification,
        success=success,
    )


async def _record_refusal(
    session: AsyncSession,
    principal: Principal,
    secret: Secret,
    *,
    reason: str,
    justification: str | None = None,
    request: Request | None = None,
) -> None:
    """Commit a refused reveal before the exception unwinds the request.

    The request dependency rolls the session back when an error propagates, so
    a denial written in the normal way would vanish along with the refusal —
    losing precisely the record that matters. This commits the evidence on its
    own; the caller raises immediately afterwards.
    """
    session.add(
        _access_row(
            secret,
            principal,
            SecretAccessAction.REVEAL,
            request=request,
            justification=justification,
            success=False,
        )
    )
    await audit.record(
        session,
        principal=principal,
        action="secret.reveal_denied",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Reveal refused: {reason}",
        request=request,
    )
    await session.commit()


def _next_rotation(anchor: dt.datetime, period_days: int | None) -> dt.datetime | None:
    return anchor + dt.timedelta(days=period_days) if period_days else None


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat(timespec="seconds")


def _export_row(read: SecretRead) -> dict[str, Any]:
    row = read.model_dump()
    for key in _DATE_KEYS:
        row[key] = _iso(row.get(key))
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_secrets(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    filters: SecretFilters,
) -> tuple[list[SecretRead], int]:
    """One page of the vault, newest first unless ``sort`` says otherwise."""
    stmt = _listing_query(principal, params, filters)
    rows, total = await paginate(session, stmt, params)
    return await _reads(session, rows), total


async def get_secret(
    session: AsyncSession, principal: Principal, secret_id: str
) -> SecretRead:
    """One credential's metadata. Never its material."""
    secret = await _get(session, principal, secret_id)
    return await _read(session, secret)


async def list_access_log(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    params: ListParams,
    filters: AccessLogFilters,
) -> tuple[Sequence[SecretAccessLog], int]:
    """The append-only access history for one credential, newest first.

    Failed attempts are in here alongside successful ones; that is the point of
    the table.
    """
    secret = await _get(session, principal, secret_id)
    stmt = select(SecretAccessLog).where(
        SecretAccessLog.secret_id == secret.id,
        SecretAccessLog.workspace_id == principal.workspace_id,
    )
    stmt = apply_filters(
        stmt,
        {
            SecretAccessLog.action: _value(filters.action),
            SecretAccessLog.success: filters.success,
        },
    )
    stmt = apply_search(
        stmt,
        params,
        (SecretAccessLog.actor, SecretAccessLog.justification, SecretAccessLog.ip_address),
    )
    stmt = apply_sort(stmt, params, ACCESS_LOG_SORTABLE, SecretAccessLog.occurred_at)
    return await paginate(session, stmt.order_by(SecretAccessLog.id.desc()), params)


async def summarise(session: AsyncSession, principal: Principal) -> SecretsSummary:
    """The KPI cards and the donut, computed by the database.

    Three aggregate statements answer the whole panel; no row is loaded into
    Python to be counted, so a workspace with tens of thousands of credentials
    costs the same as one with ten.
    """
    now = _now()
    expiring = and_(
        Secret.expires_at.is_not(None),
        Secret.expires_at <= now + dt.timedelta(days=EXPIRING_WINDOW_DAYS),
    )
    overdue = and_(
        Secret.next_rotation_at.is_not(None),
        Secret.next_rotation_at < now,
    )
    # Compliant means neither of the above. The NULL guards above keep this a
    # two-valued test, so rows with no expiry and no cadence count as compliant.
    compliant = and_(not_(expiring), not_(overdue))

    totals = (
        await session.execute(
            select(
                func.count(Secret.id),
                func.count(func.distinct(Secret.vault)),
                func.coalesce(func.sum(case((expiring, 1), else_=0)), 0),
                func.coalesce(func.sum(case((overdue, 1), else_=0)), 0),
                func.coalesce(func.sum(case((compliant, 1), else_=0)), 0),
            ).where(Secret.workspace_id == principal.workspace_id)
        )
    ).one()
    total, active_vaults, expiring_soon, rotation_overdue, compliant_count = totals

    privileged_access = (
        await session.execute(
            select(func.count(SecretAccessLog.id))
            .join(Secret, Secret.id == SecretAccessLog.secret_id)
            .where(
                SecretAccessLog.workspace_id == principal.workspace_id,
                SecretAccessLog.occurred_at
                >= now - dt.timedelta(days=PRIVILEGED_ACCESS_WINDOW_DAYS),
                Secret.privileged.is_(True),
            )
        )
    ).scalar_one()

    by_type_rows = (
        await session.execute(
            select(
                Secret.secret_type,
                func.count(Secret.id),
                func.coalesce(func.sum(case((compliant, 1), else_=0)), 0),
            )
            .where(Secret.workspace_id == principal.workspace_id)
            .group_by(Secret.secret_type)
            .order_by(func.count(Secret.id).desc(), Secret.secret_type.asc())
        )
    ).all()

    return SecretsSummary(
        total=int(total),
        active_vaults=int(active_vaults),
        expiring_soon=int(expiring_soon),
        rotation_overdue=int(rotation_overdue),
        # An empty vault is fully compliant; there is nothing out of policy in it.
        compliance_score=round(100 * int(compliant_count) / int(total)) if total else 100,
        privileged_access_30d=int(privileged_access),
        by_type=[
            SecretTypeBreakdown(
                secret_type=secret_type,
                count=int(count),
                compliant=int(compliant_rows),
                compliance_pct=round(100 * int(compliant_rows) / int(count)) if count else 100,
            )
            for secret_type, count, compliant_rows in by_type_rows
        ],
    )


async def export_csv(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    filters: SecretFilters,
    *,
    request: Request | None = None,
) -> str:
    """Render the current view as CSV metadata, and audit the fact it was taken.

    An export is a read, but a compliance-relevant one: knowing the shape of a
    workspace's vault is worth recording, so the audit row is written even
    though no credential changes.
    """
    stmt = _listing_query(principal, params, filters)
    rows = (await session.execute(stmt.limit(EXPORT_MAX_ROWS))).scalars().all()
    reads = await _reads(session, rows)

    await audit.record(
        session,
        principal=principal,
        action="secret.exported",
        entity_type=ENTITY_TYPE,
        source_screen=SOURCE_SCREEN,
        detail=f"Exported metadata for {len(reads)} secret(s)",
        request=request,
    )
    return to_csv([_export_row(read) for read in reads], EXPORT_COLUMNS)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_secret(
    session: AsyncSession,
    principal: Principal,
    payload: SecretCreate,
    *,
    request: Request | None = None,
) -> SecretRead:
    """Store a credential. Any supplied value is encrypted before it is attached."""
    principal.require(Role.ADMIN)
    await _assert_name_free(session, principal, payload.name)

    now = _now()
    has_material = bool(payload.value)
    secret = Secret(
        workspace_id=principal.workspace_id,
        name=payload.name,
        secret_type=payload.secret_type.value,
        vault=payload.vault,
        environment=payload.environment,
        status=SecretStatus.ACTIVE.value,
        ciphertext=encrypt_secret(payload.value) if payload.value else None,
        display_hint=mask_secret(payload.value) if payload.value else None,
        rotation_period_days=payload.rotation_period_days,
        # The clock starts now for a value stored now; a pointer to an external
        # vault has no local rotation history to date from.
        last_rotated_at=now if has_material else None,
        next_rotation_at=_next_rotation(now, payload.rotation_period_days),
        expires_at=payload.expires_at,
        owner_user_id=payload.owner_user_id or principal.user_id,
        risk=payload.risk.value,
        vault_reference=payload.vault_reference,
        privileged=payload.privileged,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(secret)

    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(
            f"A secret named '{payload.name}' already exists in this workspace."
        ) from exc

    session.add(_access_row(secret, principal, SecretAccessAction.CREATE, request=request))
    stored = "encrypted material" if has_material else "a vault reference"
    await audit.record(
        session,
        principal=principal,
        action="secret.created",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Stored {secret.secret_type} in {secret.vault} as {stored}",
        request=request,
    )
    return await _read(session, secret)


async def update_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    payload: SecretUpdate,
    *,
    request: Request | None = None,
) -> SecretRead:
    """Patch metadata. The material is untouched — rotation is the only way to change it."""
    principal.require(Role.ADMIN)
    secret = await _get(session, principal, secret_id)

    changes = payload.model_dump(exclude_unset=True)
    expected = changes.pop("expected_updated_at", None)
    if expected is not None and (
        abs((expected - secret.updated_at).total_seconds()) > CONFLICT_TOLERANCE_SECONDS
    ):
        raise Conflict(
            "This secret changed after you loaded it. Reload the row and reapply your edit."
        )
    if not changes:
        return await _read(session, secret)

    new_name = changes.get("name")
    if new_name is not None and new_name.lower() != secret.name.lower():
        await _assert_name_free(session, principal, new_name, exclude_id=secret.id)

    for field, value in changes.items():
        setattr(secret, field, _value(value))

    if "rotation_period_days" in changes:
        # Re-anchor the schedule on the last real rotation so shortening a
        # cadence can legitimately make a credential overdue straight away.
        anchor = secret.last_rotated_at or _now()
        secret.next_rotation_at = _next_rotation(anchor, secret.rotation_period_days)

    secret.updated_by = principal.actor
    session.add(_access_row(secret, principal, SecretAccessAction.UPDATE, request=request))
    await audit.record(
        session,
        principal=principal,
        action="secret.updated",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        request=request,
    )
    await session.flush()
    return await _read(session, secret)


async def delete_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a credential that is already out of service.

    Deleting takes its access history with it, so a live credential must be
    disabled or revoked first: that way the deletion is a deliberate second
    step, and the audit chain still records both.
    """
    principal.require(Role.ADMIN)
    secret = await _get(session, principal, secret_id)

    if secret.status not in INERT_STATUSES:
        raise PreconditionFailed(
            f"'{secret.name}' is still {secret.status.lower()}. Disable it before deleting it."
        )

    label, secret_type, vault = secret.name, secret.secret_type, secret.vault
    await session.delete(secret)
    await audit.record(
        session,
        principal=principal,
        action="secret.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=secret_id,
        entity_label=label,
        source_screen=SOURCE_SCREEN,
        detail=f"Deleted {secret_type} from {vault}",
        request=request,
    )


async def reveal_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    payload: SecretRevealRequest,
    *,
    request: Request | None = None,
) -> SecretRevealResult:
    """Decrypt and return the plaintext once, against a justification.

    Admin only. Refused for a disabled or revoked credential, and for one the
    control plane merely points at. Every outcome — allowed or refused — leaves
    a ``SecretAccessLog`` row behind.
    """
    secret = await _get(session, principal, secret_id)
    justification = payload.justification

    if not principal.role.satisfies(Role.ADMIN):
        await _record_refusal(
            session,
            principal,
            secret,
            reason=f"{principal.role.value} role may not reveal credentials",
            justification=justification,
            request=request,
        )
        principal.require(Role.ADMIN)  # raises PermissionDenied with the house message

    if secret.status in INERT_STATUSES:
        state = secret.status.lower()
        await _record_refusal(
            session,
            principal,
            secret,
            reason=f"credential is {state}",
            justification=justification,
            request=request,
        )
        raise PreconditionFailed(
            f"'{secret.name}' is {state} and cannot be revealed. Enable it first."
        )

    if not secret.ciphertext:
        await _record_refusal(
            session,
            principal,
            secret,
            reason="no material is held by the control plane",
            justification=justification,
            request=request,
        )
        raise PreconditionFailed(
            f"'{secret.name}' is held in {secret.vault}; the control plane stores no "
            "material for it. Read it from the vault directly."
        )

    try:
        plaintext = decrypt_secret(secret.ciphertext)
    except RuntimeError as exc:
        # Wrong or rotated encryption key: a real operational fault, but not a
        # server error to the caller, and still an access that failed.
        await _record_refusal(
            session,
            principal,
            secret,
            reason="stored material could not be decrypted",
            justification=justification,
            request=request,
        )
        raise PreconditionFailed(
            f"'{secret.name}' cannot be decrypted with the active encryption key. "
            "Rotate it to store fresh material."
        ) from exc

    now = _now()
    entry = _access_row(
        secret,
        principal,
        SecretAccessAction.REVEAL,
        request=request,
        justification=justification,
    )
    session.add(entry)
    secret.last_accessed_at = now
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="secret.revealed",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Plaintext revealed in the console. Justification: {justification}",
        request=request,
    )
    return SecretRevealResult(
        secret_id=secret.id,
        name=secret.name,
        value=plaintext,
        revealed_at=now,
        revealed_by=principal.actor,
        justification=justification,
        access_log_id=entry.id,
    )


async def rotate_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    payload: SecretRotateRequest,
    *,
    request: Request | None = None,
) -> SecretRotateResult:
    """Replace the material and restart the rotation clock.

    A value may be supplied — when the credential was reissued upstream — or
    minted here. A minted value is returned exactly once, in this response; a
    supplied one is never echoed back.
    """
    principal.require(Role.ADMIN)
    secret = await _get(session, principal, secret_id)

    if secret.status in INERT_STATUSES:
        raise PreconditionFailed(
            f"'{secret.name}' is {secret.status.lower()} and cannot be rotated. "
            "Enable it first."
        )

    generated = payload.value is None
    plaintext = payload.value or token_urlsafe(GENERATED_VALUE_BYTES)
    now = _now()

    if payload.rotation_period_days is not None:
        secret.rotation_period_days = payload.rotation_period_days

    secret.ciphertext = encrypt_secret(plaintext)
    secret.display_hint = mask_secret(plaintext)
    secret.last_rotated_at = now
    secret.next_rotation_at = _next_rotation(now, secret.rotation_period_days)
    secret.last_accessed_at = now
    # Inert states were refused above, so whatever warning the row carried —
    # expiring, overdue, expired — is answered by the new material.
    secret.status = SecretStatus.ACTIVE.value
    secret.updated_by = principal.actor

    session.add(
        _access_row(
            secret,
            principal,
            SecretAccessAction.ROTATE,
            request=request,
            justification=payload.reason,
        )
    )
    origin = "generated by the control plane" if generated else "supplied by the operator"
    detail = f"Rotated with a new value {origin}"
    if payload.reason:
        detail = f"{detail}. Reason: {payload.reason}"
    await audit.record(
        session,
        principal=principal,
        action="secret.rotated",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        request=request,
    )
    await session.flush()

    return SecretRotateResult(
        secret=await _read(session, secret),
        generated=generated,
        value=plaintext if generated else None,
        rotated_at=now,
        next_rotation_at=secret.next_rotation_at,
    )


async def disable_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    payload: SecretDisableRequest | None = None,
    *,
    request: Request | None = None,
) -> SecretRead:
    """Take a credential out of service. Linked systems lose access immediately."""
    principal.require(Role.ADMIN)
    secret = await _get(session, principal, secret_id)

    if secret.status == SecretStatus.REVOKED.value:
        raise Conflict(f"'{secret.name}' is already revoked.")
    if secret.status == SecretStatus.DISABLED.value:
        raise Conflict(f"'{secret.name}' is already disabled.")

    reason = payload.reason if payload else None
    secret.status = SecretStatus.DISABLED.value
    secret.updated_by = principal.actor

    session.add(
        _access_row(
            secret,
            principal,
            SecretAccessAction.DISABLE,
            request=request,
            justification=reason,
        )
    )
    await audit.record(
        session,
        principal=principal,
        action="secret.disabled",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Disabled. Reason: {reason}" if reason else "Disabled via the console",
        request=request,
    )
    await session.flush()
    return await _read(session, secret)


async def enable_secret(
    session: AsyncSession,
    principal: Principal,
    secret_id: str,
    *,
    request: Request | None = None,
) -> SecretRead:
    """Return a disabled credential to service.

    A revoked credential is not eligible: revocation is final, and the answer
    to needing it back is a new credential.
    """
    principal.require(Role.ADMIN)
    secret = await _get(session, principal, secret_id)

    if secret.status == SecretStatus.REVOKED.value:
        raise PreconditionFailed(
            f"'{secret.name}' has been revoked and cannot be re-enabled. "
            "Store a replacement credential instead."
        )
    if secret.status != SecretStatus.DISABLED.value:
        raise Conflict(f"'{secret.name}' is already {secret.status.lower()}.")

    secret.status = SecretStatus.ACTIVE.value
    secret.updated_by = principal.actor

    session.add(_access_row(secret, principal, SecretAccessAction.ENABLE, request=request))
    await audit.record(
        session,
        principal=principal,
        action="secret.enabled",
        entity_type=ENTITY_TYPE,
        entity_id=secret.id,
        entity_label=secret.name,
        source_screen=SOURCE_SCREEN,
        detail="Returned to service via the console",
        request=request,
    )
    await session.flush()
    return await _read(session, secret)
