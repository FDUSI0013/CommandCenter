"""Day-two operations: deployments, budgets/quotas/capacity, alerts, exports.

These tables back the screens an operator lives in once agents are already
running -- Deployment & Environment, Quota/Cost & Capacity, Alerts, and Exports.

Cross-module references (``agent_id``, ``environment_id``,
``triggered_by_user_id``, ...) are plain indexed ``String(36)`` columns rather
than ``ForeignKey``s, matching the convention in ``identity.py``: those parents
live in sibling modules and a hard FK would couple table-creation order across
the whole model package. Foreign keys are used only inside this module, where
the parent row genuinely owns the child (a stage cannot outlive its deployment).

Status and severity vocabularies are the literal strings the frontend already
renders, so no translation layer sits between the API and the UI.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
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

# ``asdecimal=False`` so SQLite and Postgres hand back the same Python float;
# without it Postgres returns Decimal and the serializers diverge per backend.
MONEY_USD = Numeric(14, 2, asdecimal=False)
MEASURE = Numeric(20, 4, asdecimal=False)

# The pipeline the Deployment & Environment screen animates, in order. Seeded
# onto every new deployment so the UI replays real history instead of guessing.
DEFAULT_PIPELINE_STAGES = (
    "Build",
    "Automated Tests",
    "Security Scan",
    "Approval",
    "Deploy",
)


# --------------------------------------------------------------------------- #
# Deployments
# --------------------------------------------------------------------------- #


class DeploymentStrategy(str, enum.Enum):
    """How the new version replaces the old one; drives the rollback path."""

    ROLLING = "Rolling"
    BLUE_GREEN = "Blue-Green"
    CANARY = "Canary"
    RECREATE = "Recreate"


class DeploymentStatus(str, enum.Enum):
    """Lifecycle of one deployment. ``HALTED`` means a gate refused to open
    (approval rejected, scan failed) as opposed to the deploy itself failing."""

    QUEUED = "Queued"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    HALTED = "Halted"
    ROLLED_BACK = "RolledBack"


class DeploymentStageStatus(str, enum.Enum):
    """Per-stage state. ``APPROVED`` is the terminal state of an approval gate;
    ``WARNING`` lets a stage pass while still flagging amber in the UI."""

    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    WARNING = "Warning"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    APPROVED = "Approved"


class Deployment(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """One release of one version into one environment.

    A rollback is itself a deployment that points back at the one it undoes via
    ``rollback_of_deployment_id``, so the history table can render the pair as a
    single incident instead of two unrelated rows.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "deployment_ref"),
        # The deployment history list is ordered by recency within one
        # environment; the status filter chips sit above the same list.
        Index("ix_deployments_environment_id_started_at", "environment_id", "started_at"),
        Index("ix_deployments_workspace_id_status", "workspace_id", "status"),
    )

    # Human-facing identifier shown in the UI and in change tickets, e.g. "dep-9100".
    deployment_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    # Null for platform-wide releases that are not tied to a single agent.
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    environment_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    strategy: Mapped[str] = mapped_column(
        String(24), default=DeploymentStrategy.ROLLING.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=DeploymentStatus.QUEUED.value, nullable=False
    )
    # Null when the trigger was CI rather than a person.
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Denormalised from the timestamps: the list view sorts and averages on it.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commit_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the Approval stage routed through Approvals & Audit.
    approval_request_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    rollback_of_deployment_id: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True
    )
    # Post-deploy health percentage, e.g. 99.9. Null until the first probe lands.
    health: Mapped[float | None] = mapped_column(Float, nullable=True)

    stages: Mapped[list[DeploymentStage]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="DeploymentStage.sequence",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            DeploymentStatus.SUCCEEDED.value,
            DeploymentStatus.FAILED.value,
            DeploymentStatus.HALTED.value,
            DeploymentStatus.ROLLED_BACK.value,
        )


class DeploymentStage(Base, PrimaryKeyMixin, TimestampMixin):
    """One step of the Build -> Tests -> Scan -> Approval -> Deploy pipeline.

    Stored per deployment (not derived) because the UI animates the pipeline for
    historical deployments too, and because each stage carries its own log.
    """

    __tablename__ = "deployment_stages"
    __table_args__ = (
        # Sequence is the render order, so it must be unique and gap-free per run.
        UniqueConstraint("deployment_id", "sequence"),
        Index("ix_deployment_stages_deployment_id_status", "deployment_id", "status"),
    )

    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=DeploymentStageStatus.PENDING.value, nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stage-shaped extras: test counts, scan findings, approver id, image digest.
    detail: Mapped[dict] = mapped_column(default=dict)

    deployment: Mapped[Deployment] = relationship(back_populates="stages")


# --------------------------------------------------------------------------- #
# Quota, cost & capacity
# --------------------------------------------------------------------------- #


class LimitScope(str, enum.Enum):
    """What a budget or quota is attached to; ``scope_ref`` holds the target id."""

    WORKSPACE = "Workspace"
    ENVIRONMENT = "Environment"
    TEAM = "Team"
    AGENT = "Agent"


class LimitPeriod(str, enum.Enum):
    """Reset cadence shared by budgets and quotas."""

    MONTHLY = "Monthly"
    QUARTERLY = "Quarterly"
    ANNUAL = "Annual"


class LimitStatus(str, enum.Enum):
    """Shared budget/quota state. ``WARNING`` and ``EXCEEDED`` are set by the
    threshold evaluator, not by a human, and drive the amber/red chips."""

    ACTIVE = "Active"
    WARNING = "Warning"
    EXCEEDED = "Exceeded"
    EXPIRED = "Expired"
    DISABLED = "Disabled"


class QuotaResource(str, enum.Enum):
    """The metered dimension a quota limits."""

    TOKENS = "Tokens"
    REQUESTS = "Requests"
    COST = "Cost"
    CONCURRENCY = "Concurrency"
    STORAGE = "Storage"


class QuotaEnforcement(str, enum.Enum):
    """What happens on breach -- the difference between a guardrail and a report."""

    BLOCK = "Block"
    WARN = "Warn"
    LOG = "Log"


class CapacityStatus(str, enum.Enum):
    """Headroom verdict for a provisioned resource pool."""

    HEALTHY = "Healthy"
    WARNING = "Warning"
    CRITICAL = "Critical"


class Budget(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A spend ceiling for one period. Money only -- enforceable limits live in
    ``Quota``; a budget warns and reports, it does not block traffic."""

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "period_start"),
        # The Quota/Cost screen lists budgets by scope and filters on status.
        Index("ix_budgets_workspace_id_scope", "workspace_id", "scope"),
        Index("ix_budgets_workspace_id_status", "workspace_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    scope: Mapped[str] = mapped_column(
        String(24), default=LimitScope.WORKSPACE.value, nullable=False
    )
    # Id or label of the scoped target: an environment id, a team name, an agent id.
    scope_ref: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    period: Mapped[str] = mapped_column(
        String(16), default=LimitPeriod.MONTHLY.value, nullable=False
    )
    amount_usd: Mapped[float] = mapped_column(MONEY_USD, default=0.0, nullable=False)
    # Rolled up from usage on a schedule so the progress bars need no live join.
    spent_usd: Mapped[float] = mapped_column(MONEY_USD, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    warn_threshold_percent: Mapped[float] = mapped_column(Float, default=80.0, nullable=False)
    hard_threshold_percent: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    period_start: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=LimitStatus.ACTIVE.value, nullable=False
    )

    @property
    def utilization_percent(self) -> float:
        if not self.amount_usd:
            return 0.0
        return round(self.spent_usd / self.amount_usd * 100, 1)


class Quota(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """An enforceable ceiling on a metered resource.

    Unlike ``Budget`` this is consulted in the request path, so ``used_value`` is
    written by the enforcer itself and ``resets_at`` is authoritative.
    """

    __tablename__ = "quotas"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_quotas_workspace_id_resource", "workspace_id", "resource"),
        Index("ix_quotas_workspace_id_status", "workspace_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    resource: Mapped[str] = mapped_column(
        String(24), default=QuotaResource.TOKENS.value, nullable=False
    )
    scope: Mapped[str] = mapped_column(
        String(24), default=LimitScope.WORKSPACE.value, nullable=False
    )
    scope_ref: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    limit_value: Mapped[float] = mapped_column(MEASURE, default=0.0, nullable=False)
    period: Mapped[str] = mapped_column(
        String(16), default=LimitPeriod.MONTHLY.value, nullable=False
    )
    used_value: Mapped[float] = mapped_column(MEASURE, default=0.0, nullable=False)
    # Free-text unit so one table covers tokens, requests, RPM, GB and dollars.
    unit: Mapped[str] = mapped_column(String(24), default="tokens", nullable=False)
    enforcement: Mapped[str] = mapped_column(
        String(16), default=QuotaEnforcement.WARN.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=LimitStatus.ACTIVE.value, nullable=False
    )
    resets_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True, nullable=True)

    @property
    def utilization_percent(self) -> float:
        if not self.limit_value:
            return 0.0
        return round(self.used_value / self.limit_value * 100, 1)


class CapacityRecord(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A point-in-time reading of a provisioned resource pool.

    One row per resource per measurement, so the capacity table shows the latest
    reading and the sparkline reads the trailing window from the same table.
    """

    __tablename__ = "capacity_records"
    __table_args__ = (
        # Every read is "latest N readings for this resource" or a trend window.
        Index(
            "ix_capacity_records_workspace_id_name_measured_at",
            "workspace_id",
            "name",
            "measured_at",
        ),
        Index("ix_capacity_records_resource_type_measured_at", "resource_type", "measured_at"),
    )

    # Display label, e.g. "GPU Capacity (A100)" or "Network Ingress (Gbps)".
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Coarse family used for grouping and icon selection: GPU, CPU, Memory, ...
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    region: Mapped[str | None] = mapped_column(String(60), index=True, nullable=True)
    provisioned: Mapped[float] = mapped_column(MEASURE, default=0.0, nullable=False)
    used: Mapped[float] = mapped_column(MEASURE, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), default="units", nullable=False)
    # Stored rather than derived: the source system reports it and rounding must
    # match what the operator saw when the reading was taken.
    utilization_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=CapacityStatus.HEALTHY.value, nullable=False
    )
    measured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertSeverity(str, enum.Enum):
    """Severity ladder shown as the coloured chip on every alert row."""

    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INFO = "Info"


class AlertStatus(str, enum.Enum):
    """Triage state. ``MUTED`` suppresses notification without closing the alert,
    which is what an operator wants during a known maintenance window."""

    OPEN = "Open"
    INVESTIGATING = "Investigating"
    ACKNOWLEDGED = "Acknowledged"
    RESOLVED = "Resolved"
    MUTED = "Muted"


class Alert(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A raised condition needing human attention.

    ``dedupe_key`` is the anti-flood mechanism: a recurring condition bumps
    ``occurrence_count`` and ``last_occurred_at`` on the existing open row rather
    than inserting a new one, so the Alerts screen stays readable during an
    incident.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "alert_ref"),
        # Dedupe lookup on every raise; the rest are the screen's filter chips.
        Index("ix_alerts_workspace_id_dedupe_key", "workspace_id", "dedupe_key"),
        Index("ix_alerts_workspace_id_status_severity", "workspace_id", "status", "severity"),
        Index("ix_alerts_workspace_id_raised_at", "workspace_id", "raised_at"),
        Index(
            "ix_alerts_source_entity_type_source_entity_id",
            "source_entity_type",
            "source_entity_id",
        ),
        # The dedupe rule this table's docstring describes, made true. Two
        # simultaneous raises of one condition both passed the lookup and both
        # inserted; raise_alert already absorbs the IntegrityError onto the
        # twin, so this index is the whole fix.
        Index(
            "uq_alerts_open_dedupe",
            "workspace_id",
            "dedupe_key",
            unique=True,
            sqlite_where=text("dedupe_key IS NOT NULL AND status <> 'Resolved'"),
            postgresql_where=text("dedupe_key IS NOT NULL AND status <> 'Resolved'"),
        ),
    )

    # Human-facing identifier quoted in incident channels, e.g. "al-1".
    alert_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default=AlertSeverity.MEDIUM.value, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Originating screen name, e.g. "Connection Center", "Quota, Cost & Capacity".
    # Stored as the label so the alert can deep-link back without a lookup table.
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    # The row that caused it: ("Secret", <id>), ("Deployment", <id>), ...
    source_entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=AlertStatus.OPEN.value, nullable=False
    )
    raised_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    acknowledged_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assigned_to_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Frozen on resolve so the MTTR tile never re-derives from moving timestamps.
    mttr_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stable fingerprint of the condition, e.g. "budget:<id>:threshold:80".
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_occurred_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Set when the telemetry engine's own alerting raised this, so an
    # acknowledgement here can be pushed back to the engine.
    engine_alert_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # Payload from the raiser: thresholds, observed values, affected agent count.
    event_metadata: Mapped[dict] = mapped_column(default=dict)

    @property
    def is_open(self) -> bool:
        return self.status not in (AlertStatus.RESOLVED.value, AlertStatus.MUTED.value)


class AlertRule(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """The condition that raises an ``Alert``.

    ``condition`` is left as an opaque document because each ``source`` screen
    evaluates its own shape (a threshold, a rate, a missing heartbeat) and the
    rule editor round-trips it untouched.
    """

    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_alert_rules_workspace_id_source", "workspace_id", "source"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Screen this rule watches; matches Alert.source so the two lists join by label.
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    condition: Mapped[dict] = mapped_column(default=dict)
    severity: Mapped[str] = mapped_column(
        String(16), default=AlertSeverity.MEDIUM.value, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Channel refs: ["email:ops@...", "slack:#ai-ops", "webhook:<id>"].
    notify_channels: Mapped[list] = mapped_column(default=list)
    # Minimum gap between notifications for the same dedupe key.
    throttle_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #


class ExportFormat(str, enum.Enum):
    """Output formats offered by the Exports screen."""

    CSV = "CSV"
    JSON = "JSON"
    XLSX = "XLSX"
    PDF = "PDF"
    PARQUET = "Parquet"


class ExportStatus(str, enum.Enum):
    """Lifecycle of a generated file. ``READY`` is the downloadable state and is
    what the UI labels as completed; ``EXPIRED`` means the object was reaped."""

    QUEUED = "Queued"
    GENERATING = "Generating"
    READY = "Ready"
    FAILED = "Failed"
    EXPIRED = "Expired"


class ExportJob(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One generated extract of a screen's current view.

    ``filters`` records the exact query that produced the file, which is what
    makes an export defensible in an audit months after the fact.
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "export_ref"),
        Index("ix_export_jobs_workspace_id_requested_at", "workspace_id", "requested_at"),
        Index("ix_export_jobs_workspace_id_status", "workspace_id", "status"),
    )

    # Human-facing identifier, e.g. "exp-1".
    export_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Screen the data came from, e.g. "Live Runs", "Approvals & Audit".
    source_screen: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    export_format: Mapped[str] = mapped_column(
        String(16), default=ExportFormat.CSV.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ExportStatus.QUEUED.value, nullable=False
    )
    filters: Mapped[dict] = mapped_column(default=dict)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Null for scheduled runs, which the UI attributes to "System (Scheduled)".
    requested_by_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Retention deadline; the reaper flips the row to EXPIRED and drops the object.
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExportSchedule(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A recurring export. Each firing creates an ``ExportJob``, so the job table
    remains the single record of what was actually produced."""

    __tablename__ = "export_schedules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        # The scheduler sweeps enabled rows that are due.
        Index("ix_export_schedules_enabled_next_run_at", "enabled", "next_run_at"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_screen: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    export_format: Mapped[str] = mapped_column(
        String(16), default=ExportFormat.CSV.value, nullable=False
    )
    # Standard five-field cron expression, evaluated in UTC.
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    filters: Mapped[dict] = mapped_column(default=dict)
    # Delivery targets: email addresses or channel refs.
    recipients: Mapped[list] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
