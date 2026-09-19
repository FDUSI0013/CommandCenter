"""Pydantic contracts for Licensing & Entitlements.

Read models mirror exactly what the console's Licensing screen renders: the plan
catalogue, one tenant licence with its seat and renewal state, the resolved
entitlement set with metered usage against the plan allowance, and the KPI row.

Write models are deliberately narrower than the tables behind them. Lifecycle
statuses the platform owns — ``Expiring Soon`` and ``Expired`` are written by the
renewal sweep, ``Suspended`` and ``Revoked`` by their own endpoints — are not
accepted from a client, so a caller cannot forge licence state through a plain
update. Money is typed ``Decimal`` end to end; a rounded float must never reach
an invoice line.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ..models.identity import Role
from ..models.licensing import (
    BillingPeriod,
    EntitlementValueType,
    InvoiceStatus,
    LicenseStatus,
    PlanStatus,
    PlanTier,
    UsageMetric,
)

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
PLAN_CODE_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"

#: Licence statuses a client may set directly through create/update. The rest are
#: platform-owned and reachable only through the suspend/reactivate/revoke
#: endpoints or the renewal sweep.
SettableLicenseStatus = Literal[LicenseStatus.ACTIVE, LicenseStatus.TRIAL]

#: Datasets ``GET /licensing/export`` can render.
ExportDataset = Literal["plans", "tenants", "seats", "usage", "invoices"]

#: Seat rows the console's Seats & Assignments tab filters on.
SeatState = Literal["active", "released"]


def _term_is_backwards(starts_at: dt.datetime | None, expires_at: dt.datetime | None) -> bool:
    """Whether a term ends at or before it starts, when both ends were sent.

    A timestamp without an offset is UTC, as it is everywhere else here. Python
    refuses to compare one with an offset to one without, and that ``TypeError``
    is not a validation error: it left the validator as a 500.
    """
    if starts_at is None or expires_at is None:
        return False
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=dt.UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    return expires_at <= starts_at


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


class LicensePlanRead(BaseModel):
    """A sellable plan in the catalogue, with the caller's take-up alongside it.

    The four counters at the end are aggregates over the calling workspace's
    licences, not global figures: the Plans & Tiers grid shows how much of each
    plan this tenant is actually consuming.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str
    tier: PlanTier
    description: str | None = None
    price_per_seat_usd: Decimal
    billing_period: BillingPeriod
    included_seats: int
    included_tokens: int
    included_runs: int
    overage_rate_per_1k_tokens: Decimal
    features: list[str] = Field(default_factory=list)
    status: PlanStatus
    effective_from: dt.datetime | None = None
    effective_to: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    license_count: int = 0
    seats_purchased: int = 0
    seats_assigned: int = 0
    seat_utilization_pct: float = 0.0


class LicensePlanCreate(BaseModel):
    """Everything needed to publish a new plan into the catalogue."""

    name: str = Field(min_length=2, max_length=120)
    code: str = Field(min_length=2, max_length=60, pattern=PLAN_CODE_PATTERN)
    tier: PlanTier = PlanTier.STARTER
    description: str | None = Field(default=None, max_length=2000)
    price_per_seat_usd: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=12, decimal_places=2
    )
    billing_period: BillingPeriod = BillingPeriod.ANNUAL
    included_seats: int = Field(default=0, ge=0, le=1_000_000)
    included_tokens: int = Field(default=0, ge=0)
    included_runs: int = Field(default=0, ge=0)
    overage_rate_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=10, decimal_places=4
    )
    features: list[str] = Field(default_factory=list, max_length=50)
    status: PlanStatus = PlanStatus.DRAFT
    effective_from: dt.datetime | None = None
    effective_to: dt.datetime | None = None

    @field_validator("features")
    @classmethod
    def _clean_features(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if any(len(item) > 160 for item in cleaned):
            raise ValueError("each feature bullet must be 160 characters or fewer")
        return cleaned

    @model_validator(mode="after")
    def _check_window(self) -> LicensePlanCreate:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must be later than effective_from")
        return self


class LicensePlanUpdate(BaseModel):
    """Partial update. Omitted fields are left exactly as they are."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    code: str | None = Field(
        default=None, min_length=2, max_length=60, pattern=PLAN_CODE_PATTERN
    )
    tier: PlanTier | None = None
    description: str | None = Field(default=None, max_length=2000)
    price_per_seat_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )
    billing_period: BillingPeriod | None = None
    included_seats: int | None = Field(default=None, ge=0, le=1_000_000)
    included_tokens: int | None = Field(default=None, ge=0)
    included_runs: int | None = Field(default=None, ge=0)
    overage_rate_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=4
    )
    features: list[str] | None = Field(default=None, max_length=50)
    status: PlanStatus | None = None
    effective_from: dt.datetime | None = None
    effective_to: dt.datetime | None = None

    @field_validator("features")
    @classmethod
    def _clean_features(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value if item and item.strip()]
        if any(len(item) > 160 for item in cleaned):
            raise ValueError("each feature bullet must be 160 characters or fewer")
        return cleaned

    @model_validator(mode="after")
    def _check_window(self) -> LicensePlanUpdate:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must be later than effective_from")
        return self


# --------------------------------------------------------------------------- #
# Tenant licences
# --------------------------------------------------------------------------- #


class TenantLicenseRead(BaseModel):
    """One plan sold to one workspace — the row every quota check resolves to."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_workspace_id: str
    tenant_name: str | None = None
    plan_id: str
    plan_name: str | None = None
    plan_code: str | None = None
    plan_tier: PlanTier | None = None
    status: LicenseStatus
    seats_purchased: int
    seats_assigned: int
    seats_available: int
    seat_utilization_pct: float
    starts_at: dt.datetime
    expires_at: dt.datetime | None = None
    days_until_expiry: int | None = None
    auto_renew: bool
    billing_contact_email: str | None = None
    purchase_order_ref: str | None = None
    notes: str | None = None
    suspended_at: dt.datetime | None = None
    suspended_reason: str | None = None
    # Plan allowances, denormalised so the tenant grid can show token and
    # workflow capacity without a second request per row.
    included_tokens: int | None = None
    included_runs: int | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class TenantLicenseCreate(BaseModel):
    """Sell a plan to the calling workspace.

    ``tenant_workspace_id`` is accepted so the console can echo back what it
    loaded, but it must name the caller's own workspace; licensing a different
    tenant is not something this API grants.
    """

    plan_id: str = Field(min_length=1, max_length=36)
    tenant_workspace_id: str | None = Field(default=None, max_length=36)
    status: SettableLicenseStatus = LicenseStatus.ACTIVE
    seats_purchased: int = Field(default=0, ge=0, le=1_000_000)
    starts_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    auto_renew: bool = True
    billing_contact_email: str | None = Field(
        default=None, max_length=255, pattern=EMAIL_PATTERN
    )
    purchase_order_ref: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _check_term(self) -> TenantLicenseCreate:
        if _term_is_backwards(self.starts_at, self.expires_at):
            raise ValueError("expires_at must be later than starts_at")
        return self


class TenantLicenseUpdate(BaseModel):
    """Partial update, including the plan change behind the Upgrade Plan action."""

    plan_id: str | None = Field(default=None, min_length=1, max_length=36)
    status: SettableLicenseStatus | None = None
    seats_purchased: int | None = Field(default=None, ge=0, le=1_000_000)
    starts_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    auto_renew: bool | None = None
    billing_contact_email: str | None = Field(
        default=None, max_length=255, pattern=EMAIL_PATTERN
    )
    purchase_order_ref: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _check_term(self) -> TenantLicenseUpdate:
        if _term_is_backwards(self.starts_at, self.expires_at):
            raise ValueError("expires_at must be later than starts_at")
        return self


class LicenseSuspendRequest(BaseModel):
    """Body for ``POST /licensing/tenants/{id}/suspend``."""

    reason: str | None = Field(default=None, max_length=1000)
    # Suspension always blocks new seat assignment; releasing the seats that are
    # already held is a separate, deliberate choice.
    release_seats: bool = False


class LicenseRevokeRequest(BaseModel):
    """Body for ``POST /licensing/tenants/{id}/revoke``.

    There is no ``release_seats`` here: revocation is terminal, and a licence
    that serves nobody holds no seats, so they are always released.
    """

    reason: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------- #
# Seats
# --------------------------------------------------------------------------- #


class SeatAssignmentRead(BaseModel):
    """A licensed seat held by one user, with its release history preserved."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    license_id: str
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    user_initials: str | None = None
    role: Role
    assigned_at: dt.datetime
    assigned_by_user_id: str | None = None
    released_at: dt.datetime | None = None
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class SeatAssignmentCreate(BaseModel):
    """Assign a seat, optionally by taking one back from another user first.

    ``replaces_user_id`` is what the console's Reassign control sends: release
    that user's seat and hand it to ``user_id`` in the same transaction, so a
    fully allocated licence can be reshuffled without buying a spare seat.
    """

    user_id: str = Field(min_length=1, max_length=36)
    role: Role = Role.MEMBER
    replaces_user_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def _check_distinct(self) -> SeatAssignmentCreate:
        if self.replaces_user_id is not None and self.replaces_user_id == self.user_id:
            raise ValueError("replaces_user_id must name a different user than user_id")
        return self


class SeatPurchaseRequest(BaseModel):
    """Body for ``POST /licensing/tenants/{id}/seats/purchase``."""

    seats: int = Field(ge=1, le=100_000, description="Seats to add to the licence")
    purchase_order_ref: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Entitlements and usage
# --------------------------------------------------------------------------- #


class EntitlementRead(BaseModel):
    """One enforceable capability or limit resolved from a licence."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    license_id: str
    key: str
    value_type: EntitlementValueType
    bool_value: bool | None = None
    int_value: int | None = None
    string_value: str | None = None
    value: bool | int | str | None = None
    hard_limit: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class EntitlementUsageRead(BaseModel):
    """Metered usage for one meter against the plan's included allowance.

    ``used`` and ``remaining`` are quantities, not money, and go out as JSON
    numbers. A ``Decimal`` serialises as a string -- right for an invoice line,
    wrong here: the console formats these with ``toLocaleString``, which leaves a
    string untouched, so the bar read ``1500000.0000 / 1,000,000``. Whole counts
    (tokens, runs, seats) go out as integers; only a fractional meter such as
    ``storage_gb`` is a float.
    """

    metric: UsageMetric
    used: Decimal
    included: int | None = None
    remaining: Decimal | None = None
    utilization_pct: float | None = None
    over_limit: bool = False
    amount_usd: Decimal = Decimal("0")

    @field_serializer("used", "remaining", when_used="json")
    def _quantity_as_number(self, value: Decimal | None) -> int | float | None:
        if value is None:
            return None
        return int(value) if value == value.to_integral_value() else float(value)


class TenantEntitlementsRead(BaseModel):
    """Everything the Usage & Limits tab needs for one licence, in one call."""

    license_id: str
    tenant_workspace_id: str
    tenant_name: str | None = None
    plan_id: str
    plan_name: str | None = None
    plan_tier: PlanTier | None = None
    status: LicenseStatus
    seats_purchased: int
    seats_assigned: int
    seats_available: int
    features: list[str] = Field(default_factory=list)
    entitlements: list[EntitlementRead] = Field(default_factory=list)
    usage: list[EntitlementUsageRead] = Field(default_factory=list)
    usage_window_days: int
    over_limit_metrics: list[UsageMetric] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #


class InvoiceRead(BaseModel):
    """A billing document for one licence period.

    ``storage_key`` is deliberately absent: where the rendered PDF lives is an
    internal detail, and the bytes are served by the PDF endpoint instead.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_ref: str
    license_id: str
    tenant_name: str | None = None
    plan_name: str | None = None
    period_start: dt.datetime
    period_end: dt.datetime
    subtotal_usd: Decimal
    overage_usd: Decimal
    total_usd: Decimal
    currency: str
    status: InvoiceStatus
    issued_at: dt.datetime | None = None
    due_at: dt.datetime | None = None
    paid_at: dt.datetime | None = None
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    created_at: dt.datetime
    updated_at: dt.datetime


# --------------------------------------------------------------------------- #
# KPI summary
# --------------------------------------------------------------------------- #


class LicensingSummary(BaseModel):
    """The six KPI cards on Licensing & Entitlements, plus the billing tallies.

    Every number here is a SQL aggregate; nothing is counted in Python.
    """

    plans: int
    active_plans: int
    active_tenant_licenses: int
    trial_licenses: int
    seats_purchased: int
    seats_assigned: int
    seats_available: int
    seat_utilization_pct: float
    expiring_in_30_days: int
    suspended_or_revoked: int
    overage_alerts: int
    open_invoices: int
    outstanding_usd: Decimal
    generated_at: dt.datetime
