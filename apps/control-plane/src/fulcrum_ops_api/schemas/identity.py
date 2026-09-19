"""Wire contracts for identity, session and API-key management.

This is the front door for both callers: the console signs a person in and gets
a session envelope back, an SDK presents a key minted here and gets to write
telemetry. Two rules shape every model below.

* **Secret material travels exactly once.** A minted key's plaintext appears in
  :class:`ApiKeyCreated` and nowhere else — no read model carries it, no export
  column names it, and the stored row only ever holds a hash and a display
  hint. Password fields are write-only for the same reason.
* **The envelope is what the console renders.** ``user``, ``workspace``,
  ``role`` and the switchable workspace list are the shape the shell's sidebar
  card, workspace switcher and role gates read directly, so the field names
  here are the field names the console uses.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.registry import EnvironmentType

# ---------------------------------------------------------------------------
# Vocabulary and limits
# ---------------------------------------------------------------------------

#: Deliberately permissive: this validates shape, not deliverability. The only
#: thing that proves an address works is mail arriving at it.
EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

MIN_PASSWORD_LENGTH: Final[int] = 12
MAX_PASSWORD_LENGTH: Final[int] = 128

#: Scopes a key may carry. ``admin`` implies the other two.
API_KEY_SCOPES: Final[tuple[str, ...]] = ("ingest", "read", "admin")

#: An unbounded credential is a liability; two years is the ceiling.
MAX_API_KEY_TTL_DAYS: Final[int] = 730

#: A key inside this window is flagged on the KPI card so it is rotated before
#: an agent starts failing in production.
EXPIRING_SOON_DAYS: Final[int] = 30

SeverityFloor = Literal["Critical", "High", "Medium", "Low"]


class ApiKeyStatus(enum.StrEnum):
    """What the key's status pill shows. Derived from the row, never stored."""

    ACTIVE = "Active"
    EXPIRED = "Expired"
    REVOKED = "Revoked"


class MemberStatus(enum.StrEnum):
    """Whether the account may sign in at all. Membership is separate."""

    ACTIVE = "Active"
    INACTIVE = "Inactive"


def normalise_email(value: str) -> str:
    """Trim and lower-case an address, rejecting anything that is not one."""
    cleaned = value.strip().lower()
    if not EMAIL_PATTERN.match(cleaned):
        raise ValueError("must be a valid email address")
    return cleaned


def validate_password(value: str) -> str:
    """Enforce the password floor.

    Length carries most of the strength, so the rules stay short: long enough
    to resist offline attack, mixed enough to rule out a single dictionary
    word, and not a run of one repeated character.
    """
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(value) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if value.strip() != value:
        raise ValueError("must not start or end with whitespace")
    if not any(c.isalpha() for c in value):
        raise ValueError("must contain at least one letter")
    if not any(c.isdigit() or not c.isalnum() for c in value):
        raise ValueError("must contain at least one digit or symbol")
    if len(set(value)) < 5:
        raise ValueError("must not repeat the same few characters")
    return value


# ---------------------------------------------------------------------------
# Preferences  (Profile & Preferences / Notification Settings)
# ---------------------------------------------------------------------------


class NotificationPreferences(BaseModel):
    """What the account menu's Notification Settings panel writes.

    Stored per person per workspace, so someone who runs production in one
    tenant and experiments in another is not paged by the experiments.
    """

    email_alerts: bool = True
    email_approvals: bool = True
    email_deployments: bool = False
    email_weekly_digest: bool = True
    in_app_alerts: bool = True
    in_app_approvals: bool = True
    in_app_mentions: bool = True
    alert_severity_floor: SeverityFloor = Field(
        "Medium", description="Alerts below this severity never notify"
    )
    quiet_hours_start: str | None = Field(
        None, description="24h local time, HH:MM; notifications queue until quiet hours end"
    )
    quiet_hours_end: str | None = Field(None, description="24h local time, HH:MM")

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _check_time(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            raise ValueError("must be a 24-hour time in HH:MM form")
        return value


class UiPreferences(BaseModel):
    """What the account menu's Profile & Preferences panel writes."""

    theme: Literal["light", "dark", "system"] = "system"
    density: Literal["comfortable", "compact"] = "comfortable"
    landing_screen: str = Field(
        "live-runs", max_length=48, description="Console route opened on sign-in"
    )
    timezone: str = Field("UTC", max_length=64, description="IANA zone used to render timestamps")
    date_format: Literal["ISO", "DMY", "MDY"] = "ISO"
    results_per_page: int = Field(25, ge=10, le=200)


class UserPreferences(BaseModel):
    """The whole preference document for one person in one workspace."""

    notifications: NotificationPreferences = Field(default_factory=NotificationPreferences)
    ui: UiPreferences = Field(default_factory=UiPreferences)


class PreferencesUpdate(BaseModel):
    """Partial preference write; omitted sections and fields are left alone."""

    notifications: NotificationPreferences | None = None
    ui: UiPreferences | None = None


# ---------------------------------------------------------------------------
# Session envelope
# ---------------------------------------------------------------------------


class SessionUser(BaseModel):
    """The person behind the session, as the sidebar user card renders them."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    initials: str = Field(description="Avatar text, derived from the name unless overridden")
    job_title: str | None = None
    team: str | None = None
    is_active: bool = True
    last_login_at: dt.datetime | None = None


class SessionApiKey(BaseModel):
    """The credential behind a machine session, when the caller is not a person."""

    id: str
    name: str
    display_hint: str
    scopes: list[str] = Field(default_factory=list)
    environment: str | None = None
    agent_id: str | None = None


class SessionWorkspace(BaseModel):
    """The tenant this session is scoped to."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    status: str = "active"


class WorkspaceOption(BaseModel):
    """One entry in the workspace switcher."""

    id: str
    name: str
    slug: str
    role: str
    is_current: bool = False


class SessionEnvelope(BaseModel):
    """Everything the console needs to render the shell after sign-in.

    Returned by login, by the session probe and by a workspace switch, so the
    three paths cannot drift apart.
    """

    user: SessionUser | None = Field(
        None, description="Null when the caller authenticated with an API key"
    )
    api_key: SessionApiKey | None = Field(
        None, description="Set only for API-key callers, so an SDK can verify its own key"
    )
    workspace: SessionWorkspace
    role: str
    workspaces: list[WorkspaceOption] = Field(
        default_factory=list, description="Every workspace this session may switch to"
    )
    entitlements: dict[str, bool | int | str | None] = Field(
        default_factory=dict, description="Resolved from the workspace's active license"
    )
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    issued_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Auth requests
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Credentials posted by the sign-in form."""

    email: str = Field(max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        # Shape only: a rejected address must not tell the caller whether the
        # account exists, so this raises the same 422 for every bad string.
        return normalise_email(value)


class SwitchWorkspaceRequest(BaseModel):
    """Move the current session to another workspace the user belongs to."""

    workspace: str = Field(
        min_length=1, max_length=80, description="Workspace slug or id to switch to"
    )

    @field_validator("workspace")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class ChangePasswordRequest(BaseModel):
    """Rotate your own password. The current one is always required."""

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, value: str) -> str:
        return validate_password(value)


class ProfileUpdate(BaseModel):
    """Edit your own display fields. Email and role are not self-service."""

    full_name: str | None = Field(None, min_length=1, max_length=160)
    job_title: str | None = Field(None, max_length=120)
    team: str | None = Field(None, max_length=120)
    avatar_initials: str | None = Field(
        None, max_length=4, description="Override the initials derived from the name"
    )

    @field_validator("full_name")
    @classmethod
    def _trim_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("avatar_initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        return value.strip().upper()


class UserProfile(BaseModel):
    """Your own record: the display fields plus the preference document."""

    user: SessionUser
    role: str
    workspace: SessionWorkspace
    preferences: UserPreferences


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class MemberRef(BaseModel):
    """A name an assignment picker can offer — nothing an API key could mine."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    full_name: str
    initials: str


class MemberRead(BaseModel):
    """One person in this workspace, as the member table renders the row."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="User id; the path parameter of every member route")
    membership_id: str
    email: str
    full_name: str
    initials: str
    job_title: str | None = None
    team: str | None = None
    role: str
    status: MemberStatus
    last_login_at: dt.datetime | None = None
    joined_at: dt.datetime
    password_set: bool = Field(description="False until the account has a usable password")
    is_current_user: bool = False
    shared_account: bool = Field(
        False,
        description=(
            "True when the account also belongs to another workspace. Its password, "
            "profile and active flag are then the person's own: an admin here can change "
            "the role or remove the membership, and a PATCH of anything else answers 403"
        ),
    )


class MemberCreate(BaseModel):
    """Add someone to this workspace.

    An address already known to the platform is *joined* to the workspace
    rather than duplicated — one person, one account, many memberships. In that
    case ``password`` is rejected: their existing credential belongs to them,
    not to whoever is adding them here.
    """

    email: str = Field(max_length=255)
    full_name: str = Field(min_length=1, max_length=160)
    role: str = Field("member", description="Role granted in this workspace")
    job_title: str | None = Field(None, max_length=120)
    team: str | None = Field(None, max_length=120)
    password: str | None = Field(
        None,
        max_length=MAX_PASSWORD_LENGTH,
        description="Initial password for a brand-new account; omit to create it without one",
    )

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalise_email(value)

    @field_validator("full_name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str | None) -> str | None:
        return None if value is None else validate_password(value)


class MemberUpdate(BaseModel):
    """Partial update of someone else's record. Every field is optional.

    ``role`` belongs to the membership. Everything else belongs to the account,
    which is platform-wide, and is refused with 403 when the account also
    belongs to another workspace (``MemberRead.shared_account``).
    """

    full_name: str | None = Field(None, min_length=1, max_length=160)
    job_title: str | None = Field(None, max_length=120)
    team: str | None = Field(None, max_length=120)
    avatar_initials: str | None = Field(None, max_length=4)
    role: str | None = None
    is_active: bool | None = Field(
        None,
        description=(
            "Deactivating blocks sign-in everywhere, so it is allowed only for an account "
            "that belongs to this workspace alone; otherwise remove the membership"
        ),
    )
    password: str | None = Field(
        None,
        max_length=MAX_PASSWORD_LENGTH,
        description=(
            "Set a sign-in password for a member who has none, or reset one. Refused for "
            "an account shared with another workspace, and for your own"
        ),
    )

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str | None) -> str | None:
        return None if value is None else validate_password(value)

    @field_validator("full_name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("avatar_initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        return value.strip().upper()


class MemberRoleChange(BaseModel):
    """Change one member's role in this workspace."""

    role: str
    reason: str | None = Field(None, max_length=500, description="Recorded on the audit row")


class RoleCount(BaseModel):
    """One bar of the role breakdown."""

    role: str
    count: int
    percent: float = Field(description="Share of all members, 0-100, one decimal")


class MembersSummary(BaseModel):
    """KPI cards above the member table."""

    total: int
    active: int
    inactive: int
    owners: int
    admins: int
    privileged: int = Field(description="Owners, admins and operators combined")
    never_signed_in: int
    signed_in_last_30d: int
    added_last_30d: int
    last_joined_at: dt.datetime | None = None
    by_role: list[RoleCount] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyRead(BaseModel):
    """One key as the API Access Tokens table renders it. Never the secret."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    display_hint: str = Field(description="First and last characters only; not a usable key")
    scopes: list[str] = Field(default_factory=list)
    environment: str | None = None
    agent_id: str | None = None
    agent_name: str | None = Field(None, description="Resolved name of the bound agent, if any")
    status: ApiKeyStatus
    created_at: dt.datetime
    created_by: str | None = None
    last_used_at: dt.datetime | None = None
    last_used_ip: str | None = None
    expires_at: dt.datetime | None = None
    expires_in_days: int | None = Field(
        None, description="Whole days until expiry; negative once expired"
    )
    revoked_at: dt.datetime | None = None
    revoked_reason: str | None = None


class ApiKeyCreate(BaseModel):
    """Mint a key. The plaintext is returned once and never again."""

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(
        default_factory=lambda: ["ingest", "read"],
        description=f"Any of {', '.join(API_KEY_SCOPES)}",
    )
    environment: EnvironmentType | None = None
    agent_id: str | None = Field(
        None,
        max_length=36,
        description="Bind the key to one agent so it can only write that agent's telemetry",
    )
    expires_in_days: int | None = Field(
        None,
        ge=1,
        le=MAX_API_KEY_TTL_DAYS,
        description="Omit for a non-expiring key; expiry is strongly preferred",
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip().lower() for s in value if s and s.strip()]
        if not cleaned:
            raise ValueError("at least one scope is required")
        unknown = sorted(set(cleaned) - set(API_KEY_SCOPES))
        if unknown:
            raise ValueError(
                f"unknown scope(s) {', '.join(unknown)}; valid scopes are "
                f"{', '.join(API_KEY_SCOPES)}"
            )
        # Order is fixed so two keys with the same grants compare equal.
        return [scope for scope in API_KEY_SCOPES if scope in cleaned]


class ApiKeySnippet(BaseModel):
    """A copy-ready way to use the key that was just minted."""

    language: str
    label: str
    code: str


class ApiKeyCreated(ApiKeyRead):
    """The mint response: the read model plus the one and only look at the key."""

    token: str = Field(description="The full key. Shown once — it cannot be recovered.")
    snippets: list[ApiKeySnippet] = Field(
        default_factory=list, description="Copy-ready setup for each SDK and for curl"
    )
    warning: str = (
        "Store this key now. It is hashed on the server and cannot be shown again."
    )


class ApiKeyRevoke(BaseModel):
    """Reason recorded against a revocation."""

    reason: str | None = Field(None, max_length=500)


class ApiKeyUsageAction(BaseModel):
    """How often this key performed one kind of operation."""

    action: str
    count: int


class ApiKeyUsageDay(BaseModel):
    """One column of the usage sparkline."""

    date: dt.date
    calls: int


class ApiKeyUsage(BaseModel):
    """What a key has actually done, read from the audit trail it wrote."""

    key_id: str
    name: str
    status: ApiKeyStatus
    last_used_at: dt.datetime | None = None
    last_used_ip: str | None = None
    first_seen_at: dt.datetime | None = None
    total_calls: int = Field(description="Audited operations attributed to this key, all time")
    calls_in_window: int
    ingest_calls: int
    ingest_records: int = Field(description="Spans, traces and events this key ingested")
    ingest_bytes: int = 0
    window_days: int
    by_action: list[ApiKeyUsageAction] = Field(default_factory=list)
    daily: list[ApiKeyUsageDay] = Field(default_factory=list)


class EnvironmentCount(BaseModel):
    """Key count for one environment."""

    environment: str
    count: int


class ApiKeysSummary(BaseModel):
    """KPI cards above the key table."""

    total: int
    active: int
    revoked: int
    expired: int
    expiring_soon: int = Field(description=f"Active keys expiring within {EXPIRING_SOON_DAYS} days")
    never_used: int
    used_last_7d: int
    agent_bound: int
    last_used_at: dt.datetime | None = None
    by_environment: list[EnvironmentCount] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class WorkspaceRead(BaseModel):
    """The current tenant, as the workspace switcher and settings panel show it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    status: str
    settings: dict[str, Any] = Field(
        default_factory=dict, description="Tenant attributes; per-person preferences are stripped"
    )
    role: str = Field(description="The calling principal's role in this workspace")
    member_count: int = 0
    api_key_count: int = 0
    active_api_key_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkspaceUpdate(BaseModel):
    """Rename the workspace or edit its tenant attributes."""

    name: str | None = Field(None, min_length=1, max_length=120)
    settings: dict[str, Any] | None = Field(
        None, description="Replaces the tenant attributes; per-person preferences are preserved"
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed
