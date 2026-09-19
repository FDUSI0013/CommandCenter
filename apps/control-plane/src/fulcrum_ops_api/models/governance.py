"""Governance: policies, approvals, the audit trail, and the secret vault.

These are the tables behind the Policy Center, Approvals & Audit, and Secrets &
Credentials screens. Two of them are *append only* by contract — ``audit_events``
and ``secret_access_logs`` are written once and never updated, because they are
the evidence a compliance reviewer reads when something goes wrong.

String vocabularies below are the exact labels the console renders, so the API
can return them verbatim and the frontend needs no translation layer.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import (
    ActorMixin,
    Base,
    PrimaryKeyMixin,
    TimestampMixin,
    UtcDateTime,
    WorkspaceScopedMixin,
)

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------


class RiskLevel(str, enum.Enum):
    """Shared risk chip across policies, approvals, violations and secrets."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class PolicyCategory(str, enum.Enum):
    """Policy Center's left-hand filter. Labels are rendered as-is."""

    ACCESS_CONTROL = "Access Control"        # who/what may call which tool
    GUARDRAILS = "Guardrails"                # prompt/response safety
    DATA_PROTECTION = "Data Protection"      # DLP, residency, isolation
    APPROVAL_ESCALATION = "Approval & Escalation"
    ROUTING_ORCHESTRATION = "Routing & Orchestration"
    USAGE_QUOTAS = "Usage & Quotas"
    LOGGING_RETENTION = "Logging & Retention"
    SECURITY = "Security"
    COMPLIANCE = "Compliance"


class PolicyScope(str, enum.Enum):
    """What a policy attaches to. ``Policy.scope_ref`` holds the target id."""

    GLOBAL = "Global"            # scope_ref is NULL
    ENVIRONMENT = "Environment"  # scope_ref = environment name
    AGENT = "Agent"              # scope_ref = agents.id
    CONNECTOR = "Connector"      # scope_ref = connectors.id


class PolicyStatus(str, enum.Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    WARNING = "Warning"                # firing more than its expected baseline
    PENDING_REVIEW = "Pending Review"


class PolicyEnforcement(str, enum.Enum):
    """What the enforcement path does on a match, strongest first.

    ``Block``, ``Warn``, ``Log Only`` and ``Require Approval`` are the four the
    console summarises as block / warn / log / approval-required.
    """

    BLOCK = "Block"
    REQUIRE_APPROVAL = "Require Approval"
    ESCALATE = "Escalate"
    MASK = "Mask"
    ROUTE = "Route"
    THROTTLE = "Throttle"
    WARN = "Warn"
    LOG_ONLY = "Log Only"
    ALLOW = "Allow"


class ViolationSeverity(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class ApprovalStatus(str, enum.Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    ESCALATED = "Escalated"
    EXPIRED = "Expired"    # SLA elapsed with no decision; set by the sweeper job


class ApprovalStep(str, enum.Enum):
    """The four fixed rungs of the approval workflow tracker."""

    REQUESTED = "Requested"
    IN_REVIEW = "In Review"
    APPROVED = "Approved"
    COMPLETED = "Completed"


class SecretType(str, enum.Enum):
    API_KEY = "API Key"
    SERVICE_PRINCIPAL = "Service Principal"
    OAUTH_CLIENT = "OAuth Client"
    CERTIFICATE = "Certificate"
    CONNECTOR_CREDENTIAL = "Connector Credential"


class SecretStatus(str, enum.Enum):
    """Vault health chip. ``Warning``/``Expired`` are the console's shorthand
    for ``Expiring Soon``/``Rotation Overdue`` and are accepted on write."""

    ACTIVE = "Active"
    EXPIRING_SOON = "Expiring Soon"
    WARNING = "Warning"
    ROTATION_OVERDUE = "Rotation Overdue"
    EXPIRED = "Expired"
    DISABLED = "Disabled"
    REVOKED = "Revoked"


class SecretAccessAction(str, enum.Enum):
    REVEAL = "reveal"
    ROTATE = "rotate"
    USE = "use"
    DISABLE = "disable"
    ENABLE = "enable"
    CREATE = "create"
    UPDATE = "update"
    # Written since the secrets vault learned to revoke; it was missing here, so
    # the access log recorded "revoke" rows that ?action=revoke then refused to
    # filter for (422) -- the one action an auditor most wants to isolate.
    REVOKE = "revoke"


def default_workflow() -> list:
    """Skeleton tracker stamped on every new request; steps flip to done=True."""
    return [
        {"step": ApprovalStep.REQUESTED.value, "done": False, "by": None, "ts": None},
        {"step": ApprovalStep.IN_REVIEW.value, "done": False, "by": None, "ts": None},
        {"step": ApprovalStep.APPROVED.value, "done": False, "by": None, "ts": None},
        {"step": ApprovalStep.COMPLETED.value, "done": False, "by": None, "ts": None},
    ]


# --------------------------------------------------------------------------
# Policy Center
# --------------------------------------------------------------------------


class Policy(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin, ActorMixin):
    """A governance rule the enforcement path evaluates before an agent acts."""

    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        # The Policy Center table filters on status, category and risk.
        Index("ix_policies_workspace_status", "workspace_id", "status"),
        Index("ix_policies_workspace_category", "workspace_id", "category"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(
        String(48), default=PolicyCategory.GUARDRAILS.value, nullable=False
    )
    scope: Mapped[str] = mapped_column(
        String(24), default=PolicyScope.GLOBAL.value, nullable=False
    )
    # Target of a non-global scope: an agent id, connector id or environment name.
    scope_ref: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # Human label the table shows for the scope, e.g. "All Agents", "CX Agents".
    scope_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_level: Mapped[str] = mapped_column(
        String(16), default=RiskLevel.MEDIUM.value, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=PolicyStatus.ACTIVE.value, nullable=False
    )
    enforcement: Mapped[str] = mapped_column(
        String(24), default=PolicyEnforcement.BLOCK.value, nullable=False
    )
    # Machine-readable body the enforcement path interprets: conditions,
    # thresholds, allowlists. Shape is versioned by `version`.
    rules: Mapped[dict] = mapped_column(default=dict)
    version: Mapped[str] = mapped_column(String(20), default="v1.0.0", nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    last_evaluated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_triggered_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Cached rollups. Refreshed by the governance aggregation job, never written
    # inline by the request path — counting violations per page load is too slow.
    violations_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    approved_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applies_agents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applies_tools: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applies_connectors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applies_envs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    bindings: Mapped[list[PolicyBinding]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )
    violations: Mapped[list[PolicyViolation]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )


class PolicyBinding(Base, PrimaryKeyMixin, WorkspaceScopedMixin):
    """Explicit policy-to-agent attachment, on top of any scope-wide match."""

    __tablename__ = "policy_bindings"
    __table_args__ = (
        UniqueConstraint("policy_id", "agent_id"),
        # "Which policies apply to this agent?" is the agent-detail query.
        Index("ix_policy_bindings_agent", "workspace_id", "agent_id"),
    )

    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    bound_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    bound_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    policy: Mapped[Policy] = relationship(back_populates="bindings")


class PolicyViolation(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One breach, written by the enforcement path at decision time."""

    __tablename__ = "policy_violations"
    __table_args__ = (
        Index("ix_policy_violations_workspace_occurred", "workspace_id", "occurred_at"),
        Index("ix_policy_violations_agent", "agent_id", "occurred_at"),
    )

    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Trace identifier emitted by the telemetry engine; lets the console deep-link
    # from a violation straight to the run that caused it. Null for offline checks.
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    severity: Mapped[str] = mapped_column(
        String(16), default=ViolationSeverity.MEDIUM.value, index=True, nullable=False
    )
    # Which enforcement actually fired — may be weaker than the policy's default
    # when the policy was in warn-only rollout.
    action_taken: Mapped[str] = mapped_column(
        String(24), default=PolicyEnforcement.BLOCK.value, nullable=False
    )
    detail: Mapped[dict] = mapped_column(default=dict)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    policy: Mapped[Policy] = relationship(back_populates="violations")


# --------------------------------------------------------------------------
# Approvals & Audit
# --------------------------------------------------------------------------


class ApprovalRequest(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Human-in-the-loop gate for an action an agent is not allowed to take alone."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "request_ref"),
        # Queue view: open requests per workspace, ordered by SLA pressure.
        Index("ix_approval_requests_workspace_status", "workspace_id", "status"),
        Index("ix_approval_requests_sla", "status", "sla_due_at"),
    )

    # Human-facing reference shown in the UI and quoted in audit rows, e.g. "REQ-1042".
    request_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Screen or system that raised the request, e.g. "Approvals", "Agent Detail".
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    action_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)
    risk: Mapped[str] = mapped_column(
        String(16), default=RiskLevel.MEDIUM.value, index=True, nullable=False
    )
    # The policy that demanded the approval; kept if the policy is later deleted.
    policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("policies.id", ondelete="SET NULL"), index=True, nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True
    )
    requested_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    sla_due_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Label the queue renders next to the countdown, e.g. "15m", "1h 30m".
    sla_label: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=ApprovalStatus.PENDING.value, nullable=False
    )
    decided_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_to_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Exactly what executes on approval — the replayable action payload.
    payload: Mapped[dict] = mapped_column(default=dict)
    # Blast radius shown on the decision card: financial, systems, sensitivity, customers.
    impact: Mapped[dict] = mapped_column(default=dict)
    workflow: Mapped[list] = mapped_column(default=default_workflow)

    comments: Mapped[list[ApprovalComment]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        """Still awaiting a decision — drives the queue badge and the SLA sweeper."""
        return self.status in (
            ApprovalStatus.PENDING.value,
            ApprovalStatus.ESCALATED.value,
        )


class ApprovalComment(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Reviewer discussion thread on a request; part of the decision record."""

    __tablename__ = "approval_comments"

    request_id: Mapped[str] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    request: Mapped[ApprovalRequest] = relationship(back_populates="comments")


class AuditEvent(Base, PrimaryKeyMixin, WorkspaceScopedMixin):
    """APPEND ONLY. Every governed change lands here and is never updated or deleted.

    Rows carry no ``updated_at`` on purpose: a mutable audit row is not evidence.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # The audit screen pages by time within a workspace...
        Index("ix_audit_events_workspace_occurred", "workspace_id", "occurred_at"),
        # ...and every entity detail screen shows "history for this record".
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
    )

    occurred_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    # Denormalised on purpose: the display name must survive user deletion.
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_screen: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Before/after rendered side by side in the audit drawer.
    prev_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Named event_metadata because `metadata` is reserved on the declarative base.
    event_metadata: Mapped[dict] = mapped_column(default=dict)

    # Hash chain: checksum = SHA-256 over this row's canonical serialisation
    # concatenated with previous_checksum. Editing or deleting any earlier row
    # breaks every checksum after it, so tampering is detectable by replaying
    # the chain — the log can be silently altered only by rewriting all of it.
    previous_checksum: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Checksum of the preceding event in this workspace; NULL for the genesis row.",
    )
    checksum: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 of the canonical row plus previous_checksum; makes tampering detectable.",
    )


# --------------------------------------------------------------------------
# Secrets & Credentials
# --------------------------------------------------------------------------


class Secret(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin, ActorMixin):
    """A credential in the vault.

    ``ciphertext`` must NEVER be exposed through a response schema, log line or
    list endpoint — the UI only ever renders ``display_hint``. Reads go through
    the service layer, which decrypts on an explicit reveal and writes a
    ``SecretAccessLog`` row for it.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        # Vault table filters on status and environment; the rotation job scans dates.
        Index("ix_secrets_workspace_status", "workspace_id", "status"),
        Index("ix_secrets_next_rotation", "workspace_id", "next_rotation_at"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    secret_type: Mapped[str] = mapped_column(
        String(40), default=SecretType.API_KEY.value, index=True, nullable=False
    )
    # Logical store the material lives in, e.g. "Azure Key Vault".
    vault: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    environment: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default=SecretStatus.ACTIVE.value, nullable=False
    )
    # Fernet blob. Written only via the service layer using
    # core.security.encrypt_secret; nothing else may touch this column.
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Masked form safe for the table, e.g. "sk-****************a1c".
    display_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rotation_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_rotated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    next_rotation_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_accessed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    risk: Mapped[str] = mapped_column(
        String(16), default=RiskLevel.MEDIUM.value, index=True, nullable=False
    )
    # External locator, e.g. https://fulcrum-kv-prod.vault.azure.net/secrets/openai-key.
    vault_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Privileged credentials require step-up auth before a reveal.
    privileged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    access_logs: Mapped[list[SecretAccessLog]] = relationship(
        back_populates="secret", cascade="all, delete-orphan"
    )


class SecretAccessLog(Base, PrimaryKeyMixin, WorkspaceScopedMixin):
    """APPEND ONLY. Every reveal, rotation and use of a credential, successful or not."""

    __tablename__ = "secret_access_logs"
    __table_args__ = (
        Index("ix_secret_access_logs_secret_occurred", "secret_id", "occurred_at"),
        Index("ix_secret_access_logs_workspace_occurred", "workspace_id", "occurred_at"),
    )

    secret_id: Mapped[str] = mapped_column(
        ForeignKey("secrets.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Email or "system" — kept verbatim so the row survives user deletion.
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(
        String(24), default=SecretAccessAction.USE.value, index=True, nullable=False
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Required by the UI for reveals of privileged secrets.
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    secret: Mapped[Secret] = relationship(back_populates="access_logs")
