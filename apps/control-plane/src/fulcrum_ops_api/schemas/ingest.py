"""Wire contracts for the SDK ingest path.

This is the only surface a customer's agent talks to, so it is the one place
where "strict validation" and "never lose a batch" pull in opposite directions.
The models below resolve that tension in a specific way:

* the **batch envelope** is validated as a whole — a body that is not an object,
  or that carries no item list, is a malformed request and is refused outright;
* each **item** inside the batch is validated on its own with
  :func:`parse_batch`, so one unparsable span never costs the caller the other
  999. A failed item comes back as a rejected row carrying the reason, and the
  rest are ingested.

The typed batch models (:class:`TraceBatchIn` and friends) exist so the shape is
still published in the OpenAPI document and the SDKs can be generated from it;
the routes attach them with :func:`request_body_schema` and then parse the body
themselves, because exact byte accounting for
``settings.ingest_max_body_bytes`` needs the raw body anyway.

Nothing here knows about the telemetry engine's HTTP contract — the service
layer shapes the payloads. These models describe what *we* accept.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
from typing import Any, Final, Generic, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..core.errors import ValidationFailed

# ---------------------------------------------------------------------------
# Caps
#
# Every cap below bounds a single request's memory footprint. They sit *under*
# the transport limits in settings so a caller hits a precise, actionable error
# ("too many traces") rather than a generic 413.
# ---------------------------------------------------------------------------

MAX_TRACES_PER_BATCH: Final[int] = 1_000
MAX_SPANS_PER_BATCH: Final[int] = 1_000
MAX_SPANS_PER_TRACE: Final[int] = 1_000
MAX_SCORES_PER_BATCH: Final[int] = 1_000
MAX_EVENTS_PER_BATCH: Final[int] = 500

MAX_TAGS: Final[int] = 32
MAX_TAG_LENGTH: Final[int] = 64
MAX_METADATA_KEYS: Final[int] = 64
MAX_USAGE_KEYS: Final[int] = 24
MAX_SCORES_PER_ITEM: Final[int] = 25
MAX_NAME_LENGTH: Final[int] = 200

#: Longest excerpt kept on a guardrail event row. The full payload stays in the
#: telemetry engine; the control plane keeps only enough to recognise the hit.
MAX_SAMPLE_LENGTH: Final[int] = 500

#: Marker written in place of content a policy or guardrail ordered masked.
REDACTION_MARKER: Final[str] = "[redacted by policy]"


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class SpanType(enum.StrEnum):
    """Span classes the telemetry engine models separately."""

    GENERAL = "general"
    LLM = "llm"
    TOOL = "tool"
    GUARDRAIL = "guardrail"


class ScoreTarget(enum.StrEnum):
    """What a feedback score is attached to."""

    TRACE = "trace"
    SPAN = "span"
    THREAD = "thread"


class ItemOutcome(enum.StrEnum):
    """Per-item verdict returned for every row in a batch.

    ``blocked`` is deliberately distinct from ``rejected``: a rejected row was
    unusable, a blocked row was well formed and refused by governance. The SDK
    retries neither, but only the second is a compliance event.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class RejectionCode(enum.StrEnum):
    """Machine-readable reason an item did not reach the telemetry store."""

    MALFORMED = "malformed"
    UNKNOWN_AGENT = "unknown_agent"
    AGENT_NOT_PERMITTED = "agent_not_permitted"
    AGENT_UNPROVISIONED = "agent_unprovisioned"
    MISSING_TRACE_ID = "missing_trace_id"
    INVALID_ID = "invalid_id"
    POLICY_BLOCKED = "policy_blocked"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    CONNECTOR_BLOCKED = "connector_blocked"
    TELEMETRY_REJECTED = "telemetry_rejected"
    UNSUPPORTED_EVENT = "unsupported_event"
    UNKNOWN_GUARDRAIL = "unknown_guardrail"
    UNKNOWN_POLICY = "unknown_policy"
    DUPLICATE = "duplicate"


class IngestEventKind(enum.StrEnum):
    """Governance events an SDK reports alongside its telemetry.

    These are the occurrences the control plane owns rather than the telemetry
    engine: a guardrail that fired in the customer's own process, a policy the
    SDK enforced locally, and end-user feedback captured in the product.
    """

    GUARDRAIL_TRIGGERED = "guardrail.triggered"
    POLICY_VIOLATION = "policy.violation"
    FEEDBACK_SUBMITTED = "feedback.submitted"


# ---------------------------------------------------------------------------
# Shared field helpers
# ---------------------------------------------------------------------------


def as_document(value: Any) -> dict[str, Any] | None:
    """Normalise a free-form payload into the object shape the store expects.

    An SDK may hand us a bare string or list for ``input``/``output``; wrapping
    it keeps one shape on the wire without forcing every caller to wrap by hand.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": value}


def _clean_tags(value: list[str]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for raw in value:
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tags are limited to {MAX_TAG_LENGTH} characters")
        seen.add(tag)
        tags.append(tag)
    return tags


def _clean_usage(value: dict[str, int] | None) -> dict[str, int] | None:
    if value is None:
        return None
    if len(value) > MAX_USAGE_KEYS:
        raise ValueError(f"usage carries at most {MAX_USAGE_KEYS} counters")
    cleaned: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"usage.{key} must be a whole number of units")
        if raw < 0:
            raise ValueError(f"usage.{key} cannot be negative")
        cleaned[key.strip()] = raw
    return cleaned


def _clean_metadata(value: dict[str, Any]) -> dict[str, Any]:
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(f"metadata carries at most {MAX_METADATA_KEYS} keys")
    return value


class _TelemetryBase(BaseModel):
    """Fields every telemetry item shares, with their normalisation."""

    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        return _clean_tags(value)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _clean_metadata(value)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ErrorInfoIn(BaseModel):
    """A failure captured by the SDK, carried verbatim onto the span."""

    model_config = ConfigDict(extra="forbid")

    exception_type: str = Field(min_length=1, max_length=200)
    message: str | None = Field(None, max_length=4_000)
    traceback: str | None = Field(None, max_length=16_000)


class FeedbackScoreIn(BaseModel):
    """One score attached to a trace, span or thread."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    value: float = Field(allow_inf_nan=False)
    category_name: str | None = Field(None, max_length=80)
    reason: str | None = Field(None, max_length=1_000)
    source: str = Field("sdk", max_length=32)

    @field_validator("name", "category_name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class ScoreIn(FeedbackScoreIn):
    """Body item of ``POST /ingest/scores``: a score plus what it scores."""

    target: ScoreTarget = Field(
        ScoreTarget.TRACE, description="Whether id addresses a trace, a span or a thread."
    )
    id: str = Field(
        min_length=1,
        max_length=120,
        description="Trace id, span id or thread id, according to target.",
    )
    agent: str | None = Field(
        None,
        max_length=160,
        description="Agent name or slug; omit when the key is bound to one agent.",
    )


class SpanIn(_TelemetryBase):
    """One unit of work inside a trace.

    ``id`` is optional: when the SDK omits it the control plane mints a
    time-ordered id. Supplying one makes a retry idempotent, which is what the
    SDKs do, so a network timeout never double-counts a span.
    """

    id: str | None = Field(None, max_length=64)
    trace_id: str | None = Field(
        None, max_length=64, description="Required when posting to /ingest/spans."
    )
    parent_span_id: str | None = Field(None, max_length=64)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    type: SpanType = SpanType.GENERAL
    start_time: dt.datetime
    end_time: dt.datetime | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    model: str | None = Field(None, max_length=120)
    provider: str | None = Field(None, max_length=80)
    total_estimated_cost: float | None = Field(None, ge=0, allow_inf_nan=False)
    error_info: ErrorInfoIn | None = None
    feedback_scores: list[FeedbackScoreIn] = Field(
        default_factory=list, max_length=MAX_SCORES_PER_ITEM
    )
    agent: str | None = Field(None, max_length=160)

    @field_validator("input", "output", mode="before")
    @classmethod
    def _document(cls, value: Any) -> dict[str, Any] | None:
        return as_document(value)

    @field_validator("usage")
    @classmethod
    def _usage(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        return _clean_usage(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("a span needs a name")
        return name

    @model_validator(mode="after")
    def _ordered(self) -> SpanIn:
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time cannot precede start_time")
        return self

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * 1000


class TraceIn(_TelemetryBase):
    """One end-to-end agent invocation, with its spans nested inside it."""

    id: str | None = Field(None, max_length=64)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    start_time: dt.datetime
    end_time: dt.datetime | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    thread_id: str | None = Field(
        None, max_length=120, description="Groups traces into one conversation."
    )
    error_info: ErrorInfoIn | None = None
    spans: list[SpanIn] = Field(default_factory=list, max_length=MAX_SPANS_PER_TRACE)
    feedback_scores: list[FeedbackScoreIn] = Field(
        default_factory=list, max_length=MAX_SCORES_PER_ITEM
    )
    agent: str | None = Field(
        None,
        max_length=160,
        description="Agent name or slug; overridden when the API key is bound to an agent.",
    )

    @field_validator("input", "output", mode="before")
    @classmethod
    def _document(cls, value: Any) -> dict[str, Any] | None:
        return as_document(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("a trace needs a name")
        return name

    @model_validator(mode="after")
    def _ordered(self) -> TraceIn:
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time cannot precede start_time")
        return self

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * 1000

    @property
    def total_cost(self) -> float:
        return sum(span.total_estimated_cost or 0.0 for span in self.spans)

    @property
    def total_usage(self) -> dict[str, int]:
        """Token counters summed across the trace's spans."""
        totals: dict[str, int] = {}
        for span in self.spans:
            for key, value in (span.usage or {}).items():
                totals[key] = totals.get(key, 0) + value
        return totals


class EventIn(BaseModel):
    """A governance event the SDK observed in the customer's own process.

    One model rather than three because the SDK sends a single stream; the
    per-kind required fields are enforced below, so a guardrail event without a
    guardrail is rejected as its own row rather than accepted and dropped.
    """

    model_config = ConfigDict(extra="forbid")

    kind: IngestEventKind
    ref: str | None = Field(
        None,
        max_length=64,
        description="Caller-supplied idempotency key; a repeat is reported as a duplicate.",
    )
    occurred_at: dt.datetime | None = None
    agent: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=120)
    span_id: str | None = Field(None, max_length=120)

    # guardrail.triggered
    guardrail: str | None = Field(
        None, max_length=160, description="Guardrail id or name, for guardrail.triggered."
    )
    action_taken: str | None = Field(None, max_length=24)
    score: float | None = Field(None, allow_inf_nan=False)
    matched: dict[str, Any] = Field(default_factory=dict)
    sample: str | None = Field(None, max_length=MAX_SAMPLE_LENGTH)

    # policy.violation
    policy: str | None = Field(
        None, max_length=160, description="Policy id or name, for policy.violation."
    )
    severity: str | None = Field(None, max_length=16)
    detail: dict[str, Any] = Field(default_factory=dict)

    # feedback.submitted
    rating: int | None = Field(None, ge=1, le=5)
    sentiment: str | None = Field(None, max_length=16)
    body: str | None = Field(None, max_length=4_000)
    source: str | None = Field(None, max_length=40)
    submitted_by: str | None = Field(None, max_length=160)

    @model_validator(mode="after")
    def _required_for_kind(self) -> EventIn:
        if self.kind is IngestEventKind.GUARDRAIL_TRIGGERED and not (self.guardrail or "").strip():
            raise ValueError("guardrail.triggered needs the guardrail it fired")
        if self.kind is IngestEventKind.POLICY_VIOLATION and not (self.policy or "").strip():
            raise ValueError("policy.violation needs the policy that was breached")
        if (
            self.kind is IngestEventKind.FEEDBACK_SUBMITTED
            and self.rating is None
            and not self.body
        ):
            raise ValueError("feedback.submitted needs a rating or a body")
        return self


# ---------------------------------------------------------------------------
# Batch envelopes
#
# Declared with typed item lists so the OpenAPI document publishes the full
# contract. The routes attach them with `request_body_schema` and parse the
# body through `parse_batch`, which validates items one at a time.
# ---------------------------------------------------------------------------


class BatchEnvelope(BaseModel):
    """The fields every ingest batch carries around its items."""

    model_config = ConfigDict(extra="ignore")

    agent: str | None = Field(
        None,
        max_length=160,
        description="Default agent for every item that does not name its own.",
    )
    sdk: str | None = Field(None, max_length=80, description="Reporting SDK, e.g. 'python'.")
    sdk_version: str | None = Field(None, max_length=40)
    environment: str | None = Field(
        None,
        max_length=40,
        description="Environment the reporting process was configured for.",
    )

    @field_validator("environment", mode="before")
    @classmethod
    def _usable_environment(cls, value: Any) -> Any:
        # Until this field was declared the envelope ignored the key, whatever
        # it held. It is a hint about where to file an agent nobody has seen
        # before, so a value that cannot be one is dropped: it is never worth
        # refusing a whole batch of telemetry over.
        if not isinstance(value, str) or len(value.strip()) > 40:
            return None
        return value.strip()

    @field_validator("agent", "sdk", "sdk_version", "environment")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TraceBatchIn(BatchEnvelope):
    """Body of ``POST /ingest/traces``."""

    traces: list[TraceIn] = Field(min_length=1, max_length=MAX_TRACES_PER_BATCH)


class SpanBatchIn(BatchEnvelope):
    """Body of ``POST /ingest/spans`` — spans for traces already reported."""

    spans: list[SpanIn] = Field(min_length=1, max_length=MAX_SPANS_PER_BATCH)


class ScoreBatchIn(BatchEnvelope):
    """Body of ``POST /ingest/scores``."""

    scores: list[ScoreIn] = Field(min_length=1, max_length=MAX_SCORES_PER_BATCH)


class EventBatchIn(BatchEnvelope):
    """Body of ``POST /ingest/events``."""

    events: list[EventIn] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


T = TypeVar("T", bound=BaseModel)


@dataclasses.dataclass(frozen=True)
class ParsedBatch(Generic[T]):
    """Result of parsing one batch body.

    ``items`` is index-aligned with what the caller sent: a position holding
    ``None`` failed validation and its reason is in ``errors`` under the same
    index. Keeping the indices means the per-item results the caller gets back
    line up with the rows it sent, which is what makes a partial failure
    actionable.
    """

    envelope: BatchEnvelope
    items: list[T | None]
    errors: dict[int, str]

    @property
    def received(self) -> int:
        return len(self.items)

    @property
    def valid(self) -> list[tuple[int, T]]:
        return [(index, item) for index, item in enumerate(self.items) if item is not None]


def _first_error(exc: ValidationError) -> str:
    """One readable sentence from a pydantic failure, for the item's result row."""
    errors = exc.errors()
    if not errors:
        return "The item could not be validated."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "item"
    message = first.get("msg", "is invalid")
    suffix = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{location}: {message}{suffix}"


def parse_batch(
    body: bytes,
    *,
    field: str,
    item_model: type[T],
    max_items: int,
) -> ParsedBatch[T]:
    """Parse a batch body, validating the envelope strictly and items one by one.

    Raises :class:`ValidationFailed` only for problems with the request as a
    whole — unparsable JSON, a body that is not an object, a missing or empty
    item list, or more items than the cap allows. Everything else is reported
    against the individual item so the rest of the batch still lands.
    """
    if not body:
        raise ValidationFailed("The request body is empty.")

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ValidationFailed(f"The request body is not valid JSON: {exc}.") from exc

    if not isinstance(payload, dict):
        raise ValidationFailed(
            f"The body must be an object carrying a '{field}' array.",
            details={"field": field},
        )

    raw_items = payload.get(field)
    if raw_items is None:
        raise ValidationFailed(
            f"The body is missing its '{field}' array.", details={"field": field}
        )
    if not isinstance(raw_items, list):
        raise ValidationFailed(f"'{field}' must be an array.", details={"field": field})
    if not raw_items:
        raise ValidationFailed(f"'{field}' is empty; send at least one item.")
    if len(raw_items) > max_items:
        raise ValidationFailed(
            f"A batch carries at most {max_items:,} items; this one carries {len(raw_items):,}.",
            details={"field": field, "max_items": max_items, "received": len(raw_items)},
        )

    try:
        envelope = BatchEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailed(
            f"The batch envelope is invalid: {_first_error(exc)}"
        ) from exc

    items: list[T | None] = []
    errors: dict[int, str] = {}
    for index, raw in enumerate(raw_items):
        try:
            items.append(item_model.model_validate(raw))
        except ValidationError as exc:
            items.append(None)
            errors[index] = _first_error(exc)
    return ParsedBatch(envelope=envelope, items=items, errors=errors)


def request_body_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``openapi_extra`` that publishes ``model`` as the route's request body.

    The routes read the raw body themselves, so FastAPI never sees a body
    parameter to document; this puts the contract back into ``/api/docs``.
    """
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }


# ---------------------------------------------------------------------------
# Governance hand-off
# ---------------------------------------------------------------------------


class GuardrailCandidate(BaseModel):
    """One piece of content offered to the guardrail evaluator.

    The Guardrails domain owns the evaluation itself; ingest owns extracting the
    content, applying the verdicts and writing the evidence rows.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(description="Position of the item in the submitted batch.")
    agent_id: str | None = None
    environment: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    text: str = Field(description="Concatenated input and output text of the item.")


class GuardrailVerdict(BaseModel):
    """One guardrail decision about one candidate.

    ``action`` uses the Guardrails vocabulary (``Block``, ``Mask``, ``Warn``,
    ``Log``). Only ``Block`` and ``Mask`` change what reaches the telemetry
    store; the other two are recorded and let through.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    guardrail_id: str
    guardrail_name: str | None = None
    action: str = Field(max_length=24)
    score: float | None = Field(None, allow_inf_nan=False)
    matched: dict[str, Any] = Field(default_factory=dict)
    sample: str | None = Field(None, max_length=MAX_SAMPLE_LENGTH)
    reason: str | None = Field(None, max_length=500)
    #: The literal strings a ``Mask`` verdict found, so ingest can remove exactly
    #: those and nothing else. Excluded from every serialisation: this is the
    #: sensitive text itself, and it exists only between the check and the write.
    redactions: list[str] = Field(default_factory=list, exclude=True, repr=False)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class IngestItemResult(BaseModel):
    """What happened to one submitted row."""

    index: int = Field(description="Zero-based position in the submitted array.")
    id: str | None = Field(None, description="Id the item was stored under, when accepted.")
    outcome: ItemOutcome
    code: RejectionCode | None = Field(None, description="Machine-readable rejection reason.")
    reason: str | None = None
    agent_id: str | None = None
    spans: int = Field(0, description="Spans this item carried; drives OTLP partial success.")
    spans_rejected: int = Field(
        0,
        description=(
            "Spans of an accepted trace the telemetry store would not take. The trace "
            "itself was stored; reason says why these were not."
        ),
    )
    policy_id: str | None = Field(None, description="Policy that blocked or masked the item.")
    guardrail_id: str | None = Field(None, description="Guardrail that blocked or masked it.")
    masked: bool = Field(False, description="Content was removed before storage.")


class AutoRegisteredAgent(BaseModel):
    """An agent the ingest path created because telemetry arrived for it."""

    id: str
    name: str
    slug: str
    environment: str
    status: str
    engine_project_name: str | None = None


class IngestQuotaState(BaseModel):
    """A quota this batch was measured against, after the batch was counted."""

    id: str
    name: str
    resource: str
    scope: str
    scope_ref: str | None = None
    unit: str
    limit_value: float
    used_value: float
    remaining: float
    utilization_pct: float
    enforcement: str
    status: str
    resets_at: dt.datetime | None = None


class IngestBatchResult(BaseModel):
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
    spans_accepted: int = 0
    scores_accepted: int = 0
    events_recorded: int = 0
    violations_recorded: int = 0
    guardrails_evaluated: bool = Field(
        False, description="False when no guardrail evaluator is available to this deployment."
    )
    agents: list[str] = Field(default_factory=list, description="Agent ids this batch touched.")
    auto_registered: list[AutoRegisteredAgent] = Field(default_factory=list)
    quotas: list[IngestQuotaState] = Field(default_factory=list)
    results: list[IngestItemResult] = Field(default_factory=list)
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# SDK start-up configuration
# ---------------------------------------------------------------------------


class GuardrailDescriptor(BaseModel):
    """A guardrail the SDK should know about, as the config endpoint reports it."""

    id: str
    name: str
    type: str
    action: str
    threshold: float
    scope: str
    scope_ref: str | None = None
    status: str


class RedactionRule(BaseModel):
    """A rule the SDK applies locally, before content ever leaves the process."""

    id: str
    name: str
    source: str = Field(description="'guardrail' or 'workspace'.")
    entity_types: list[str] = Field(
        default_factory=list, description="Named entity classes to remove, e.g. 'email'."
    )
    pattern: str | None = Field(None, description="Regular expression matched against content.")
    replacement: str = REDACTION_MARKER
    applies_to: list[str] = Field(
        default_factory=list, description="Fields the rule covers: input, output, metadata."
    )


class IngestConfigRead(BaseModel):
    """What an SDK fetches once at start-up and re-fetches when the ETag moves.

    Deliberately small and cacheable: it is the first call every agent process
    makes, and a fleet restart must not turn into a stampede on the database.
    """

    workspace: str
    environment: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    agent_bound: bool = Field(
        False, description="True when the API key may only report for one agent."
    )

    sampling_rate: float = Field(ge=0.0, le=1.0)
    batch_max_spans: int
    batch_max_bytes: int
    flush_interval_seconds: float
    max_queue_size: int
    retry_max_attempts: int
    retry_backoff_seconds: float

    capture_input: bool = True
    capture_output: bool = True

    endpoints: dict[str, str] = Field(default_factory=dict)
    guardrails: list[GuardrailDescriptor] = Field(default_factory=list)
    redaction: list[RedactionRule] = Field(default_factory=list)

    revision: str = Field(description="Opaque revision; changes when anything above changes.")
    refresh_after_seconds: int = Field(description="How long the SDK may cache this document.")
