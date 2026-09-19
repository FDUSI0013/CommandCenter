"""Feedback & Quality Loop routes.

Twenty-four endpoints back one screen and its eight tabs: the KPI row and the
funnel, the feedback table with its filters and export, the clustering pass
behind Analyze Feedback, the issue and backlog workflows, the improvement
ledger, and the two settings documents the last tab edits.

``POST /feedback`` is the only endpoint in this domain an SDK calls, so it
accepts an API key with the ingest scope as readily as a browser session;
everything else is console traffic.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
the state machines, the SLA clock and the audit writes live in
``services.feedback``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.quality import (
    BacklogPriority,
    BacklogStatus,
    FeedbackSource,
    IssueSeverity,
    IssueStatus,
    Sentiment,
)
from ...schemas.feedback import (
    DEFAULT_WINDOW_DAYS,
    MAX_RATING,
    MAX_WINDOW_DAYS,
    MIN_RATING,
    AnalyzeRequest,
    AnalyzeResult,
    BacklogItemCreate,
    BacklogItemRead,
    BacklogItemUpdate,
    BacklogPromoteRequest,
    CollectionSettings,
    FeedbackCreate,
    FeedbackFunnel,
    FeedbackInsights,
    FeedbackIssueCreate,
    FeedbackIssueRead,
    FeedbackIssueUpdate,
    FeedbackRead,
    FeedbackSummary,
    FeedbackUpdate,
    FixTaskCreate,
    ImprovementRead,
    SlaRules,
    ThemeRead,
)
from ...services import feedback as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/feedback", tags=["Feedback & Quality"])

ListArgs = Annotated[ListParams, Depends(list_params)]
WindowArg = Annotated[
    int, Query(ge=1, le=MAX_WINDOW_DAYS, description="Rolling window in days")
]
SentimentFilter = Annotated[
    Sentiment | None, Query(description="Positive, Neutral or Negative")
]
SourceFilter = Annotated[
    FeedbackSource | None, Query(alias="source", description="Where the feedback came from")
]
AgentFilter = Annotated[str | None, Query(description="Agent the feedback is about")]
RatingFilter = Annotated[int | None, Query(ge=MIN_RATING, le=MAX_RATING)]
ThemeFilter = Annotated[str | None, Query(description="Exact cluster label")]
IssueFilter = Annotated[str | None, Query(description="Issue the feedback is linked to")]


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{feedback_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=FeedbackSummary, summary="Feedback KPI summary")
async def get_summary(
    principal: CurrentPrincipal, session: Db, window_days: WindowArg = DEFAULT_WINDOW_DAYS
) -> FeedbackSummary:
    """Every KPI card, its period-over-period delta, and the three breakdowns.

    The sentiment donut, the source bars and the funnel come back in the same
    payload, so the whole header renders from one request. Deltas compare the
    window with the equally long window before it.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get("/funnel", response_model=FeedbackFunnel, summary="Feedback outcome funnel")
async def get_funnel(
    principal: CurrentPrincipal, session: Db, window_days: WindowArg = DEFAULT_WINDOW_DAYS
) -> FeedbackFunnel:
    """Received, auto-clustered, issues opened, backlog, deployed.

    Each stage is a filtered count of real rows, with the conversion from the
    stage before it.
    """
    return await service.funnel(session, principal, window_days=window_days)


@router.get("/insights", response_model=FeedbackInsights, summary="Quality insights")
async def get_insights(
    principal: CurrentPrincipal, session: Db, window_days: WindowArg = DEFAULT_WINDOW_DAYS
) -> FeedbackInsights:
    """The daily trend and sentiment series, the agent ranking and the top themes."""
    return await service.insights(session, principal, window_days=window_days)


@router.get("/themes", response_model=list[ThemeRead], summary="List feedback themes")
async def list_themes(
    principal: CurrentPrincipal, session: Db, window_days: WindowArg = DEFAULT_WINDOW_DAYS
) -> list[ThemeRead]:
    """Persisted clusters with their size, sentiment mix and open issue, if any."""
    return await service.list_themes(session, principal, window_days=window_days)


@router.get("/export", summary="Export feedback as CSV")
async def export_feedback(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    sentiment: SentimentFilter = None,
    source: SourceFilter = None,
    agent_id: AgentFilter = None,
    rating: RatingFilter = None,
    theme: ThemeFilter = None,
    issue_id: IssueFilter = None,
    window_days: Annotated[int | None, Query(ge=1, le=MAX_WINDOW_DAYS)] = None,
) -> StreamingResponse:
    """Download the filtered feedback, comment, theme and linked issue included.

    Honours the same search, filters and sort as the list, so what downloads is
    what the reviewer is looking at.
    """
    rows = await service.export_feedback_rows(
        session,
        principal,
        params,
        sentiment=sentiment,
        source=source,
        agent_id=agent_id,
        rating=rating,
        theme=theme,
        issue_id=issue_id,
        window_days=window_days,
    )
    body = to_csv(rows, service.EXPORT_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="feedback-{stamp}.csv"'},
    )


@router.post("/analyze", response_model=AnalyzeResult, summary="Analyze feedback")
async def analyze_feedback(
    principal: CurrentPrincipal,
    session: Db,
    payload: AnalyzeRequest,
    request: Request,
) -> AnalyzeResult:
    """Cluster unthemed feedback and persist the theme each item landed in.

    Deterministic and embedding-free: the same window analysed twice produces
    the same clusters. Each cluster comes back with its size, keywords,
    representative examples, a suggested issue title and a suggested severity,
    which is exactly what `POST /feedback/issues` takes next.

    Themes earlier passes persisted take new reports too, and a theme with an
    open issue hands its unlinked reports to that issue (`open_issue_id`,
    `linked_to_issues`). A theme whose unanswered negative reports reach the SLA
    rules' `auto_issue_threshold` has its issue opened by the pass itself
    (`issue_auto_opened`).
    """
    return await service.analyze(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/sla-rules", response_model=SlaRules, summary="Get the SLA rules")
async def get_sla_rules(principal: CurrentPrincipal, session: Db) -> SlaRules:
    """The SLA & Routing rules.

    The business-hours clock, the severity windows, routing and the auto-issue
    threshold are acted on. The triage and escalation hours are the team's
    stated targets: recorded and audited, not swept for.
    """
    return await service.get_sla_rules(session, principal)


@router.put("/sla-rules", response_model=SlaRules, summary="Update the SLA rules")
async def put_sla_rules(
    principal: CurrentPrincipal, session: Db, payload: SlaRules, request: Request
) -> SlaRules:
    """Replace the SLA rules.

    The severity windows must widen as severity falls, and a fixed-team routing
    rule must name the team, so a saved rule set is always one the triage clock
    can actually run on. Requires the admin role.
    """
    return await service.save_sla_rules(session, principal, payload, request=request)


@router.get(
    "/settings", response_model=CollectionSettings, summary="Get collection settings"
)
async def get_collection_settings(
    principal: CurrentPrincipal, session: Db
) -> CollectionSettings:
    """How feedback is collected: prompts, thumbs, ticket ingestion, sampling, PII."""
    return await service.get_collection_settings(session, principal)


@router.put(
    "/settings", response_model=CollectionSettings, summary="Update collection settings"
)
async def put_collection_settings(
    principal: CurrentPrincipal,
    session: Db,
    payload: CollectionSettings,
    request: Request,
) -> CollectionSettings:
    """Replace the collection settings. Requires the admin role."""
    return await service.save_collection_settings(
        session, principal, payload, request=request
    )


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


@router.get("/issues", response_model=Page[FeedbackIssueRead], summary="List issues")
async def list_issues(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    issue_status: Annotated[
        IssueStatus | None, Query(alias="status", description="Open, In Progress, Resolved…")
    ] = None,
    severity: Annotated[IssueSeverity | None, Query()] = None,
    agent_id: AgentFilter = None,
    team: Annotated[str | None, Query(description="Assigned team")] = None,
    theme: ThemeFilter = None,
    overdue: Annotated[
        bool | None, Query(description="Only issues past their SLA, or only issues within it")
    ] = None,
) -> Page[FeedbackIssueRead]:
    """One page of the Issues tab.

    Each row carries its linked-feedback count for the KPI window, whether it is
    past its SLA, and the backlog item it was planned as.
    """
    rows, total = await service.list_issues(
        session,
        principal,
        params,
        status=issue_status,
        severity=severity,
        agent_id=agent_id,
        assigned_team=team,
        theme=theme,
        overdue=overdue,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "/issues",
    response_model=FeedbackIssueRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an issue",
)
async def create_issue(
    principal: CurrentPrincipal,
    session: Db,
    payload: FeedbackIssueCreate,
    request: Request,
) -> FeedbackIssueRead:
    """Open an issue from a cluster, a theme, or hand-picked feedback.

    Every matching unlinked item is attached to the new issue, the owning team
    is resolved from the routing rule, and the SLA due date is computed on the
    business-hours clock.

    A theme whose reports are all already behind an open issue — the clustering
    pass links them, and opens that issue itself at the auto-issue threshold —
    answers 409 with that issue in `details.issue_id` / `details.issue_ref`
    rather than opening a second issue with nothing in it.
    """
    return await service.create_issue(session, principal, payload, request=request)


@router.get(
    "/issues/{issue_id}", response_model=FeedbackIssueRead, summary="Get an issue"
)
async def get_issue(
    principal: CurrentPrincipal, session: Db, issue_id: str
) -> FeedbackIssueRead:
    """One issue, by id or by its public reference."""
    return await service.read_issue(session, principal, issue_id)


@router.patch(
    "/issues/{issue_id}", response_model=FeedbackIssueRead, summary="Update an issue"
)
async def update_issue(
    principal: CurrentPrincipal,
    session: Db,
    issue_id: str,
    payload: FeedbackIssueUpdate,
    request: Request,
) -> FeedbackIssueRead:
    """Assign the issue to a team, re-grade it, or move its status.

    Status moves are checked against the state machine and refused with 409 when
    illegal. Resolving freezes whether the SLA was met; re-grading an open issue
    recomputes its due date.
    """
    return await service.update_issue(session, principal, issue_id, payload, request=request)


@router.post(
    "/issues/{issue_id}/backlog",
    response_model=BacklogItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an issue to the backlog",
)
async def backlog_issue(
    principal: CurrentPrincipal,
    session: Db,
    issue_id: str,
    payload: BacklogPromoteRequest,
    request: Request,
) -> BacklogItemRead:
    """Plan an issue as improvement work.

    Priority is inherited from severity unless overridden, the issue moves to In
    Progress, and asking twice answers 409 with the existing backlog item rather
    than forking the work.
    """
    return await service.promote_issue(session, principal, issue_id, payload, request=request)


# ---------------------------------------------------------------------------
# Backlog and improvements
# ---------------------------------------------------------------------------


@router.get(
    "/backlog", response_model=Page[BacklogItemRead], summary="List the improvement backlog"
)
async def list_backlog(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    backlog_status: Annotated[
        BacklogStatus | None, Query(alias="status", description="Backlog, Planned…")
    ] = None,
    priority: Annotated[BacklogPriority | None, Query()] = None,
    team: Annotated[str | None, Query(description="Assigned team")] = None,
    issue_id: IssueFilter = None,
) -> Page[BacklogItemRead]:
    """One page of the Improvement Backlog tab.

    `votes` is the number of feedback items behind the item's issue — the demand
    signal the tab ranks on — counted rather than stored.
    """
    rows, total = await service.list_backlog(
        session,
        principal,
        params,
        status=backlog_status,
        priority=priority,
        assigned_team=team,
        issue_id=issue_id,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "/backlog",
    response_model=BacklogItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a backlog item",
)
async def create_backlog_item(
    principal: CurrentPrincipal,
    session: Db,
    payload: BacklogItemCreate,
    request: Request,
) -> BacklogItemRead:
    """Record an improvement candidate, with or without an issue behind it.

    This is the Add to Backlog action on a feedback row: pass `feedback_id` and
    the item inherits that feedback's issue when it has one. An issue is planned
    by one item at a time, so when that issue already has an item that has not
    shipped the answer is 409, with the existing item in `details.backlog_item_id`
    — the same answer `POST /feedback/issues/{id}/backlog` gives.
    """
    return await service.create_backlog_item(session, principal, payload, request=request)


@router.patch(
    "/backlog/{item_id}", response_model=BacklogItemRead, summary="Update a backlog item"
)
async def update_backlog_item(
    principal: CurrentPrincipal,
    session: Db,
    item_id: str,
    payload: BacklogItemUpdate,
    request: Request,
) -> BacklogItemRead:
    """Move or re-plan a backlog item.

    Moving one to Done is the deployment: it stamps the fix task with the moment
    it shipped and the measured after-value, verifies the improvement only when
    both sides of the metric are present and it moved, and resolves the issue
    behind it. Illegal moves are refused with 409.
    """
    return await service.update_backlog_item(
        session, principal, item_id, payload, request=request
    )


@router.post(
    "/backlog/{item_id}/fix-task",
    response_model=ImprovementRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a fix task",
)
async def create_fix_task(
    principal: CurrentPrincipal,
    session: Db,
    item_id: str,
    payload: FixTaskCreate,
    request: Request,
) -> ImprovementRead:
    """Open the fix task that tracks a backlog item to a measured outcome.

    The metric is captured as it stands now, so the before/after published when
    the fix ships is a measurement rather than a recollection. The backlog item
    moves to In Progress.
    """
    return await service.create_fix_task(session, principal, item_id, payload, request=request)


@router.get(
    "/improvements", response_model=Page[ImprovementRead], summary="List improvements"
)
async def list_improvements(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    verified: Annotated[bool | None, Query(description="Only confirmed gains")] = None,
    deployed: Annotated[bool | None, Query(description="Only shipped fixes")] = None,
    agent_id: AgentFilter = None,
) -> Page[ImprovementRead]:
    """Shipped and in-flight fixes, with the before/after that proves the loop closed."""
    rows, total = await service.list_improvements(
        session, principal, params, verified=verified, deployed=deployed, agent_id=agent_id
    )
    return Page.build(rows, total, params.page, params.page_size)


# ---------------------------------------------------------------------------
# Feedback collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[FeedbackRead], summary="List feedback")
async def list_feedback(
    principal: CurrentPrincipal,
    session: Db,
    params: ListArgs,
    sentiment: SentimentFilter = None,
    source: SourceFilter = None,
    agent_id: AgentFilter = None,
    rating: RatingFilter = None,
    theme: ThemeFilter = None,
    issue_id: IssueFilter = None,
    window_days: Annotated[int | None, Query(ge=1, le=MAX_WINDOW_DAYS)] = None,
) -> Page[FeedbackRead]:
    """One page of feedback, newest first.

    Free-text search covers the comment, the reference, the submitter, the source
    and the theme; the three dropdown filters the table offers are sentiment,
    agent and source.
    """
    rows, total = await service.list_feedback(
        session,
        principal,
        params,
        sentiment=sentiment,
        source=source,
        agent_id=agent_id,
        rating=rating,
        theme=theme,
        issue_id=issue_id,
        window_days=window_days,
    )
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=FeedbackRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit feedback",
)
async def submit_feedback(
    principal: CurrentPrincipal,
    session: Db,
    payload: FeedbackCreate,
    request: Request,
) -> FeedbackRead:
    """Capture one piece of feedback and mirror its rating onto the trace.

    This is the SDK's endpoint as well as the console's, so an API key with the
    ingest scope may call it. Send `feedback_ref` to make the submission
    idempotent — a repeat answers 409 naming the item that already exists. The
    row is written even if the telemetry mirror fails; `scored_in_telemetry`
    reports which happened, and is true only when the run belongs to an agent of
    this workspace and the score was filed under that run's own project.

    While the collection settings have PII scrubbing on, email addresses, phone
    numbers, SSNs and card numbers in the comment are replaced before it is
    stored or mirrored; `pii_scrubbed` on the row lists the kinds that were found.
    """
    return await service.submit(session, principal, payload, request=request)


@router.get("/{feedback_id}", response_model=FeedbackRead, summary="Get feedback")
async def get_feedback(
    principal: CurrentPrincipal, session: Db, feedback_id: str
) -> FeedbackRead:
    """One item, by id or by its public reference, as the inspector shows it."""
    return await service.read_feedback(session, principal, feedback_id)


@router.patch("/{feedback_id}", response_model=FeedbackRead, summary="Triage feedback")
async def triage_feedback(
    principal: CurrentPrincipal,
    session: Db,
    feedback_id: str,
    payload: FeedbackUpdate,
    request: Request,
) -> FeedbackRead:
    """Correct one item's triage fields: its theme, its issue, its sentiment, its tags.

    What the user said is never rewritten here. Linking an item to an issue
    recounts both the old and the new issue in the same transaction, which is
    how the Assign to Team action routes a report to the owning team.
    """
    return await service.triage_feedback(
        session, principal, feedback_id, payload, request=request
    )
