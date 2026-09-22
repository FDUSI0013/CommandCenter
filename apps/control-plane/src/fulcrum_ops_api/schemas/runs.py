"""Wire contracts for Live Runs and Replay Studio.

A *run* is one agent execution. The platform does not store runs: they
live in the telemetry engine as traces, and this module is the vocabulary we
map them into — the sixteen columns of the Live Runs table, the inspector's
seven sections, the execution-trace span tree, and the ordered step list the
replay player scrubs through.

Two rules shape every schema here:

* nothing is invented. A field the trace does not carry comes back null (or an
  empty list), never a plausible-looking default, because these numbers are
  read as evidence during an incident review;
* the governance vocabulary (tenant, risk, policy) is ours, not the engine's.
  Those values ride on the trace's metadata, written by the ingest contract,
  and are resolved against the owning agent when the trace does not carry them.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from pydantic import BaseModel, Field

from ..models.registry import Platform, RiskLevel

#: How much of a prompt or completion travels in a table cell.
PREVIEW_CHARS: Final[int] = 240

#: How much of a span payload travels in the trace tree and replay steps.
SPAN_PREVIEW_CHARS: Final[int] = 2000

#: Feedback score written when an operator flags a run for review.
FLAG_SCORE_NAME: Final[str] = "flagged_for_review"


class RunStatus(enum.StrEnum):
    """Status chip on the run row. ``Running`` is a trace with no end time."""

    COMPLETED = "Completed"
    WARNED = "Warned"
    FAILED = "Failed"
    RUNNING = "Running"


class RunPolicy(enum.StrEnum):
    """Verdict the enforcement path recorded for the run."""

    ALLOWED = "Allowed"
    WARNED = "Warned"
    BLOCKED = "Blocked"


class TimeRange(enum.StrEnum):
    """The Time Range dropdown. Values are the labels the console renders."""

    LAST_HOUR = "Last hour"
    LAST_6_HOURS = "Last 6 hours"
    LAST_24_HOURS = "Last 24 hours"

    @property
    def seconds(self) -> int:
        return {
            TimeRange.LAST_HOUR: 3_600,
            TimeRange.LAST_6_HOURS: 6 * 3_600,
            TimeRange.LAST_24_HOURS: 24 * 3_600,
        }[self]


class ReplayStepKind(enum.StrEnum):
    """What one replay step did. Drives the player's per-step icon and panel."""

    PROMPT = "Prompt"
    GUARDRAIL = "Guardrail"
    RETRIEVAL = "Retrieval"
    MODEL = "Model"
    TOOL = "Tool"
    RESPONSE = "Response"
    STEP = "Step"


# ---------------------------------------------------------------------------
# Table row and inspector
# ---------------------------------------------------------------------------


class RunRead(BaseModel):
    """One row of the Live Runs table.

    The sixteen rendered columns are ``id`` (Run ID), ``source``, ``agent``,
    ``status``, ``model``, ``input_preview`` (Input Preview), ``tools``,
    ``tokens``, ``cost``, ``duration_seconds`` (Duration), ``confidence``,
    ``risk``, ``policy``, ``occurred_at`` (Time) and ``tenant``; ``agent_id``
    carries the link target behind the Agent cell.
    """

    id: str
    tenant: str | None = None
    source: Platform | None = Field(None, description="Platform the run executed on")
    agent: str | None = Field(None, description="Agent display name")
    agent_id: str | None = None
    status: RunStatus
    model: str | None = None
    input_preview: str = ""
    tools: list[str] = Field(
        default_factory=list, description="Tool names the run called, in call order"
    )
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_seconds: float | None = None
    confidence: float | None = Field(None, description="Model confidence score, 0-1")
    risk: RiskLevel | None = None
    policy: RunPolicy = RunPolicy.ALLOWED
    occurred_at: dt.datetime
    ended_at: dt.datetime | None = None
    session_id: str | None = None
    user: str | None = None


class RunHistoryPage(BaseModel):
    """One page of an agent's complete run history, newest first.

    Cursor-paged rather than offset-paged: the history has no time floor, so a
    total would mean scanning the agent's whole project on every page. The
    cursor is opaque -- pass ``next_cursor`` back exactly as it was received;
    absent means the history is exhausted.
    """

    agent_id: str
    agent_name: str
    items: list[RunRead]
    next_cursor: str | None = None


class RunGuardrails(BaseModel):
    """The inspector's Guardrails & Policy section."""

    prompt_injection_check: str | None = None
    pii_detection: str | None = None
    final_policy: RunPolicy = RunPolicy.ALLOWED
    verdicts: list[GuardrailVerdict] = Field(default_factory=list)


class RunRetrieval(BaseModel):
    """The inspector's Retrieval & Citations section."""

    documents: int = 0
    grounding_score: float | None = Field(None, description="0-1")
    citation_accuracy: float | None = Field(None, description="Percent, 0-100")
    source_freshness: float | None = Field(None, description="Percent, 0-100")


class RunErrors(BaseModel):
    """The inspector's Errors & Fallbacks section."""

    retry_count: int = 0
    fallback_used: bool = False
    escalated: bool = False
    message: str | None = Field(None, description="Error text recorded on the trace")


class RunDetail(RunRead):
    """Everything the run inspector shows, including the full response body."""

    input: str = ""
    response: str = ""
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="The run's own metadata, exactly as it was recorded"
    )
    environment: str | None = None
    guardrails: RunGuardrails = Field(default_factory=RunGuardrails)
    retrieval: RunRetrieval = Field(default_factory=RunRetrieval)
    errors: RunErrors = Field(default_factory=RunErrors)
    feedback_scores: list[RunScore] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    span_count: int = 0
    llm_span_count: int = 0
    flagged_for_review: bool = False
    trace_available: bool = True
    replay_supported: bool = Field(
        True,
        description=(
            "False only when there is nothing to step through: no spans, and no "
            "input, output or error on the run itself"
        ),
    )


class RunScore(BaseModel):
    """One feedback or evaluation score attached to the run."""

    name: str
    value: float
    reason: str | None = None
    source: str | None = None


class RunResponse(BaseModel):
    """The Full Response modal: the complete completion, untruncated."""

    run_id: str
    agent: str | None = None
    model: str | None = None
    occurred_at: dt.datetime
    response: str
    error: str | None = Field(
        None, description="Error recorded on the run; a failed run often has no response"
    )
    output_tokens: int = 0
    character_count: int = 0


# ---------------------------------------------------------------------------
# Execution trace
# ---------------------------------------------------------------------------


class GuardrailVerdict(BaseModel):
    """One guardrail decision recorded against a run or one of its spans."""

    name: str
    result: str = Field(description="Passed, Failed or the engine's own verdict label")
    passed: bool | None = None
    detail: str | None = None


class RunSpan(BaseModel):
    """One node of the execution trace tree."""

    id: str
    parent_span_id: str | None = None
    name: str
    span_type: str | None = Field(None, description="llm, tool, guardrail or general")
    status: str = Field("done", description="'done' or 'fail' — what the pipe dot renders")
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    duration_ms: float | None = None
    offset_ms: float | None = Field(None, description="Start offset from the run's start")
    model: str | None = None
    tokens: int = 0
    cost: float = 0.0
    input_preview: str | None = None
    output_preview: str | None = None
    error: str | None = None
    guardrails: list[GuardrailVerdict] = Field(default_factory=list)
    children: list[RunSpan] = Field(default_factory=list)


class RunTrace(BaseModel):
    """The Execution Trace modal: header facts plus the span tree.

    ``input``, ``response``, ``error`` and ``metadata`` are the run's own, read
    off the trace rather than its spans. A run reported without spans -- one
    decorated call, nothing nested -- has an empty ``spans`` and is still fully
    described by these four.
    """

    run_id: str
    agent: str | None = None
    agent_id: str | None = None
    model: str | None = None
    status: RunStatus
    started_at: dt.datetime
    duration_seconds: float | None = None
    span_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    input: str = Field("", description="The run's own input, in full")
    response: str = Field("", description="The run's own output, in full")
    error: str | None = Field(None, description="Error recorded on the run itself")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="The run's own metadata, exactly as it was recorded"
    )
    spans: list[RunSpan] = Field(default_factory=list, description="Root spans, in start order")


# ---------------------------------------------------------------------------
# Replay Studio
# ---------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """One document chunk a retrieval step returned."""

    id: str | None = None
    source: str | None = None
    score: float | None = None
    text: str = ""


class ToolCall(BaseModel):
    """The call and result of one tool step."""

    name: str
    arguments: str | None = None
    result: str | None = None
    ok: bool = True
    duration_ms: float | None = None


class TranscriptMessage(BaseModel):
    """One bubble of the replay Conversation panel."""

    role: str = Field(description="user, agent or tool")
    author: str | None = None
    at: dt.datetime | None = None
    text: str


class ReplayStep(BaseModel):
    """One scrubbable step of the replay player."""

    index: int
    kind: ReplayStepKind
    title: str
    detail: str = ""
    span_id: str | None = None
    started_at: dt.datetime | None = None
    offset_ms: float | None = Field(None, description="Start offset from the run's start")
    duration_ms: float | None = None
    prompt: str | None = None
    response: str | None = None
    model: str | None = None
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    retrieved_chunks: list[RetrievedChunk] = Field(default_factory=list)
    tool_call: ToolCall | None = None
    guardrails: list[GuardrailVerdict] = Field(default_factory=list)
    status: str = "done"
    error: str | None = None
    has_full_payload: bool = Field(
        False, description="True when both the step's input and output were captured"
    )


class ReplaySession(BaseModel):
    """What Replay Studio loads for one run.

    A run that recorded no spans still replays: its steps are then built from
    what the run itself recorded -- a Prompt step and a Response step, with
    ``span_id`` null -- and ``steps_from_trace`` says so. ``input``,
    ``response``, ``error`` and ``metadata`` are always the run's own.
    """

    run: RunRead
    input: str = Field("", description="The run's own input, in full")
    response: str = Field("", description="The run's own output, in full")
    error: str | None = Field(None, description="Error recorded on the run itself")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="The run's own metadata, exactly as it was recorded"
    )
    steps: list[ReplayStep] = Field(default_factory=list)
    transcript: list[TranscriptMessage] = Field(default_factory=list)
    total_duration_ms: float = 0.0
    step_count: int = 0
    captured_steps: int = Field(
        0, description="Steps whose prompt and output were both recorded"
    )
    fidelity: float = Field(
        0.0, description="Share of steps with full payloads, 0-100"
    )
    steps_from_trace: bool = Field(
        False,
        description="True when the run recorded no spans and the steps were built from the run",
    )
    replayable: bool = Field(True, description="False when there is nothing to step through")


# ---------------------------------------------------------------------------
# KPI cards and sparklines
# ---------------------------------------------------------------------------


class SparkPoint(BaseModel):
    """One bucket of a mini-KPI sparkline."""

    at: dt.datetime
    value: float


class SparkKpi(BaseModel):
    """One of the sparkline mini-KPIs under the KPI row."""

    label: str
    value: float | None = Field(
        description="Null when there was nothing to measure it over, such as a rate of no runs"
    )
    unit: str = Field(description="'tokens', 'usd', 'percent', 'runs' or 'violations'")
    series: list[SparkPoint] = Field(default_factory=list)


class ScanInfo(BaseModel):
    """How much telemetry the numbers above were computed from.

    Live Runs reads a bounded window of traces per request; this block makes
    that bound visible instead of silently reporting a partial total as if it
    were complete.
    """

    runs_scanned: int = 0
    agents_scanned: int = 0
    agents_total: int = 0
    truncated: bool = Field(
        False, description="True when the window held more runs than the scan cap"
    )
    window_start: dt.datetime
    window_end: dt.datetime
    covered_from: dt.datetime | None = Field(
        None,
        description=(
            "When the row cap was reached: the instant from which the scan holds "
            "every run. A capped project contributes its newest rows, so between "
            "`window_start` and here the window was read only in part -- totals "
            "are a floor and a sparkline bucket there is not a measurement. Null "
            "when nothing was capped, and when it cannot be said (more agents than "
            "one scan covers, or capped rows that carry no start time)"
        ),
    )


class ModelSlice(BaseModel):
    """One model's share of the window's runs: a slice of the LLMs Used panel.

    Folded in the same pass as ``total_runs``, so the slices of one summary
    always add up to it -- runs, and the tokens and cost beside them.
    """

    model: str | None = Field(
        None,
        description=(
            "The run's Model column: the model recorded on the run, else the "
            "agent's registered model. Null when neither names one"
        ),
    )
    runs: int
    tokens: int = 0
    cost: float = 0.0
    runs_model_from_agent: int = Field(
        0,
        description=(
            "Of `runs`, those that recorded no model and are counted under the model "
            "their agent is registered with. Always 0 for the null model"
        ),
    )


class ViolationCountScope(BaseModel):
    """What Policy Violations and Human Escalations were counted from.

    Both are counts of the violation records the Policy Center lists, not of
    runs: one record per breach, written by the enforcement path when it
    decided. They are read from those records directly -- not from the capped
    telemetry scan -- so they are complete whatever ``scan`` says, and their
    change against the previous window is always a measured one.
    """

    window_start: dt.datetime
    window_end: dt.datetime
    previous_window_start: dt.datetime = Field(
        description="The previous window runs from here up to `window_start`"
    )
    agent_id: str | None = Field(
        None,
        description=(
            "The one agent whose records were counted. Null: every record in the "
            "workspace, as the Policy Center counts them"
        ),
    )
    escalating_actions: list[str] = Field(
        default_factory=list,
        description="Enforcement actions Human Escalations counts, as Agent Detail does",
    )
    filters_not_applied: list[str] = Field(
        default_factory=list,
        description=(
            "Filters in the request that do not narrow these counts. A violation "
            "record names its agent, not the run's tenant, source, status, risk, "
            "verdict or text"
        ),
    )


class RunsSummary(BaseModel):
    """The KPI cards, the LLMs Used panel and the sparkline mini-KPIs on Live Runs.

    Every card is computed over the selected window and compared with the
    window immediately before it, so "vs last 24h" is a measured change.

    The run figures are folded from a capped telemetry scan. Both windows are
    read under the same cap, and each reports what it was computed from
    (``scan``, ``previous_scan``). When either was capped the two are not
    comparable: ``comparable`` is false and every run-based ``*_delta_*`` field
    is null, so the console draws no arrow rather than a trend that did not
    happen.

    Policy Violations and Human Escalations are not run figures. They count the
    violation records the Policy Center counts (see ``violations_scope``), one
    indexed count per request, so they are exact under any cap and their
    deltas are always measured.
    """

    time_range: TimeRange
    total_runs: int
    total_runs_previous: int = 0
    total_runs_delta_percent: float | None = None

    success_rate: float | None = Field(None, description="Percent, 0-100")
    success_rate_previous: float | None = None
    success_rate_delta_points: float | None = None

    avg_latency_seconds: float | None = None
    avg_latency_previous_seconds: float | None = None
    avg_latency_delta_seconds: float | None = None

    policy_violations: int = Field(
        0,
        description=(
            "Violation records in the window for the agents in view -- the records "
            "the Policy Center counts -- not runs with a non-Allowed verdict"
        ),
    )
    policy_violations_previous: int = 0
    policy_violations_delta_percent: float | None = Field(
        None, description="Null only when the previous window had none to compare with"
    )

    human_escalations_previous: int = 0
    human_escalations_delta_percent: float | None = None

    models: list[ModelSlice] = Field(
        default_factory=list,
        description=(
            "Runs in the window by model, most runs first and the unrecorded "
            "(null) model last. Folded with `total_runs`: the runs add up to it"
        ),
    )

    tokens_used: SparkKpi
    estimated_cost: SparkKpi
    fallback_rate: SparkKpi
    human_escalations: SparkKpi = Field(
        description=(
            "Violation records in the window whose action put a human in the loop "
            "(`violations_scope.escalating_actions`), bucketed like the others"
        )
    )
    agent_handoffs: SparkKpi = Field(
        description=(
            "Runs the agent itself reported as escalated to a person, from the "
            "run's own metadata. Folded from the scan, like the other run figures"
        )
    )

    tenants: list[str] = Field(
        default_factory=list, description="Options for the Tenant filter, from the window"
    )
    scan: ScanInfo
    previous_scan: ScanInfo | None = Field(
        None, description="What the run figures' `*_previous` values were computed from"
    )
    comparable: bool = Field(
        True,
        description=(
            "False when either window hit the scan cap. The run-based deltas are "
            "then null: a capped window is 'the most recent N runs', which cannot "
            "be subtracted. The violation and escalation deltas are unaffected"
        ),
    )
    violations_scope: ViolationCountScope


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class RunFlagRequest(BaseModel):
    """Flag a run for human review."""

    reason: str | None = Field(None, max_length=1000)
    comment: bool = Field(
        True, description="Also attach the reason to the run as a reviewer comment"
    )


class RunFlagResult(BaseModel):
    """What the flag wrote, so the console can confirm it."""

    run_id: str
    agent: str | None = None
    flagged_at: dt.datetime
    flagged_by: str
    score_name: str = FLAG_SCORE_NAME
    reason: str | None = None
    comment_added: bool = False


class RunStreamEvent(BaseModel):
    """One server-sent event on the Live Runs stream."""

    event: str = Field(description="'run' for a new run, 'open' for the handshake")
    run: RunRead | None = None
    emitted_at: dt.datetime
    stream_id: str


RunGuardrails.model_rebuild()
RunDetail.model_rebuild()
RunSpan.model_rebuild()


def as_metadata(value: Any) -> dict[str, Any]:
    """Normalise a trace's metadata field, which may be absent or non-object."""
    return value if isinstance(value, dict) else {}
