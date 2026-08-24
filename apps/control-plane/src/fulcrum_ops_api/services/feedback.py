"""Feedback & Quality Loop business logic.

This is the loop the screen is named after, and every stage of it is real work
rather than a status field:

* **capture** — ``submit`` writes our durable ``FeedbackItem`` row and mirrors
  the rating onto the trace as a telemetry feedback score through the adapter.
  The row is the record of what a human said; the score is what makes that
  visible next to the run it is about.
* **cluster** — ``analyze`` groups unthemed feedback with a deterministic,
  embedding-free algorithm (normalise, tokenise, greedy leader clustering on
  Jaccard overlap) and persists the theme it chose on every member, so the
  funnel's "auto-clustered" stage is a count of rows and not an estimate.
* **triage** — a theme becomes a ``FeedbackIssue`` with an SLA due date computed
  on a business-hours clock, moves through an enforced state machine, and freezes
  whether it met its SLA at the moment it is resolved.
* **fix** — an issue becomes a ``BacklogItem``, the backlog item's fix task is an
  ``Improvement`` capturing the metric before the change, and marking it done
  stamps the deployment, measures the delta and resolves the issue behind it.

Two conventions hold throughout: every statement filters on
``principal.workspace_id``, and every state change writes an audit row with this
screen as its source. The SLA rules and collection settings are validated
documents stored under the workspace's own settings, namespaced so no other
domain's keys are disturbed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import secrets
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..engine import EngineError, get_engine_client
from ..models.identity import Role, User, Workspace
from ..models.quality import (
    BacklogItem,
    BacklogPriority,
    BacklogStatus,
    FeedbackIssue,
    FeedbackItem,
    FeedbackSource,
    Improvement,
    IssueSeverity,
    IssueStatus,
    Sentiment,
)
from ..models.registry import Agent
from ..schemas.feedback import (
    DEFAULT_WINDOW_DAYS,
    MAX_ANALYZE_ROWS,
    SETTINGS_NAMESPACE,
    AgentRating,
    AnalyzeRequest,
    AnalyzeResult,
    BacklogItemCreate,
    BacklogItemRead,
    BacklogItemUpdate,
    BacklogPromoteRequest,
    ClusterRead,
    CollectionSettings,
    FeedbackCountSlice,
    FeedbackCreate,
    FeedbackExample,
    FeedbackFunnel,
    FeedbackInsights,
    FeedbackIssueCreate,
    FeedbackIssueRead,
    FeedbackIssueUpdate,
    FeedbackRead,
    FeedbackSummary,
    FeedbackTrendPoint,
    FeedbackUpdate,
    FixTaskCreate,
    FunnelStage,
    FunnelStageName,
    ImprovementRead,
    IssueRouting,
    SlaRules,
    ThemeRead,
    sentiment_for,
)
from . import audit

SOURCE_SCREEN: Final[str] = "Feedback & Quality Loop"
ENTITY_FEEDBACK: Final[str] = "feedback"
ENTITY_ISSUE: Final[str] = "feedback_issue"
ENTITY_BACKLOG: Final[str] = "backlog_item"
ENTITY_IMPROVEMENT: Final[str] = "improvement"
ENTITY_SETTINGS: Final[str] = "feedback_settings"

#: Name the rating is mirrored under on the trace. The telemetry store addresses
#: a score by (trace, name) rather than issuing an id of its own, so that pair is
#: what we keep on the row.
TELEMETRY_SCORE_NAME: Final[str] = "user_feedback"

#: An export is a report, not a bulk data channel.
MAX_EXPORT_ROWS: Final[int] = 5000

#: Rows the trend and sentiment charts bucket. A month of feedback is thousands
#: of rows, not millions, and only three columns are read.
MAX_INSIGHT_ROWS: Final[int] = 20000

#: Agents the Rating by Agent chart shows.
MAX_AGENT_BARS: Final[int] = 20

#: Themes the insights tab lists.
MAX_THEMES: Final[int] = 25

#: Words that carry no signal about *what* went wrong.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "the", "and", "for", "was", "were", "this", "that", "with", "have", "has",
        "had", "not", "but", "you", "your", "our", "its", "it's", "are", "can",
        "could", "would", "should", "did", "does", "done", "get", "got", "been",
        "being", "into", "than", "then", "them", "they", "there", "their", "when",
        "what", "which", "while", "will", "just", "very", "really", "too", "also",
        "from", "about", "after", "before", "again", "still", "some", "any", "all",
        "how", "why", "who", "one", "two", "out", "off", "over", "under", "each",
        "more", "most", "much", "many", "such", "only", "own", "same", "other",
        "please", "thanks", "thank", "hello", "agent", "assistant", "response",
        "answer", "reply", "asked", "asking", "using", "used", "use", "make",
        "made", "need", "needs", "want", "like", "good", "great", "nice", "bad",
        "well", "work", "works", "working", "time", "times", "now", "today",
    }
)

#: Legal moves for an issue. Anything absent from a state's set raises Conflict.
ISSUE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    IssueStatus.OPEN.value: frozenset(
        {
            IssueStatus.IN_PROGRESS.value,
            IssueStatus.RESOLVED.value,
            IssueStatus.WONT_FIX.value,
        }
    ),
    IssueStatus.IN_PROGRESS.value: frozenset(
        {
            IssueStatus.RESOLVED.value,
            IssueStatus.WONT_FIX.value,
            IssueStatus.OPEN.value,
        }
    ),
    # A resolved issue reopens rather than being resolved twice.
    IssueStatus.RESOLVED.value: frozenset({IssueStatus.IN_PROGRESS.value}),
    IssueStatus.WONT_FIX.value: frozenset({IssueStatus.OPEN.value}),
}

#: Legal moves for a backlog item.
BACKLOG_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    BacklogStatus.BACKLOG.value: frozenset(
        {BacklogStatus.PLANNED.value, BacklogStatus.IN_PROGRESS.value}
    ),
    BacklogStatus.PLANNED.value: frozenset(
        {BacklogStatus.IN_PROGRESS.value, BacklogStatus.BACKLOG.value}
    ),
    BacklogStatus.IN_PROGRESS.value: frozenset(
        {BacklogStatus.DONE.value, BacklogStatus.PLANNED.value}
    ),
    # Shipped is shipped; a regression is new work, not an edit of the old row.
    BacklogStatus.DONE.value: frozenset(),
}

#: Severity a promoted issue inherits on the backlog.
PRIORITY_BY_SEVERITY: Final[dict[str, BacklogPriority]] = {
    IssueSeverity.CRITICAL.value: BacklogPriority.P0,
    IssueSeverity.HIGH.value: BacklogPriority.P1,
    IssueSeverity.MEDIUM.value: BacklogPriority.P2,
    IssueSeverity.LOW.value: BacklogPriority.P3,
}

FEEDBACK_SORTABLE: Final[dict[str, Any]] = {
    "submitted_at": FeedbackItem.submitted_at,
    "ts": FeedbackItem.submitted_at,
    "rating": FeedbackItem.rating,
    "sentiment": FeedbackItem.sentiment,
    "source": FeedbackItem.source,
    "agent_id": FeedbackItem.agent_id,
    "agent": FeedbackItem.agent_id,
    "theme": FeedbackItem.theme,
    "feedback_ref": FeedbackItem.feedback_ref,
    "id": FeedbackItem.feedback_ref,
    "trace_id": FeedbackItem.trace_id,
    "runId": FeedbackItem.trace_id,
    "created_at": FeedbackItem.created_at,
}

ISSUE_SORTABLE: Final[dict[str, Any]] = {
    "title": FeedbackIssue.title,
    "issue_ref": FeedbackIssue.issue_ref,
    "severity": FeedbackIssue.severity,
    "status": FeedbackIssue.status,
    "theme": FeedbackIssue.theme,
    "feedback_count": FeedbackIssue.feedback_count,
    "reports": FeedbackIssue.feedback_count,
    "assigned_team": FeedbackIssue.assigned_team,
    "opened_at": FeedbackIssue.opened_at,
    "resolved_at": FeedbackIssue.resolved_at,
    "sla_due_at": FeedbackIssue.sla_due_at,
}

BACKLOG_SORTABLE: Final[dict[str, Any]] = {
    "title": BacklogItem.title,
    "priority": BacklogItem.priority,
    "status": BacklogItem.status,
    "effort": BacklogItem.effort,
    "assigned_team": BacklogItem.assigned_team,
    "target_release": BacklogItem.target_release,
    "created_at": BacklogItem.created_at,
    "updated_at": BacklogItem.updated_at,
}

IMPROVEMENT_SORTABLE: Final[dict[str, Any]] = {
    "title": Improvement.title,
    "deployed_at": Improvement.deployed_at,
    "verified": Improvement.verified,
    "metric_name": Improvement.metric_name,
    "created_at": Improvement.created_at,
}

EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("feedback_ref", "Feedback ID"),
    ("agent_name", "Agent"),
    ("trace_id", "Run ID"),
    ("rating", "Rating"),
    ("sentiment", "Sentiment"),
    ("body", "Feedback"),
    ("source", "Source"),
    ("submitted_by", "User"),
    ("environment", "Environment"),
    ("theme", "Theme"),
    ("issue_ref", "Issue"),
    ("tags", "Tags"),
    ("submitted_at", "Submitted"),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _ref(prefix: str) -> str:
    """A short, collision-resistant public reference."""
    return f"{prefix}_{secrets.token_hex(4)}"


def _slug(text: str) -> str:
    """Stable url-safe id for a theme label."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "unlabelled"


def _percent(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _delta_percent(current: int, previous: int) -> float | None:
    """Period-over-period change. Undefined against a zero baseline."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _window(days: int) -> tuple[dt.datetime, dt.datetime]:
    """The current window, and where the comparable previous one started."""
    now = _now()
    start = now - dt.timedelta(days=days)
    return start, start - dt.timedelta(days=days)


# ---------------------------------------------------------------------------
# Settings documents
# ---------------------------------------------------------------------------


async def _workspace(session: AsyncSession, principal: Principal) -> Workspace:
    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace is None:
        raise NotFound("This workspace no longer exists.")
    return workspace


def _documents(workspace: Workspace) -> dict[str, Any]:
    settings = workspace.settings if isinstance(workspace.settings, Mapping) else {}
    namespace = settings.get(SETTINGS_NAMESPACE)
    return dict(namespace) if isinstance(namespace, Mapping) else {}


async def get_sla_rules(session: AsyncSession, principal: Principal) -> SlaRules:
    """The workspace's SLA rules, or the documented defaults if none were saved.

    A stored document that no longer validates — because the rules were tightened
    since it was written — falls back to the defaults rather than failing every
    read that depends on it.
    """
    stored = _documents(await _workspace(session, principal)).get("sla_rules")
    if isinstance(stored, Mapping):
        try:
            return SlaRules.model_validate(dict(stored))
        except ValueError:
            return SlaRules()
    return SlaRules()


async def save_sla_rules(
    session: AsyncSession,
    principal: Principal,
    rules: SlaRules,
    *,
    request: Request | None = None,
) -> SlaRules:
    """Replace the SLA rules. Validation happened in the schema; this persists it."""
    principal.require(Role.ADMIN)
    workspace = await _workspace(session, principal)
    documents = _documents(workspace)
    previous = documents.get("sla_rules")
    documents["sla_rules"] = rules.model_dump(mode="json")
    settings = dict(workspace.settings) if isinstance(workspace.settings, Mapping) else {}
    settings[SETTINGS_NAMESPACE] = documents
    # Reassign rather than mutate: SQLAlchemy tracks JSON columns by identity.
    workspace.settings = settings

    await audit.record(
        session,
        principal=principal,
        action="feedback.sla_rules_updated",
        entity_type=ENTITY_SETTINGS,
        entity_id=workspace.id,
        entity_label="SLA & Routing",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Triage SLA {rules.triage_sla_hours}h, auto-issue at "
            f"{rules.auto_issue_threshold} reports, routing to {rules.routing.value}"
        ),
        metadata={"previous": previous, "current": documents["sla_rules"]},
        request=request,
    )
    await session.flush()
    return rules


async def get_collection_settings(
    session: AsyncSession, principal: Principal
) -> CollectionSettings:
    """How feedback is collected, or the documented defaults if none were saved."""
    stored = _documents(await _workspace(session, principal)).get("collection")
    if isinstance(stored, Mapping):
        try:
            return CollectionSettings.model_validate(dict(stored))
        except ValueError:
            return CollectionSettings()
    return CollectionSettings()


async def save_collection_settings(
    session: AsyncSession,
    principal: Principal,
    payload: CollectionSettings,
    *,
    request: Request | None = None,
) -> CollectionSettings:
    """Replace the collection settings."""
    principal.require(Role.ADMIN)
    workspace = await _workspace(session, principal)
    documents = _documents(workspace)
    previous = documents.get("collection")
    documents["collection"] = payload.model_dump(mode="json")
    settings = dict(workspace.settings) if isinstance(workspace.settings, Mapping) else {}
    settings[SETTINGS_NAMESPACE] = documents
    workspace.settings = settings

    await audit.record(
        session,
        principal=principal,
        action="feedback.collection_settings_updated",
        entity_type=ENTITY_SETTINGS,
        entity_id=workspace.id,
        entity_label="Collection Settings",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Manual review sampling {payload.manual_review_sample_percent}%, "
            f"PII scrubbing {'on' if payload.pii_scrubbing else 'off'}"
        ),
        metadata={"previous": previous, "current": documents["collection"]},
        request=request,
    )
    await session.flush()
    return payload


def add_business_hours(
    start: dt.datetime, hours: int, rules: SlaRules
) -> dt.datetime:
    """Advance a clock that only runs during the working week.

    "Four business hours" is what the console promises, so the due date is
    computed rather than approximated: weekends are skipped and each weekday
    contributes only the configured window.
    """
    if not rules.business_hours_only:
        return start + dt.timedelta(hours=hours)

    cursor = _as_utc(start)
    remaining = float(hours)
    # One iteration per weekday consumed; the schema caps the SLA at 720 hours,
    # which is 90 working days at the shortest legal business day.
    for _ in range(400):
        if remaining <= 0:
            return cursor
        midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        if cursor.weekday() >= 5:  # Saturday, Sunday
            cursor = midnight + dt.timedelta(days=1, hours=rules.business_day_start_hour)
            continue
        opens = midnight + dt.timedelta(hours=rules.business_day_start_hour)
        closes = midnight + dt.timedelta(hours=rules.business_day_end_hour)
        if cursor < opens:
            cursor = opens
        if cursor >= closes:
            cursor = midnight + dt.timedelta(days=1, hours=rules.business_day_start_hour)
            continue
        available = (closes - cursor).total_seconds() / 3600
        if remaining <= available:
            return cursor + dt.timedelta(hours=remaining)
        remaining -= available
        cursor = midnight + dt.timedelta(days=1, hours=rules.business_day_start_hour)
    return cursor


# ---------------------------------------------------------------------------
# Lookups shared by the read paths
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(FeedbackItem).where(FeedbackItem.workspace_id == principal.workspace_id)


async def _agent_names(
    session: AsyncSession, principal: Principal, agent_ids: Iterable[str | None]
) -> dict[str, str]:
    wanted = {agent_id for agent_id in agent_ids if agent_id}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == principal.workspace_id, Agent.id.in_(wanted)
            )
        )
    ).all()
    return {agent_id: name for agent_id, name in rows}


async def _issue_labels(
    session: AsyncSession, principal: Principal, issue_ids: Iterable[str | None]
) -> dict[str, tuple[str, str]]:
    wanted = {issue_id for issue_id in issue_ids if issue_id}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(FeedbackIssue.id, FeedbackIssue.issue_ref, FeedbackIssue.title).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.id.in_(wanted),
            )
        )
    ).all()
    return {issue_id: (ref, title) for issue_id, ref, title in rows}


async def _user_names(
    session: AsyncSession, user_ids: Iterable[str | None]
) -> dict[str, str]:
    wanted = {user_id for user_id in user_ids if user_id}
    if not wanted:
        return {}
    rows = (
        await session.execute(select(User.id, User.full_name).where(User.id.in_(wanted)))
    ).all()
    return {user_id: full_name for user_id, full_name in rows}


def _metadata(item: FeedbackItem) -> dict[str, Any]:
    return dict(item.event_metadata) if isinstance(item.event_metadata, Mapping) else {}


def _feedback_read(
    item: FeedbackItem,
    *,
    agent_name: str | None = None,
    issue: tuple[str, str] | None = None,
) -> FeedbackRead:
    metadata = _metadata(item)
    tags = metadata.get("tags")
    return FeedbackRead(
        id=item.id,
        feedback_ref=item.feedback_ref,
        agent_id=item.agent_id,
        agent_name=agent_name,
        trace_id=item.trace_id,
        rating=item.rating,
        sentiment=Sentiment(item.sentiment),
        body=item.body,
        source=FeedbackSource(item.source),
        submitted_by=item.submitted_by,
        submitted_at=item.submitted_at,
        theme=item.theme,
        cluster_id=_slug(item.theme) if item.theme else None,
        issue_id=item.issue_id,
        issue_ref=issue[0] if issue else None,
        issue_title=issue[1] if issue else None,
        environment=metadata.get("environment"),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        scored_in_telemetry=bool(item.engine_feedback_score_id),
        created_at=item.created_at,
    )


async def _decorate_feedback(
    session: AsyncSession, principal: Principal, items: Sequence[FeedbackItem]
) -> list[FeedbackRead]:
    agents = await _agent_names(session, principal, (item.agent_id for item in items))
    issues = await _issue_labels(session, principal, (item.issue_id for item in items))
    return [
        _feedback_read(
            item,
            agent_name=agents.get(item.agent_id or ""),
            issue=issues.get(item.issue_id or ""),
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Feedback reads
# ---------------------------------------------------------------------------


def _feedback_query(
    principal: Principal,
    params: ListParams,
    *,
    sentiment: Sentiment | None,
    source: FeedbackSource | None,
    agent_id: str | None,
    rating: int | None,
    theme: str | None,
    issue_id: str | None,
    since: dt.datetime | None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt,
        params,
        [
            FeedbackItem.body,
            FeedbackItem.feedback_ref,
            FeedbackItem.submitted_by,
            FeedbackItem.source,
            FeedbackItem.theme,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            FeedbackItem.sentiment: sentiment.value if sentiment else None,
            FeedbackItem.source: source.value if source else None,
            FeedbackItem.agent_id: agent_id,
            FeedbackItem.rating: rating,
            FeedbackItem.theme: theme,
            FeedbackItem.issue_id: issue_id,
        },
    )
    if since is not None:
        stmt = stmt.where(FeedbackItem.submitted_at >= since)
    return apply_sort(
        stmt, params, FEEDBACK_SORTABLE, default=FeedbackItem.submitted_at, default_desc=True
    )


async def list_feedback(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    sentiment: Sentiment | None = None,
    source: FeedbackSource | None = None,
    agent_id: str | None = None,
    rating: int | None = None,
    theme: str | None = None,
    issue_id: str | None = None,
    window_days: int | None = None,
) -> tuple[list[FeedbackRead], int]:
    """One page of feedback, filtered the way the table is."""
    since = _now() - dt.timedelta(days=window_days) if window_days else None
    stmt = _feedback_query(
        principal,
        params,
        sentiment=sentiment,
        source=source,
        agent_id=agent_id,
        rating=rating,
        theme=theme,
        issue_id=issue_id,
        since=since,
    )
    rows, total = await paginate(session, stmt, params)
    return await _decorate_feedback(session, principal, rows), total


async def get_feedback(
    session: AsyncSession, principal: Principal, feedback_id: str
) -> FeedbackItem:
    """Load one item, or raise :class:`NotFound`.

    An item in another workspace raises the same 404 as one that never existed.
    """
    item = (
        await session.execute(
            _scoped(principal).where(
                (FeedbackItem.id == feedback_id) | (FeedbackItem.feedback_ref == feedback_id)
            )
        )
    ).scalars().first()
    if item is None:
        raise NotFound(f"Feedback '{feedback_id}' does not exist.")
    return item


async def read_feedback(
    session: AsyncSession, principal: Principal, feedback_id: str
) -> FeedbackRead:
    item = await get_feedback(session, principal, feedback_id)
    rows = await _decorate_feedback(session, principal, [item])
    return rows[0]


async def export_feedback_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    sentiment: Sentiment | None = None,
    source: FeedbackSource | None = None,
    agent_id: str | None = None,
    rating: int | None = None,
    theme: str | None = None,
    issue_id: str | None = None,
    window_days: int | None = None,
) -> list[dict[str, Any]]:
    """Every item matching the current filters, flattened for CSV."""
    principal.require(Role.VIEWER)
    since = _now() - dt.timedelta(days=window_days) if window_days else None
    stmt = _feedback_query(
        principal,
        params,
        sentiment=sentiment,
        source=source,
        agent_id=agent_id,
        rating=rating,
        theme=theme,
        issue_id=issue_id,
        since=since,
    ).limit(MAX_EXPORT_ROWS)
    items = (await session.execute(stmt)).scalars().all()
    return [
        row.model_dump(mode="json")
        for row in await _decorate_feedback(session, principal, items)
    ]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def mirror_score(
    session: AsyncSession, principal: Principal, item: FeedbackItem
) -> str | None:
    """Write the rating onto the trace as a telemetry feedback score.

    Returns the reference the score is addressable by, or None when there was
    nothing to mirror (no trace, or no rating). A telemetry outage degrades the
    mirror, not the capture: the durable record is ours, so the submission still
    succeeds and ``scored_in_telemetry`` reports what actually happened.
    """
    if not item.trace_id or item.rating is None:
        return None

    project_name = principal.engine_workspace
    if item.agent_id:
        agent_project = (
            await session.execute(
                select(Agent.engine_project_name).where(
                    Agent.workspace_id == principal.workspace_id,
                    Agent.id == item.agent_id,
                )
            )
        ).scalar_one_or_none()
        if agent_project:
            project_name = agent_project

    try:
        await get_engine_client().score_traces_batch(
            [
                {
                    "id": item.trace_id,
                    "project_name": project_name,
                    "name": TELEMETRY_SCORE_NAME,
                    "value": float(item.rating),
                    "category_name": item.sentiment,
                    "source": "sdk" if principal.kind == "api_key" else "ui",
                    "reason": (item.body or "")[:500] or None,
                }
            ]
        )
    except EngineError:
        return None
    return f"{item.trace_id}/{TELEMETRY_SCORE_NAME}"


async def submit(
    session: AsyncSession,
    principal: Principal,
    payload: FeedbackCreate,
    *,
    request: Request | None = None,
) -> FeedbackRead:
    """Capture one piece of feedback.

    This is both the console's Submit Feedback dialog and the SDK's endpoint, so
    it accepts an API key with the ingest scope as readily as a session. The
    rating is mirrored onto the trace it is about; the row is written either way.
    """
    principal.require(Role.MEMBER)
    principal.require_scope("ingest")

    submitted_at = _as_utc(payload.submitted_at) if payload.submitted_at else _now()
    if submitted_at > _now() + dt.timedelta(minutes=1):
        raise ValidationFailed("submitted_at cannot be in the future.")

    if payload.agent_id:
        agent = (
            await session.execute(
                select(Agent.id).where(
                    Agent.workspace_id == principal.workspace_id,
                    Agent.id == payload.agent_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise NotFound(f"Agent '{payload.agent_id}' does not exist.")

    reference = payload.feedback_ref or _ref("fb")
    existing = (
        await session.execute(_scoped(principal).where(FeedbackItem.feedback_ref == reference))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(
            f"Feedback '{reference}' was already submitted.",
            details={"feedback_id": existing.id},
        )

    metadata = dict(payload.metadata)
    if payload.environment:
        metadata["environment"] = payload.environment
    if payload.tags:
        metadata["tags"] = list(payload.tags)
    metadata["captured_via"] = principal.kind

    sentiment = payload.sentiment or sentiment_for(payload.rating)
    item = FeedbackItem(
        workspace_id=principal.workspace_id,
        feedback_ref=reference,
        agent_id=payload.agent_id or principal.api_key_agent_id,
        trace_id=payload.trace_id,
        rating=payload.rating,
        sentiment=sentiment.value,
        body=payload.body,
        source=payload.source.value,
        submitted_by=payload.submitted_by or principal.email or principal.display_name,
        submitted_at=submitted_at,
        event_metadata=metadata,
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:  # someone used the same ref between check and flush
        await session.rollback()
        raise Conflict(f"Feedback '{reference}' was already submitted.") from exc

    item.engine_feedback_score_id = await mirror_score(session, principal, item)

    await audit.record(
        session,
        principal=principal,
        action="feedback.submitted",
        entity_type=ENTITY_FEEDBACK,
        entity_id=item.id,
        entity_label=reference,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{sentiment.value} feedback from {payload.source.value}"
            + (f", rated {payload.rating}/5" if payload.rating is not None else "")
            + ("" if item.engine_feedback_score_id else "; not mirrored to telemetry")
        ),
        metadata={
            "rating": payload.rating,
            "sentiment": sentiment.value,
            "source": payload.source.value,
            "trace_id": payload.trace_id,
            "scored_in_telemetry": bool(item.engine_feedback_score_id),
        },
        request=request,
    )
    await session.flush()
    await session.refresh(item)
    rows = await _decorate_feedback(session, principal, [item])
    return rows[0]


async def triage_feedback(
    session: AsyncSession,
    principal: Principal,
    feedback_id: str,
    payload: FeedbackUpdate,
    *,
    request: Request | None = None,
) -> FeedbackRead:
    """Correct one item's triage fields.

    What the user said is never rewritten here — only the theme, the issue it
    belongs to, the sentiment a reviewer corrected, and its tags.
    """
    principal.require(Role.MEMBER)
    item = await get_feedback(session, principal, feedback_id)
    changes = payload.model_dump(mode="json", exclude_unset=True)

    if "issue_id" in changes and changes["issue_id"]:
        issue = await get_issue(session, principal, changes["issue_id"])
        changes["issue_id"] = issue.id

    previous_issue = item.issue_id
    if "tags" in changes:
        metadata = _metadata(item)
        metadata["tags"] = changes.pop("tags") or []
        item.event_metadata = metadata
    for field, value in changes.items():
        setattr(item, field, value)

    await session.flush()
    if previous_issue != item.issue_id:
        for issue_id in {previous_issue, item.issue_id} - {None}:
            await _recount_issue(session, principal, str(issue_id))

    await audit.record(
        session,
        principal=principal,
        action="feedback.triaged",
        entity_type=ENTITY_FEEDBACK,
        entity_id=item.id,
        entity_label=item.feedback_ref,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(payload.model_fields_set)),
        metadata={"fields": sorted(payload.model_fields_set)},
        request=request,
    )
    await session.flush()
    rows = await _decorate_feedback(session, principal, [item])
    return rows[0]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _tokenise(text: str | None) -> set[str]:
    """Normalise one comment into the token set clustering compares.

    Deliberately embedding-free: lowercase, strip everything that is not a
    letter or digit, drop stopwords and very short words, and shed a trailing
    plural so "invoices" and "invoice" are the same complaint.
    """
    if not text:
        return set()
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    tokens: set[str] = set()
    for raw in cleaned.split():
        word = raw
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) < 3 or word.isdigit() or word in STOPWORDS:
            continue
        tokens.add(word)
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


@dataclasses.dataclass
class _Cluster:
    """One group under construction: its leader's tokens and its members."""

    leader: set[str]
    members: list[FeedbackItem] = dataclasses.field(default_factory=list)
    counts: Counter = dataclasses.field(default_factory=Counter)

    def add(self, item: FeedbackItem, tokens: set[str]) -> None:
        self.members.append(item)
        self.counts.update(tokens)

    def label(self) -> str:
        top = [word for word, _ in self.counts.most_common(3)]
        if not top:
            return "Unlabelled feedback"
        return " ".join(word.capitalize() for word in sorted(top, key=self._rank))

    def _rank(self, word: str) -> tuple[int, str]:
        return (-self.counts[word], word)


def _severity_for(size: int, negative_share: float, threshold: int) -> IssueSeverity:
    """Grade a cluster: volume and how negative it is, both deterministic."""
    if size >= threshold * 4 and negative_share >= 0.8:
        return IssueSeverity.CRITICAL
    if size >= threshold * 2 and negative_share >= 0.6:
        return IssueSeverity.HIGH
    if size >= threshold:
        return IssueSeverity.MEDIUM
    return IssueSeverity.LOW


async def analyze(
    session: AsyncSession,
    principal: Principal,
    payload: AnalyzeRequest,
    *,
    request: Request | None = None,
) -> AnalyzeResult:
    """Cluster unthemed feedback and persist the theme each item landed in.

    The algorithm is greedy leader clustering over Jaccard overlap of normalised
    token sets: deterministic, explainable and free of a model dependency, which
    matters because the result is written to rows and counted by the funnel.
    Items are visited oldest first, so the earliest report of a problem becomes
    the cluster's leader and re-running the pass reproduces the same grouping.
    """
    principal.require(Role.MEMBER)
    rules = await get_sla_rules(session, principal)
    start, _ = _window(payload.window_days)

    stmt = _scoped(principal).where(FeedbackItem.submitted_at >= start)
    if not payload.recluster:
        stmt = stmt.where(FeedbackItem.theme.is_(None))
    if payload.negative_only:
        stmt = stmt.where(FeedbackItem.sentiment == Sentiment.NEGATIVE.value)
    if payload.agent_id:
        stmt = stmt.where(FeedbackItem.agent_id == payload.agent_id)
    stmt = stmt.order_by(FeedbackItem.submitted_at.asc(), FeedbackItem.id.asc()).limit(
        MAX_ANALYZE_ROWS
    )
    items = (await session.execute(stmt)).scalars().all()

    clusters: list[_Cluster] = []
    unclustered = 0
    for item in items:
        tokens = _tokenise(item.body)
        if not tokens:
            unclustered += 1
            continue
        best: _Cluster | None = None
        best_score = 0.0
        for cluster in clusters:
            score = _jaccard(tokens, cluster.leader)
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= payload.similarity:
            best.add(item, tokens)
        else:
            fresh = _Cluster(leader=tokens)
            fresh.add(item, tokens)
            clusters.append(fresh)

    kept = [cluster for cluster in clusters if len(cluster.members) >= payload.min_cluster_size]
    unclustered += sum(
        len(cluster.members)
        for cluster in clusters
        if len(cluster.members) < payload.min_cluster_size
    )

    agents = await _agent_names(
        session, principal, (item.agent_id for cluster in kept for item in cluster.members)
    )

    reads: list[ClusterRead] = []
    clustered = 0
    for cluster in kept:
        label = cluster.label()
        ratings = [item.rating for item in cluster.members if item.rating is not None]
        negatives = sum(
            1 for item in cluster.members if item.sentiment == Sentiment.NEGATIVE.value
        )
        size = len(cluster.members)
        share = negatives / size if size else 0.0
        for item in cluster.members:
            item.theme = label[:120]
            clustered += 1

        moments = [item.submitted_at for item in cluster.members]
        representatives = sorted(
            cluster.members,
            key=lambda member: (
                -_jaccard(_tokenise(member.body), cluster.leader),
                member.submitted_at,
            ),
        )[:3]
        reads.append(
            ClusterRead(
                cluster_id=_slug(label),
                theme=label[:120],
                size=size,
                keywords=[word for word, _ in cluster.counts.most_common(8)],
                negative_share=round(share, 3),
                avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
                agents=sorted(
                    {
                        agents.get(item.agent_id or "", "")
                        for item in cluster.members
                        if item.agent_id
                    }
                    - {""}
                ),
                sources=sorted({item.source for item in cluster.members}),
                first_seen_at=min(moments) if moments else None,
                last_seen_at=max(moments) if moments else None,
                suggested_issue_title=label[:200],
                suggested_severity=_severity_for(size, share, rules.auto_issue_threshold),
                meets_auto_issue_threshold=size >= rules.auto_issue_threshold,
                examples=[
                    FeedbackExample(
                        id=member.id,
                        feedback_ref=member.feedback_ref,
                        rating=member.rating,
                        sentiment=Sentiment(member.sentiment),
                        body=member.body,
                        agent_name=agents.get(member.agent_id or ""),
                        submitted_at=member.submitted_at,
                    )
                    for member in representatives
                ],
            )
        )

    reads.sort(key=lambda cluster: (-cluster.size, cluster.theme))
    ran_at = _now()
    await audit.record(
        session,
        principal=principal,
        action="feedback.analyzed",
        entity_type=ENTITY_FEEDBACK,
        entity_label=f"{len(items)} item(s)",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Clustered {clustered} of {len(items)} item(s) into {len(reads)} theme(s) "
            f"over {payload.window_days} day(s)"
        ),
        metadata={
            "analysed": len(items),
            "clustered": clustered,
            "clusters": len(reads),
            "similarity": payload.similarity,
            "window_days": payload.window_days,
        },
        request=request,
    )
    await session.flush()

    return AnalyzeResult(
        analysed=len(items),
        clustered=clustered,
        unclustered=unclustered,
        clusters=reads,
        window_days=payload.window_days,
        similarity=payload.similarity,
        ran_at=ran_at,
    )


async def list_themes(
    session: AsyncSession, principal: Principal, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[ThemeRead]:
    """Persisted themes with their size and how negative they read.

    Counted in SQL over the window, so this is the same number the funnel's
    auto-clustered stage reports.
    """
    start, _ = _window(window_days)
    negative = case((FeedbackItem.sentiment == Sentiment.NEGATIVE.value, 1), else_=0)
    rows = (
        await session.execute(
            select(
                FeedbackItem.theme,
                func.count(FeedbackItem.id),
                func.coalesce(func.sum(negative), 0),
                func.avg(FeedbackItem.rating),
                func.min(FeedbackItem.submitted_at),
                func.max(FeedbackItem.submitted_at),
            )
            .where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.theme.is_not(None),
                FeedbackItem.submitted_at >= start,
            )
            .group_by(FeedbackItem.theme)
            .order_by(func.count(FeedbackItem.id).desc())
            .limit(MAX_THEMES)
        )
    ).all()

    themes = [str(theme) for theme, *_ in rows]
    open_issues: dict[str, str] = {}
    if themes:
        issue_rows = (
            await session.execute(
                select(FeedbackIssue.theme, FeedbackIssue.id).where(
                    FeedbackIssue.workspace_id == principal.workspace_id,
                    FeedbackIssue.theme.in_(themes),
                    FeedbackIssue.status.in_(
                        [IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value]
                    ),
                )
            )
        ).all()
        open_issues = {str(theme): issue_id for theme, issue_id in issue_rows}

    return [
        ThemeRead(
            cluster_id=_slug(str(theme)),
            theme=str(theme),
            size=int(size),
            negative_share=round(int(negatives) / int(size), 3) if size else 0.0,
            avg_rating=round(float(avg_rating), 2) if avg_rating is not None else None,
            open_issue_id=open_issues.get(str(theme)),
            first_seen_at=first_seen,
            last_seen_at=last_seen,
        )
        for theme, size, negatives, avg_rating, first_seen, last_seen in rows
    ]


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


async def get_issue(
    session: AsyncSession, principal: Principal, issue_id: str
) -> FeedbackIssue:
    """Load one issue, or raise :class:`NotFound`."""
    issue = (
        await session.execute(
            select(FeedbackIssue).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                (FeedbackIssue.id == issue_id) | (FeedbackIssue.issue_ref == issue_id),
            )
        )
    ).scalars().first()
    if issue is None:
        raise NotFound(f"Issue '{issue_id}' does not exist.")
    return issue


async def _recount_issue(
    session: AsyncSession, principal: Principal, issue_id: str
) -> int:
    """Recompute the cached linked-feedback count from the rows themselves."""
    count = (
        await session.execute(
            select(func.count(FeedbackItem.id)).where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.issue_id == issue_id,
            )
        )
    ).scalar_one()
    issue = await session.get(FeedbackIssue, issue_id)
    if issue is not None and issue.workspace_id == principal.workspace_id:
        issue.feedback_count = int(count)
    return int(count)


async def _decorate_issues(
    session: AsyncSession,
    principal: Principal,
    issues: Sequence[FeedbackIssue],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[FeedbackIssueRead]:
    ids = [issue.id for issue in issues]
    reports: dict[str, int] = {}
    backlog: dict[str, str] = {}
    if ids:
        start, _ = _window(window_days)
        reports = {
            str(issue_id): int(count)
            for issue_id, count in (
                await session.execute(
                    select(FeedbackItem.issue_id, func.count(FeedbackItem.id))
                    .where(
                        FeedbackItem.workspace_id == principal.workspace_id,
                        FeedbackItem.issue_id.in_(ids),
                        FeedbackItem.submitted_at >= start,
                    )
                    .group_by(FeedbackItem.issue_id)
                )
            ).all()
        }
        backlog = {
            str(issue_id): item_id
            for issue_id, item_id in (
                await session.execute(
                    select(BacklogItem.issue_id, BacklogItem.id).where(
                        BacklogItem.workspace_id == principal.workspace_id,
                        BacklogItem.issue_id.in_(ids),
                    )
                )
            ).all()
        }

    agents = await _agent_names(session, principal, (issue.agent_id for issue in issues))
    users = await _user_names(session, (issue.assigned_to_user_id for issue in issues))
    now = _now()
    return [
        FeedbackIssueRead(
            id=issue.id,
            issue_ref=issue.issue_ref,
            title=issue.title,
            description=issue.description,
            theme=issue.theme,
            severity=IssueSeverity(issue.severity),
            status=IssueStatus(issue.status),
            agent_id=issue.agent_id,
            agent_name=agents.get(issue.agent_id or ""),
            feedback_count=issue.feedback_count,
            reports_30d=reports.get(issue.id, 0),
            assigned_team=issue.assigned_team,
            assigned_to_user_id=issue.assigned_to_user_id,
            assigned_to_name=users.get(issue.assigned_to_user_id or ""),
            opened_at=issue.opened_at,
            resolved_at=issue.resolved_at,
            sla_due_at=issue.sla_due_at,
            sla_met=issue.sla_met,
            overdue=bool(
                issue.sla_due_at is not None
                and issue.resolved_at is None
                and _as_utc(issue.sla_due_at) < now
                and issue.status
                in (IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value)
            ),
            backlog_item_id=backlog.get(issue.id),
            created_at=issue.created_at,
            updated_at=issue.updated_at,
        )
        for issue in issues
    ]


async def list_issues(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: IssueStatus | None = None,
    severity: IssueSeverity | None = None,
    agent_id: str | None = None,
    assigned_team: str | None = None,
    theme: str | None = None,
    overdue: bool | None = None,
) -> tuple[list[FeedbackIssueRead], int]:
    """One page of the Issues tab."""
    stmt = select(FeedbackIssue).where(
        FeedbackIssue.workspace_id == principal.workspace_id
    )
    stmt = apply_search(
        stmt,
        params,
        [
            FeedbackIssue.title,
            FeedbackIssue.description,
            FeedbackIssue.theme,
            FeedbackIssue.issue_ref,
            FeedbackIssue.assigned_team,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            FeedbackIssue.status: status.value if status else None,
            FeedbackIssue.severity: severity.value if severity else None,
            FeedbackIssue.agent_id: agent_id,
            FeedbackIssue.assigned_team: assigned_team,
            FeedbackIssue.theme: theme,
        },
    )
    if overdue is not None:
        now = _now()
        condition = (
            FeedbackIssue.sla_due_at.is_not(None)
            & (FeedbackIssue.sla_due_at < now)
            & FeedbackIssue.resolved_at.is_(None)
        )
        stmt = stmt.where(condition if overdue else ~condition)
    stmt = apply_sort(
        stmt, params, ISSUE_SORTABLE, default=FeedbackIssue.opened_at, default_desc=True
    )
    rows, total = await paginate(session, stmt, params)
    return await _decorate_issues(session, principal, rows), total


async def read_issue(
    session: AsyncSession, principal: Principal, issue_id: str
) -> FeedbackIssueRead:
    issue = await get_issue(session, principal, issue_id)
    rows = await _decorate_issues(session, principal, [issue])
    return rows[0]


async def _route_team(
    session: AsyncSession, principal: Principal, agent_id: str | None, rules: SlaRules
) -> str | None:
    """Which team an issue goes to, per the routing rule that is configured."""
    if rules.routing is IssueRouting.FIXED_TEAM:
        return rules.routing_team
    if not agent_id:
        return None
    return (
        await session.execute(
            select(Agent.team).where(
                Agent.workspace_id == principal.workspace_id, Agent.id == agent_id
            )
        )
    ).scalar_one_or_none()


async def create_issue(
    session: AsyncSession,
    principal: Principal,
    payload: FeedbackIssueCreate,
    *,
    request: Request | None = None,
) -> FeedbackIssueRead:
    """Open an issue from a cluster, a theme or a hand-picked set of feedback.

    Every matching item is linked to the new issue in the same transaction, the
    cached count is set from those rows, and the SLA due date is computed on the
    business-hours clock the rules configure — so the Issues tab is never a
    detached list of titles.
    """
    principal.require(Role.MEMBER)
    rules = await get_sla_rules(session, principal)

    theme = payload.theme
    if theme is None and payload.cluster_id:
        candidates = (
            await session.execute(
                select(FeedbackItem.theme)
                .where(
                    FeedbackItem.workspace_id == principal.workspace_id,
                    FeedbackItem.theme.is_not(None),
                )
                .distinct()
            )
        ).scalars().all()
        theme = next(
            (str(name) for name in candidates if _slug(str(name)) == payload.cluster_id),
            None,
        )
        if theme is None:
            raise NotFound(f"Cluster '{payload.cluster_id}' has no feedback behind it.")

    linked = _scoped(principal).where(FeedbackItem.issue_id.is_(None))
    conditions = []
    if theme is not None:
        conditions.append(FeedbackItem.theme == theme)
    if payload.feedback_ids:
        conditions.append(FeedbackItem.id.in_(payload.feedback_ids))
    if not conditions:
        raise ValidationFailed("Nothing identifies the feedback behind this issue.")
    combined = conditions[0]
    for extra in conditions[1:]:
        combined = combined | extra
    items = (await session.execute(linked.where(combined))).scalars().all()

    agent_id = payload.agent_id
    if agent_id is None:
        agent_ids = Counter(item.agent_id for item in items if item.agent_id)
        agent_id = agent_ids.most_common(1)[0][0] if agent_ids else None

    opened_at = _now()
    issue = FeedbackIssue(
        workspace_id=principal.workspace_id,
        issue_ref=_ref("iss"),
        title=payload.title,
        description=payload.description,
        theme=theme,
        severity=payload.severity.value,
        status=IssueStatus.OPEN.value,
        agent_id=agent_id,
        feedback_count=len(items),
        assigned_team=(
            payload.assigned_team
            or await _route_team(session, principal, agent_id, rules)
        ),
        assigned_to_user_id=payload.assigned_to_user_id,
        opened_at=opened_at,
        sla_due_at=add_business_hours(
            opened_at, rules.severity_sla.hours_for(payload.severity), rules
        ),
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(issue)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("That issue reference is already in use.") from exc

    for item in items:
        item.issue_id = issue.id
        if theme and not item.theme:
            item.theme = theme

    await audit.record(
        session,
        principal=principal,
        action="feedback.issue_created",
        entity_type=ENTITY_ISSUE,
        entity_id=issue.id,
        entity_label=issue.issue_ref,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{payload.severity.value} issue '{issue.title}' opened from "
            f"{len(items)} feedback item(s); due {issue.sla_due_at.isoformat()}"
        ),
        metadata={
            "theme": theme,
            "severity": payload.severity.value,
            "linked_feedback": len(items),
            "assigned_team": issue.assigned_team,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(issue)
    rows = await _decorate_issues(session, principal, [issue])
    return rows[0]


def _assert_issue_transition(issue: FeedbackIssue, target: IssueStatus) -> None:
    """Guard the state machine. Re-closing a closed issue is a conflict."""
    allowed = ISSUE_TRANSITIONS.get(issue.status, frozenset())
    if target.value == issue.status or target.value in allowed:
        return
    raise Conflict(
        f"A {issue.status.lower()} issue cannot move to {target.value.lower()}.",
        details={
            "status": issue.status,
            "attempted": target.value,
            "allowed": sorted(allowed),
        },
    )


async def update_issue(
    session: AsyncSession,
    principal: Principal,
    issue_id: str,
    payload: FeedbackIssueUpdate,
    *,
    request: Request | None = None,
) -> FeedbackIssueRead:
    """Assign, re-grade or move an issue.

    Status changes are checked against the state machine. Resolving one stamps
    the resolution time and freezes whether the SLA was met, which is what the
    SLA Met card counts; re-grading an open issue recomputes its due date from
    the new severity, and never touches an issue that is already closed.
    """
    principal.require(Role.MEMBER)
    issue = await get_issue(session, principal, issue_id)
    rules = await get_sla_rules(session, principal)
    changes = payload.model_dump(mode="json", exclude_unset=True)
    resolution_note = changes.pop("resolution_note", None)

    target = IssueStatus(changes["status"]) if "status" in changes else None
    if target is not None:
        _assert_issue_transition(issue, target)

    if "severity" in changes:
        issue.severity = changes["severity"]
        if issue.resolved_at is None:
            issue.sla_due_at = add_business_hours(
                _as_utc(issue.opened_at),
                rules.severity_sla.hours_for(IssueSeverity(issue.severity)),
                rules,
            )
    for field in ("title", "description", "assigned_team", "assigned_to_user_id"):
        if field in changes:
            setattr(issue, field, changes[field])

    if target is not None and target.value != issue.status:
        if target is IssueStatus.RESOLVED:
            now = _now()
            issue.resolved_at = now
            # Frozen at resolution: a later policy change cannot rewrite history.
            issue.sla_met = (
                True if issue.sla_due_at is None else now <= _as_utc(issue.sla_due_at)
            )
        elif target is IssueStatus.IN_PROGRESS and issue.status == IssueStatus.RESOLVED.value:
            issue.resolved_at = None
        issue.status = target.value
    if resolution_note:
        issue.description = (
            f"{issue.description}\n\n{resolution_note}" if issue.description else resolution_note
        )
    issue.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="feedback.issue_updated",
        entity_type=ENTITY_ISSUE,
        entity_id=issue.id,
        entity_label=issue.issue_ref,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(payload.model_fields_set)),
        metadata={
            "fields": sorted(payload.model_fields_set),
            "status": issue.status,
            "sla_met": issue.sla_met,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(issue)
    rows = await _decorate_issues(session, principal, [issue])
    return rows[0]


# ---------------------------------------------------------------------------
# Backlog and improvements
# ---------------------------------------------------------------------------


async def get_backlog_item(
    session: AsyncSession, principal: Principal, item_id: str
) -> BacklogItem:
    item = (
        await session.execute(
            select(BacklogItem).where(
                BacklogItem.workspace_id == principal.workspace_id,
                BacklogItem.id == item_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise NotFound(f"Backlog item '{item_id}' does not exist.")
    return item


async def _decorate_backlog(
    session: AsyncSession, principal: Principal, items: Sequence[BacklogItem]
) -> list[BacklogItemRead]:
    issue_ids = [item.issue_id for item in items if item.issue_id]
    refs: dict[str, str] = {}
    votes: dict[str, int] = {}
    if issue_ids:
        refs = {
            str(issue_id): str(ref)
            for issue_id, ref in (
                await session.execute(
                    select(FeedbackIssue.id, FeedbackIssue.issue_ref).where(
                        FeedbackIssue.workspace_id == principal.workspace_id,
                        FeedbackIssue.id.in_(issue_ids),
                    )
                )
            ).all()
        }
        votes = {
            str(issue_id): int(count)
            for issue_id, count in (
                await session.execute(
                    select(FeedbackItem.issue_id, func.count(FeedbackItem.id))
                    .where(
                        FeedbackItem.workspace_id == principal.workspace_id,
                        FeedbackItem.issue_id.in_(issue_ids),
                    )
                    .group_by(FeedbackItem.issue_id)
                )
            ).all()
        }

    improvements: dict[str, str] = {}
    if items:
        improvements = {
            str(backlog_id): improvement_id
            for backlog_id, improvement_id in (
                await session.execute(
                    select(Improvement.backlog_item_id, Improvement.id).where(
                        Improvement.workspace_id == principal.workspace_id,
                        Improvement.backlog_item_id.in_([item.id for item in items]),
                    )
                )
            ).all()
        }

    return [
        BacklogItemRead(
            id=item.id,
            issue_id=item.issue_id,
            issue_ref=refs.get(item.issue_id or ""),
            title=item.title,
            description=item.description,
            priority=BacklogPriority(item.priority),
            effort=item.effort,
            status=BacklogStatus(item.status),
            assigned_team=item.assigned_team,
            target_release=item.target_release,
            votes=votes.get(item.issue_id or "", 0),
            improvement_id=improvements.get(item.id),
            created_by_user_id=item.created_by_user_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        for item in items
    ]


async def list_backlog(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: BacklogStatus | None = None,
    priority: BacklogPriority | None = None,
    assigned_team: str | None = None,
    issue_id: str | None = None,
) -> tuple[list[BacklogItemRead], int]:
    """One page of the Improvement Backlog tab."""
    stmt = select(BacklogItem).where(BacklogItem.workspace_id == principal.workspace_id)
    stmt = apply_search(
        stmt,
        params,
        [BacklogItem.title, BacklogItem.description, BacklogItem.assigned_team],
    )
    stmt = apply_filters(
        stmt,
        {
            BacklogItem.status: status.value if status else None,
            BacklogItem.priority: priority.value if priority else None,
            BacklogItem.assigned_team: assigned_team,
            BacklogItem.issue_id: issue_id,
        },
    )
    stmt = apply_sort(
        stmt, params, BACKLOG_SORTABLE, default=BacklogItem.created_at, default_desc=True
    )
    rows, total = await paginate(session, stmt, params)
    return await _decorate_backlog(session, principal, rows), total


async def create_backlog_item(
    session: AsyncSession,
    principal: Principal,
    payload: BacklogItemCreate,
    *,
    request: Request | None = None,
) -> BacklogItemRead:
    """Add an improvement candidate, optionally straight from one feedback item."""
    principal.require(Role.MEMBER)

    issue_id = payload.issue_id
    if issue_id:
        issue_id = (await get_issue(session, principal, issue_id)).id
    if payload.feedback_id:
        item = await get_feedback(session, principal, payload.feedback_id)
        issue_id = issue_id or item.issue_id

    backlog_item = BacklogItem(
        workspace_id=principal.workspace_id,
        issue_id=issue_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority.value,
        effort=payload.effort,
        status=BacklogStatus.BACKLOG.value,
        assigned_team=payload.assigned_team,
        target_release=payload.target_release,
        created_by_user_id=principal.user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(backlog_item)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="feedback.backlog_item_created",
        entity_type=ENTITY_BACKLOG,
        entity_id=backlog_item.id,
        entity_label=backlog_item.title,
        source_screen=SOURCE_SCREEN,
        detail=f"{payload.priority.value} candidate added to the improvement backlog",
        metadata={"issue_id": issue_id, "priority": payload.priority.value},
        request=request,
    )
    await session.flush()
    await session.refresh(backlog_item)
    rows = await _decorate_backlog(session, principal, [backlog_item])
    return rows[0]


async def promote_issue(
    session: AsyncSession,
    principal: Principal,
    issue_id: str,
    payload: BacklogPromoteRequest,
    *,
    request: Request | None = None,
) -> BacklogItemRead:
    """Put an issue on the backlog.

    An issue gets one backlog item: asking twice returns a conflict naming the
    existing one rather than quietly forking the work.
    """
    principal.require(Role.MEMBER)
    issue = await get_issue(session, principal, issue_id)
    if issue.status == IssueStatus.WONT_FIX.value:
        raise PreconditionFailed(
            f"Issue {issue.issue_ref} is marked Wont Fix. Reopen it before planning work."
        )

    existing = (
        await session.execute(
            select(BacklogItem).where(
                BacklogItem.workspace_id == principal.workspace_id,
                BacklogItem.issue_id == issue.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(
            f"Issue {issue.issue_ref} is already on the backlog.",
            details={"backlog_item_id": existing.id},
        )

    priority = payload.priority or PRIORITY_BY_SEVERITY.get(
        issue.severity, BacklogPriority.P2
    )
    item = BacklogItem(
        workspace_id=principal.workspace_id,
        issue_id=issue.id,
        title=issue.title,
        description=issue.description,
        priority=priority.value,
        effort=payload.effort,
        status=BacklogStatus.BACKLOG.value,
        assigned_team=payload.assigned_team or issue.assigned_team,
        target_release=payload.target_release,
        created_by_user_id=principal.user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(item)
    if issue.status == IssueStatus.OPEN.value:
        issue.status = IssueStatus.IN_PROGRESS.value
        issue.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="feedback.issue_backlogged",
        entity_type=ENTITY_BACKLOG,
        entity_id=item.id,
        entity_label=item.title,
        source_screen=SOURCE_SCREEN,
        detail=f"Issue {issue.issue_ref} planned as {priority.value}",
        metadata={"issue_id": issue.id, "priority": priority.value},
        request=request,
    )
    await session.flush()
    await session.refresh(item)
    rows = await _decorate_backlog(session, principal, [item])
    return rows[0]


async def create_fix_task(
    session: AsyncSession,
    principal: Principal,
    item_id: str,
    payload: FixTaskCreate,
    *,
    request: Request | None = None,
) -> ImprovementRead:
    """Open the fix task that tracks a backlog item to a measured outcome.

    The task is an ``Improvement`` row created up front with the metric as it
    stands *now*, so when the fix ships the before/after is a measurement rather
    than a recollection. Creating it moves the backlog item into progress.
    """
    principal.require(Role.MEMBER)
    item = await get_backlog_item(session, principal, item_id)
    if item.status == BacklogStatus.DONE.value:
        raise PreconditionFailed(
            f"'{item.title}' has already shipped. Add a new backlog item for further work."
        )

    existing = (
        await session.execute(
            select(Improvement).where(
                Improvement.workspace_id == principal.workspace_id,
                Improvement.backlog_item_id == item.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(
            f"'{item.title}' already has a fix task.",
            details={"improvement_id": existing.id},
        )

    agent_id = payload.agent_id
    if agent_id is None and item.issue_id:
        agent_id = (
            await session.execute(
                select(FeedbackIssue.agent_id).where(
                    FeedbackIssue.workspace_id == principal.workspace_id,
                    FeedbackIssue.id == item.issue_id,
                )
            )
        ).scalar_one_or_none()

    improvement = Improvement(
        workspace_id=principal.workspace_id,
        title=item.title,
        description=payload.note or item.description,
        issue_id=item.issue_id,
        backlog_item_id=item.id,
        agent_id=agent_id,
        metric_name=payload.metric_name,
        before_metric=payload.before_metric,
        verified=False,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(improvement)

    if item.status in (BacklogStatus.BACKLOG.value, BacklogStatus.PLANNED.value):
        item.status = BacklogStatus.IN_PROGRESS.value
    if payload.assigned_team:
        item.assigned_team = payload.assigned_team
    if payload.target_release:
        item.target_release = payload.target_release
    item.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="feedback.fix_task_created",
        entity_type=ENTITY_IMPROVEMENT,
        entity_id=improvement.id,
        entity_label=improvement.title,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Fix task opened for '{item.title}'"
            + (
                f"; baseline {payload.metric_name}={payload.before_metric}"
                if payload.metric_name and payload.before_metric is not None
                else ""
            )
        ),
        metadata={
            "backlog_item_id": item.id,
            "issue_id": item.issue_id,
            "metric_name": payload.metric_name,
            "before_metric": payload.before_metric,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(improvement)
    rows = await _decorate_improvements(session, principal, [improvement])
    return rows[0]


def _assert_backlog_transition(item: BacklogItem, target: BacklogStatus) -> None:
    allowed = BACKLOG_TRANSITIONS.get(item.status, frozenset())
    if target.value == item.status or target.value in allowed:
        return
    if not allowed:
        raise Conflict(
            f"'{item.title}' is already {item.status.lower()} and cannot move again.",
            details={"status": item.status, "attempted": target.value},
        )
    raise Conflict(
        f"A {item.status.lower()} item cannot move to {target.value.lower()}.",
        details={
            "status": item.status,
            "attempted": target.value,
            "allowed": sorted(allowed),
        },
    )


async def update_backlog_item(
    session: AsyncSession,
    principal: Principal,
    item_id: str,
    payload: BacklogItemUpdate,
    *,
    request: Request | None = None,
) -> BacklogItemRead:
    """Move or re-plan a backlog item.

    Marking one Done is the deployment: it stamps the fix task with the moment
    it shipped and the measured after-value, marks the improvement verified only
    when both sides of the metric are present and it actually moved, and resolves
    the issue behind it — which is how the funnel's last stage gets counted.
    """
    principal.require(Role.MEMBER)
    item = await get_backlog_item(session, principal, item_id)
    changes = payload.model_dump(mode="json", exclude_unset=True)

    target = BacklogStatus(changes["status"]) if "status" in changes else None
    if target is not None:
        _assert_backlog_transition(item, target)

    for field in (
        "title",
        "description",
        "priority",
        "effort",
        "assigned_team",
        "target_release",
    ):
        if field in changes:
            setattr(item, field, changes[field])

    improvement: Improvement | None = None
    if target is BacklogStatus.DONE and item.status != BacklogStatus.DONE.value:
        improvement = (
            await session.execute(
                select(Improvement).where(
                    Improvement.workspace_id == principal.workspace_id,
                    Improvement.backlog_item_id == item.id,
                )
            )
        ).scalar_one_or_none()
        now = _now()
        if improvement is None:
            # Shipping without a fix task still has to produce the record the
            # Improvements Deployed card counts.
            improvement = Improvement(
                workspace_id=principal.workspace_id,
                title=item.title,
                description=item.description,
                issue_id=item.issue_id,
                backlog_item_id=item.id,
                created_by=principal.actor,
            )
            session.add(improvement)
        improvement.deployed_at = now
        improvement.deployment_id = payload.deployment_id
        if payload.after_metric is not None:
            improvement.after_metric = payload.after_metric
        if payload.impact_summary:
            improvement.impact_summary = payload.impact_summary
        improvement.verified = (
            improvement.before_metric is not None
            and improvement.after_metric is not None
            and improvement.after_metric > improvement.before_metric
        )
        improvement.updated_by = principal.actor

        if item.issue_id:
            issue = await session.get(FeedbackIssue, item.issue_id)
            if (
                issue is not None
                and issue.workspace_id == principal.workspace_id
                and issue.status
                in (IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value)
            ):
                issue.status = IssueStatus.RESOLVED.value
                issue.resolved_at = now
                issue.sla_met = (
                    True if issue.sla_due_at is None else now <= _as_utc(issue.sla_due_at)
                )
                issue.updated_by = principal.actor

    if target is not None:
        item.status = target.value
    item.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action=(
            "feedback.improvement_deployed"
            if target is BacklogStatus.DONE
            else "feedback.backlog_item_updated"
        ),
        entity_type=ENTITY_BACKLOG,
        entity_id=item.id,
        entity_label=item.title,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Deployed '{item.title}'"
            if target is BacklogStatus.DONE
            else "Updated " + ", ".join(sorted(payload.model_fields_set))
        ),
        metadata={
            "fields": sorted(payload.model_fields_set),
            "status": item.status,
            "improvement_id": improvement.id if improvement is not None else None,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(item)
    rows = await _decorate_backlog(session, principal, [item])
    return rows[0]


async def _decorate_improvements(
    session: AsyncSession, principal: Principal, rows: Sequence[Improvement]
) -> list[ImprovementRead]:
    agents = await _agent_names(session, principal, (row.agent_id for row in rows))
    return [
        ImprovementRead(
            id=row.id,
            title=row.title,
            description=row.description,
            issue_id=row.issue_id,
            backlog_item_id=row.backlog_item_id,
            agent_id=row.agent_id,
            agent_name=agents.get(row.agent_id or ""),
            deployed_at=row.deployed_at,
            deployment_id=row.deployment_id,
            impact_summary=row.impact_summary,
            before_metric=row.before_metric,
            after_metric=row.after_metric,
            delta=row.delta,
            metric_name=row.metric_name,
            verified=row.verified,
            status="Deployed" if row.deployed_at else "In Progress",
            created_at=row.created_at,
        )
        for row in rows
    ]


async def list_improvements(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    verified: bool | None = None,
    agent_id: str | None = None,
    deployed: bool | None = None,
) -> tuple[list[ImprovementRead], int]:
    """One page of shipped and in-flight fixes, newest deployment first."""
    stmt = select(Improvement).where(Improvement.workspace_id == principal.workspace_id)
    stmt = apply_search(
        stmt, params, [Improvement.title, Improvement.description, Improvement.impact_summary]
    )
    stmt = apply_filters(
        stmt, {Improvement.verified: verified, Improvement.agent_id: agent_id}
    )
    if deployed is not None:
        stmt = stmt.where(
            Improvement.deployed_at.is_not(None)
            if deployed
            else Improvement.deployed_at.is_(None)
        )
    stmt = apply_sort(
        stmt, params, IMPROVEMENT_SORTABLE, default=Improvement.created_at, default_desc=True
    )
    rows, total = await paginate(session, stmt, params)
    return await _decorate_improvements(session, principal, rows), total


# ---------------------------------------------------------------------------
# Funnel, KPI cards and insights
# ---------------------------------------------------------------------------


async def _funnel_counts(
    session: AsyncSession, principal: Principal, start: dt.datetime
) -> list[int]:
    """The five funnel stages, one filtered aggregate each."""
    received = (
        await session.execute(
            select(func.count(FeedbackItem.id)).where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= start,
            )
        )
    ).scalar_one()
    clustered = (
        await session.execute(
            select(func.count(FeedbackItem.id)).where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= start,
                FeedbackItem.theme.is_not(None),
            )
        )
    ).scalar_one()
    issues = (
        await session.execute(
            select(func.count(FeedbackIssue.id)).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.opened_at >= start,
            )
        )
    ).scalar_one()
    backlog = (
        await session.execute(
            select(func.count(BacklogItem.id)).where(
                BacklogItem.workspace_id == principal.workspace_id,
                BacklogItem.created_at >= start,
            )
        )
    ).scalar_one()
    deployed = (
        await session.execute(
            select(func.count(Improvement.id)).where(
                Improvement.workspace_id == principal.workspace_id,
                Improvement.deployed_at.is_not(None),
                Improvement.deployed_at >= start,
            )
        )
    ).scalar_one()
    return [int(received), int(clustered), int(issues), int(backlog), int(deployed)]


def _funnel_stages(counts: Sequence[int]) -> list[FunnelStage]:
    received = counts[0]
    stages: list[FunnelStage] = []
    for index, name in enumerate(FunnelStageName):
        count = counts[index]
        previous = counts[index - 1] if index else None
        stages.append(
            FunnelStage(
                stage=name,
                count=count,
                percent_of_received=_percent(count, received),
                conversion_from_previous=(
                    _percent(count, previous) if previous is not None else None
                ),
            )
        )
    return stages


async def funnel(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> FeedbackFunnel:
    """Feedback received, clustered, opened, planned and shipped, for one window."""
    start, _ = _window(window_days)
    counts = await _funnel_counts(session, principal, start)
    return FeedbackFunnel(window_days=window_days, stages=_funnel_stages(counts))


async def _feedback_window_totals(
    session: AsyncSession,
    principal: Principal,
    start: dt.datetime,
    end: dt.datetime | None,
) -> tuple[int, float | None, int, int, int, int]:
    """Volume, average rating and the sentiment split for one window."""
    conditions = [
        FeedbackItem.workspace_id == principal.workspace_id,
        FeedbackItem.submitted_at >= start,
    ]
    if end is not None:
        conditions.append(FeedbackItem.submitted_at < end)

    def _count(sentiment: Sentiment) -> Any:
        return func.coalesce(
            func.sum(case((FeedbackItem.sentiment == sentiment.value, 1), else_=0)), 0
        )

    row = (
        await session.execute(
            select(
                func.count(FeedbackItem.id),
                func.avg(FeedbackItem.rating),
                func.coalesce(
                    func.sum(case((FeedbackItem.rating.is_not(None), 1), else_=0)), 0
                ),
                _count(Sentiment.POSITIVE),
                _count(Sentiment.NEUTRAL),
                _count(Sentiment.NEGATIVE),
            ).where(*conditions)
        )
    ).one()
    total, avg_rating, rated, positive, neutral, negative = row
    return (
        int(total or 0),
        round(float(avg_rating), 2) if avg_rating is not None else None,
        int(rated or 0),
        int(positive or 0),
        int(neutral or 0),
        int(negative or 0),
    )


async def summarise(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> FeedbackSummary:
    """Every KPI the screen shows, computed over the window and the one before it.

    Nothing here is stored: volumes, averages, the sentiment donut, the source
    bars and the funnel are all filtered SQL aggregates, and the deltas are the
    same aggregates over the preceding window of equal length.
    """
    start, previous_start = _window(window_days)

    total, avg_rating, rated, positive, neutral, negative = await _feedback_window_totals(
        session, principal, start, None
    )
    (
        prev_total,
        prev_avg,
        _prev_rated,
        prev_positive,
        _prev_neutral,
        _prev_negative,
    ) = await _feedback_window_totals(session, principal, previous_start, start)

    sources = {
        str(source): int(count)
        for source, count in (
            await session.execute(
                select(FeedbackItem.source, func.count(FeedbackItem.id))
                .where(
                    FeedbackItem.workspace_id == principal.workspace_id,
                    FeedbackItem.submitted_at >= start,
                )
                .group_by(FeedbackItem.source)
            )
        ).all()
    }

    clustered = (
        await session.execute(
            select(func.count(FeedbackItem.id)).where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= start,
                FeedbackItem.theme.is_not(None),
            )
        )
    ).scalar_one()

    issue_rows = (
        await session.execute(
            select(FeedbackIssue.status, func.count(FeedbackIssue.id))
            .where(FeedbackIssue.workspace_id == principal.workspace_id)
            .group_by(FeedbackIssue.status)
        )
    ).all()
    issues_by_status = {str(status): int(count) for status, count in issue_rows}

    issues_opened = (
        await session.execute(
            select(func.count(FeedbackIssue.id)).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.opened_at >= start,
            )
        )
    ).scalar_one()
    prev_issues_opened = (
        await session.execute(
            select(func.count(FeedbackIssue.id)).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.opened_at >= previous_start,
                FeedbackIssue.opened_at < start,
            )
        )
    ).scalar_one()

    now = _now()
    overdue = (
        await session.execute(
            select(func.count(FeedbackIssue.id)).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.sla_due_at.is_not(None),
                FeedbackIssue.sla_due_at < now,
                FeedbackIssue.resolved_at.is_(None),
                FeedbackIssue.status.in_(
                    [IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value]
                ),
            )
        )
    ).scalar_one()

    sla_rows = (
        await session.execute(
            select(
                func.count(FeedbackIssue.id),
                func.coalesce(
                    func.sum(case((FeedbackIssue.sla_met.is_(True), 1), else_=0)), 0
                ),
            ).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.sla_met.is_not(None),
                FeedbackIssue.resolved_at >= start,
            )
        )
    ).one()
    prev_sla_rows = (
        await session.execute(
            select(
                func.count(FeedbackIssue.id),
                func.coalesce(
                    func.sum(case((FeedbackIssue.sla_met.is_(True), 1), else_=0)), 0
                ),
            ).where(
                FeedbackIssue.workspace_id == principal.workspace_id,
                FeedbackIssue.sla_met.is_not(None),
                FeedbackIssue.resolved_at >= previous_start,
                FeedbackIssue.resolved_at < start,
            )
        )
    ).one()
    sla_met = _percent(int(sla_rows[1]), int(sla_rows[0])) if int(sla_rows[0]) else None
    prev_sla_met = (
        _percent(int(prev_sla_rows[1]), int(prev_sla_rows[0]))
        if int(prev_sla_rows[0])
        else None
    )

    backlog_rows = (
        await session.execute(
            select(BacklogItem.status, func.count(BacklogItem.id))
            .where(BacklogItem.workspace_id == principal.workspace_id)
            .group_by(BacklogItem.status)
        )
    ).all()
    backlog_by_status = {str(status): int(count) for status, count in backlog_rows}

    counts = await _funnel_counts(session, principal, start)
    deployed = counts[4]
    prev_deployed = (
        await session.execute(
            select(func.count(Improvement.id)).where(
                Improvement.workspace_id == principal.workspace_id,
                Improvement.deployed_at.is_not(None),
                Improvement.deployed_at >= previous_start,
                Improvement.deployed_at < start,
            )
        )
    ).scalar_one()

    positive_percent = _percent(positive, total)
    prev_positive_percent = _percent(prev_positive, prev_total)
    return FeedbackSummary(
        window_days=window_days,
        total_feedback=total,
        avg_rating=avg_rating,
        positive_percent=positive_percent,
        issues_identified=int(issues_opened),
        improvements_deployed=deployed,
        sla_met_percent=sla_met,
        total_feedback_delta_percent=_delta_percent(total, prev_total),
        avg_rating_delta=(
            round(avg_rating - prev_avg, 2)
            if avg_rating is not None and prev_avg is not None
            else None
        ),
        positive_percent_delta_pp=(
            round(positive_percent - prev_positive_percent, 1) if prev_total else None
        ),
        issues_identified_delta_percent=_delta_percent(
            int(issues_opened), int(prev_issues_opened)
        ),
        improvements_deployed_delta_percent=_delta_percent(deployed, int(prev_deployed)),
        sla_met_delta_pp=(
            round(sla_met - prev_sla_met, 1)
            if sla_met is not None and prev_sla_met is not None
            else None
        ),
        negative_percent=_percent(negative, total),
        neutral_percent=_percent(neutral, total),
        rated_feedback=rated,
        clustered_percent=_percent(int(clustered), total),
        open_issues=issues_by_status.get(IssueStatus.OPEN.value, 0)
        + issues_by_status.get(IssueStatus.IN_PROGRESS.value, 0),
        overdue_issues=int(overdue),
        backlog_open=backlog_by_status.get(BacklogStatus.BACKLOG.value, 0)
        + backlog_by_status.get(BacklogStatus.PLANNED.value, 0),
        backlog_in_progress=backlog_by_status.get(BacklogStatus.IN_PROGRESS.value, 0),
        resolved_issues=issues_by_status.get(IssueStatus.RESOLVED.value, 0),
        auto_issue_threshold=(await get_sla_rules(session, principal)).auto_issue_threshold,
        sentiment_breakdown=[
            FeedbackCountSlice(
                label=sentiment.value, count=count, percent=_percent(count, total)
            )
            for sentiment, count in (
                (Sentiment.POSITIVE, positive),
                (Sentiment.NEUTRAL, neutral),
                (Sentiment.NEGATIVE, negative),
            )
        ],
        source_breakdown=[
            FeedbackCountSlice(
                label=source.value,
                count=sources.get(source.value, 0),
                percent=_percent(sources.get(source.value, 0), total),
            )
            for source in FeedbackSource
        ],
        funnel=_funnel_stages(counts),
    )


async def insights(
    session: AsyncSession,
    principal: Principal,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> FeedbackInsights:
    """The Quality Insights tab: the daily trend, the agent ranking and the themes.

    The per-agent ranking is a SQL aggregate. The daily series is bucketed here
    from the window's rows because no date-truncation expression is portable
    across the two databases this service runs on; only three columns are read
    and the read is capped.
    """
    start, _ = _window(window_days)

    rows = (
        await session.execute(
            select(FeedbackItem.submitted_at, FeedbackItem.rating, FeedbackItem.sentiment)
            .where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= start,
            )
            .order_by(FeedbackItem.submitted_at.asc())
            .limit(MAX_INSIGHT_ROWS)
        )
    ).all()

    buckets: dict[dt.date, dict[str, Any]] = {}
    for submitted_at, rating, sentiment in rows:
        day = _as_utc(submitted_at).date()
        bucket = buckets.setdefault(
            day,
            {"count": 0, "ratings": [], "positive": 0, "neutral": 0, "negative": 0},
        )
        bucket["count"] += 1
        if rating is not None:
            bucket["ratings"].append(rating)
        if sentiment == Sentiment.POSITIVE.value:
            bucket["positive"] += 1
        elif sentiment == Sentiment.NEUTRAL.value:
            bucket["neutral"] += 1
        else:
            bucket["negative"] += 1

    daily = [
        FeedbackTrendPoint(
            date=day,
            count=bucket["count"],
            avg_rating=(
                round(sum(bucket["ratings"]) / len(bucket["ratings"]), 2)
                if bucket["ratings"]
                else None
            ),
            positive=bucket["positive"],
            neutral=bucket["neutral"],
            negative=bucket["negative"],
            positive_percent=_percent(bucket["positive"], bucket["count"]),
            negative_percent=_percent(bucket["negative"], bucket["count"]),
        )
        for day, bucket in sorted(buckets.items())
    ]

    agent_rows = (
        await session.execute(
            select(
                FeedbackItem.agent_id,
                func.avg(FeedbackItem.rating),
                func.count(FeedbackItem.id),
                func.coalesce(
                    func.sum(
                        case(
                            (FeedbackItem.sentiment == Sentiment.NEGATIVE.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= start,
                FeedbackItem.agent_id.is_not(None),
            )
            .group_by(FeedbackItem.agent_id)
            .order_by(func.count(FeedbackItem.id).desc())
            .limit(MAX_AGENT_BARS)
        )
    ).all()
    names = await _agent_names(session, principal, (agent_id for agent_id, *_ in agent_rows))

    return FeedbackInsights(
        window_days=window_days,
        daily=daily,
        rating_by_agent=[
            AgentRating(
                agent_id=agent_id,
                agent_name=names.get(str(agent_id), str(agent_id)),
                avg_rating=round(float(avg_rating), 2) if avg_rating is not None else None,
                feedback_count=int(count),
                negative_count=int(negatives),
            )
            for agent_id, avg_rating, count, negatives in agent_rows
        ],
        top_themes=await list_themes(session, principal, window_days=window_days),
    )
