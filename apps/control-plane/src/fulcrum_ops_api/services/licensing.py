"""Licensing and entitlements business logic.

Everything commercial about a tenant lives behind this module: the plan
catalogue, the licence a workspace holds, the seats issued against it, the
entitlements other domains enforce, the metered usage that produces overage, and
the invoices that bill it.

Two rules shape the whole file.

*Scope.* A licence is addressed by ``tenant_workspace_id``, which is the only
workspace column on the row, so every query here filters on
``principal.workspace_id`` and a licence belonging to somebody else is reported
as :class:`NotFound` — never :class:`PermissionDenied`, which would confirm it
exists. Plans are catalogue-level and deliberately global; take-up figures shown
next to a plan are still aggregated over the caller's own licences.

*Authority.* Anything that changes commercial terms — publishing a plan, issuing
or amending a licence, buying seats, suspending or reactivating — requires
``Role.OWNER``. Handing an existing seat to a colleague is day-to-day user
administration and requires ``Role.ADMIN``. Reading requires nothing beyond
membership.

Other domains do not import the models directly; they call
:func:`check_entitlement` and :func:`enforce_entitlement`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import enum
import logging
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from fastapi import Request
from sqlalchemy import Select, and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import (
    MAX_PAGE_SIZE,
    ActionResult,
    ListParams,
    apply_filters,
    apply_search,
    apply_sort,
    paginate,
    to_csv,
)
from ..api.deps import Principal
from ..core.errors import (
    Conflict,
    NotFound,
    PreconditionFailed,
    QuotaExceeded,
    ValidationFailed,
)
from ..models.identity import Membership, Role, User, Workspace
from ..models.licensing import (
    Entitlement,
    EntitlementValueType,
    Invoice,
    InvoiceStatus,
    LicensePlan,
    LicenseStatus,
    PlanStatus,
    PlanTier,
    SeatAssignment,
    TenantLicense,
    UsageMetric,
    UsageRecord,
)
from ..schemas.licensing import (
    EntitlementRead,
    EntitlementUsageRead,
    ExportDataset,
    LicensePlanCreate,
    LicensePlanRead,
    LicensePlanUpdate,
    LicenseRevokeRequest,
    LicenseSuspendRequest,
    LicensingSummary,
    SeatAssignmentCreate,
    SeatAssignmentRead,
    SeatPurchaseRequest,
    SeatState,
    TenantEntitlementsRead,
    TenantLicenseCreate,
    TenantLicenseRead,
    TenantLicenseUpdate,
)
from . import audit

log = logging.getLogger(__name__)

SOURCE_SCREEN = "Licensing & Entitlements"
#: ``UsageRecord.source`` for what the ingest path meters, batch by batch.
USAGE_SOURCE_INGEST = "ingest"

#: The KPI card is fixed at 30 days, and the renewal sweep uses the same horizon.
EXPIRY_HORIZON_DAYS = 30
#: Usage is aggregated over a rolling window so the Usage & Limits tab and the
#: overage KPI always answer the same question regardless of billing anniversary.
USAGE_WINDOW_DAYS = 30
#: Hard ceiling on an export so one click cannot pull an unbounded result set
#: into memory. Beyond this, callers page the list endpoint instead.
MAX_EXPORT_ROWS = 10_000
#: The invoice PDF is a single page; anything past this is summarised.
MAX_INVOICE_PDF_LINES = 22
MAX_LICENSE_SEATS = 1_000_000

#: The metered ceilings a plan sells. Each is a plan column and, when the plan
#: sets it, a hard integer entitlement under the same key.
CEILING_KEYS: tuple[str, ...] = ("included_seats", "included_tokens", "included_runs")
#: The plan columns entitlement rows are resolved from: an edit to any of them
#: changes what every licence on the plan is entitled to.
ENTITLEMENT_PLAN_FIELDS: frozenset[str] = frozenset({"features", *CEILING_KEYS})

#: Statuses that mean the licence is currently serving the tenant.
LIVE_STATUSES: tuple[str, ...] = (
    LicenseStatus.ACTIVE.value,
    LicenseStatus.TRIAL.value,
    LicenseStatus.EXPIRING_SOON.value,
)
#: Live plus suspended: a workspace already holding one of these cannot be sold
#: a second licence, because entitlement resolution must stay unambiguous.
CURRENT_STATUSES: tuple[str, ...] = (*LIVE_STATUSES, LicenseStatus.SUSPENDED.value)
#: Statuses that refuse new seat assignment.
SEAT_BLOCKING_STATUSES: tuple[str, ...] = (
    LicenseStatus.SUSPENDED.value,
    LicenseStatus.REVOKED.value,
    LicenseStatus.EXPIRED.value,
)
#: Statuses past the point of amendment.
TERMINAL_STATUSES: tuple[str, ...] = (
    LicenseStatus.REVOKED.value,
    LicenseStatus.EXPIRED.value,
)
SUSPENDED_OR_REVOKED: tuple[str, ...] = (
    LicenseStatus.SUSPENDED.value,
    LicenseStatus.REVOKED.value,
)
OPEN_INVOICE_STATUSES: tuple[str, ...] = (
    InvoiceStatus.ISSUED.value,
    InvoiceStatus.OVERDUE.value,
)

PLAN_SORTS = {
    "name": LicensePlan.name,
    "code": LicensePlan.code,
    "tier": LicensePlan.tier,
    "status": LicensePlan.status,
    "billing_period": LicensePlan.billing_period,
    "price_per_seat_usd": LicensePlan.price_per_seat_usd,
    "included_seats": LicensePlan.included_seats,
    "included_tokens": LicensePlan.included_tokens,
    "effective_from": LicensePlan.effective_from,
    "effective_to": LicensePlan.effective_to,
    "created_at": LicensePlan.created_at,
    "updated_at": LicensePlan.updated_at,
}

LICENSE_SORTS = {
    "tenant": Workspace.name,
    "plan": LicensePlan.name,
    "tier": LicensePlan.tier,
    "status": TenantLicense.status,
    "seats_purchased": TenantLicense.seats_purchased,
    "seats_assigned": TenantLicense.seats_assigned,
    "starts_at": TenantLicense.starts_at,
    "expires_at": TenantLicense.expires_at,
    "created_at": TenantLicense.created_at,
    "updated_at": TenantLicense.updated_at,
}

SEAT_SORTS = {
    "user": User.full_name,
    "email": User.email,
    "role": SeatAssignment.role,
    "assigned_at": SeatAssignment.assigned_at,
    "released_at": SeatAssignment.released_at,
    "created_at": SeatAssignment.created_at,
}

PLAN_CSV_COLUMNS: list[tuple[str, str]] = [
    ("name", "Plan"),
    ("code", "Code"),
    ("tier", "Tier"),
    ("billing_period", "Billing Period"),
    ("price_per_seat_usd", "Price Per Seat (USD)"),
    ("included_seats", "Included Seats"),
    ("included_tokens", "Included Tokens"),
    ("included_runs", "Included Runs"),
    ("status", "Status"),
    ("license_count", "Licenses"),
    ("seats_purchased", "Seats Purchased"),
    ("seats_assigned", "Seats Assigned"),
    ("seat_utilization_pct", "Utilization %"),
    ("effective_from", "Effective From"),
    ("effective_to", "Effective To"),
]

LICENSE_CSV_COLUMNS: list[tuple[str, str]] = [
    ("id", "License ID"),
    ("tenant_name", "Tenant"),
    ("plan_name", "Plan"),
    ("plan_tier", "Tier"),
    ("status", "Status"),
    ("seats_purchased", "Seats Purchased"),
    ("seats_assigned", "Seats Assigned"),
    ("seats_available", "Seats Available"),
    ("seat_utilization_pct", "Utilization %"),
    ("included_tokens", "Token Capacity"),
    ("included_runs", "Run Capacity"),
    ("starts_at", "Starts"),
    ("expires_at", "Expires"),
    ("days_until_expiry", "Days To Expiry"),
    ("auto_renew", "Auto Renew"),
    ("billing_contact_email", "Billing Contact"),
    ("purchase_order_ref", "PO Reference"),
    ("suspended_at", "Suspended At"),
]

SEAT_CSV_COLUMNS: list[tuple[str, str]] = [
    ("license_id", "License ID"),
    ("user_name", "User"),
    ("user_email", "Email"),
    ("role", "Role"),
    ("assigned_at", "Assigned At"),
    ("released_at", "Released At"),
    ("is_active", "Active"),
]

USAGE_CSV_COLUMNS: list[tuple[str, str]] = [
    ("license_id", "License ID"),
    ("metric", "Metric"),
    ("period_start", "Period Start"),
    ("period_end", "Period End"),
    ("quantity", "Quantity"),
    ("unit_cost_usd", "Unit Cost (USD)"),
    ("amount_usd", "Amount (USD)"),
    ("source", "Source"),
    ("recorded_at", "Recorded At"),
]

INVOICE_CSV_COLUMNS: list[tuple[str, str]] = [
    ("invoice_ref", "Invoice"),
    ("license_id", "License ID"),
    ("period_start", "Period Start"),
    ("period_end", "Period End"),
    ("subtotal_usd", "Subtotal"),
    ("overage_usd", "Overage"),
    ("total_usd", "Total"),
    ("currency", "Currency"),
    ("status", "Status"),
    ("issued_at", "Issued"),
    ("due_at", "Due"),
    ("paid_at", "Paid"),
]


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt.UTC).isoformat()


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """A timestamp sent without an offset is UTC, which is how the column stores it.

    Without this a term check between a stored (aware) date and a naive one from
    a client raises ``TypeError`` and the request answers 500 instead of 422.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _days_until(value: dt.datetime | None, now: dt.datetime) -> int | None:
    if value is None:
        return None
    return (value - now).days


def _plan_tier(value: str | None) -> PlanTier | None:
    """Tolerate a tier string the enum no longer knows rather than 500-ing."""
    if not value:
        return None
    try:
        return PlanTier(value)
    except ValueError:
        return None


def _usage_metric(value: str | None) -> UsageMetric | None:
    if not value:
        return None
    try:
        return UsageMetric(value)
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


async def _persist(session: AsyncSession, instance: Any) -> None:
    """Flush, then re-read the server-populated timestamp columns.

    ``created_at``/``updated_at`` carry SQL defaults, so after a flush they are
    expired on the instance. Touching them later would trigger implicit IO from
    async context; refreshing here keeps every read model safe to build.
    """
    await session.flush()
    await session.refresh(instance)


def _apply_updates(instance: Any, data: dict[str, Any]) -> list[str]:
    """Copy a partial payload onto an ORM row; return the fields that moved."""
    changed: list[str] = []
    for field, raw in data.items():
        value = raw.value if isinstance(raw, enum.Enum) else raw
        if getattr(instance, field) != value:
            setattr(instance, field, value)
            changed.append(field)
    return changed


async def _workspace_name(session: AsyncSession, workspace_id: str) -> str | None:
    stmt = select(Workspace.name).where(Workspace.id == workspace_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _plan_or_404(session: AsyncSession, plan_id: str) -> LicensePlan:
    plan = await session.get(LicensePlan, plan_id)
    if plan is None:
        raise NotFound("That plan does not exist.")
    return plan


def _license_lookup(workspace_id: str, license_id: str, *, lock: bool = False) -> Select:
    """The scoped licence query; ``lock`` takes the row ``FOR UPDATE``.

    Everything that changes a licence's seats or status reads a number, decides,
    and writes -- "9 of 10 assigned, so one more is fine". Under READ COMMITTED
    two such transactions do not see each other's uncommitted rows, so both
    decide the same thing and both write: a licence one seat over its ceiling, a
    user holding two active seats, a purchase that vanished. Taking the licence
    row first makes those transactions queue on it, and the loser then reads
    what the winner committed. SQLite has no row locks and serialises writers
    anyway, so the clause is simply not emitted there.

    ``populate_existing`` goes with the lock: should the session already hold
    the licence, the locked read must overwrite that copy, or the loser decides
    on the numbers it had before it waited.
    """
    stmt = select(TenantLicense).where(
        TenantLicense.id == license_id,
        TenantLicense.tenant_workspace_id == workspace_id,
    )
    if not lock:
        return stmt
    return stmt.with_for_update().execution_options(populate_existing=True)


async def _license_or_404(
    session: AsyncSession, principal: Principal, license_id: str, *, lock: bool = False
) -> TenantLicense:
    stmt = _license_lookup(principal.workspace_id, license_id, lock=lock)
    license_ = (await session.execute(stmt)).scalar_one_or_none()
    if license_ is None:
        # A licence owned by another workspace must be indistinguishable from
        # one that never existed, so this is 404 and never 403.
        raise NotFound("That license does not exist.")
    return license_


async def _active_seat_count(session: AsyncSession, license_id: str) -> int:
    stmt = select(func.count(SeatAssignment.id)).where(
        SeatAssignment.license_id == license_id,
        SeatAssignment.released_at.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


def _utilization(assigned: int, purchased: int) -> float:
    if purchased <= 0:
        return 0.0
    return round(assigned / purchased * 100, 1)


# --------------------------------------------------------------------------- #
# Read-model construction
# --------------------------------------------------------------------------- #


def _plan_read(plan: LicensePlan, counters: dict[str, int] | None) -> LicensePlanRead:
    counts = counters or {}
    purchased = counts.get("seats_purchased", 0)
    assigned = counts.get("seats_assigned", 0)
    return LicensePlanRead.model_validate(plan).model_copy(
        update={
            "license_count": counts.get("license_count", 0),
            "seats_purchased": purchased,
            "seats_assigned": assigned,
            "seat_utilization_pct": _utilization(assigned, purchased),
        }
    )


def _license_read(
    license_: TenantLicense,
    plan: LicensePlan | None,
    tenant_name: str | None,
    now: dt.datetime,
) -> TenantLicenseRead:
    return TenantLicenseRead.model_validate(license_).model_copy(
        update={
            "tenant_name": tenant_name,
            "plan_name": plan.name if plan is not None else None,
            "plan_code": plan.code if plan is not None else None,
            "plan_tier": _plan_tier(plan.tier) if plan is not None else None,
            "included_tokens": plan.included_tokens if plan is not None else None,
            "included_runs": plan.included_runs if plan is not None else None,
            "days_until_expiry": _days_until(license_.expires_at, now),
        }
    )


def _seat_read(seat: SeatAssignment, user: User | None) -> SeatAssignmentRead:
    return SeatAssignmentRead.model_validate(seat).model_copy(
        update={
            "user_name": user.full_name if user is not None else None,
            "user_email": user.email if user is not None else None,
            "user_initials": user.initials if user is not None else None,
        }
    )


async def _plan_counters(
    session: AsyncSession, workspace_id: str, plan_ids: list[str]
) -> dict[str, dict[str, int]]:
    """Take-up per plan for one workspace, as a single grouped aggregate."""
    if not plan_ids:
        return {}
    stmt = (
        select(
            TenantLicense.plan_id,
            func.count(TenantLicense.id),
            func.coalesce(func.sum(TenantLicense.seats_purchased), 0),
            func.coalesce(func.sum(TenantLicense.seats_assigned), 0),
        )
        .where(
            TenantLicense.tenant_workspace_id == workspace_id,
            TenantLicense.plan_id.in_(plan_ids),
        )
        .group_by(TenantLicense.plan_id)
    )
    return {
        plan_id: {
            "license_count": int(count or 0),
            "seats_purchased": int(purchased or 0),
            "seats_assigned": int(assigned or 0),
        }
        for plan_id, count, purchased, assigned in (await session.execute(stmt)).all()
    }


async def _hydrate_plans(
    session: AsyncSession, workspace_id: str, plans: list[LicensePlan]
) -> list[LicensePlanRead]:
    counters = await _plan_counters(session, workspace_id, [p.id for p in plans])
    return [_plan_read(plan, counters.get(plan.id)) for plan in plans]


async def _hydrate_licenses(
    session: AsyncSession,
    workspace_id: str,
    licenses: list[TenantLicense],
    now: dt.datetime,
) -> list[TenantLicenseRead]:
    if not licenses:
        return []
    plan_ids = {lic.plan_id for lic in licenses}
    plan_rows = (
        (await session.execute(select(LicensePlan).where(LicensePlan.id.in_(plan_ids))))
        .scalars()
        .all()
    )
    plans = {plan.id: plan for plan in plan_rows}
    tenant_name = await _workspace_name(session, workspace_id)
    return [_license_read(lic, plans.get(lic.plan_id), tenant_name, now) for lic in licenses]


async def _hydrate_seats(
    session: AsyncSession, seats: list[SeatAssignment]
) -> list[SeatAssignmentRead]:
    if not seats:
        return []
    user_rows = (
        (await session.execute(select(User).where(User.id.in_({s.user_id for s in seats}))))
        .scalars()
        .all()
    )
    users = {user.id: user for user in user_rows}
    return [_seat_read(seat, users.get(seat.user_id)) for seat in seats]


# --------------------------------------------------------------------------- #
# Statement builders, shared by the list endpoints and the CSV export
# --------------------------------------------------------------------------- #


def _plan_stmt(
    params: ListParams,
    *,
    tier: str | None,
    status: str | None,
    billing_period: str | None,
) -> Select:
    stmt = select(LicensePlan)
    stmt = apply_search(
        stmt, params, [LicensePlan.name, LicensePlan.code, LicensePlan.description]
    )
    stmt = apply_filters(
        stmt,
        {
            LicensePlan.tier: tier,
            LicensePlan.status: status,
            LicensePlan.billing_period: billing_period,
        },
    )
    return apply_sort(stmt, params, PLAN_SORTS, LicensePlan.name, default_desc=False)


def _license_stmt(
    params: ListParams,
    workspace_id: str,
    now: dt.datetime,
    *,
    status: str | None,
    plan_id: str | None,
    tier: str | None,
    auto_renew: bool | None,
    expiring_in_days: int | None,
) -> Select:
    stmt = (
        select(TenantLicense)
        .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
        .join(Workspace, Workspace.id == TenantLicense.tenant_workspace_id)
        .where(TenantLicense.tenant_workspace_id == workspace_id)
    )
    stmt = apply_search(
        stmt,
        params,
        [
            Workspace.name,
            LicensePlan.name,
            LicensePlan.code,
            TenantLicense.purchase_order_ref,
            TenantLicense.billing_contact_email,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            TenantLicense.status: status,
            TenantLicense.plan_id: plan_id,
            LicensePlan.tier: tier,
            TenantLicense.auto_renew: auto_renew,
        },
    )
    if expiring_in_days is not None:
        horizon = now + dt.timedelta(days=expiring_in_days)
        stmt = stmt.where(
            TenantLicense.expires_at.is_not(None),
            TenantLicense.expires_at >= now,
            TenantLicense.expires_at <= horizon,
        )
    return apply_sort(stmt, params, LICENSE_SORTS, TenantLicense.created_at)


def _seat_stmt(
    params: ListParams,
    license_id: str,
    *,
    state: SeatState | None,
    role: str | None,
) -> Select:
    stmt = (
        select(SeatAssignment)
        .join(User, User.id == SeatAssignment.user_id)
        .where(SeatAssignment.license_id == license_id)
    )
    stmt = apply_search(stmt, params, [User.full_name, User.email])
    stmt = apply_filters(stmt, {SeatAssignment.role: role})
    if state == "active":
        stmt = stmt.where(SeatAssignment.released_at.is_(None))
    elif state == "released":
        stmt = stmt.where(SeatAssignment.released_at.is_not(None))
    return apply_sort(stmt, params, SEAT_SORTS, SeatAssignment.assigned_at)


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


async def list_plans(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    tier: str | None = None,
    status: str | None = None,
    billing_period: str | None = None,
) -> tuple[list[LicensePlanRead], int]:
    """Page the plan catalogue, annotated with this workspace's take-up."""
    stmt = _plan_stmt(params, tier=tier, status=status, billing_period=billing_period)
    rows, total = await paginate(session, stmt, params)
    return await _hydrate_plans(session, principal.workspace_id, list(rows)), total


async def get_plan(
    session: AsyncSession, principal: Principal, plan_id: str
) -> LicensePlanRead:
    """One plan from the catalogue."""
    plan = await _plan_or_404(session, plan_id)
    return (await _hydrate_plans(session, principal.workspace_id, [plan]))[0]


async def create_plan(
    session: AsyncSession,
    principal: Principal,
    payload: LicensePlanCreate,
    request: Request | None = None,
) -> LicensePlanRead:
    """Publish a new plan. Plan codes are the stable identifier and are unique."""
    principal.require(Role.OWNER)

    clash = (
        await session.execute(select(LicensePlan.id).where(LicensePlan.code == payload.code))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A plan with the code '{payload.code}' already exists.")

    plan = LicensePlan(
        name=payload.name.strip(),
        code=payload.code,
        tier=payload.tier.value,
        description=payload.description,
        price_per_seat_usd=payload.price_per_seat_usd,
        billing_period=payload.billing_period.value,
        included_seats=payload.included_seats,
        included_tokens=payload.included_tokens,
        included_runs=payload.included_runs,
        overage_rate_per_1k_tokens=payload.overage_rate_per_1k_tokens,
        features=list(payload.features),
        status=payload.status.value,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(plan)
    await _persist(session, plan)

    await audit.record(
        session,
        principal=principal,
        action="licensing.plan.created",
        entity_type="license_plan",
        entity_id=plan.id,
        entity_label=plan.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{plan.tier} plan '{plan.name}' ({plan.code}) created with status {plan.status}.",
        metadata={"code": plan.code, "tier": plan.tier, "status": plan.status},
        request=request,
    )
    return _plan_read(plan, None)


async def update_plan(
    session: AsyncSession,
    principal: Principal,
    plan_id: str,
    payload: LicensePlanUpdate,
    request: Request | None = None,
) -> LicensePlanRead:
    """Amend a plan. Retiring one is a status change, not a delete."""
    principal.require(Role.OWNER)
    plan = await _plan_or_404(session, plan_id)

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise ValidationFailed("Provide at least one field to update.")

    new_code = data.get("code")
    if new_code is not None and new_code != plan.code:
        clash = (
            await session.execute(
                select(LicensePlan.id).where(
                    LicensePlan.code == new_code, LicensePlan.id != plan.id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A plan with the code '{new_code}' already exists.")

    effective_from = data.get("effective_from", plan.effective_from)
    effective_to = data.get("effective_to", plan.effective_to)
    if effective_from is not None and effective_to is not None and effective_to <= effective_from:
        raise ValidationFailed("effective_to must be later than effective_from.")

    if "name" in data and isinstance(data["name"], str):
        data["name"] = data["name"].strip()

    changed = _apply_updates(plan, data)
    if not changed:
        return (await _hydrate_plans(session, principal.workspace_id, [plan]))[0]

    plan.updated_by = principal.actor
    await _persist(session, plan)

    # The plan row is written first, so a second edit to the same plan queues on
    # it before either touches a licence.
    reseeded = 0
    if ENTITLEMENT_PLAN_FIELDS.intersection(changed):
        reseeded = await _reseed_plan_licenses(session, plan)

    detail = f"Plan '{plan.name}' updated: {', '.join(sorted(changed))}."
    if reseeded:
        detail += f" Entitlements re-resolved for {reseeded} current license(s) on it."
    await audit.record(
        session,
        principal=principal,
        action="licensing.plan.updated",
        entity_type="license_plan",
        entity_id=plan.id,
        entity_label=plan.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"fields": sorted(changed), "licenses_reseeded": reseeded},
        request=request,
    )
    return (await _hydrate_plans(session, principal.workspace_id, [plan]))[0]


async def delete_plan(
    session: AsyncSession,
    principal: Principal,
    plan_id: str,
    request: Request | None = None,
) -> None:
    """Remove a plan that was never sold. Sold plans must be retired instead."""
    principal.require(Role.OWNER)
    plan = await _plan_or_404(session, plan_id)

    in_use = (
        await session.execute(
            select(func.count(TenantLicense.id)).where(TenantLicense.plan_id == plan.id)
        )
    ).scalar_one()
    if in_use:
        raise Conflict(
            f"{in_use} tenant license(s) reference this plan. "
            "Retire it by setting its status to Retired instead of deleting it."
        )

    label, code = plan.name, plan.code
    await session.delete(plan)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="licensing.plan.deleted",
        entity_type="license_plan",
        entity_id=plan_id,
        entity_label=label,
        source_screen=SOURCE_SCREEN,
        detail=f"Plan '{label}' ({code}) deleted; it had no tenant licenses.",
        request=request,
    )


# --------------------------------------------------------------------------- #
# Tenant licences
# --------------------------------------------------------------------------- #


async def list_licenses(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    plan_id: str | None = None,
    tier: str | None = None,
    auto_renew: bool | None = None,
    expiring_in_days: int | None = None,
) -> tuple[list[TenantLicenseRead], int]:
    """Page the licences held by the calling workspace."""
    now = _now()
    stmt = _license_stmt(
        params,
        principal.workspace_id,
        now,
        status=status,
        plan_id=plan_id,
        tier=tier,
        auto_renew=auto_renew,
        expiring_in_days=expiring_in_days,
    )
    rows, total = await paginate(session, stmt, params)
    items = await _hydrate_licenses(session, principal.workspace_id, list(rows), now)
    return items, total


async def get_license(
    session: AsyncSession, principal: Principal, license_id: str
) -> TenantLicenseRead:
    """One licence, with its plan allowances and renewal countdown resolved."""
    license_ = await _license_or_404(session, principal, license_id)
    items = await _hydrate_licenses(session, principal.workspace_id, [license_], _now())
    return items[0]


def _feature_keys(plan: LicensePlan) -> list[str]:
    """The plan's feature bullets that can be entitlement keys, once each.

    A bullet is marketing copy and a key is ``(license_id, key)``-unique and 120
    characters wide, so not every bullet can be one: two that read the same
    collide, one named like a ceiling collides with the ceiling's own row, and
    one past the column's width is refused outright by Postgres. Any of those
    failed the whole insert with a 500 -- when a licence was issued on the plan
    and, now that a plan edit re-resolves the licences sold on it, on the edit
    itself. The plan card still shows every bullet; what is dropped here is only
    a row nothing could have looked up (``entitlement-check`` caps ``key`` at
    the same width) or that the ceiling already stands for.
    """
    width = Entitlement.key.type.length
    keys: list[str] = []
    for feature in plan.features or []:
        key = str(feature).strip()
        if not key or key in keys or key in CEILING_KEYS:
            continue
        if width is not None and len(key) > width:
            continue
        keys.append(key)
    return keys


async def _seed_entitlements(
    session: AsyncSession, license_: TenantLicense, plan: LicensePlan
) -> None:
    """Turn a plan into the enforceable rows the gate actually reads.

    A plan's ``features`` list and its ``included_*`` numbers describe what was
    sold; :class:`Entitlement` rows are what ``check_entitlement`` resolves.
    Without this step a licence looks correct on screen while every gated
    capability answers "not granted", because nothing ever wrote the rows.

    Feature flags are granted as soft entitlements — present and true, but not
    blocking — while the metered ceilings are hard, because a token or run
    ceiling is the thing a plan actually sells and letting it run past silently
    is how a bill becomes a surprise.
    """
    rows: list[Entitlement] = []

    for key in _feature_keys(plan):
        rows.append(
            Entitlement(
                license_id=license_.id,
                key=key,
                value_type=EntitlementValueType.BOOL.value,
                bool_value=True,
                hard_limit=False,
            )
        )

    for key in CEILING_KEYS:
        amount = getattr(plan, key)
        if not amount:
            continue
        rows.append(
            Entitlement(
                license_id=license_.id,
                key=key,
                value_type=EntitlementValueType.INT.value,
                int_value=int(amount),
                hard_limit=True,
            )
        )

    if not rows:
        return
    session.add_all(rows)
    await session.flush()


async def _reseed_entitlements(
    session: AsyncSession, licenses: Sequence[TenantLicense], plan: LicensePlan
) -> None:
    """Re-resolve the entitlement rows of licences that are on ``plan``.

    :func:`_seed_entitlements` ran once, when the licence was issued, and nothing
    ran it again. Moving a licence to another plan, or editing the plan it is on,
    changed the plan card and the feature chips -- both read the plan live -- and
    left the rows the gate reads exactly as they were: an upgrade to 100M tokens
    went on answering 402 at the old 1M, and a downgrade kept every feature of
    the plan it had left.

    What goes is what a plan seeds: the three ceilings, the new plan's own keys,
    and every soft boolean grant -- the shape a feature bullet is seeded in. That
    last clause is what clears the features of a plan the licence has *left*,
    including one it left before this existed. A row of any other shape (a hard
    ``ingest.enabled = false``, an integer ``max_agents``) was put there by an
    operator rather than resolved from a plan, and is kept.
    """
    if not licenses:
        return
    keys = set(_feature_keys(plan)) | set(CEILING_KEYS)
    await session.execute(
        delete(Entitlement)
        .where(
            Entitlement.license_id.in_([license_.id for license_ in licenses]),
            or_(
                Entitlement.key.in_(sorted(keys)),
                and_(
                    Entitlement.value_type == EntitlementValueType.BOOL.value,
                    Entitlement.bool_value.is_(True),
                    Entitlement.hard_limit.is_(False),
                ),
            ),
        )
        .execution_options(synchronize_session=False)
    )
    for license_ in licenses:
        await _seed_entitlements(session, license_, plan)


async def _reseed_plan_licenses(session: AsyncSession, plan: LicensePlan) -> int:
    """Carry an edit to a plan through to every current licence sold on it.

    Plans are global, so this crosses workspaces -- which is the point: the plan
    is what each of them was sold. Revoked and expired licences are history and
    keep the rows they ended with. The licence rows are taken ``FOR UPDATE``, in
    id order, so this queues behind a plan change or a seat assignment in flight
    on one of them instead of interleaving its delete and insert with theirs.
    """
    licenses = (
        (
            await session.execute(
                select(TenantLicense)
                .where(
                    TenantLicense.plan_id == plan.id,
                    TenantLicense.status.in_(CURRENT_STATUSES),
                )
                .order_by(TenantLicense.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    await _reseed_entitlements(session, licenses, plan)
    return len(licenses)


async def create_license(
    session: AsyncSession,
    principal: Principal,
    payload: TenantLicenseCreate,
    request: Request | None = None,
) -> TenantLicenseRead:
    """Issue a licence to the calling workspace.

    A workspace holds at most one current licence at a time. Entitlement
    resolution has to be unambiguous, so a second one is refused until the
    existing licence is revoked or has expired.
    """
    principal.require(Role.OWNER)

    if (
        payload.tenant_workspace_id is not None
        and payload.tenant_workspace_id != principal.workspace_id
    ):
        raise ValidationFailed(
            "A license can only be issued to the workspace you are signed in to."
        )

    plan = await _plan_or_404(session, payload.plan_id)
    if plan.status == PlanStatus.RETIRED.value:
        raise ValidationFailed(f"Plan '{plan.name}' is retired and can no longer be sold.")

    existing = (
        await session.execute(
            select(TenantLicense.id)
            .where(
                TenantLicense.tenant_workspace_id == principal.workspace_id,
                TenantLicense.status.in_(CURRENT_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(
            "This workspace already holds a current license. "
            "Amend or revoke it before issuing another."
        )

    now = _now()
    # The schema can only compare the two dates when both are sent, and the
    # console never sends ``starts_at``: picking today or an earlier day in the
    # Expires field issued a licence whose term ended before it began. That row
    # then refused every amendment and still blocked a replacement (409).
    if payload.expires_at is not None and _as_utc(payload.expires_at) <= (
        _as_utc(payload.starts_at) or now
    ):
        raise ValidationFailed(
            "expires_at must be later than the start of the term"
            + ("." if payload.starts_at is not None else ", which is now.")
        )

    license_ = TenantLicense(
        tenant_workspace_id=principal.workspace_id,
        plan_id=plan.id,
        status=payload.status.value,
        seats_purchased=payload.seats_purchased,
        seats_assigned=0,
        starts_at=payload.starts_at or now,
        expires_at=payload.expires_at,
        auto_renew=payload.auto_renew,
        billing_contact_email=payload.billing_contact_email,
        purchase_order_ref=payload.purchase_order_ref,
        notes=payload.notes,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(license_)
    await _persist(session, license_)
    await _seed_entitlements(session, license_, plan)

    await audit.record(
        session,
        principal=principal,
        action="licensing.license.created",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=plan.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"License issued on plan '{plan.name}' with {license_.seats_purchased} seat(s), "
            f"status {license_.status}."
        ),
        metadata={"plan_code": plan.code, "seats_purchased": license_.seats_purchased},
        request=request,
    )
    workspace_name = await _workspace_name(session, principal.workspace_id)
    return _license_read(license_, plan, workspace_name, now)


async def update_license(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    payload: TenantLicenseUpdate,
    request: Request | None = None,
) -> TenantLicenseRead:
    """Amend a licence, including the plan change behind Upgrade Plan.

    The row is locked: the seat floor below is a count read and then relied on,
    and the entitlement rows are deleted and written again, neither of which
    survives a second amendment or a seat assignment running alongside it.
    """
    principal.require(Role.OWNER)
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status in TERMINAL_STATUSES:
        raise PreconditionFailed(
            f"This license is {license_.status.lower()} and can no longer be amended."
        )

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise ValidationFailed("Provide at least one field to update.")

    previous_plan_id = license_.plan_id
    new_plan_id = data.get("plan_id")
    plan_moved = new_plan_id is not None and new_plan_id != previous_plan_id
    sent_plan: LicensePlan | None = None
    if new_plan_id is not None:
        sent_plan = await _plan_or_404(session, new_plan_id)
        if plan_moved and sent_plan.status == PlanStatus.RETIRED.value:
            raise ValidationFailed(
                f"Plan '{sent_plan.name}' is retired and cannot be assigned to a license."
            )

    if "seats_purchased" in data:
        assigned = await _active_seat_count(session, license_.id)
        if data["seats_purchased"] < assigned:
            raise ValidationFailed(
                f"{assigned} seat(s) are currently assigned; purchased seats cannot drop "
                "below that. Release seats first.",
                details={"seats_assigned": assigned, "requested": data["seats_purchased"]},
            )

    # A suspension is lifted by ``reactivate_license`` and nowhere else: it is
    # what checks the term has not lapsed and clears ``suspended_at`` and the
    # reason. ``status: Active`` through here skipped both, so a licence came
    # back into service past its end date still labelled with why it was stopped.
    if "status" in data and license_.status == LicenseStatus.SUSPENDED.value:
        raise PreconditionFailed(
            "This license is suspended. Reactivate it to return it to service; "
            "its status cannot be set directly."
        )

    # The term is judged only when the request moves one of its dates. Judging
    # the *stored* term on every PATCH meant a licence that already held a bad
    # one refused a plan change or a seat change it had nothing to do with --
    # and the way out, sending a later ``expires_at``, still passes through here.
    if "starts_at" in data or "expires_at" in data:
        starts_at = _as_utc(data.get("starts_at", license_.starts_at))
        expires_at = _as_utc(data.get("expires_at", license_.expires_at))
        if starts_at is not None and expires_at is not None and expires_at <= starts_at:
            raise ValidationFailed("expires_at must be later than starts_at.")

    changed = _apply_updates(license_, data)
    now = _now()

    # Whenever the request names a plan, not only when it names a different one.
    # The rows are a pure function of the plan, so writing them again is
    # harmless, and it is the way back for a licence that changed plan before
    # this existed and still carries the old plan's limits: Change Plan, keep the
    # plan, save.
    if sent_plan is not None:
        await _reseed_entitlements(session, [license_], sent_plan)

    if not changed:
        items = await _hydrate_licenses(session, principal.workspace_id, [license_], now)
        return items[0]

    license_.updated_by = principal.actor
    await _persist(session, license_)

    detail = f"License updated: {', '.join(sorted(changed))}."
    if plan_moved:
        detail = (
            f"License moved from plan {previous_plan_id} to {new_plan_id}; its "
            "entitlements were re-resolved from the new plan. " + detail
        )

    await audit.record(
        session,
        principal=principal,
        action="licensing.license.updated",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=license_.purchase_order_ref or license_.id,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"fields": sorted(changed), "previous_plan_id": previous_plan_id},
        request=request,
    )
    items = await _hydrate_licenses(session, principal.workspace_id, [license_], now)
    return items[0]


async def delete_license(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    request: Request | None = None,
) -> None:
    """Delete a licence that was never billed; a billed one is suspended or revoked."""
    principal.require(Role.OWNER)
    license_ = await _license_or_404(session, principal, license_id)

    invoiced = (
        await session.execute(
            select(func.count(Invoice.id)).where(Invoice.license_id == license_.id)
        )
    ).scalar_one()
    if invoiced:
        raise Conflict(
            f"This license has {invoiced} invoice(s) and cannot be deleted. "
            "Suspend or revoke it instead so the billing history stays intact."
        )

    plan_id = license_.plan_id
    # ``AsyncSession.delete`` is awaited because the delete-orphan cascades on
    # seats, entitlements and usage records need loading first.
    await session.delete(license_)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="licensing.license.deleted",
        entity_type="tenant_license",
        entity_id=license_id,
        entity_label=license_id,
        source_screen=SOURCE_SCREEN,
        detail="License deleted along with its seats, entitlements and usage records.",
        metadata={"plan_id": plan_id},
        request=request,
    )


async def suspend_license(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    payload: LicenseSuspendRequest,
    request: Request | None = None,
) -> ActionResult:
    """Suspend a licence. New seat assignment is blocked from this moment on.

    So is everything :func:`enforce_entitlement` gates, and that includes agent
    telemetry: every ingest batch for the workspace answers 402 until the
    licence is reactivated, and the SDKs treat 402 as final and drop the batch.
    That is what a suspension means, but it is not something to find out from a
    silent Live Runs screen, so the result says it in as many words.
    """
    principal.require(Role.OWNER)
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status == LicenseStatus.SUSPENDED.value:
        raise Conflict("This license is already suspended.")
    if license_.status in TERMINAL_STATUSES:
        raise PreconditionFailed(
            f"This license is {license_.status.lower()} and cannot be suspended."
        )

    now = _now()
    license_.status = LicenseStatus.SUSPENDED.value
    license_.suspended_at = now
    license_.suspended_reason = payload.reason
    license_.updated_by = principal.actor

    released = 0
    if payload.release_seats:
        result = await session.execute(
            update(SeatAssignment)
            .where(
                SeatAssignment.license_id == license_.id,
                SeatAssignment.released_at.is_(None),
            )
            .values(released_at=now)
        )
        released = int(result.rowcount or 0)
        license_.seats_assigned = await _active_seat_count(session, license_.id)

    await _persist(session, license_)

    reason = payload.reason or "no reason recorded"
    await audit.record(
        session,
        principal=principal,
        action="licensing.license.suspended",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=license_.purchase_order_ref or license_.id,
        source_screen=SOURCE_SCREEN,
        detail=f"License suspended ({reason}); {released} seat(s) released.",
        metadata={"reason": payload.reason, "seats_released": released},
        request=request,
    )
    return ActionResult(
        message=(
            "License suspended. Until it is reactivated, new seat assignment is blocked "
            "and agent telemetry for this workspace is refused (402), not queued."
        ),
        entity_id=license_.id,
        data={
            "status": license_.status,
            "suspended_at": _iso(license_.suspended_at),
            "seats_released": released,
            "seats_assigned": license_.seats_assigned,
            "ingest_refused": True,
        },
    )


async def revoke_license(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    payload: LicenseRevokeRequest,
    request: Request | None = None,
) -> ActionResult:
    """Revoke a licence for good and free the workspace to be licensed again.

    ``Revoked`` was in the status list, the grid's filter and the KPI, and the
    409 on a second licence told the owner to "revoke" the first -- but nothing
    could write it. A licence issued by mistake, or suspended past the end of
    its term, was a dead end: it could not be replaced, and deleting it stops
    being possible the moment an invoice exists.

    Revocation is terminal, unlike suspension: the row stays as history, can no
    longer be amended, and stops being the workspace's current licence, so a new
    one can be issued. Every seat still held is released -- a licence that serves
    nobody holds no seats -- and the reason is kept on the audit event, which is
    where the Audit Log tab reads it.
    """
    principal.require(Role.OWNER)
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status == LicenseStatus.REVOKED.value:
        raise Conflict("This license is already revoked.")
    if license_.status == LicenseStatus.EXPIRED.value:
        raise PreconditionFailed("This license has expired; there is nothing left to revoke.")

    now = _now()
    previous_status = license_.status
    result = await session.execute(
        update(SeatAssignment)
        .where(
            SeatAssignment.license_id == license_.id,
            SeatAssignment.released_at.is_(None),
        )
        .values(released_at=now)
    )
    released = int(result.rowcount or 0)

    license_.status = LicenseStatus.REVOKED.value
    license_.seats_assigned = 0
    license_.updated_by = principal.actor
    await _persist(session, license_)

    reason = payload.reason or "no reason recorded"
    await audit.record(
        session,
        principal=principal,
        action="licensing.license.revoked",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=license_.purchase_order_ref or license_.id,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"License revoked ({reason}); it was {previous_status}. "
            f"{released} seat(s) released."
        ),
        metadata={
            "reason": payload.reason,
            "previous_status": previous_status,
            "seats_released": released,
        },
        request=request,
    )
    return ActionResult(
        message=(
            "License revoked. Its seats were released and it can no longer be amended; "
            "a new license can now be issued to this workspace."
        ),
        entity_id=license_.id,
        data={
            "status": license_.status,
            "previous_status": previous_status,
            "seats_released": released,
            "seats_assigned": license_.seats_assigned,
        },
    )


async def reactivate_license(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    request: Request | None = None,
) -> ActionResult:
    """Lift a suspension and return the licence to service."""
    principal.require(Role.OWNER)
    # Locked, so a revocation landing at the same moment is not written over.
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status != LicenseStatus.SUSPENDED.value:
        raise Conflict("Only a suspended license can be reactivated.")

    now = _now()
    if license_.expires_at is not None and license_.expires_at <= now:
        raise PreconditionFailed(
            "This license term has already ended. Extend expires_at before reactivating it."
        )

    horizon = now + dt.timedelta(days=EXPIRY_HORIZON_DAYS)
    if license_.expires_at is not None and license_.expires_at <= horizon:
        license_.status = LicenseStatus.EXPIRING_SOON.value
    else:
        license_.status = LicenseStatus.ACTIVE.value
    license_.suspended_at = None
    license_.suspended_reason = None
    license_.updated_by = principal.actor
    await _persist(session, license_)

    await audit.record(
        session,
        principal=principal,
        action="licensing.license.reactivated",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=license_.purchase_order_ref or license_.id,
        source_screen=SOURCE_SCREEN,
        detail=f"License reactivated with status {license_.status}.",
        metadata={"status": license_.status},
        request=request,
    )
    return ActionResult(
        message="License reactivated. Seats can be assigned again.",
        entity_id=license_.id,
        data={
            "status": license_.status,
            "seats_assigned": license_.seats_assigned,
            "seats_available": license_.seats_available,
        },
    )


# --------------------------------------------------------------------------- #
# Entitlements
# --------------------------------------------------------------------------- #


def _included_allowance(plan: LicensePlan | None, metric: str) -> int | None:
    """The plan's allowance for one meter, or ``None`` when it sets none.

    Zero is how a plan says "not capped" -- it is the column default, and
    :func:`_seed_entitlements` writes no ceiling for it -- so it must not read as
    an allowance of nothing. It did, which was invisible while nothing metered
    usage and would have flagged the first token on such a plan as overage.
    """
    if plan is None:
        return None
    if metric == UsageMetric.TOKENS.value:
        return plan.included_tokens or None
    if metric == UsageMetric.RUNS.value:
        return plan.included_runs or None
    if metric == UsageMetric.SEATS.value:
        return plan.included_seats or None
    return None


async def _usage_breakdown(
    session: AsyncSession, license_id: str, plan: LicensePlan | None, now: dt.datetime
) -> list[EntitlementUsageRead]:
    """Usage per meter for the rolling window, aggregated in SQL."""
    window_start = now - dt.timedelta(days=USAGE_WINDOW_DAYS)
    stmt = (
        select(
            UsageRecord.metric,
            func.coalesce(func.sum(UsageRecord.quantity), 0),
            func.coalesce(func.sum(UsageRecord.amount_usd), 0),
        )
        .where(UsageRecord.license_id == license_id, UsageRecord.period_end >= window_start)
        .group_by(UsageRecord.metric)
        .order_by(UsageRecord.metric.asc())
    )

    rows: list[EntitlementUsageRead] = []
    for raw_metric, quantity, amount in (await session.execute(stmt)).all():
        metric = _usage_metric(raw_metric)
        if metric is None:
            continue
        used = _as_decimal(quantity) or Decimal("0")
        included = _included_allowance(plan, raw_metric)
        remaining: Decimal | None = None
        utilization: float | None = None
        over_limit = False
        if included is not None:
            remaining = Decimal(included) - used
            over_limit = used > Decimal(included)
            utilization = round(float(used) / included * 100, 1)
        rows.append(
            EntitlementUsageRead(
                metric=metric,
                used=used,
                included=included,
                remaining=remaining,
                utilization_pct=utilization,
                over_limit=over_limit,
                amount_usd=_as_decimal(amount) or Decimal("0"),
            )
        )
    return rows


async def license_entitlements(
    session: AsyncSession, principal: Principal, license_id: str
) -> TenantEntitlementsRead:
    """The resolved entitlement set and metered usage for one licence."""
    license_ = await _license_or_404(session, principal, license_id)
    plan = await session.get(LicensePlan, license_.plan_id)
    now = _now()

    entitlement_rows = (
        (
            await session.execute(
                select(Entitlement)
                .where(Entitlement.license_id == license_.id)
                .order_by(Entitlement.key.asc())
            )
        )
        .scalars()
        .all()
    )
    usage = await _usage_breakdown(session, license_.id, plan, now)

    return TenantEntitlementsRead(
        license_id=license_.id,
        tenant_workspace_id=license_.tenant_workspace_id,
        tenant_name=await _workspace_name(session, license_.tenant_workspace_id),
        plan_id=license_.plan_id,
        plan_name=plan.name if plan is not None else None,
        plan_tier=_plan_tier(plan.tier) if plan is not None else None,
        status=LicenseStatus(license_.status),
        seats_purchased=license_.seats_purchased,
        seats_assigned=license_.seats_assigned,
        seats_available=license_.seats_available,
        features=list(plan.features or []) if plan is not None else [],
        entitlements=[EntitlementRead.model_validate(row) for row in entitlement_rows],
        usage=usage,
        usage_window_days=USAGE_WINDOW_DAYS,
        over_limit_metrics=[row.metric for row in usage if row.over_limit],
    )


async def _current_license(
    session: AsyncSession, workspace_id: str, *, include_suspended: bool = False
) -> TenantLicense | None:
    """The single licence a workspace currently holds, if it holds one."""
    statuses = CURRENT_STATUSES if include_suspended else LIVE_STATUSES
    stmt = (
        select(TenantLicense)
        .where(
            TenantLicense.tenant_workspace_id == workspace_id,
            TenantLicense.status.in_(statuses),
        )
        .order_by(TenantLicense.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _entitlement_for(
    session: AsyncSession, license_id: str, key: str
) -> Entitlement | None:
    stmt = select(Entitlement).where(
        Entitlement.license_id == license_id, Entitlement.key == key
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def check_entitlement(
    session: AsyncSession, workspace_id: str, key: str
) -> bool | int | str | None:
    """Resolve one entitlement value for a workspace.

    This is the read side other domains call before offering a gated feature —
    ``await check_entitlement(session, principal.workspace_id, "guardrails.ml_detection")``.
    ``None`` means *not entitled*: either the workspace has no live licence, or
    the licence does not grant the key.
    """
    license_ = await _current_license(session, workspace_id)
    if license_ is None:
        return None
    entitlement = await _entitlement_for(session, license_.id, key)
    return entitlement.value if entitlement is not None else None


async def enforce_entitlement(
    session: AsyncSession,
    workspace_id: str,
    key: str,
    *,
    amount: int = 1,
    current_usage: int = 0,
) -> bool | int | str | None:
    """Raise :class:`QuotaExceeded` when a hard entitlement would be breached.

    Soft entitlements (``hard_limit`` false) never raise: the tenant is warned on
    Usage & Limits and let through, which is what the shipped enforcement policy
    — soft block plus an upgrade prompt — describes.

    A workspace with no licence row at all is *not* blocked. Absence of a licence
    is a data gap, not a breached limit, and refusing every unlicensed workspace
    would take unrelated surfaces down with it.

    Returns the resolved value so a caller can branch on it without a second
    round trip.
    """
    license_ = await _current_license(session, workspace_id, include_suspended=True)
    if license_ is None:
        return None
    if license_.status == LicenseStatus.SUSPENDED.value:
        raise QuotaExceeded(
            "This workspace's license is suspended. Reactivate it to continue.",
            details={"entitlement": key, "license_status": license_.status},
        )

    entitlement = await _entitlement_for(session, license_.id, key)
    if entitlement is None:
        return None

    if entitlement.value_type == EntitlementValueType.BOOL.value:
        if entitlement.hard_limit and not entitlement.bool_value:
            raise QuotaExceeded(
                f"Your plan does not include '{key}'. Upgrade the plan to enable it.",
                details={"entitlement": key},
            )
        return entitlement.bool_value

    if entitlement.value_type == EntitlementValueType.INT.value:
        limit = entitlement.int_value
        projected = current_usage + amount
        if limit is not None and entitlement.hard_limit and projected > limit:
            raise QuotaExceeded(
                f"'{key}' is capped at {limit:,} on your plan; this request would "
                f"take you to {projected:,}.",
                details={
                    "entitlement": key,
                    "limit": limit,
                    "current_usage": current_usage,
                    "requested": amount,
                },
            )
        return limit

    return entitlement.string_value


# --------------------------------------------------------------------------- #
# Metering
# --------------------------------------------------------------------------- #


async def _meter(
    session: AsyncSession,
    license_id: str,
    metric: str,
    amount: int,
    *,
    day: dt.datetime,
    now: dt.datetime,
    source: str,
) -> None:
    """Add ``amount`` to one licence's bucket for one meter and one UTC day."""
    bucket = (
        await session.execute(
            select(UsageRecord.id)
            .where(
                UsageRecord.license_id == license_id,
                UsageRecord.metric == metric,
                UsageRecord.period_start == day,
                UsageRecord.source == source,
            )
            # Oldest first, so every writer settles on the same row even if the
            # day's first two batches raced and each opened a bucket.
            .order_by(UsageRecord.created_at.asc(), UsageRecord.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if bucket is None:
        session.add(
            UsageRecord(
                license_id=license_id,
                period_start=day,
                period_end=day + dt.timedelta(days=1),
                metric=metric,
                quantity=Decimal(amount),
                recorded_at=now,
                source=source,
            )
        )
        await session.flush()
        return
    # Incremented *in the database*, for the reason ``ingest._commit_quotas``
    # gives: four workers reading a number and writing it back lose updates.
    await session.execute(
        update(UsageRecord)
        .where(UsageRecord.id == bucket)
        .values(quantity=UsageRecord.quantity + Decimal(amount), recorded_at=now)
        .execution_options(synchronize_session=False)
    )


async def record_usage(
    session: AsyncSession,
    workspace_id: str,
    *,
    tokens: int = 0,
    runs: int = 0,
    source: str = USAGE_SOURCE_INGEST,
) -> None:
    """Meter what a workspace just consumed against the licence it holds.

    Usage & Limits, the Overage Alerts KPI and the usage export all *read*
    ``usage_records``, and nothing in the product ever wrote one: a tenant could
    ingest for months and the tab still said nothing had been metered. This is
    the write side. The ingest path calls it once per batch, after the telemetry
    store has taken the batch, with the tokens and runs that were accepted.

    One row per licence, meter and UTC day, grown by a SQL increment. Day
    buckets keep the table small however busy a tenant is while still letting
    the rolling window slide a day at a time. There is no unique key on the
    bucket, so two workers can each open one for the day's first batch; every
    reader sums, and later increments settle on the older row, so the totals
    stay right either way.

    A suspended licence is still metered -- what was consumed was consumed --
    and a workspace holding no licence has nothing to meter against.

    Strictly best effort, inside a savepoint: by now the batch is stored, and a
    fault in metering must not become a 500 that the reporter answers by sending
    the whole batch again.
    """
    amounts = {
        UsageMetric.TOKENS.value: int(tokens or 0),
        UsageMetric.RUNS.value: int(runs or 0),
    }
    if not any(amount > 0 for amount in amounts.values()):
        return
    try:
        async with session.begin_nested():
            license_ = await _current_license(session, workspace_id, include_suspended=True)
            if license_ is None:
                return
            now = _now()
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # A fixed order, so two workers metering the same licence cannot
            # take the two bucket rows in opposite orders and deadlock.
            for metric in sorted(amounts):
                if amounts[metric] > 0:
                    await _meter(
                        session,
                        license_.id,
                        metric,
                        amounts[metric],
                        day=day,
                        now=now,
                        source=source,
                    )
    except Exception:  # noqa: BLE001 - see above: never at the batch's expense
        log.exception("usage could not be metered for workspace %s", workspace_id)


# --------------------------------------------------------------------------- #
# Seats
# --------------------------------------------------------------------------- #


async def list_seats(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    params: ListParams,
    *,
    state: SeatState | None = None,
    role: str | None = None,
) -> tuple[list[SeatAssignmentRead], int]:
    """Page the seats issued against one licence, released ones included."""
    license_ = await _license_or_404(session, principal, license_id)
    stmt = _seat_stmt(params, license_.id, state=state, role=role)
    rows, total = await paginate(session, stmt, params)
    return await _hydrate_seats(session, list(rows)), total


async def assign_seat(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    payload: SeatAssignmentCreate,
    request: Request | None = None,
) -> SeatAssignmentRead:
    """Give a user a seat on a licence.

    Enforces the two invariants the table cannot: one active seat per user per
    licence, and never more active seats than were purchased. The seat count is
    re-read from the database inside this transaction rather than trusted from
    the denormalised counter -- but a re-read alone does not stop two concurrent
    assignments: under READ COMMITTED neither sees the other's uncommitted seat,
    so both counted 9 of 10 and the licence ended at 11. What stops them is the
    lock on the licence row (:func:`_license_lookup`): the second assignment
    waits for the first to commit and then counts its seat.

    The two lookups below tolerate a user who already holds *two* active seats,
    which is what such a race left behind (a retry after a client timeout is
    enough). ``scalar_one_or_none`` raised on that row, so every later Assign or
    Reassign involving that user answered 500 and the seat could no longer be
    managed; now Assign answers the 409 it should, and Reassign releases every
    seat the outgoing user holds, which is also how the duplicate is cleared.
    """
    principal.require(Role.ADMIN)
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status in SEAT_BLOCKING_STATUSES:
        raise PreconditionFailed(
            f"This license is {license_.status.lower()}; seats cannot be assigned "
            "until it is reactivated."
        )

    user = await session.get(User, payload.user_id)
    if user is None or not user.is_active:
        raise NotFound("That user does not exist.")

    membership = (
        await session.execute(
            select(Membership.id).where(
                Membership.user_id == user.id,
                Membership.workspace_id == license_.tenant_workspace_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise ValidationFailed(
            f"{user.email} is not a member of the licensed workspace. "
            "Invite them before assigning a seat."
        )

    held = (
        await session.execute(
            select(SeatAssignment.id)
            .where(
                SeatAssignment.license_id == license_.id,
                SeatAssignment.user_id == user.id,
                SeatAssignment.released_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if held is not None:
        raise Conflict(f"{user.email} already holds an active seat on this license.")

    now = _now()
    replaced: SeatAssignment | None = None
    if payload.replaces_user_id is not None:
        outgoing = (
            (
                await session.execute(
                    select(SeatAssignment)
                    .where(
                        SeatAssignment.license_id == license_.id,
                        SeatAssignment.user_id == payload.replaces_user_id,
                        SeatAssignment.released_at.is_(None),
                    )
                    .order_by(SeatAssignment.assigned_at.asc(), SeatAssignment.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not outgoing:
            raise NotFound("That user holds no active seat on this license.")
        for row in outgoing:
            row.released_at = now
        replaced = outgoing[0]
        await session.flush()

    active = await _active_seat_count(session, license_.id)
    if active >= license_.seats_purchased:
        raise QuotaExceeded(
            f"All {license_.seats_purchased} purchased seat(s) are assigned. "
            "Purchase more seats or release one before assigning another.",
            details={
                "seats_purchased": license_.seats_purchased,
                "seats_assigned": active,
            },
        )

    seat = SeatAssignment(
        license_id=license_.id,
        user_id=user.id,
        assigned_at=now,
        assigned_by_user_id=principal.user_id,
        role=payload.role.value,
    )
    session.add(seat)
    await _persist(session, seat)

    license_.seats_assigned = await _active_seat_count(session, license_.id)
    license_.updated_by = principal.actor
    await session.flush()

    action = "licensing.seat.reassigned" if replaced is not None else "licensing.seat.assigned"
    detail = f"Seat assigned to {user.email} as {seat.role}."
    if replaced is not None:
        detail = f"Seat moved from user {replaced.user_id} to {user.email} as {seat.role}."

    await audit.record(
        session,
        principal=principal,
        action=action,
        entity_type="seat_assignment",
        entity_id=seat.id,
        entity_label=user.email,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "license_id": license_.id,
            "user_id": user.id,
            "released_user_id": replaced.user_id if replaced is not None else None,
            "seats_assigned": license_.seats_assigned,
        },
        request=request,
    )
    return _seat_read(seat, user)


async def purchase_seats(
    session: AsyncSession,
    principal: Principal,
    license_id: str,
    payload: SeatPurchaseRequest,
    request: Request | None = None,
) -> ActionResult:
    """Add seats to a licence. This changes what the tenant is billed.

    ``seats_purchased + n`` is a read and a write-back, so the row is locked:
    two purchases in flight would otherwise both start from the same pool and
    the second would overwrite the first -- seats paid for and not there.
    """
    principal.require(Role.OWNER)
    license_ = await _license_or_404(session, principal, license_id, lock=True)

    if license_.status in TERMINAL_STATUSES:
        raise PreconditionFailed(
            f"This license is {license_.status.lower()}; seats cannot be purchased against it."
        )

    total = license_.seats_purchased + payload.seats
    if total > MAX_LICENSE_SEATS:
        raise ValidationFailed(
            f"A single license is capped at {MAX_LICENSE_SEATS:,} seats; "
            f"this purchase would take it to {total:,}.",
            details={"seats_purchased": license_.seats_purchased, "requested": payload.seats},
        )

    previous = license_.seats_purchased
    license_.seats_purchased = total
    if payload.purchase_order_ref is not None:
        license_.purchase_order_ref = payload.purchase_order_ref
    license_.updated_by = principal.actor
    await _persist(session, license_)

    note = f" Note: {payload.note}" if payload.note else ""
    await audit.record(
        session,
        principal=principal,
        action="licensing.seats.purchased",
        entity_type="tenant_license",
        entity_id=license_.id,
        entity_label=license_.purchase_order_ref or license_.id,
        source_screen=SOURCE_SCREEN,
        detail=f"Seat pool increased {previous:,} -> {total:,} (+{payload.seats:,}).{note}",
        metadata={
            "seats_added": payload.seats,
            "seats_purchased": total,
            "purchase_order_ref": payload.purchase_order_ref,
        },
        request=request,
    )
    return ActionResult(
        message=f"{payload.seats:,} seat(s) added. The license now carries {total:,}.",
        entity_id=license_.id,
        data={
            "seats_purchased": total,
            "seats_assigned": license_.seats_assigned,
            "seats_available": license_.seats_available,
            "seat_utilization_pct": license_.seat_utilization_pct,
        },
    )


# --------------------------------------------------------------------------- #
# KPI summary
# --------------------------------------------------------------------------- #


async def summary(session: AsyncSession, principal: Principal) -> LicensingSummary:
    """The KPI row on Licensing & Entitlements.

    Five aggregate statements, no row loading: the licence tallies collapse into
    one filtered-sum query, and overage is a grouped HAVING against each plan's
    included allowance.
    """
    now = _now()
    workspace_id = principal.workspace_id
    horizon = now + dt.timedelta(days=EXPIRY_HORIZON_DAYS)
    window_start = now - dt.timedelta(days=USAGE_WINDOW_DAYS)

    plans_total, plans_active = (
        await session.execute(
            select(
                func.count(LicensePlan.id),
                func.coalesce(
                    func.sum(
                        case((LicensePlan.status == PlanStatus.ACTIVE.value, 1), else_=0)
                    ),
                    0,
                ),
            )
        )
    ).one()

    license_stmt = select(
        func.coalesce(
            func.sum(case((TenantLicense.status.in_(LIVE_STATUSES), 1), else_=0)), 0
        ),
        func.coalesce(
            func.sum(case((TenantLicense.status == LicenseStatus.TRIAL.value, 1), else_=0)), 0
        ),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            TenantLicense.status.in_(LIVE_STATUSES),
                            TenantLicense.expires_at.is_not(None),
                            TenantLicense.expires_at >= now,
                            TenantLicense.expires_at <= horizon,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ),
        func.coalesce(
            func.sum(case((TenantLicense.status.in_(SUSPENDED_OR_REVOKED), 1), else_=0)), 0
        ),
        func.coalesce(
            func.sum(
                case(
                    (TenantLicense.status.in_(LIVE_STATUSES), TenantLicense.seats_purchased),
                    else_=0,
                )
            ),
            0,
        ),
    ).where(TenantLicense.tenant_workspace_id == workspace_id)
    active_licenses, trial_licenses, expiring, suspended, seats_purchased = (
        await session.execute(license_stmt)
    ).one()

    seats_assigned = (
        await session.execute(
            select(func.count(SeatAssignment.id))
            .join(TenantLicense, TenantLicense.id == SeatAssignment.license_id)
            .where(
                TenantLicense.tenant_workspace_id == workspace_id,
                SeatAssignment.released_at.is_(None),
            )
        )
    ).scalar_one()

    # An overage alert is one (licence, meter) pair whose usage in the window has
    # passed the allowance its plan includes for that meter.
    allowance = case(
        (UsageRecord.metric == UsageMetric.TOKENS.value, LicensePlan.included_tokens),
        (UsageRecord.metric == UsageMetric.RUNS.value, LicensePlan.included_runs),
        (UsageRecord.metric == UsageMetric.SEATS.value, LicensePlan.included_seats),
        else_=None,
    )
    breached = (
        select(UsageRecord.license_id)
        .join(TenantLicense, TenantLicense.id == UsageRecord.license_id)
        .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
        .where(
            TenantLicense.tenant_workspace_id == workspace_id,
            UsageRecord.period_end >= window_start,
        )
        .group_by(
            UsageRecord.license_id,
            UsageRecord.metric,
            LicensePlan.included_tokens,
            LicensePlan.included_runs,
            LicensePlan.included_seats,
        )
        # Zero is "not capped", exactly as in ``_included_allowance``: the KPI and
        # the Usage & Limits tab must agree on what counts as over.
        .having(
            allowance > 0,
            func.coalesce(func.sum(UsageRecord.quantity), 0) > allowance,
        )
        .subquery()
    )
    overage_alerts = (
        await session.execute(select(func.count()).select_from(breached))
    ).scalar_one()

    open_invoices, outstanding = (
        await session.execute(
            select(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total_usd), 0),
            )
            .join(TenantLicense, TenantLicense.id == Invoice.license_id)
            .where(
                TenantLicense.tenant_workspace_id == workspace_id,
                Invoice.status.in_(OPEN_INVOICE_STATUSES),
            )
        )
    ).one()

    purchased = int(seats_purchased or 0)
    assigned = int(seats_assigned or 0)
    return LicensingSummary(
        plans=int(plans_total or 0),
        active_plans=int(plans_active or 0),
        active_tenant_licenses=int(active_licenses or 0),
        trial_licenses=int(trial_licenses or 0),
        seats_purchased=purchased,
        seats_assigned=assigned,
        seats_available=max(purchased - assigned, 0),
        seat_utilization_pct=_utilization(assigned, purchased),
        expiring_in_30_days=int(expiring or 0),
        suspended_or_revoked=int(suspended or 0),
        overage_alerts=int(overage_alerts or 0),
        open_invoices=int(open_invoices or 0),
        outstanding_usd=_as_decimal(outstanding) or Decimal("0"),
        generated_at=now,
    )


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #


async def _export_plan_rows(
    session: AsyncSession, principal: Principal, params: ListParams, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    stmt = _plan_stmt(
        params,
        tier=filters.get("tier"),
        status=filters.get("status"),
        billing_period=filters.get("billing_period"),
    ).limit(MAX_EXPORT_ROWS)
    plans = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "name": item.name,
            "code": item.code,
            "tier": item.tier.value,
            "billing_period": item.billing_period.value,
            "price_per_seat_usd": item.price_per_seat_usd,
            "included_seats": item.included_seats,
            "included_tokens": item.included_tokens,
            "included_runs": item.included_runs,
            "status": item.status.value,
            "license_count": item.license_count,
            "seats_purchased": item.seats_purchased,
            "seats_assigned": item.seats_assigned,
            "seat_utilization_pct": item.seat_utilization_pct,
            "effective_from": _iso(item.effective_from),
            "effective_to": _iso(item.effective_to),
        }
        for item in await _hydrate_plans(session, principal.workspace_id, plans)
    ]


async def _export_license_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    filters: dict[str, Any],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    stmt = _license_stmt(
        params,
        principal.workspace_id,
        now,
        status=filters.get("status"),
        plan_id=filters.get("plan_id"),
        tier=filters.get("tier"),
        auto_renew=filters.get("auto_renew"),
        expiring_in_days=filters.get("expiring_in_days"),
    ).limit(MAX_EXPORT_ROWS)
    licenses = list((await session.execute(stmt)).scalars().all())
    items = await _hydrate_licenses(session, principal.workspace_id, licenses, now)
    return [
        {
            "id": item.id,
            "tenant_name": item.tenant_name,
            "plan_name": item.plan_name,
            "plan_tier": item.plan_tier.value if item.plan_tier is not None else "",
            "status": item.status.value,
            "seats_purchased": item.seats_purchased,
            "seats_assigned": item.seats_assigned,
            "seats_available": item.seats_available,
            "seat_utilization_pct": item.seat_utilization_pct,
            "included_tokens": item.included_tokens,
            "included_runs": item.included_runs,
            "starts_at": _iso(item.starts_at),
            "expires_at": _iso(item.expires_at),
            "days_until_expiry": item.days_until_expiry,
            "auto_renew": item.auto_renew,
            "billing_contact_email": item.billing_contact_email,
            "purchase_order_ref": item.purchase_order_ref,
            "suspended_at": _iso(item.suspended_at),
        }
        for item in items
    ]


async def _license_ids_in_scope(session: AsyncSession, workspace_id: str) -> list[str]:
    stmt = select(TenantLicense.id).where(TenantLicense.tenant_workspace_id == workspace_id)
    return list((await session.execute(stmt)).scalars().all())


async def _export_seat_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    license_ids = await _license_ids_in_scope(session, principal.workspace_id)
    requested = filters.get("license_id")
    if requested is not None:
        if requested not in license_ids:
            raise NotFound("That license does not exist.")
        license_ids = [requested]
    if not license_ids:
        return []

    stmt = (
        select(SeatAssignment)
        .join(User, User.id == SeatAssignment.user_id)
        .where(SeatAssignment.license_id.in_(license_ids))
    )
    stmt = apply_search(stmt, params, [User.full_name, User.email])
    stmt = apply_filters(stmt, {SeatAssignment.role: filters.get("role")})
    state = filters.get("state")
    if state == "active":
        stmt = stmt.where(SeatAssignment.released_at.is_(None))
    elif state == "released":
        stmt = stmt.where(SeatAssignment.released_at.is_not(None))
    stmt = apply_sort(stmt, params, SEAT_SORTS, SeatAssignment.assigned_at)

    seats = list((await session.execute(stmt.limit(MAX_EXPORT_ROWS))).scalars().all())
    return [
        {
            "license_id": item.license_id,
            "user_name": item.user_name,
            "user_email": item.user_email,
            "role": item.role.value,
            "assigned_at": _iso(item.assigned_at),
            "released_at": _iso(item.released_at),
            "is_active": item.is_active,
        }
        for item in await _hydrate_seats(session, seats)
    ]


async def _export_usage_rows(
    session: AsyncSession, principal: Principal, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    license_ids = await _license_ids_in_scope(session, principal.workspace_id)
    requested = filters.get("license_id")
    if requested is not None:
        if requested not in license_ids:
            raise NotFound("That license does not exist.")
        license_ids = [requested]
    if not license_ids:
        return []

    stmt = (
        select(UsageRecord)
        .where(UsageRecord.license_id.in_(license_ids))
        .order_by(UsageRecord.period_start.desc(), UsageRecord.metric.asc())
        .limit(MAX_EXPORT_ROWS)
    )
    metric = filters.get("metric")
    if metric:
        stmt = stmt.where(UsageRecord.metric == metric)
    return [
        {
            "license_id": row.license_id,
            "metric": row.metric,
            "period_start": _iso(row.period_start),
            "period_end": _iso(row.period_end),
            "quantity": row.quantity,
            "unit_cost_usd": row.unit_cost_usd,
            "amount_usd": row.amount_usd,
            "source": row.source,
            "recorded_at": _iso(row.recorded_at),
        }
        for row in (await session.execute(stmt)).scalars().all()
    ]


async def _export_invoice_rows(
    session: AsyncSession, principal: Principal, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    stmt = (
        select(Invoice)
        .join(TenantLicense, TenantLicense.id == Invoice.license_id)
        .where(TenantLicense.tenant_workspace_id == principal.workspace_id)
        .order_by(Invoice.period_start.desc(), Invoice.invoice_ref.desc())
        .limit(MAX_EXPORT_ROWS)
    )
    status = filters.get("status")
    if status:
        stmt = stmt.where(Invoice.status == status)
    license_id = filters.get("license_id")
    if license_id:
        stmt = stmt.where(Invoice.license_id == license_id)
    return [
        {
            "invoice_ref": row.invoice_ref,
            "license_id": row.license_id,
            "period_start": _iso(row.period_start),
            "period_end": _iso(row.period_end),
            "subtotal_usd": row.subtotal_usd,
            "overage_usd": row.overage_usd,
            "total_usd": row.total_usd,
            "currency": row.currency,
            "status": row.status,
            "issued_at": _iso(row.issued_at),
            "due_at": _iso(row.due_at),
            "paid_at": _iso(row.paid_at),
        }
        for row in (await session.execute(stmt)).scalars().all()
    ]


async def export_csv(
    session: AsyncSession,
    principal: Principal,
    *,
    dataset: ExportDataset = "plans",
    q: str | None = None,
    sort: str | None = None,
    status: str | None = None,
    tier: str | None = None,
    billing_period: str | None = None,
    plan_id: str | None = None,
    license_id: str | None = None,
    role: str | None = None,
    state: SeatState | None = None,
    metric: str | None = None,
    auto_renew: bool | None = None,
    expiring_in_days: int | None = None,
) -> tuple[str, str]:
    """Render one licensing dataset as CSV. Returns ``(csv_text, filename)``.

    The same filters the console's dropdowns send to the list endpoints apply
    here, so an export always matches the grid the user is looking at.
    """
    now = _now()
    params = ListParams(page=1, page_size=MAX_PAGE_SIZE, q=q, sort=sort)
    filters: dict[str, Any] = {
        "status": status,
        "tier": tier,
        "billing_period": billing_period,
        "plan_id": plan_id,
        "license_id": license_id,
        "role": role,
        "state": state,
        "metric": metric,
        "auto_renew": auto_renew,
        "expiring_in_days": expiring_in_days,
    }

    if dataset == "tenants":
        rows = await _export_license_rows(session, principal, params, filters, now)
        columns = LICENSE_CSV_COLUMNS
    elif dataset == "seats":
        rows = await _export_seat_rows(session, principal, params, filters)
        columns = SEAT_CSV_COLUMNS
    elif dataset == "usage":
        rows = await _export_usage_rows(session, principal, filters)
        columns = USAGE_CSV_COLUMNS
    elif dataset == "invoices":
        rows = await _export_invoice_rows(session, principal, filters)
        columns = INVOICE_CSV_COLUMNS
    else:
        rows = await _export_plan_rows(session, principal, params, filters)
        columns = PLAN_CSV_COLUMNS

    # An empty dataset still produces a header row, so the download is a valid
    # CSV the user can open rather than a zero-byte file.
    return to_csv(rows, columns), f"licensing-{dataset}-{now:%Y%m%d}.csv"


# --------------------------------------------------------------------------- #
# Invoice PDF
# --------------------------------------------------------------------------- #

_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0
_MARGIN = 54.0
_CONTENT_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
_COL_DESC = _MARGIN
_COL_METRIC = _MARGIN + 210.0
_COL_QTY = _MARGIN + 360.0
_COL_UNIT = _MARGIN + 430.0
_COL_AMOUNT = _PAGE_WIDTH - _MARGIN


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_width(text: str, size: float, bold: bool) -> float:
    """Approximate Helvetica advance width, good enough to right-align columns."""
    return len(text) * size * (0.55 if bold else 0.5)


def _pdf_text(x: float, y: float, size: float, text: str, *, bold: bool = False) -> str:
    font = "/F2" if bold else "/F1"
    return f"BT {font} {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({_pdf_escape(text)}) Tj ET\n"


def _pdf_text_right(
    x_right: float, y: float, size: float, text: str, *, bold: bool = False
) -> str:
    return _pdf_text(x_right - _pdf_width(text, size, bold), y, size, text, bold=bold)


def _pdf_rule(x: float, y: float, width: float, *, thickness: float = 0.6) -> str:
    return f"0.78 0.78 0.78 rg {x:.1f} {y:.1f} {width:.1f} {thickness:.2f} re f 0 0 0 rg\n"


def _pdf_clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _pdf_date(value: dt.datetime | None) -> str:
    if value is None:
        return "Not set"
    return value.astimezone(dt.UTC).strftime("%d %b %Y")


def _number(value: Any, places: int = 2) -> str:
    amount = _as_decimal(value)
    if amount is None:
        return "-"
    with contextlib.suppress(InvalidOperation):
        amount = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{amount:,f}"


def _money(value: Any, currency: str) -> str:
    return f"{currency} {_number(value, 2)}"


def _assemble_pdf(content: str, *, title: str) -> bytes:
    """Wrap a content stream in a complete, single-page PDF document.

    Written by hand on purpose: a billing document must not pull a rendering
    library into the control plane's dependency surface. The result is a valid
    PDF 1.4 file with a correct cross-reference table, which is all a browser
    needs to display and print it.
    """
    stream = content.encode("cp1252", "replace")
    created = _now().strftime("D:%Y%m%d%H%M%SZ")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
        (
            f"<< /Title ({_pdf_escape(title)}) /Producer (Fulcrum Ops control plane) "
            f"/Creator (Fulcrum Ops control plane) /CreationDate ({created}) >>"
        ).encode("cp1252", "replace"),
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii")
        out += body
        out += b"\nendobj\n"

    startxref = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{startxref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _build_invoice_pdf(
    *,
    invoice: Invoice,
    license_: TenantLicense,
    plan: LicensePlan | None,
    tenant_name: str | None,
) -> bytes:
    """Lay out one invoice on a single US Letter page."""
    currency = (invoice.currency or "USD").upper()
    parts: list[str] = []
    y = _PAGE_HEIGHT - _MARGIN

    parts.append(_pdf_text(_MARGIN, y, 20, "Fulcrum Ops", bold=True))
    parts.append(_pdf_text_right(_COL_AMOUNT, y, 20, "INVOICE", bold=True))
    y -= 15
    parts.append(_pdf_text(_MARGIN, y, 9, "AI Agent Control Plane"))
    parts.append(_pdf_text_right(_COL_AMOUNT, y, 11, invoice.invoice_ref, bold=True))
    y -= 12
    parts.append(_pdf_rule(_MARGIN, y, _CONTENT_WIDTH))
    y -= 26

    detail_x = _MARGIN + 300.0
    parts.append(_pdf_text(_MARGIN, y, 8, "BILLED TO", bold=True))
    parts.append(_pdf_text(detail_x, y, 8, "INVOICE DETAILS", bold=True))
    y -= 15
    parts.append(_pdf_text(_MARGIN, y, 11, tenant_name or "Unnamed workspace", bold=True))
    parts.append(_pdf_text(detail_x, y, 10, f"Status: {invoice.status}"))
    y -= 13
    contact = license_.billing_contact_email or "No billing contact on file"
    parts.append(_pdf_text(_MARGIN, y, 10, contact))
    parts.append(_pdf_text(detail_x, y, 10, f"Issued: {_pdf_date(invoice.issued_at)}"))
    y -= 13
    parts.append(
        _pdf_text(_MARGIN, y, 10, f"PO reference: {license_.purchase_order_ref or 'None'}")
    )
    parts.append(_pdf_text(detail_x, y, 10, f"Due: {_pdf_date(invoice.due_at)}"))
    y -= 13
    parts.append(_pdf_text(_MARGIN, y, 10, f"License: {license_.id}"))
    parts.append(_pdf_text(detail_x, y, 10, f"Paid: {_pdf_date(invoice.paid_at)}"))
    y -= 26

    if plan is not None:
        plan_line = f"{plan.name} ({plan.tier}), billed {plan.billing_period.lower()}"
    else:
        plan_line = "Plan record unavailable"
    parts.append(_pdf_text(_MARGIN, y, 10, f"Plan: {plan_line}"))
    y -= 14
    period = f"{_pdf_date(invoice.period_start)} to {_pdf_date(invoice.period_end)}"
    parts.append(_pdf_text(_MARGIN, y, 10, f"Billing period: {period}"))
    y -= 26

    parts.append(_pdf_text(_COL_DESC, y, 8, "DESCRIPTION", bold=True))
    parts.append(_pdf_text(_COL_METRIC, y, 8, "METRIC", bold=True))
    parts.append(_pdf_text_right(_COL_QTY, y, 8, "QUANTITY", bold=True))
    parts.append(_pdf_text_right(_COL_UNIT, y, 8, "UNIT COST", bold=True))
    parts.append(_pdf_text_right(_COL_AMOUNT, y, 8, "AMOUNT", bold=True))
    y -= 7
    parts.append(_pdf_rule(_MARGIN, y, _CONTENT_WIDTH))
    y -= 16

    items = [item for item in (invoice.line_items or []) if isinstance(item, dict)]
    if not items:
        parts.append(
            _pdf_text(_COL_DESC, y, 9.5, "No metered line items were recorded for this period.")
        )
        y -= 15
    for item in items[:MAX_INVOICE_PDF_LINES]:
        description = str(item.get("description") or item.get("metric") or "Line item")
        parts.append(_pdf_text(_COL_DESC, y, 9.5, _pdf_clip(description, 38)))
        parts.append(_pdf_text(_COL_METRIC, y, 9.5, _pdf_clip(str(item.get("metric") or ""), 18)))
        parts.append(_pdf_text_right(_COL_QTY, y, 9.5, _number(item.get("quantity"), 2)))
        unit_cost = item.get("unit_cost", item.get("unit_cost_usd"))
        parts.append(_pdf_text_right(_COL_UNIT, y, 9.5, _number(unit_cost, 4)))
        amount = item.get("amount", item.get("amount_usd"))
        parts.append(_pdf_text_right(_COL_AMOUNT, y, 9.5, _number(amount, 2)))
        y -= 15
    omitted = len(items) - MAX_INVOICE_PDF_LINES
    if omitted > 0:
        parts.append(
            _pdf_text(
                _COL_DESC,
                y,
                9,
                f"{omitted} further line(s) omitted; use the CSV export for full detail.",
            )
        )
        y -= 15

    y -= 6
    parts.append(_pdf_rule(_MARGIN, y, _CONTENT_WIDTH))
    y -= 19
    totals = (
        ("Subtotal", invoice.subtotal_usd, False),
        ("Overage", invoice.overage_usd, False),
        ("Total due", invoice.total_usd, True),
    )
    for label, value, bold in totals:
        parts.append(_pdf_text_right(_COL_UNIT, y, 10.5, label, bold=bold))
        parts.append(_pdf_text_right(_COL_AMOUNT, y, 10.5, _money(value, currency), bold=bold))
        y -= 16

    footer_y = _MARGIN
    parts.append(_pdf_rule(_MARGIN, footer_y + 16, _CONTENT_WIDTH))
    parts.append(
        _pdf_text(
            _MARGIN,
            footer_y,
            8,
            f"Generated by the Fulcrum Ops control plane on {_pdf_date(_now())}. "
            f"All amounts are in {currency}.",
        )
    )
    return _assemble_pdf("".join(parts), title=f"Invoice {invoice.invoice_ref}")


async def invoice_pdf(
    session: AsyncSession, principal: Principal, invoice_id: str
) -> tuple[bytes, str]:
    """Render one invoice as a PDF. Returns ``(pdf_bytes, filename)``.

    The identifier may be the invoice's primary key or its human reference
    (``INV-2026-0712``); the console's Billing & Renewals grid uses the latter.
    """
    stmt = (
        select(Invoice, TenantLicense, LicensePlan, Workspace.name)
        .join(TenantLicense, TenantLicense.id == Invoice.license_id)
        .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
        .join(Workspace, Workspace.id == TenantLicense.tenant_workspace_id)
        .where(
            or_(Invoice.id == invoice_id, Invoice.invoice_ref == invoice_id),
            TenantLicense.tenant_workspace_id == principal.workspace_id,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound("That invoice does not exist.")

    invoice, license_, plan, tenant_name = row
    pdf = _build_invoice_pdf(
        invoice=invoice, license_=license_, plan=plan, tenant_name=tenant_name
    )
    return pdf, f"{invoice.invoice_ref}.pdf"
