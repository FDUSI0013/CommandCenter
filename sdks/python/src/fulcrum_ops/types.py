"""The ingest wire contract, typed.

These ``TypedDict``s mirror ``POST /ingest/{traces,spans,scores,events}`` and
``GET /ingest/config`` field for field, in the snake_case the API speaks. They
are the boundary types: everything above them is ordinary Python, and the
conversion happens in :mod:`fulcrum_ops.trace` so a contract change lands in one
place.

Nothing in the SDK validates against these at runtime — the server is the
authority, and a client-side schema that drifts from it is worse than none. They
exist so a type checker can catch a misspelled key, and so a reader can see the
whole contract without opening the OpenAPI document.

``TypedDict`` and ``Literal`` moved into ``typing`` at 3.8, but ``total=False``
inheritance is only reliable from 3.9 — which is the floor this package
declares, so both are imported directly.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 8):  # pragma: no cover - always true at the declared floor
    from typing import Literal, TypedDict
else:  # pragma: no cover - defensive only
    from typing_extensions import Literal, TypedDict

__all__ = [
    "SpanType",
    "ScoreTarget",
    "EventKind",
    "ItemOutcome",
    "RejectionCode",
    "SPAN_TYPES",
    "SCORE_TARGETS",
    "EVENT_KINDS",
    "ErrorInfoIn",
    "FeedbackScoreIn",
    "SpanIn",
    "TraceIn",
    "ScoreIn",
    "EventIn",
    "TraceBatchIn",
    "SpanBatchIn",
    "ScoreBatchIn",
    "EventBatchIn",
    "IngestItemResult",
    "IngestBatchResult",
    "AutoRegisteredAgent",
    "IngestQuotaState",
    "GuardrailDescriptor",
    "RedactionRuleIn",
    "IngestConfig",
]

#: Span classes the telemetry engine models separately.
SpanType = Literal["general", "llm", "tool", "guardrail"]

#: What a feedback score is attached to.
ScoreTarget = Literal["trace", "span", "thread"]

#: Governance events an SDK may report from inside a customer's own process.
EventKind = Literal["guardrail.triggered", "policy.violation", "feedback.submitted"]

#: Per-item verdict returned for every row in a batch.
#:
#: ``blocked`` is deliberately distinct from ``rejected``: a rejected row was
#: unusable, a blocked row was well formed and refused by governance. The SDK
#: retries neither, but only the second is a compliance event.
ItemOutcome = Literal["accepted", "rejected", "blocked"]

#: Machine-readable reason an item did not reach the telemetry store.
RejectionCode = Literal[
    "malformed",
    "unknown_agent",
    "agent_not_permitted",
    "agent_unprovisioned",
    "missing_trace_id",
    "invalid_id",
    "policy_blocked",
    "guardrail_blocked",
    "telemetry_rejected",
    "unsupported_event",
    "unknown_guardrail",
    "unknown_policy",
    "duplicate",
]

#: The same values as tuples, for the runtime membership checks that cannot use
#: a ``Literal``.
SPAN_TYPES = ("general", "llm", "tool", "guardrail")
SCORE_TARGETS = ("trace", "span", "thread")
EVENT_KINDS = ("guardrail.triggered", "policy.violation", "feedback.submitted")


class ErrorInfoIn(TypedDict, total=False):
    """A failure captured by the SDK, carried verbatim onto the span."""

    exception_type: str
    message: Optional[str]
    traceback: Optional[str]


class FeedbackScoreIn(TypedDict, total=False):
    """One score attached to a trace, span or thread, folded into the item it scores."""

    name: str
    value: float
    category_name: Optional[str]
    reason: Optional[str]
    source: str


class SpanIn(TypedDict, total=False):
    """One unit of work inside a trace.

    ``id`` is optional on the wire: when it is omitted the server mints a
    time-ordered one. This SDK always supplies its own, which is what makes a
    retry after a network timeout idempotent rather than a duplicate row.
    """

    id: str
    trace_id: Optional[str]
    parent_span_id: Optional[str]
    name: str
    type: SpanType
    start_time: str
    end_time: Optional[str]
    input: Optional[Dict[str, Any]]
    output: Optional[Dict[str, Any]]
    usage: Optional[Dict[str, int]]
    model: Optional[str]
    provider: Optional[str]
    total_estimated_cost: Optional[float]
    error_info: Optional[ErrorInfoIn]
    feedback_scores: List[FeedbackScoreIn]
    metadata: Dict[str, Any]
    tags: List[str]
    agent: Optional[str]


class TraceIn(TypedDict, total=False):
    """One end-to-end agent invocation, with its spans nested inside it."""

    id: str
    name: str
    start_time: str
    end_time: Optional[str]
    input: Optional[Dict[str, Any]]
    output: Optional[Dict[str, Any]]
    thread_id: Optional[str]
    error_info: Optional[ErrorInfoIn]
    spans: List[SpanIn]
    feedback_scores: List[FeedbackScoreIn]
    metadata: Dict[str, Any]
    tags: List[str]
    agent: Optional[str]


class ScoreIn(TypedDict, total=False):
    """Body item of ``POST /ingest/scores``: a score plus what it scores."""

    name: str
    value: float
    category_name: Optional[str]
    reason: Optional[str]
    source: str
    target: ScoreTarget
    id: str
    agent: Optional[str]


class EventIn(TypedDict, total=False):
    """A governance event the SDK observed in the customer's own process.

    One model rather than three because the SDK sends a single stream. The
    per-kind required fields are enforced server-side, so a guardrail event
    without a guardrail is rejected as its own row rather than accepted and
    dropped.
    """

    kind: EventKind
    ref: Optional[str]
    occurred_at: Optional[str]
    agent: Optional[str]
    trace_id: Optional[str]
    span_id: Optional[str]
    # guardrail.triggered
    guardrail: Optional[str]
    action_taken: Optional[str]
    score: Optional[float]
    matched: Dict[str, Any]
    sample: Optional[str]
    # policy.violation
    policy: Optional[str]
    severity: Optional[str]
    detail: Dict[str, Any]
    # feedback.submitted
    rating: Optional[int]
    sentiment: Optional[str]
    body: Optional[str]
    source: Optional[str]
    submitted_by: Optional[str]


class _BatchEnvelope(TypedDict, total=False):
    """Every ingest body carries the same three envelope fields."""

    agent: Optional[str]
    sdk: Optional[str]
    sdk_version: Optional[str]


class TraceBatchIn(_BatchEnvelope, total=False):
    traces: List[TraceIn]


class SpanBatchIn(_BatchEnvelope, total=False):
    spans: List[SpanIn]


class ScoreBatchIn(_BatchEnvelope, total=False):
    scores: List[ScoreIn]


class EventBatchIn(_BatchEnvelope, total=False):
    events: List[EventIn]


class IngestItemResult(TypedDict, total=False):
    """What happened to one submitted row."""

    index: int
    id: Optional[str]
    outcome: ItemOutcome
    code: Optional[RejectionCode]
    reason: Optional[str]
    agent_id: Optional[str]
    spans: int
    policy_id: Optional[str]
    guardrail_id: Optional[str]
    masked: bool


class AutoRegisteredAgent(TypedDict, total=False):
    """An agent the ingest path created because telemetry arrived for it."""

    id: str
    name: str
    slug: str
    environment: str
    status: str
    engine_project_name: Optional[str]


class IngestQuotaState(TypedDict, total=False):
    """A quota this batch was measured against, after the batch was counted."""

    id: str
    name: str
    resource: str
    scope: str
    scope_ref: Optional[str]
    unit: str
    limit_value: float
    used_value: float
    remaining: float
    utilization_pct: float
    enforcement: str
    status: str
    resets_at: Optional[str]


class IngestBatchResult(TypedDict, total=False):
    """The answer to every ingest POST.

    Always 200 when the request itself was well formed: the batch's fate is in
    the counters and the per-item rows, not in the status code. A non-200 means
    the request could not be processed at all — too large, unauthorised, out of
    quota, or the telemetry store is down.
    """

    received: int
    accepted: int
    rejected: int
    blocked: int
    spans_accepted: int
    scores_accepted: int
    events_recorded: int
    violations_recorded: int
    guardrails_evaluated: bool
    agents: List[str]
    auto_registered: List[AutoRegisteredAgent]
    quotas: List[IngestQuotaState]
    results: List[IngestItemResult]
    duration_ms: int


class GuardrailDescriptor(TypedDict, total=False):
    """A guardrail the SDK should know about, as the config endpoint reports it."""

    id: str
    name: str
    type: str
    action: str
    threshold: float
    scope: str
    scope_ref: Optional[str]
    status: str


class RedactionRuleIn(TypedDict, total=False):
    """A rule the SDK applies locally, before content ever leaves the process."""

    id: str
    name: str
    #: ``'guardrail'`` or ``'workspace'``.
    source: str
    #: Named entity classes to remove, e.g. ``'email'``.
    entity_types: List[str]
    #: Regular expression matched against content.
    pattern: Optional[str]
    replacement: str
    #: Fields the rule covers: ``input``, ``output``, ``metadata``.
    applies_to: List[str]


class IngestConfig(TypedDict, total=False):
    """What an SDK fetches once at start-up and re-fetches when the ETag moves.

    Deliberately small and cacheable: it is the first call every agent process
    makes, and a fleet restart must not turn into a stampede on the database.
    """

    workspace: str
    environment: Optional[str]
    agent_id: Optional[str]
    agent_name: Optional[str]
    #: True when the API key may only report for one agent.
    agent_bound: bool
    sampling_rate: float
    batch_max_spans: int
    batch_max_bytes: int
    flush_interval_seconds: float
    max_queue_size: int
    retry_max_attempts: int
    retry_backoff_seconds: float
    capture_input: bool
    capture_output: bool
    endpoints: Dict[str, str]
    guardrails: List[GuardrailDescriptor]
    redaction: List[RedactionRuleIn]
    #: Opaque revision; changes when anything above changes.
    revision: str
    #: How long the SDK may cache this document.
    refresh_after_seconds: int
