"""Wire contracts for the Alerts screen.

The console renders one table of raised conditions plus a rules editor, so this
module carries two entity families (``Alert``, ``AlertRule``), the small bodies
behind the triage verbs (acknowledge / resolve / assign / mute), and the summary
that fills the KPI row above the table.

Read models are populated straight from ORM rows; write models validate
everything the database would otherwise have to reject.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from ..models.operations import AlertSeverity, AlertStatus

# Bounds mirror the column widths in ``models.operations`` so a value that
# validates here can always be stored.
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SourceLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RuleName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
EntityRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
DedupeKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Channel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]

# A raiser's payload is free-form, but it is stored in a JSON column and echoed
# to every console client, so it is capped rather than left unbounded.
MAX_DOCUMENT_BYTES = 16 * 1024


def _check_document_size(value: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    encoded = len(json.dumps(value, default=str).encode("utf-8"))
    if encoded > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} must serialise to at most {MAX_DOCUMENT_BYTES} bytes.")
    return value


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertRead(BaseModel):
    """One raised condition, exactly as the Alerts table renders it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    alert_ref: str
    severity: AlertSeverity
    title: str
    description: str | None = None
    source: str
    source_entity_type: str | None = None
    source_entity_id: str | None = None
    status: AlertStatus
    raised_at: dt.datetime
    acknowledged_at: dt.datetime | None = None
    acknowledged_by_user_id: str | None = None
    resolved_at: dt.datetime | None = None
    resolved_by_user_id: str | None = None
    assigned_to_user_id: str | None = None
    mttr_seconds: int | None = None
    dedupe_key: str | None = None
    occurrence_count: int
    last_occurred_at: dt.datetime | None = None
    engine_alert_id: str | None = None
    event_metadata: dict[str, Any] = Field(default_factory=dict)
    # Model property: anything not resolved and not muted still needs attention.
    is_open: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class AlertCreate(BaseModel):
    """Raise an alert by hand, or forward one from an external monitor.

    Supplying ``dedupe_key`` opts into flood control: a recurring condition
    updates the alert that is already open instead of creating another row.
    """

    title: Title
    source: SourceLabel
    severity: AlertSeverity = AlertSeverity.MEDIUM
    description: str | None = Field(None, max_length=8000)
    source_entity_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
    ] | None = None
    source_entity_id: EntityRef | None = None
    dedupe_key: DedupeKey | None = None
    engine_alert_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ] | None = None
    assigned_to_user_id: EntityRef | None = None
    # Backfill window for forwarded alerts; defaults to now and may not be future-dated.
    raised_at: dt.datetime | None = None
    event_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raised_at")
    @classmethod
    def _not_in_the_future(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        # A minute of slack absorbs clock skew on the reporting host.
        if value > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1):
            raise ValueError("raised_at cannot be in the future.")
        return value

    @field_validator("event_metadata")
    @classmethod
    def _metadata_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_document_size(value, "event_metadata") or {}


class AlertUpdate(BaseModel):
    """Edit the descriptive fields of an alert.

    Triage transitions are deliberately absent: acknowledging and resolving also
    stamp timings that feed MTTA and MTTR, so they go through their own
    endpoints. ``status`` here only moves an alert between the two states that
    carry no timing (``Open`` and ``Investigating``).
    """

    title: Title | None = None
    severity: AlertSeverity | None = None
    description: str | None = Field(None, max_length=8000)
    status: AlertStatus | None = None
    assigned_to_user_id: EntityRef | None = None
    dedupe_key: DedupeKey | None = None
    event_metadata: dict[str, Any] | None = None

    @field_validator("event_metadata")
    @classmethod
    def _metadata_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_document_size(value, "event_metadata")


class AlertAcknowledgeRequest(BaseModel):
    """Optional note recorded with an acknowledgement."""

    note: str | None = Field(None, max_length=2000)


class AlertAcknowledgeAllRequest(BaseModel):
    """Bulk acknowledgement, optionally narrowed to part of the queue."""

    severity: list[AlertSeverity] | None = Field(
        None, description="Only acknowledge alerts at these severities."
    )
    source: list[SourceLabel] | None = Field(
        None, description="Only acknowledge alerts raised by these screens."
    )
    note: str | None = Field(None, max_length=2000)


class AlertResolveRequest(BaseModel):
    """Close an alert. The note lands in the audit trail and on the alert."""

    resolution_note: str | None = Field(None, max_length=4000)


class AlertAssignRequest(BaseModel):
    """Hand an alert to a named member of the workspace."""

    assignee_user_id: EntityRef
    note: str | None = Field(None, max_length=2000)


class AlertMuteRequest(BaseModel):
    """Suppress notifications for a window without closing the alert."""

    # One minute to thirty days; the default matches the console's "Mute for 24h".
    duration_minutes: int = Field(1440, ge=1, le=43_200)
    reason: str | None = Field(None, max_length=2000)


# --------------------------------------------------------------------------- #
# Alert rules
# --------------------------------------------------------------------------- #


class AlertRuleRead(BaseModel):
    """A rule as the Alert Rules editor shows it.

    It governs the alerts its ``source`` screen raises at its ``severity``; it
    does not raise them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    name: str
    description: str | None = None
    source: str
    condition: dict[str, Any] = Field(default_factory=dict)
    severity: AlertSeverity
    enabled: bool
    notify_channels: list[str] = Field(default_factory=list)
    throttle_minutes: int
    created_by_user_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class AlertRuleCreate(BaseModel):
    """Define a new rule.

    ``source`` and ``severity`` are what match a rule to the alerts it governs:
    an alert raised from that screen at that severity names the rule, and a
    disabled rule silences it. ``condition`` is stored as written (a threshold,
    a rate, a missing heartbeat) and the editor round-trips the document
    untouched; it records what the screen alerts on and is not evaluated here.
    """

    name: RuleName
    source: SourceLabel
    condition: dict[str, Any] = Field(default_factory=dict)
    severity: AlertSeverity = AlertSeverity.MEDIUM
    description: str | None = Field(None, max_length=4000)
    enabled: bool = True
    notify_channels: list[Channel] = Field(default_factory=list, max_length=25)
    # Zero disables throttling; the ceiling is one week.
    throttle_minutes: int = Field(60, ge=0, le=10_080)

    @field_validator("condition")
    @classmethod
    def _condition_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_document_size(value, "condition") or {}

    @field_validator("notify_channels")
    @classmethod
    def _unique_channels(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for channel in value:
            if channel not in seen:
                seen.append(channel)
        return seen


class AlertRuleUpdate(BaseModel):
    """Partial edit of a rule; every field is optional."""

    name: RuleName | None = None
    source: SourceLabel | None = None
    condition: dict[str, Any] | None = None
    severity: AlertSeverity | None = None
    description: str | None = Field(None, max_length=4000)
    enabled: bool | None = None
    notify_channels: list[Channel] | None = Field(None, max_length=25)
    throttle_minutes: int | None = Field(None, ge=0, le=10_080)

    @field_validator("condition")
    @classmethod
    def _condition_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_document_size(value, "condition")

    @field_validator("notify_channels")
    @classmethod
    def _unique_channels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        seen: list[str] = []
        for channel in value:
            if channel not in seen:
                seen.append(channel)
        return seen


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


class AlertsSummary(BaseModel):
    """The KPI row above the Alerts table.

    Every figure is a database aggregate over the whole workspace, not over the
    page the table happens to be showing.
    """

    open: int = Field(description="Alerts still in the Open state.")
    critical: int = Field(description="Critical alerts that are not yet resolved.")
    investigating: int = Field(description="Alerts someone is actively working.")
    acknowledged: int = Field(description="Acknowledged alerts awaiting a fix window.")
    muted: int = Field(description="Alerts suppressed for a maintenance window.")
    resolved_24h: int = Field(description="Alerts closed in the last 24 hours.")
    total: int = Field(description="Every alert ever raised in this workspace.")
    mtta_seconds: float | None = Field(
        None, description="Mean time to acknowledge, over alerts that were acknowledged."
    )
    mttr_seconds: float | None = Field(
        None, description="Mean time to resolve, over alerts that were resolved."
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Distinct source screens, for the console's Source filter.",
    )
