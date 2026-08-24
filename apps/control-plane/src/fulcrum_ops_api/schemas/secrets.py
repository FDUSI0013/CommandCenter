"""Wire contracts for Secrets & Credentials.

One rule shapes this module: material leaves the control plane through exactly
one type, :class:`SecretRevealResult`, returned by exactly one endpoint, which
is role-gated, justified, and written to two separate evidence trails. Every
other response — list, detail, create, update, disable, enable — is a
:class:`SecretRead`, which has no field capable of holding a ciphertext or a
plaintext. The console renders the masked ``display_hint`` instead.

Two smaller conventions, both shared with the rest of the API:

* read models type their vocabulary fields as ``str`` rather than as enums.
  The columns are plain strings by design (see ``models.governance``), and a
  row written before a vocabulary was extended must still be readable;
* the derived labels the vault table shows — ``rotation_label``,
  ``compliance`` — are computed here, from the same window constants the
  summary aggregates use, so a row chip and a KPI card can never disagree.
"""

from __future__ import annotations

import datetime as dt
import enum
import math
from typing import TYPE_CHECKING, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from ..models.governance import (
    RiskLevel,
    SecretAccessAction,
    SecretStatus,
    SecretType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; the wire layer stays ORM-free
    from ..models.governance import Secret


#: A credential is "expiring soon" when it lapses inside this window; a
#: rotation is "overdue" the moment ``next_rotation_at`` passes. The KPI cards,
#: the compliance score and the per-row chips all read these two constants.
EXPIRING_WINDOW_DAYS: Final[int] = 30
PRIVILEGED_ACCESS_WINDOW_DAYS: Final[int] = 30

MAX_SECRET_VALUE_CHARS: Final[int] = 8192
MAX_JUSTIFICATION_CHARS: Final[int] = 1000
MAX_REASON_CHARS: Final[int] = 500
MAX_ROTATION_PERIOD_DAYS: Final[int] = 3650


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Read a naive datetime off the wire as UTC rather than rejecting it."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


class RotationState(str, enum.Enum):
    """Rotation-health slice behind the vault filters and the overdue panel."""

    OVERDUE = "overdue"        # next_rotation_at is in the past
    DUE_SOON = "due_soon"      # next_rotation_at falls inside the window
    SCHEDULED = "scheduled"    # next_rotation_at is beyond the window
    NONE = "none"              # no rotation policy set


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


class SecretRead(BaseModel):
    """One vault row, and the payload behind the detail inspector.

    There is deliberately no ``value`` and no ``ciphertext`` field here. Adding
    one would make every list response a disclosure.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    secret_type: str
    vault: str
    environment: str | None = None
    status: str
    #: Masked form only, e.g. "************a1c" — never the value itself.
    display_hint: str | None = None
    #: True when the control plane holds encrypted material for this row; false
    #: for entries that only point at an externally managed vault.
    has_material: bool = False
    rotation_period_days: int | None = None
    last_rotated_at: dt.datetime | None = None
    next_rotation_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    last_accessed_at: dt.datetime | None = None
    owner_user_id: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    risk: str
    vault_reference: str | None = None
    privileged: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expiring_soon(self) -> bool:
        """Lapses inside the window. An already-expired credential counts too."""
        if self.expires_at is None:
            return False
        return self.expires_at <= _now() + dt.timedelta(days=EXPIRING_WINDOW_DAYS)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_rotation_overdue(self) -> bool:
        if self.next_rotation_at is None:
            return False
        return self.next_rotation_at < _now()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rotation_label(self) -> str:
        """The Rotation column verbatim: "Overdue", "Due in 5d", "60d" or "N/A"."""
        if self.status in (SecretStatus.DISABLED.value, SecretStatus.REVOKED.value):
            return "N/A"
        if self.next_rotation_at is None:
            return "N/A"
        remaining = (self.next_rotation_at - _now()).total_seconds()
        if remaining <= 0:
            return "Overdue"
        days = max(1, math.ceil(remaining / 86400))
        return f"Due in {days}d" if days <= EXPIRING_WINDOW_DAYS else f"{days}d"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def compliance(self) -> str:
        """The same test the summary's compliance score applies, per row."""
        if self.is_expiring_soon or self.is_rotation_overdue:
            return "Non-Compliant"
        return "Compliant"

    @classmethod
    def from_model(
        cls,
        secret: Secret,
        *,
        owner_name: str | None = None,
        owner_email: str | None = None,
    ) -> SecretRead:
        """Assemble a row, resolving the owner's display name.

        ``has_material`` is derived here rather than mapped from a column, so
        ``Secret.ciphertext`` is read in exactly one expression in the whole
        response path — one that yields a boolean.
        """
        read = cls.model_validate(secret)
        read.has_material = secret.ciphertext is not None
        read.owner_name = owner_name
        read.owner_email = owner_email
        return read


class SecretAccessLogRead(BaseModel):
    """One append-only access row: who touched a credential, why, and whether it worked."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    secret_id: str
    actor: str
    actor_user_id: str | None = None
    action: str
    occurred_at: dt.datetime
    ip_address: str | None = None
    justification: str | None = None
    success: bool


class SecretTypeBreakdown(BaseModel):
    """One arc of the donut, and one bar of "Compliance by Secret Type"."""

    secret_type: str
    count: int
    compliant: int
    compliance_pct: int


class SecretsSummary(BaseModel):
    """The KPI cards above the vault table, plus the by-type breakdown."""

    total: int
    active_vaults: int
    expiring_soon: int
    rotation_overdue: int
    compliance_score: int
    privileged_access_30d: int
    by_type: list[SecretTypeBreakdown] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Write models
# ---------------------------------------------------------------------------


class SecretCreate(BaseModel):
    """Store a credential. ``value`` is encrypted on arrival and never echoed back."""

    name: str = Field(min_length=1, max_length=160)
    secret_type: SecretType = SecretType.API_KEY
    vault: str = Field(min_length=1, max_length=80)
    environment: str | None = Field(default=None, max_length=40)
    value: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SECRET_VALUE_CHARS,
        description="Plaintext material. Encrypted at rest on arrival; never returned.",
    )
    rotation_period_days: int | None = Field(
        default=None,
        ge=1,
        le=MAX_ROTATION_PERIOD_DAYS,
        description="Rotation cadence in days; drives next_rotation_at.",
    )
    expires_at: dt.datetime | None = None
    owner_user_id: str | None = Field(default=None, max_length=36)
    risk: RiskLevel = RiskLevel.MEDIUM
    vault_reference: str | None = Field(
        default=None,
        max_length=512,
        description="External locator for a credential this vault only points at.",
    )
    privileged: bool = False

    @field_validator("name", "vault")
    @classmethod
    def _required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("environment", "vault_reference", "owner_user_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("expires_at")
    @classmethod
    def _future_expiry(cls, value: dt.datetime | None) -> dt.datetime | None:
        normalised = _as_utc(value)
        if normalised is not None and normalised <= _now():
            raise ValueError("must be in the future")
        return normalised

    @model_validator(mode="after")
    def _material_or_reference(self) -> SecretCreate:
        """A vault row that holds nothing and points nowhere is not a credential."""
        if not self.value and not self.vault_reference:
            raise ValueError(
                "provide a value to encrypt, or a vault_reference for an "
                "externally managed credential"
            )
        return self


class SecretUpdate(BaseModel):
    """Patch metadata. Material is never changed here — that is what rotate is for."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    secret_type: SecretType | None = None
    vault: str | None = Field(default=None, min_length=1, max_length=80)
    environment: str | None = Field(default=None, max_length=40)
    status: SecretStatus | None = None
    rotation_period_days: int | None = Field(
        default=None, ge=1, le=MAX_ROTATION_PERIOD_DAYS
    )
    expires_at: dt.datetime | None = None
    owner_user_id: str | None = Field(default=None, max_length=36)
    risk: RiskLevel | None = None
    vault_reference: str | None = Field(default=None, max_length=512)
    privileged: bool | None = None
    #: Optimistic concurrency: send the ``updated_at`` the form was loaded with
    #: and the write is refused with 409 if the row moved in the meantime.
    expected_updated_at: dt.datetime | None = None

    @field_validator("name", "vault")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("environment", "vault_reference", "owner_user_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("expires_at", "expected_updated_at")
    @classmethod
    def _utc(cls, value: dt.datetime | None) -> dt.datetime | None:
        return _as_utc(value)


class SecretRevealRequest(BaseModel):
    """A reveal must say why. The reason is stored on the access-log row."""

    justification: str = Field(
        min_length=1,
        max_length=MAX_JUSTIFICATION_CHARS,
        description="Why the plaintext is needed. Recorded against your name.",
    )

    @field_validator("justification")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("a justification is required to reveal a secret")
        return cleaned


class SecretRevealResult(BaseModel):
    """The only response that carries plaintext. Shown once; do not cache it."""

    secret_id: str
    name: str
    value: str
    revealed_at: dt.datetime
    revealed_by: str
    justification: str
    access_log_id: str


class SecretRotateRequest(BaseModel):
    """Rotate a credential. Omit ``value`` and the control plane mints one."""

    value: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SECRET_VALUE_CHARS,
        description="New plaintext. Omit to have a fresh value generated for you.",
    )
    rotation_period_days: int | None = Field(
        default=None,
        ge=1,
        le=MAX_ROTATION_PERIOD_DAYS,
        description="Overrides the stored cadence from this rotation onward.",
    )
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class SecretRotateResult(BaseModel):
    """``value`` is filled only for a generated rotation; a supplied value is never echoed."""

    secret: SecretRead
    generated: bool
    value: str | None = None
    rotated_at: dt.datetime
    next_rotation_at: dt.datetime | None = None


class SecretDisableRequest(BaseModel):
    """Optional context recorded with a disable, e.g. "leaked in a support ticket"."""

    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


# ---------------------------------------------------------------------------
# Query models
# ---------------------------------------------------------------------------


class SecretFilters(BaseModel):
    """The vault table's dropdowns, shared verbatim by list and export."""

    secret_type: list[SecretType] | None = None
    environment: list[str] | None = None
    status: list[SecretStatus] | None = None
    vault: list[str] | None = None
    risk: RiskLevel | None = None
    owner_user_id: str | None = None
    privileged: bool | None = None
    rotation_state: RotationState | None = None
    expiring_within_days: int | None = Field(default=None, ge=1, le=365)


class AccessLogFilters(BaseModel):
    """Filters over one credential's access history."""

    action: SecretAccessAction | None = None
    success: bool | None = None
