"""Wire contracts for the Feedback & Quality Loop.

The loop has four stages and this module has four families of schema for them:
raw feedback comes in (from the console, from an SDK, from a support desk), the
clustering pass groups it into themes, a theme is promoted to an issue, and an
issue becomes backlog work and then a deployed improvement. Every stage carries
its own identifiers so the funnel on the screen can be counted rather than
narrated.

Two settings documents live here as well — the SLA rules the triage clock runs
on, and the collection settings that describe how feedback is gathered. Both are
validated here and stored on the workspace, so the console's editors cannot save
a rule set the service would then have to guess about.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.quality import (
    BacklogPriority,
    BacklogStatus,
    FeedbackSource,
    IssueSeverity,
    IssueStatus,
    Sentiment,
)

#: Key the feedback documents live under inside ``workspaces.settings``.
SETTINGS_NAMESPACE: Final[str] = "feedback"

MAX_BODY_LENGTH: Final[int] = 4000
MAX_TAGS: Final[int] = 10

#: Ratings are a five-point scale; four and up reads positive, three neutral.
MIN_RATING: Final[int] = 1
MAX_RATING: Final[int] = 5
POSITIVE_RATING: Final[int] = 4
NEUTRAL_RATING: Final[int] = 3

#: Longest window any KPI card or chart may ask for.
MAX_WINDOW_DAYS: Final[int] = 365
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: Clustering knobs. The threshold is Jaccard overlap between two normalised
#: token sets: high enough that "invoice extraction fails" and "cannot extract
#: invoice totals" land together, low enough that unrelated complaints do not.
DEFAULT_SIMILARITY: Final[float] = 0.34
MIN_SIMILARITY: Final[float] = 0.10
MAX_SIMILARITY: Final[float] = 0.90
DEFAULT_MIN_CLUSTER_SIZE: Final[int] = 2

#: Rows one clustering pass will consider. Beyond this the caller narrows the
#: window. The grouping compares a comment only with the leaders it shares a
#: word with and runs off the event loop, but its worst case — every comment
#: sharing one word and nothing else — is still rows x clusters, and the read
#: has to be bounded either way.
MAX_ANALYZE_ROWS: Final[int] = 2000


def sentiment_for(rating: int | None) -> Sentiment:
    """The sentiment a bare star rating implies."""
    if rating is None:
        return Sentiment.NEUTRAL
    if rating >= POSITIVE_RATING:
        return Sentiment.POSITIVE
    if rating == NEUTRAL_RATING:
        return Sentiment.NEUTRAL
    return Sentiment.NEGATIVE


# ---------------------------------------------------------------------------
# Vocabularies owned by the API rather than by a column
# ---------------------------------------------------------------------------


class IssueRouting(enum.StrEnum):
    """Where a new issue is routed when it is opened."""

    AGENT_OWNER_TEAM = "Agent owner team"
    FIXED_TEAM = "Fixed team"


class FunnelStageName(enum.StrEnum):
    """The five stages the Feedback -> Outcome funnel counts."""

    RECEIVED = "Feedback received"
    CLUSTERED = "Auto-clustered to themes"
    ISSUES = "Issues opened"
    BACKLOG = "Backlog items"
    DEPLOYED = "Improvements deployed"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class FeedbackRead(BaseModel):
    """One row of the feedback table, and everything its inspector shows."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    feedback_ref: str = Field(description="Stable public id, e.g. 'fb_a1b2c3d4'")
    agent_id: str | None = None
    agent_name: str | None = None
    trace_id: str | None = Field(None, description="Run the feedback is about")
    rating: int | None = None
    sentiment: Sentiment
    body: str | None = None
    source: FeedbackSource
    submitted_by: str | None = None
    submitted_at: dt.datetime

    theme: str | None = Field(None, description="Cluster label; null until analysed")
    cluster_id: str | None = Field(None, description="Slug of the theme")
    issue_id: str | None = None
    issue_ref: str | None = None
    issue_title: str | None = None

    environment: str | None = None
    tags: list[str] = Field(default_factory=list)
    scored_in_telemetry: bool = Field(
        False, description="True when the rating was written onto the trace"
    )
    pii_scrubbed: list[str] = Field(
        default_factory=list,
        description=(
            "Kinds of identifier removed from the comment at capture — email, "
            "phone, ssn, card. Never the values"
        ),
    )
    created_at: dt.datetime


class FeedbackCreate(BaseModel):
    """Submit feedback.

    This is the SDK's endpoint as much as the console's, so it accepts a caller
    supplied ``feedback_ref`` for idempotency and refuses a submission that
    carries neither a rating nor a comment — that is a request with no signal in
    it.
    """

    agent_id: str | None = Field(None, max_length=36)
    trace_id: str | None = Field(None, max_length=120)
    rating: int | None = Field(None, ge=MIN_RATING, le=MAX_RATING)
    sentiment: Sentiment | None = Field(
        None, description="Derived from the rating when omitted"
    )
    body: str | None = Field(None, max_length=MAX_BODY_LENGTH)
    source: FeedbackSource = FeedbackSource.END_USER
    submitted_by: str | None = Field(None, max_length=160)
    submitted_at: dt.datetime | None = Field(
        None, description="Defaults to now; a future timestamp is refused"
    )
    feedback_ref: str | None = Field(
        None, max_length=64, description="Idempotency key, unique per workspace"
    )
    environment: str | None = Field(None, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body", "submitted_by", "environment", "feedback_ref")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = [tag.strip() for tag in value if tag and tag.strip()]
        if any(len(tag) > 40 for tag in cleaned):
            raise ValueError("a tag may not exceed 40 characters")
        return cleaned

    @model_validator(mode="after")
    def _check(self) -> FeedbackCreate:
        if self.rating is None and not self.body:
            raise ValueError("feedback needs a rating, a comment, or both")
        if self.sentiment is None:
            self.sentiment = sentiment_for(self.rating)
        return self


class FeedbackUpdate(BaseModel):
    """Triage one item: correct its theme or link it to an issue.

    The rating and the comment are what the user said and are never rewritten
    here; only the triage fields move.
    """

    theme: str | None = Field(None, max_length=120)
    issue_id: str | None = Field(None, max_length=36)
    sentiment: Sentiment | None = None
    tags: list[str] | None = Field(None, max_length=MAX_TAGS)

    @model_validator(mode="after")
    def _any(self) -> FeedbackUpdate:
        if self.model_fields_set:
            return self
        raise ValueError("send at least one field to update")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


class FeedbackExample(BaseModel):
    """One representative item quoted back inside a cluster."""

    id: str
    feedback_ref: str
    rating: int | None = None
    sentiment: Sentiment
    body: str | None = None
    agent_name: str | None = None
    submitted_at: dt.datetime


class ClusterRead(BaseModel):
    """One theme the clustering pass found, or found new reports of.

    ``size``, ``negative_share``, ``avg_rating`` and the first/last seen times
    describe the theme over the window — every row that wears the label, not
    only the ones this pass added, which are counted in ``new_members``. The
    keywords, agents, sources and examples are drawn from the new members.
    """

    cluster_id: str
    theme: str
    size: int
    new_members: int = Field(0, description="Rows this pass put into the theme")
    keywords: list[str] = Field(default_factory=list)
    negative_share: float = Field(description="Share of the cluster that reads negative, 0-1")
    avg_rating: float | None = None
    agents: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    first_seen_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    suggested_issue_title: str
    suggested_severity: IssueSeverity
    meets_auto_issue_threshold: bool = Field(
        description=(
            "True when the theme's negative reports that no issue answers for "
            "reached the SLA rules' threshold — which is when the pass opens one"
        )
    )
    issue_auto_opened: bool = Field(
        False, description="True when this pass opened `open_issue_id` under that rule"
    )
    open_issue_id: str | None = Field(
        None,
        description=(
            "Issue open for this theme, whether it was already or this pass opened "
            "it. The theme's unlinked reports are linked to it, so offer the issue, "
            "not Create Issue"
        ),
    )
    open_issue_ref: str | None = None
    examples: list[FeedbackExample] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """Run the clustering pass."""

    window_days: int = Field(DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS)
    min_cluster_size: int = Field(DEFAULT_MIN_CLUSTER_SIZE, ge=1, le=100)
    similarity: float = Field(DEFAULT_SIMILARITY, ge=MIN_SIMILARITY, le=MAX_SIMILARITY)
    recluster: bool = Field(
        False, description="Re-examine items that already carry a theme"
    )
    negative_only: bool = Field(
        False, description="Cluster only the negative feedback, as the Actions tab does"
    )
    agent_id: str | None = Field(None, max_length=36)


class AnalyzeResult(BaseModel):
    """What the pass looked at, and what it decided."""

    analysed: int
    clustered: int
    unclustered: int
    linked_to_issues: int = Field(
        0, description="Reports this pass attached to the issue open for their theme"
    )
    issues_auto_opened: int = Field(
        0, description="Issues this pass opened under the auto-issue threshold"
    )
    clusters: list[ClusterRead] = Field(default_factory=list)
    window_days: int
    similarity: float
    ran_at: dt.datetime


class ThemeRead(BaseModel):
    """One persisted theme, as the Quality Insights tab lists them."""

    cluster_id: str
    theme: str
    size: int
    negative_share: float
    avg_rating: float | None = None
    open_issue_id: str | None = None
    first_seen_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


class FeedbackIssueRead(BaseModel):
    """One row of the Issues tab."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    issue_ref: str
    title: str
    description: str | None = None
    theme: str | None = Field(None, description="Rendered as the Category column")
    severity: IssueSeverity
    status: IssueStatus
    agent_id: str | None = None
    agent_name: str | None = None
    feedback_count: int = Field(0, description="Linked feedback, all time")
    reports_30d: int = Field(0, description="Linked feedback inside the KPI window")
    assigned_team: str | None = None
    assigned_to_user_id: str | None = None
    assigned_to_name: str | None = None
    opened_at: dt.datetime
    resolved_at: dt.datetime | None = None
    sla_due_at: dt.datetime | None = None
    sla_met: bool | None = None
    overdue: bool = Field(False, description="Past its SLA and still open")
    backlog_item_id: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class FeedbackIssueCreate(BaseModel):
    """Open an issue, normally from a cluster the analysis pass found.

    Either ``cluster_id`` or ``feedback_ids`` identifies what the issue is about;
    both may be sent, and every matching item is linked to the new issue.
    """

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=MAX_BODY_LENGTH)
    severity: IssueSeverity = IssueSeverity.MEDIUM
    theme: str | None = Field(None, max_length=120)
    cluster_id: str | None = Field(None, max_length=120)
    feedback_ids: list[str] = Field(default_factory=list, max_length=500)
    agent_id: str | None = Field(None, max_length=36)
    assigned_team: str | None = Field(None, max_length=120)
    assigned_to_user_id: str | None = Field(None, max_length=36)

    @field_validator("title")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @model_validator(mode="after")
    def _check(self) -> FeedbackIssueCreate:
        if not self.cluster_id and not self.feedback_ids and not self.theme:
            raise ValueError(
                "identify the issue with cluster_id, theme, or feedback_ids"
            )
        return self


class FeedbackIssueUpdate(BaseModel):
    """Assign an issue, re-grade it, or move it through its state machine."""

    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=MAX_BODY_LENGTH)
    severity: IssueSeverity | None = None
    status: IssueStatus | None = None
    assigned_team: str | None = Field(None, max_length=120)
    assigned_to_user_id: str | None = Field(None, max_length=36)
    resolution_note: str | None = Field(None, max_length=MAX_BODY_LENGTH)

    @model_validator(mode="after")
    def _any(self) -> FeedbackIssueUpdate:
        if self.model_fields_set:
            return self
        raise ValueError("send at least one field to update")


# ---------------------------------------------------------------------------
# Backlog and improvements
# ---------------------------------------------------------------------------


class BacklogItemRead(BaseModel):
    """One row of the Improvement Backlog tab."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    issue_id: str | None = None
    issue_ref: str | None = None
    title: str
    description: str | None = None
    priority: BacklogPriority
    effort: str | None = None
    status: BacklogStatus
    assigned_team: str | None = None
    target_release: str | None = None
    votes: int = Field(
        0, description="Feedback items behind this item, via its issue — the demand signal"
    )
    improvement_id: str | None = Field(None, description="Fix task tracking this item")
    created_by_user_id: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class BacklogItemCreate(BaseModel):
    """Add an improvement candidate, with or without an issue behind it."""

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=MAX_BODY_LENGTH)
    priority: BacklogPriority = BacklogPriority.P2
    effort: str | None = Field(None, max_length=40)
    assigned_team: str | None = Field(None, max_length=120)
    target_release: str | None = Field(None, max_length=40)
    issue_id: str | None = Field(None, max_length=36)
    feedback_id: str | None = Field(
        None, max_length=36, description="Feedback this candidate came from"
    )

    @field_validator("title")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class BacklogPromoteRequest(BaseModel):
    """Promote an issue onto the backlog."""

    priority: BacklogPriority | None = Field(
        None, description="Derived from the issue's severity when omitted"
    )
    effort: str | None = Field(None, max_length=40)
    target_release: str | None = Field(None, max_length=40)
    assigned_team: str | None = Field(None, max_length=120)


class BacklogItemUpdate(BaseModel):
    """Move a backlog item, or re-plan it.

    Marking an item Done requires the deployment detail, because Done is what
    the Improvements Deployed card counts.
    """

    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=MAX_BODY_LENGTH)
    priority: BacklogPriority | None = None
    status: BacklogStatus | None = None
    effort: str | None = Field(None, max_length=40)
    assigned_team: str | None = Field(None, max_length=120)
    target_release: str | None = Field(None, max_length=40)
    deployment_id: str | None = Field(None, max_length=36)
    after_metric: float | None = Field(
        None,
        description=(
            "Measured value after the fix shipped; the improvement counts as "
            "verified only when it is above the baseline captured by the fix task"
        ),
    )
    impact_summary: str | None = Field(None, max_length=MAX_BODY_LENGTH)

    @model_validator(mode="after")
    def _any(self) -> BacklogItemUpdate:
        if self.model_fields_set:
            return self
        raise ValueError("send at least one field to update")


class FixTaskCreate(BaseModel):
    """Open the fix task that tracks a backlog item to a measured outcome."""

    assigned_team: str | None = Field(None, max_length=120)
    target_release: str | None = Field(None, max_length=40)
    metric_name: str | None = Field(
        None,
        max_length=80,
        description=(
            "Metric the fix is meant to move, stated so that higher is better — "
            "that is the direction the improvement is verified against"
        ),
    )
    before_metric: float | None = Field(
        None, description="Current value of that metric, captured now"
    )
    agent_id: str | None = Field(None, max_length=36)
    note: str | None = Field(None, max_length=MAX_BODY_LENGTH)


class ImprovementRead(BaseModel):
    """One shipped fix, with the before/after that proves the loop closed."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None = None
    issue_id: str | None = None
    backlog_item_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    deployed_at: dt.datetime | None = None
    deployment_id: str | None = None
    impact_summary: str | None = None
    before_metric: float | None = None
    after_metric: float | None = None
    delta: float | None = None
    metric_name: str | None = None
    verified: bool = False
    status: str = Field(description="Deployed once it has shipped, otherwise In Progress")
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Settings documents
# ---------------------------------------------------------------------------


class SeveritySla(BaseModel):
    """Hours a team has to resolve an issue, by severity.

    The windows must widen as severity falls: a Low issue may not be due before
    a Critical one, or the triage queue sorts itself backwards.
    """

    critical: int = Field(4, ge=1, le=2160)
    high: int = Field(8, ge=1, le=2160)
    medium: int = Field(24, ge=1, le=2160)
    low: int = Field(72, ge=1, le=2160)

    @model_validator(mode="after")
    def _monotonic(self) -> SeveritySla:
        if not self.critical <= self.high <= self.medium <= self.low:
            raise ValueError(
                "severity windows must widen: critical <= high <= medium <= low"
            )
        return self

    def hours_for(self, severity: IssueSeverity) -> int:
        return {
            IssueSeverity.CRITICAL: self.critical,
            IssueSeverity.HIGH: self.high,
            IssueSeverity.MEDIUM: self.medium,
            IssueSeverity.LOW: self.low,
        }[severity]


class SlaRules(BaseModel):
    """The SLA & Routing card, validated.

    ``business_hours_only`` makes the triage clock run Monday to Friday inside
    ``business_day_start_hour``..``business_day_end_hour`` UTC, which is how the
    "4 business hours" the console shows is actually measured.

    What the service acts on: the business-hours clock and ``severity_sla`` set
    every issue's due date, ``routing`` picks its team, and ``auto_issue_threshold``
    opens issues from the clustering pass. ``triage_sla_hours`` and the two
    escalation fields are the team's stated targets — stored, audited and shown,
    but nothing sweeps for a breach or notifies the contact, and no reader
    should present them as if something did.
    """

    triage_sla_hours: int = Field(
        4,
        ge=1,
        le=720,
        description="Target for first triage of negative feedback. A target: not swept for",
    )
    business_hours_only: bool = True
    business_day_start_hour: int = Field(9, ge=0, le=23)
    business_day_end_hour: int = Field(17, ge=1, le=24)
    auto_issue_threshold: int = Field(
        5,
        ge=2,
        le=1000,
        description=(
            "Similar negative reports, not yet behind an issue, at which the "
            "clustering pass opens one itself"
        ),
    )
    routing: IssueRouting = IssueRouting.AGENT_OWNER_TEAM
    routing_team: str | None = Field(None, max_length=120)
    escalation_contact: str = Field(
        "CX lead", max_length=120, description="Who a breach goes to. Nobody is notified by us"
    )
    escalate_after_hours: int = Field(
        8, ge=1, le=720, description="When a breach is theirs. A target: not swept for"
    )
    severity_sla: SeveritySla = Field(default_factory=SeveritySla)

    @model_validator(mode="after")
    def _check(self) -> SlaRules:
        if self.business_day_end_hour <= self.business_day_start_hour:
            raise ValueError("the business day must end after it starts")
        if self.routing is IssueRouting.FIXED_TEAM and not (self.routing_team or "").strip():
            raise ValueError("routing_team is required when routing to a fixed team")
        if self.routing_team is not None:
            self.routing_team = self.routing_team.strip() or None
        return self


class CollectionSettings(BaseModel):
    """The Collection Settings card, validated.

    ``pii_scrubbing`` is the one switch the service itself obeys: while it is on,
    every comment is scrubbed at capture. The rest describe how the customer's
    own application and review process gather feedback; they are recorded here
    so the team has one place to state them, and nothing samples runs for review.
    """

    in_app_rating_prompt: bool = True
    in_app_rating_trigger: str = Field("After each session", max_length=80)
    response_thumbs: bool = True
    support_ticket_ingestion: bool = False
    support_ticket_system: str | None = Field(None, max_length=80)
    manual_review_sample_percent: float = Field(5.0, ge=0, le=100)
    pii_scrubbing: bool = Field(
        True,
        description=(
            "Replace email addresses, phone numbers, SSNs and card numbers in a "
            "comment before it is stored, exported or mirrored to telemetry"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> CollectionSettings:
        if self.support_ticket_ingestion and not (self.support_ticket_system or "").strip():
            raise ValueError(
                "name the system tickets are ingested from, or turn ingestion off"
            )
        if self.support_ticket_system is not None:
            self.support_ticket_system = self.support_ticket_system.strip() or None
        return self


# ---------------------------------------------------------------------------
# KPI cards, funnel and insights
# ---------------------------------------------------------------------------


class FeedbackCountSlice(BaseModel):
    """One bucket of a grouped count on the summary."""

    label: str
    count: int
    percent: float = Field(description="Share of the window's feedback, 0-100")


class FunnelStage(BaseModel):
    """One stage of the Feedback -> Outcome funnel."""

    stage: FunnelStageName
    count: int
    percent_of_received: float
    conversion_from_previous: float | None = Field(
        None, description="Share of the previous stage that reached this one, 0-100"
    )


class FeedbackFunnel(BaseModel):
    """The whole funnel for one window."""

    window_days: int
    stages: list[FunnelStage] = Field(default_factory=list)


class FeedbackTrendPoint(BaseModel):
    """One day of the feedback trend and sentiment charts."""

    date: dt.date
    count: int
    avg_rating: float | None = None
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    positive_percent: float = 0.0
    negative_percent: float = 0.0


class AgentRating(BaseModel):
    """One bar of the Rating by Agent chart."""

    agent_id: str | None = None
    agent_name: str
    avg_rating: float | None = None
    feedback_count: int = 0
    negative_count: int = 0


class FeedbackInsights(BaseModel):
    """Everything the Quality Insights tab charts, in one round trip."""

    window_days: int
    daily: list[FeedbackTrendPoint] = Field(default_factory=list)
    rating_by_agent: list[AgentRating] = Field(default_factory=list)
    top_themes: list[ThemeRead] = Field(default_factory=list)


class FeedbackSummary(BaseModel):
    """The KPI cards above the tabs, with their period-over-period deltas.

    Every field is computed from the rows in the window; nothing here is stored
    or configured, so the cards cannot drift from the tables underneath them.
    """

    window_days: int

    total_feedback: int
    avg_rating: float | None = None
    positive_percent: float = 0.0
    issues_identified: int = 0
    improvements_deployed: int = 0
    sla_met_percent: float | None = None

    total_feedback_delta_percent: float | None = None
    avg_rating_delta: float | None = None
    positive_percent_delta_pp: float | None = None
    issues_identified_delta_percent: float | None = None
    improvements_deployed_delta_percent: float | None = None
    sla_met_delta_pp: float | None = None

    negative_percent: float = 0.0
    neutral_percent: float = 0.0
    rated_feedback: int = 0
    clustered_percent: float = 0.0
    open_issues: int = 0
    overdue_issues: int = 0
    backlog_open: int = 0
    backlog_in_progress: int = 0
    resolved_issues: int = 0
    auto_issue_threshold: int = 5

    sentiment_breakdown: list[FeedbackCountSlice] = Field(default_factory=list)
    source_breakdown: list[FeedbackCountSlice] = Field(default_factory=list)
    funnel: list[FunnelStage] = Field(default_factory=list)
