"""Licensing and entitlements: plans, tenant licenses, seats, limits, billing.

A *plan* is catalogue-level and shared by every tenant, so it is deliberately
not workspace-scoped. A *tenant license* is one plan sold to one workspace and
is the row every enforcement check hangs off: seats are assigned against it,
entitlements are resolved from it, metered usage is billed to it.

Money is stored as ``Numeric`` rather than float — cent drift in an invoice
total is not recoverable — and token counters use ``BigInteger`` because
enterprise allowances are quoted in billions.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import (
    ActorMixin,
    Base,
    PrimaryKeyMixin,
    TimestampMixin,
    UtcDateTime,
)
from .identity import Role


class PlanTier(str, enum.Enum):
    """Commercial tier. Drives the tier badge colour on Plans & Tiers."""

    STARTER = "Starter"
    PROFESSIONAL = "Professional"
    ENTERPRISE = "Enterprise"
    CUSTOM = "Custom"
    # Tiers the shipped console still renders; kept so the UI needs no mapping.
    PRO = "Pro"
    STANDARD = "Standard"
    TRIAL = "Trial"
    FREE = "Free"


class BillingPeriod(str, enum.Enum):
    MONTHLY = "Monthly"
    ANNUAL = "Annual"


class PlanStatus(str, enum.Enum):
    """Retired plans stay queryable — existing licenses still reference them."""

    ACTIVE = "Active"
    DRAFT = "Draft"
    RETIRED = "Retired"


class LicenseStatus(str, enum.Enum):
    ACTIVE = "Active"
    TRIAL = "Trial"
    # Written by the renewal sweep, not derived at read time, so the tenant
    # grid and the "Expiring in 30 Days" KPI can filter on one indexed column.
    EXPIRING_SOON = "Expiring Soon"
    SUSPENDED = "Suspended"
    REVOKED = "Revoked"
    EXPIRED = "Expired"


class EntitlementValueType(str, enum.Enum):
    """Which typed column on the row is authoritative."""

    BOOL = "bool"
    INT = "int"
    STRING = "string"


class UsageMetric(str, enum.Enum):
    """Meters that feed overage. Each is billed at its own unit cost."""

    TOKENS = "tokens"
    RUNS = "runs"
    SEATS = "seats"
    STORAGE_GB = "storage_gb"
    # Also metered on Usage & Limits, though not currently billed for overage.
    WORKFLOWS = "workflows"
    API_CALLS = "api_calls"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "Draft"
    ISSUED = "Issued"
    PAID = "Paid"
    OVERDUE = "Overdue"
    VOID = "Void"


class LicensePlan(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin):
    """A sellable plan in the catalogue. Global, not owned by any workspace."""

    __tablename__ = "license_plans"
    __table_args__ = (Index("ix_license_plans_status_tier", "status", "tier"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    tier: Mapped[str] = mapped_column(
        String(24), default=PlanTier.STARTER.value, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_per_seat_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(12, 2), default=decimal.Decimal("0"), nullable=False
    )
    billing_period: Mapped[str] = mapped_column(
        String(16), default=BillingPeriod.ANNUAL.value, nullable=False
    )
    included_seats: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    included_runs: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Overage is quoted per 1k tokens, so four decimal places are meaningful.
    overage_rate_per_1k_tokens: Mapped[decimal.Decimal] = mapped_column(
        Numeric(10, 4), default=decimal.Decimal("0"), nullable=False
    )
    # Marketing-level feature bullets, e.g. ["Priority SLA (99.9%)"]. The
    # enforceable form lives in Entitlement.
    features: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(
        String(16), default=PlanStatus.DRAFT.value, nullable=False, index=True
    )
    # Null while the plan is still Draft; a null effective_to means open-ended.
    effective_from: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    licenses: Mapped[list[TenantLicense]] = relationship(back_populates="plan")


class TenantLicense(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin):
    """One plan sold to one workspace — the unit every quota check resolves to.

    Not WorkspaceScopedMixin: the row is owned by the platform operator, and
    ``tenant_workspace_id`` names the workspace being licensed rather than the
    workspace allowed to edit the record.
    """

    __tablename__ = "tenant_licenses"
    __table_args__ = (
        Index("ix_tenant_licenses_tenant_status", "tenant_workspace_id", "status"),
        # Powers the renewal sweep and the "Expiring in 30 Days" KPI.
        Index("ix_tenant_licenses_status_expires_at", "status", "expires_at"),
    )

    tenant_workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("license_plans.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default=LicenseStatus.ACTIVE.value, nullable=False, index=True
    )
    seats_purchased: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Denormalised count of open SeatAssignment rows; the seat service keeps it
    # in step so the tenant grid renders without an aggregate per row.
    seats_assigned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    starts_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    billing_contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purchase_order_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    suspended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    plan: Mapped[LicensePlan] = relationship(back_populates="licenses")
    seats: Mapped[list[SeatAssignment]] = relationship(
        back_populates="license", cascade="all, delete-orphan"
    )
    entitlements: Mapped[list[Entitlement]] = relationship(
        back_populates="license", cascade="all, delete-orphan"
    )
    usage_records: Mapped[list[UsageRecord]] = relationship(
        back_populates="license", cascade="all, delete-orphan"
    )
    invoices: Mapped[list[Invoice]] = relationship(back_populates="license")

    @property
    def seats_available(self) -> int:
        return max(self.seats_purchased - self.seats_assigned, 0)

    @property
    def seat_utilization_pct(self) -> float:
        if not self.seats_purchased:
            return 0.0
        return round(self.seats_assigned / self.seats_purchased * 100, 1)


class SeatAssignment(Base, PrimaryKeyMixin, TimestampMixin):
    """A licensed seat held by one user, with its release history preserved."""

    __tablename__ = "seat_assignments"
    __table_args__ = (
        # A user may hold the same seat again after release, so the timestamp is
        # part of the key. NOTE: "one *active* seat per user" cannot be a
        # portable partial unique index across SQLite and Postgres — the seat
        # service enforces it inside the assignment transaction.
        UniqueConstraint("license_id", "user_id", "assigned_at"),
        Index("ix_seat_assignments_license_released_at", "license_id", "released_at"),
    )

    license_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_licenses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    assigned_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    released_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    role: Mapped[str] = mapped_column(
        String(24), default=Role.MEMBER.value, nullable=False
    )

    license: Mapped[TenantLicense] = relationship(back_populates="seats")

    @property
    def is_active(self) -> bool:
        return self.released_at is None


class Entitlement(Base, PrimaryKeyMixin, TimestampMixin):
    """One enforceable capability or limit resolved from a license.

    Keys are dotted paths the enforcement middleware looks up directly, e.g.
    ``max_agents``, ``guardrails.ml_detection``, ``export.scheduled``.
    """

    __tablename__ = "entitlements"
    __table_args__ = (UniqueConstraint("license_id", "key"),)

    license_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_licenses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    value_type: Mapped[str] = mapped_column(
        String(12), default=EntitlementValueType.BOOL.value, nullable=False
    )
    bool_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # BigInteger: token and API-call ceilings exceed a 32-bit int.
    int_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    string_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # False means the tenant is warned and allowed through; True blocks.
    hard_limit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    license: Mapped[TenantLicense] = relationship(back_populates="entitlements")

    @property
    def value(self) -> bool | int | str | None:
        """The one typed column that ``value_type`` designates."""
        if self.value_type == EntitlementValueType.INT.value:
            return self.int_value
        if self.value_type == EntitlementValueType.STRING.value:
            return self.string_value
        return self.bool_value


class UsageRecord(Base, PrimaryKeyMixin, TimestampMixin):
    """Metered usage for one license, one metric, one billing period."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_license_period", "license_id", "period_start"),
        Index("ix_usage_records_metric_period", "metric", "period_start"),
    )

    license_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_licenses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period_start: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    metric: Mapped[str] = mapped_column(
        String(24), default=UsageMetric.TOKENS.value, nullable=False, index=True
    )
    # Numeric, not int: storage_gb is fractional and token counts are huge.
    quantity: Mapped[decimal.Decimal] = mapped_column(
        Numeric(20, 4), default=decimal.Decimal("0"), nullable=False
    )
    unit_cost_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(12, 6), default=decimal.Decimal("0"), nullable=False
    )
    amount_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), default=decimal.Decimal("0"), nullable=False
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    # Where the number came from — "telemetry engine", "metering job", "manual".
    source: Mapped[str] = mapped_column(String(60), default="engine", nullable=False)

    license: Mapped[TenantLicense] = relationship(back_populates="usage_records")


class Invoice(Base, PrimaryKeyMixin, TimestampMixin):
    """A billing document for one license period, plus its rendered PDF."""

    __tablename__ = "invoices"
    __table_args__ = (
        # Drives the overdue sweep and the Billing & Renewals ordering.
        Index("ix_invoices_status_due_at", "status", "due_at"),
        Index("ix_invoices_license_period", "license_id", "period_start"),
    )

    invoice_ref: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    license_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_licenses.id"), index=True, nullable=False
    )
    period_start: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    subtotal_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), default=decimal.Decimal("0"), nullable=False
    )
    overage_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), default=decimal.Decimal("0"), nullable=False
    )
    total_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), default=decimal.Decimal("0"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=InvoiceStatus.DRAFT.value, nullable=False, index=True
    )
    issued_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    due_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Object-store key for the generated PDF; null until the renderer runs.
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Frozen line snapshot: description, metric, quantity, unit cost, amount.
    line_items: Mapped[list] = mapped_column(default=list)

    license: Mapped[TenantLicense] = relationship(back_populates="invoices")
