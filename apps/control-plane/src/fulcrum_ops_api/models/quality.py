"""Quality workflow: test suites and runs, evaluations, guardrails, feedback loop.

These tables hold only the *workflow* state around quality — what a team decided
to run, who owns it, what was opened, assigned, fixed and shipped. The actual
scores, spans and traces are **not** stored here: they live in the telemetry
engine and are fetched live at read time through ``engine_*`` reference columns
(``engine_experiment_id``, ``engine_guardrail_id``, ``trace_id``, ...). What is
duplicated here — ``pass_rate``, ``scores``, the 30-day guardrail counters — is a
deliberate cache of the last engine answer so list screens render without a
fan-out call per row; treat it as stale-tolerant and re-read the engine for
detail views.

The status vocabularies below are the exact strings the 23-screen control plane
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
    String,
    Text,
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

# --------------------------------------------------------------------------- #
# Vocabularies. Stored as plain strings (see identity.Role) so a new UI value
# never needs a Postgres ENUM migration; the enum is the validation surface.
# --------------------------------------------------------------------------- #


class SuiteType(str, enum.Enum):
    REGRESSION = "Regression"
    EVALUATION = "Evaluation"
    LOAD = "Load"
    SMOKE = "Smoke"
    SECURITY = "Security"


class SuiteStatus(str, enum.Enum):
    """Lifecycle of the suite definition — not the verdict of its last run."""

    ACTIVE = "Active"
    DRAFT = "Draft"
    DISABLED = "Disabled"


class TestRunStatus(str, enum.Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    PASSED = "Passed"
    FAILED = "Failed"
    ERROR = "Error"          # harness blew up; distinct from assertion failure
    CANCELLED = "Cancelled"


class RunTrigger(str, enum.Enum):
    MANUAL = "Manual"
    SCHEDULE = "Schedule"
    CI = "CI"
    DEPLOYMENT = "Deployment"


class EvaluationStatus(str, enum.Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class GuardrailType(str, enum.Enum):
    PROMPT_INJECTION = "Prompt Injection"
    PII = "PII"
    TOXICITY = "Toxicity"
    HALLUCINATION = "Hallucination"
    SECRETS = "Secrets"
    TOPIC = "Topic"
    CUSTOM = "Custom"


class GuardrailStatus(str, enum.Enum):
    ACTIVE = "Active"
    DISABLED = "Disabled"
    TUNING = "Tuning"        # shadow mode: scores are recorded, nothing enforced


class GuardrailAction(str, enum.Enum):
    BLOCK = "Block"
    MASK = "Mask"
    WARN = "Warn"
    LOG = "Log"


class GuardrailScope(str, enum.Enum):
    GLOBAL = "Global"
    AGENT = "Agent"
    ENVIRONMENT = "Environment"


class Sentiment(str, enum.Enum):
    POSITIVE = "Positive"
    NEUTRAL = "Neutral"
    NEGATIVE = "Negative"


class FeedbackSource(str, enum.Enum):
    END_USER = "End User (In-App)"
    AGENT_RATING = "Agent Response Rating"
    SUPPORT_TICKET = "Support Ticket"
    MANUAL_REVIEW = "Manual Review"


class IssueSeverity(str, enum.Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class IssueStatus(str, enum.Enum):
    OPEN = "Open"
    IN_PROGRESS = "In Progress"
    RESOLVED = "Resolved"
    WONT_FIX = "Wont Fix"


class BacklogPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class BacklogStatus(str, enum.Enum):
    BACKLOG = "Backlog"
    PLANNED = "Planned"
    IN_PROGRESS = "In Progress"
    DONE = "Done"


# --------------------------------------------------------------------------- #
# Testing & Regression
# --------------------------------------------------------------------------- #


class TestSuite(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A named, schedulable set of cases. The cases themselves are an engine dataset."""

    __tablename__ = "test_suites"
    __table_args__ = (
        Index("ix_test_suites_workspace_status", "workspace_id", "status"),
        Index("ix_test_suites_workspace_type", "workspace_id", "suite_type"),
        Index("ix_test_suites_workspace_agent", "workspace_id", "agent_id"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    suite_type: Mapped[str] = mapped_column(
        String(32), default=SuiteType.REGRESSION.value, nullable=False
    )
    environment: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=SuiteStatus.ACTIVE.value, nullable=False
    )
    # Null when the suite exercises a shared capability rather than one agent.
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Name/id of the dataset in the telemetry engine this suite runs against.
    dataset_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Run whose results every new run is diffed against on the suite detail screen.
    baseline_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Cached from the last finished run so the list screen needs no engine call.
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    flaky_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list] = mapped_column(default=list)

    runs: Mapped[list[TestRun]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )


class TestRun(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One execution of a suite. Per-case detail stays in the engine experiment."""

    __tablename__ = "test_runs"
    __table_args__ = (
        Index("ix_test_runs_suite_started", "suite_id", "started_at"),
        Index("ix_test_runs_workspace_status", "workspace_id", "status"),
        Index("ix_test_runs_workspace_run_ref", "workspace_id", "run_ref", unique=True),
    )

    suite_id: Mapped[str] = mapped_column(
        ForeignKey("test_suites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=TestRunStatus.QUEUED.value, nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Cases that passed on the baseline and fail here — what drives the alert.
    regression_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trigger: Mapped[str] = mapped_column(
        String(24), default=RunTrigger.MANUAL.value, nullable=False
    )
    # Experiment created in the telemetry engine; scores are read back from it.
    engine_experiment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    baseline_comparison: Mapped[dict] = mapped_column(default=dict)
    summary: Mapped[dict] = mapped_column(default=dict)

    suite: Mapped[TestSuite] = relationship(back_populates="runs")


# --------------------------------------------------------------------------- #
# Evaluations
# --------------------------------------------------------------------------- #


class EvaluationRun(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """An LLM-judged scoring pass over a dataset. Judged scores come from the engine."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        Index("ix_evaluation_runs_workspace_status", "workspace_id", "status"),
        Index("ix_evaluation_runs_workspace_agent", "workspace_id", "agent_id"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dataset_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    judge_model: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=EvaluationStatus.QUEUED.value, nullable=False
    )
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Cached averages {correctness, grounding, faithfulness, safety} — the engine
    # remains the source of truth for the per-case scores behind them.
    scores: Mapped[dict] = mapped_column(default=dict)
    engine_experiment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Set only when this evaluation fed a prompt-optimization job in the engine.
    engine_optimization_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


class GuardrailConfig(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A configured check. Enforcement runs in the engine; this row is its control record."""

    __tablename__ = "guardrail_configs"
    __table_args__ = (
        Index("ix_guardrail_configs_workspace_status", "workspace_id", "status"),
        Index("ix_guardrail_configs_workspace_type", "workspace_id", "guardrail_type"),
        Index("ix_guardrail_configs_scope", "workspace_id", "scope", "scope_ref"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    guardrail_type: Mapped[str] = mapped_column(
        String(40), default=GuardrailType.CUSTOM.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=GuardrailStatus.ACTIVE.value, nullable=False
    )
    action: Mapped[str] = mapped_column(
        String(24), default=GuardrailAction.WARN.value, nullable=False
    )
    threshold: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    scope: Mapped[str] = mapped_column(
        String(24), default=GuardrailScope.GLOBAL.value, nullable=False
    )
    # Agent id or environment name, depending on `scope`; null when Global.
    scope_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    config: Mapped[dict] = mapped_column(default=dict)
    engine_guardrail_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    added_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    effectiveness: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 30-day rollups cached from the engine so the tiles render in one query.
    triggers_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    masked_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_triggered_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)

    events: Mapped[list[GuardrailEvent]] = relationship(
        back_populates="guardrail", cascade="all, delete-orphan"
    )


class GuardrailEvent(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One trigger, persisted for the audit trail; the full trace stays in the engine."""

    __tablename__ = "guardrail_events"
    __table_args__ = (
        Index("ix_guardrail_events_guardrail_occurred", "guardrail_id", "occurred_at"),
        Index("ix_guardrail_events_workspace_occurred", "workspace_id", "occurred_at"),
    )

    guardrail_id: Mapped[str] = mapped_column(
        ForeignKey("guardrail_configs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Engine trace this trigger belongs to; the UI deep-links to it.
    trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    action_taken: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched: Mapped[dict] = mapped_column(default=dict)
    # Truncated excerpt only — never the full payload, which stays in the engine.
    sample: Mapped[str | None] = mapped_column(Text, nullable=True)

    guardrail: Mapped[GuardrailConfig] = relationship(back_populates="events")


# --------------------------------------------------------------------------- #
# Feedback & Quality Loop
# --------------------------------------------------------------------------- #


class FeedbackItem(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One piece of human feedback. Mirrored into the engine as a feedback score."""

    __tablename__ = "feedback_items"
    __table_args__ = (
        Index("ix_feedback_items_workspace_submitted", "workspace_id", "submitted_at"),
        Index("ix_feedback_items_workspace_agent", "workspace_id", "agent_id"),
        Index("ix_feedback_items_workspace_ref", "workspace_id", "feedback_ref", unique=True),
    )

    feedback_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-5
    sentiment: Mapped[str] = mapped_column(
        String(16), default=Sentiment.NEUTRAL.value, index=True, nullable=False
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(40), default=FeedbackSource.END_USER.value, nullable=False
    )
    submitted_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    submitted_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    # Filled in by the clustering pass, so it is null until that job has run.
    theme: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    issue_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_issues.id", ondelete="SET NULL"), index=True, nullable=True
    )
    engine_feedback_score_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Named `event_metadata`: `metadata` is reserved on the declarative base.
    event_metadata: Mapped[dict] = mapped_column(default=dict)

    issue: Mapped[FeedbackIssue | None] = relationship(back_populates="feedback_items")


class FeedbackIssue(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A theme promoted to tracked work, with an SLA the quality review reports on."""

    __tablename__ = "feedback_issues"
    __table_args__ = (
        Index("ix_feedback_issues_workspace_status", "workspace_id", "status"),
        Index("ix_feedback_issues_workspace_severity", "workspace_id", "severity"),
        Index("ix_feedback_issues_workspace_ref", "workspace_id", "issue_ref", unique=True),
    )

    issue_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    theme: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    severity: Mapped[str] = mapped_column(
        String(16), default=IssueSeverity.MEDIUM.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=IssueStatus.OPEN.value, nullable=False
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # Cached count of linked feedback — the number the issue list sorts on.
    feedback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assigned_team: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_to_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    opened_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sla_due_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Frozen at resolution time rather than derived, so a later SLA policy change
    # cannot rewrite history.
    sla_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    feedback_items: Mapped[list[FeedbackItem]] = relationship(back_populates="issue")


class BacklogItem(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """Planned work on the quality loop, whether or not it came from an issue."""

    __tablename__ = "backlog_items"
    __table_args__ = (
        Index("ix_backlog_items_workspace_status", "workspace_id", "status"),
        Index("ix_backlog_items_workspace_priority", "workspace_id", "priority"),
        # An issue is planned once. Closed work does not count, so the same
        # issue can be raised again later.
        Index(
            "uq_backlog_items_open_issue",
            "issue_id",
            unique=True,
            sqlite_where=text("issue_id IS NOT NULL AND status <> 'Done'"),
            postgresql_where=text("issue_id IS NOT NULL AND status <> 'Done'"),
        ),
    )

    issue_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_issues.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(8), default=BacklogPriority.P2.value, nullable=False
    )
    effort: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default=BacklogStatus.BACKLOG.value, nullable=False
    )
    assigned_team: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_release: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Improvement(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A shipped fix with its before/after metric — the proof the loop closed."""

    __tablename__ = "improvements"
    __table_args__ = (
        Index("ix_improvements_workspace_deployed", "workspace_id", "deployed_at"),
        Index("ix_improvements_workspace_agent", "workspace_id", "agent_id"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_issues.id", ondelete="SET NULL"), index=True, nullable=True
    )
    backlog_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("backlog_items.id", ondelete="SET NULL"), index=True, nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deployed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    impact_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_metric: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_metric: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # True once a post-deploy run confirmed the gain; unverified fixes are not
    # counted in the improvement rollup.
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    @property
    def delta(self) -> float | None:
        if self.before_metric is None or self.after_metric is None:
            return None
        return self.after_metric - self.before_metric
