"""Wire contracts for the Guardrails screen.

A guardrail is a runtime check applied to prompts and responses: injection,
PII, toxicity, hallucination, secrets, topic. The row in our database is the
control record — name, action, threshold, scope, ownership — while the check
itself runs in the telemetry engine's inline content checker. Test verdicts and
matched spans in this module therefore always come from a real check; nothing
here simulates a detection.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.quality import (
    GuardrailAction,
    GuardrailScope,
    GuardrailStatus,
    GuardrailType,
)

#: Name each guardrail type is known by inside the engine's content checker.
#: A guardrail may override it with ``config["validation"]`` when a workspace
#: runs a bespoke validator.
CHECKER_VALIDATIONS: Final[dict[str, str]] = {
    GuardrailType.PROMPT_INJECTION.value: "PROMPT_INJECTION",
    GuardrailType.PII.value: "PII",
    GuardrailType.TOXICITY.value: "TOXICITY",
    GuardrailType.HALLUCINATION.value: "HALLUCINATION",
    GuardrailType.SECRETS.value: "SECRETS",
    GuardrailType.TOPIC.value: "TOPIC",
    GuardrailType.CUSTOM.value: "CUSTOM",
}

#: Config key a guardrail sets to address a validator by another name.
VALIDATION_CONFIG_KEY: Final[str] = "validation"

#: Coverage label used when a guardrail is not pinned to one agent or
#: environment. It is what the console prints under the guardrail name.
GLOBAL_COVERAGE_LABEL: Final[str] = "All Agents"

#: How much text of a triggering call is kept on the event row. The full
#: payload stays in the engine; this is only enough to recognise the case.
EVENT_SAMPLE_LIMIT: Final[int] = 280

#: Columns of the Guardrails CSV export, in display order.
EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "Guardrail"),
    ("guardrail_type", "Type"),
    ("coverage", "Coverage"),
    ("status", "Status"),
    ("action", "Action"),
    ("threshold", "Threshold"),
    ("triggers_30d", "Triggers (30d)"),
    ("blocked_30d", "Blocked (30d)"),
    ("masked_30d", "Masked (30d)"),
    ("effectiveness", "Effectiveness"),
    ("added_latency_ms", "Added Latency (ms)"),
    ("last_triggered_at", "Last Triggered"),
)

#: Columns of the guardrail events CSV export.
EVENT_EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("occurred_at", "Time"),
    ("guardrail_name", "Guardrail"),
    ("guardrail_type", "Type"),
    ("agent_name", "Agent"),
    ("action_taken", "Action"),
    ("score", "Score"),
    ("match_count", "Matches"),
    ("trace_id", "Trace"),
    ("sample", "Sample"),
)


def coverage_label(scope: str, scope_ref: str | None, resolved: str | None = None) -> str:
    """What the console prints as the guardrail's coverage.

    ``resolved`` is the agent or environment name the service looked up; when a
    scoped guardrail points at something that no longer exists the raw
    reference is shown rather than a soothing default.
    """
    if scope == GuardrailScope.GLOBAL.value:
        return GLOBAL_COVERAGE_LABEL
    if scope == GuardrailScope.AGENT.value:
        return resolved or scope_ref or "Unassigned agent"
    return resolved or scope_ref or "Unassigned environment"


def validation_name(guardrail_type: str, config: dict[str, Any] | None) -> str:
    """The validator the checker should run for this guardrail."""
    override = (config or {}).get(VALIDATION_CONFIG_KEY)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return CHECKER_VALIDATIONS.get(guardrail_type, CHECKER_VALIDATIONS[GuardrailType.CUSTOM.value])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class MatchedSpan(BaseModel):
    """One thing the checker found in the text."""

    label: str = Field(description="Entity or pattern the checker named, e.g. 'EMAIL'")
    text: str | None = Field(None, description="Excerpt, already masked when the action masks")
    start: int | None = None
    end: int | None = None
    score: float | None = None


class GuardrailRead(BaseModel):
    """One row of the Guardrails table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    guardrail_type: GuardrailType
    status: GuardrailStatus
    action: GuardrailAction
    threshold: float
    scope: GuardrailScope
    scope_ref: str | None = None
    coverage: str = Field(description="Human label for scope + scope_ref")

    triggers_30d: int = 0
    blocked_30d: int = 0
    masked_30d: int = 0
    effectiveness: float | None = Field(
        None, description="Percentage of triggers confirmed as true positives"
    )
    added_latency_ms: int = 0
    last_triggered_at: dt.datetime | None = None

    config: dict[str, Any] = Field(default_factory=dict)
    engine_guardrail_id: str | None = None
    owner_user_id: str | None = None
    owner_name: str | None = None

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class GuardrailEventRead(BaseModel):
    """One trigger in the recent-detections feed."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    guardrail_id: str
    guardrail_name: str | None = None
    guardrail_type: GuardrailType | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    trace_id: str | None = None
    occurred_at: dt.datetime
    action_taken: GuardrailAction
    score: float | None = None
    match_count: int = 0
    matched: dict[str, Any] = Field(default_factory=dict)
    sample: str | None = None


class GuardrailsSummary(BaseModel):
    """The five KPI cards. Counts are SQL aggregates over the event table."""

    window_days: int = 30
    active: int
    configured: int
    disabled: int = 0
    tuning: int = 0
    triggers: int = Field(description="Triggers (30d)")
    triggers_delta_percent: float | None = Field(
        None, description="Against the preceding window; null when there is no history"
    )
    blocked: int = Field(description="Blocked (30d)")
    pii_items_masked: int = Field(description="PII Items Masked (30d)")
    pii_items_masked_delta_percent: float | None = None
    avg_added_latency_ms: float | None = Field(
        None, description="Mean added latency across active guardrails"
    )
    last_triggered_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class GuardrailCreate(BaseModel):
    """Register a guardrail. It is created Active unless asked otherwise."""

    name: str = Field(min_length=1, max_length=160)
    guardrail_type: GuardrailType = GuardrailType.CUSTOM
    action: GuardrailAction = GuardrailAction.WARN
    status: GuardrailStatus = GuardrailStatus.ACTIVE
    threshold: float = Field(0.5, ge=0.0, le=1.0)
    scope: GuardrailScope = GuardrailScope.GLOBAL
    scope_ref: str | None = Field(
        None, max_length=120, description="Agent id or environment name; null when Global"
    )
    config: dict[str, Any] = Field(default_factory=dict)
    owner_user_id: str | None = Field(None, max_length=36)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class GuardrailUpdate(BaseModel):
    """Partial update. Omitted fields are left alone."""

    name: str | None = Field(None, min_length=1, max_length=160)
    guardrail_type: GuardrailType | None = None
    action: GuardrailAction | None = None
    status: GuardrailStatus | None = None
    threshold: float | None = Field(None, ge=0.0, le=1.0)
    scope: GuardrailScope | None = None
    scope_ref: str | None = Field(None, max_length=120)
    config: dict[str, Any] | None = None
    owner_user_id: str | None = Field(None, max_length=36)
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class GuardrailThresholdUpdate(BaseModel):
    """The Tune action: move the confidence threshold, and optionally the action."""

    threshold: float = Field(ge=0.0, le=1.0)
    action: GuardrailAction | None = Field(
        None, description="Change what happens on a trigger at the same time"
    )
    reason: str | None = Field(None, max_length=500)


class GuardrailTestRequest(BaseModel):
    """The Test modal's payload: a sample prompt or response to check."""

    input: str = Field(min_length=1, max_length=20000)


# ---------------------------------------------------------------------------
# Check results
# ---------------------------------------------------------------------------


class GuardrailTestResult(BaseModel):
    """What one real check measured against the sample."""

    guardrail_id: str
    name: str
    guardrail_type: GuardrailType
    triggered: bool
    verdict: str = Field(description="Triggered or Clear")
    score: float | None = Field(None, description="Detection confidence the checker returned")
    threshold: float
    action: GuardrailAction = Field(description="What would happen to a live call")
    action_applied: bool = Field(
        description="False in Tuning status, where the check runs but nothing is enforced"
    )
    matched: list[MatchedSpan] = Field(default_factory=list)
    masked_text: str | None = Field(
        None, description="The sample with matched spans masked, when the action masks"
    )
    added_latency_ms: int = Field(description="Measured round trip of the check")
    detail: str


class GuardrailHit(BaseModel):
    """One guardrail's verdict inside a content evaluation."""

    guardrail_id: str
    name: str
    guardrail_type: GuardrailType
    action: GuardrailAction
    score: float | None = None
    threshold: float
    matched: list[MatchedSpan] = Field(default_factory=list)
    enforced: bool = Field(description="False when the guardrail is in Tuning (shadow) mode")


class ContentEvaluation(BaseModel):
    """The verdict the ingest path acts on.

    ``text`` is the content after masking, so a caller that trusts this object
    never has to re-apply a guardrail itself.
    """

    allowed: bool
    blocked: bool
    action: GuardrailAction | None = Field(
        None, description="Strongest action applied; null when nothing triggered"
    )
    text: str
    masked: bool = False
    hits: list[GuardrailHit] = Field(default_factory=list)
    evaluated: int = Field(0, description="Guardrails that ran")
    added_latency_ms: int = 0
    detail: str | None = None
