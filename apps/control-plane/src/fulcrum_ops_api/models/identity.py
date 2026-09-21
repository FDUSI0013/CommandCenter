"""Tenancy and identity: workspaces, users, memberships, API keys, sessions.

A *workspace* is the tenant boundary. Every other table in the system carries a
``workspace_id`` and every query filters on it. Each workspace also owns a
telemetry namespace in the private engine, addressed by ``engine_workspace``.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import (
    ActorMixin,
    Base,
    PrimaryKeyMixin,
    TimestampMixin,
    UtcDateTime,
    WorkspaceScopedMixin,
)


class Role(str, enum.Enum):
    """Coarse RBAC. Checked by `require_role` on every mutating endpoint."""

    OWNER = "owner"           # billing, licensing, workspace deletion
    ADMIN = "admin"           # everything except billing
    OPERATOR = "operator"     # run/deploy/approve, no policy or secret edits
    APPROVER = "approver"     # approve/reject/escalate only
    MEMBER = "member"         # read + author prompts/tests/feedback
    VIEWER = "viewer"         # read only

    @property
    def rank(self) -> int:
        return {
            Role.VIEWER: 0,
            Role.MEMBER: 1,
            Role.APPROVER: 2,
            Role.OPERATOR: 3,
            Role.ADMIN: 4,
            Role.OWNER: 5,
        }[self]

    def satisfies(self, required: Role) -> bool:
        # APPROVER is a side-grade: it outranks MEMBER for approvals only, and
        # the approvals routes ask for it explicitly.
        return self.rank >= required.rank


class Workspace(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    # Namespace used when talking to the private telemetry engine.
    engine_workspace: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    # Free-form tenant attributes surfaced on Licensing & Entitlements.
    settings: Mapped[dict] = mapped_column(default=dict)

    members: Mapped[list[Membership]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class User(Base, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    team: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_initials: Mapped[str | None] = mapped_column(String(4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # When the password last CHANGED -- not when it was last re-hashed, and not
    # when the account was created. A session token issued before this moment is
    # refused, which is what makes changing a password end the sessions someone
    # else may be holding. Null means "never changed since this column existed",
    # which refuses nothing: an account that has not rotated its credential has
    # no sessions to invalidate.
    credentials_changed_at: Mapped[dt.datetime | None] = mapped_column(
        UtcDateTime, nullable=True
    )

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def initials(self) -> str:
        if self.avatar_initials:
            return self.avatar_initials
        parts = [p for p in self.full_name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or self.email[:2].upper()


class Membership(Base, PrimaryKeyMixin, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), default=Role.MEMBER.value, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class ApiKey(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Credential an SDK or CI job presents. The raw token is never stored."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_key_id", "key_id", unique=True),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    display_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional narrowing: a key may be bound to one agent, so a deployed agent
    # can only write its own telemetry.
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    scopes: Mapped[list] = mapped_column(default=list)  # ["ingest", "read", "admin"]
    environment: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    DEFAULT_SCOPES = ["ingest", "read"]

    @property
    def is_active(self) -> bool:
        now = dt.datetime.now(dt.UTC)
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at < now)

    def has_scope(self, scope: str) -> bool:
        return "admin" in (self.scopes or []) or scope in (self.scopes or [])
