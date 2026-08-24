"""Pydantic contracts for the Approvals & Audit screen.

One module covers both halves of that screen: the human-in-the-loop approval
queue — requests, their comment thread, and the rules that raise them — plus the
immutable audit trail rendered on its last tab.

Two conventions run through the file:

* **Read models are permissive** about vocabulary they do not own. A policy row
  is also written by the Policy Center, so rule reads type those columns as
  plain strings rather than failing a whole page because one row carries a
  label this module has never heard of.
* **Write models are strict everywhere** — enum-typed fields and explicit
  bounds — so a malformed request is rejected at the edge instead of halfway
  through a state transition.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from ..models.governance import ApprovalStatus, PolicyScope, PolicyStatus, RiskLevel


class ApprovalRisk(str, enum.Enum):
    """Risk vocabulary accepted on an approval request.

    ``governance.RiskLevel`` (Low/Medium/High) is the shared governance chip.
    Approvals add ``Critical`` because the SLA ladder needs a rung above High
    for actions that have to be decided inside the hour; the values below are
    the four keys of the SLA table in ``services.approvals``.
    """

    LOW = RiskLevel.LOW.value
    MEDIUM = RiskLevel.MEDIUM.value
    HIGH = RiskLevel.HIGH.value
    CRITICAL = "Critical"


class ApprovalTrigger(str, enum.Enum):
    """What makes a rule fire. These are the console's four trigger options."""

    FINANCIAL_THRESHOLD = "Financial action above threshold"
    BULK_DATA_EXPORT = "Bulk data export"
    EXTERNAL_COMMUNICATION = "External communication"
    PRODUCTION_CONFIG_CHANGE = "Production config change"


# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------


class ApprovalImpact(BaseModel):
    """Blast radius rendered as the four tiles on the decision card."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    financial: str | None = Field(None, max_length=64, description='e.g. "$2,450" or "—"')
    systems: int | None = Field(None, ge=0, description="Downstream systems touched")
    sensitivity: str | None = Field(
        None, max_length=64, description='e.g. "Confidential", "Highly Confidential"'
    )
    customers: int | None = Field(None, ge=0, description="Customer records affected")

    @field_validator("systems", "customers", mode="before")
    @classmethod
    def _coerce_count(cls, value: Any) -> Any:
        """Accept "12,450" and "—" from older payloads without failing the read."""
        if value is None or isinstance(value, int):
            return value
        try:
            return int(str(value).replace(",", "").strip())
        except ValueError:
            return None


class ApprovalWorkflowStep(BaseModel):
    """One rung of the four-step tracker drawn beside the decision card."""

    model_config = ConfigDict(from_attributes=True)

    step: str
    done: bool = False
    by: str | None = None
    ts: dt.datetime | None = None


class FollowOnAction(BaseModel):
    """The action the caller must now perform, lifted from an approved payload.

    The control plane never executes an agent's action itself; approving a
    request hands the replayable payload back so the caller — the agent runtime
    or the operator's tooling — can carry it out under the approval it just won.
    """

    action: str = Field(..., description="Named operation the payload asks for")
    target: str | None = Field(None, description="Resource, endpoint or record it acts on")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Arguments to replay verbatim"
    )


# ---------------------------------------------------------------------------
# Approval requests
# ---------------------------------------------------------------------------


class ApprovalRequestRead(BaseModel):
    """A request as the queue, the decision card and the CSV export see it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    request_ref: str
    status: ApprovalStatus
    source: str | None = None

    agent_id: str | None = None
    agent_name: str | None = None
    agent_platform: str | None = None

    action: str
    action_detail: str | None = None
    resource: str | None = None
    risk: ApprovalRisk
    reason: str | None = None

    policy_id: str | None = None
    policy_name: str | None = None

    requested_by_user_id: str | None = None
    requested_by_name: str | None = None
    requested_by_team: str | None = None
    requested_at: dt.datetime

    sla_due_at: dt.datetime | None = None
    sla_label: str | None = None

    decided_by_user_id: str | None = None
    decided_by_name: str | None = None
    decided_at: dt.datetime | None = None
    decision_note: str | None = None
    escalated_to_user_id: str | None = None
    escalated_to_name: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)
    impact: ApprovalImpact = Field(default_factory=ApprovalImpact)
    workflow: list[ApprovalWorkflowStep] = Field(default_factory=list)
    comment_count: int = 0

    created_at: dt.datetime
    updated_at: dt.datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        """Still awaiting a decision — drives the queue badge."""
        return self.status in (ApprovalStatus.PENDING, ApprovalStatus.ESCALATED)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sla_remaining_seconds(self) -> int | None:
        """Seconds left on the clock; negative once the deadline has passed."""
        if self.sla_due_at is None or not self.is_open:
            return None
        return int((self.sla_due_at - dt.datetime.now(dt.UTC)).total_seconds())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sla_breached(self) -> bool:
        """True when the deadline passed before a decision landed."""
        if self.sla_due_at is None:
            return False
        if self.decided_at is not None:
            return self.decided_at > self.sla_due_at
        return dt.datetime.now(dt.UTC) > self.sla_due_at


class ApprovalRequestCreate(BaseModel):
    """Raise a new request. Everything the enforcement path knows goes in here."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., min_length=2, max_length=80, description='e.g. "Refund Initiate"')
    action_detail: str | None = Field(None, max_length=255)
    resource: str | None = Field(None, max_length=255)
    risk: ApprovalRisk = ApprovalRisk.MEDIUM
    reason: str | None = Field(None, max_length=2000)

    agent_id: str | None = Field(None, max_length=36)
    policy_id: str | None = Field(
        None, max_length=36, description="Policy that demanded the approval"
    )
    source: str | None = Field(None, max_length=64, description="Screen or system that raised it")
    requested_by_user_id: str | None = Field(
        None, max_length=36, description="Defaults to the calling user"
    )
    request_ref: str | None = Field(
        None,
        min_length=3,
        max_length=32,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Optional caller-supplied reference; allocated automatically when omitted",
    )
    sla_minutes: int | None = Field(
        None, ge=5, le=20160, description="Overrides the risk-derived SLA window"
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Replayable action returned on approval"
    )
    impact: ApprovalImpact = Field(default_factory=ApprovalImpact)


class ApprovalRequestUpdate(BaseModel):
    """Amend a request that has not been decided yet. All fields optional."""

    model_config = ConfigDict(extra="forbid")

    action: str | None = Field(None, min_length=2, max_length=80)
    action_detail: str | None = Field(None, max_length=255)
    resource: str | None = Field(None, max_length=255)
    risk: ApprovalRisk | None = None
    reason: str | None = Field(None, max_length=2000)
    policy_id: str | None = Field(None, max_length=36)
    sla_minutes: int | None = Field(None, ge=5, le=20160)
    payload: dict[str, Any] | None = None
    impact: ApprovalImpact | None = None
    expected_updated_at: dt.datetime | None = Field(
        None,
        description="Optimistic concurrency: reject the edit if the row moved since this stamp",
    )


class ApprovalDecisionRequest(BaseModel):
    """Body for approve and reject. A reject must carry a note."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(
        None, max_length=2000, description="Decision rationale; recorded in the audit trail"
    )


class ApprovalEscalationRequest(ApprovalDecisionRequest):
    """Body for escalate: optionally names the reviewer it lands on."""

    escalate_to_user_id: str | None = Field(
        None, max_length=36, description="Workspace member who takes over the decision"
    )


class ApprovalDecisionResponse(BaseModel):
    """Result of approve/reject/escalate: the new state, plus what to do next."""

    ok: bool = True
    message: str
    request: ApprovalRequestRead
    follow_on: FollowOnAction | None = Field(
        None, description="Set on approval when the payload names an action to carry out"
    )


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class ApprovalCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    request_id: str
    author_user_id: str | None = None
    author_name: str | None = None
    body: str
    created_at: dt.datetime


class ApprovalCommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(..., min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Approval rules
#
# A rule is a policy in the "Approval & Escalation" category: it is what turns
# an agent action into a request in the queue. It is stored in ``policies`` so
# the Policy Center, the enforcement path and this screen all read one table.
# ---------------------------------------------------------------------------


class ApprovalRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    trigger: str | None = None
    approvers: list[str] = Field(default_factory=list)
    sla_minutes: int | None = None
    threshold_amount: float | None = None

    category: str
    scope: str
    scope_ref: str | None = None
    scope_label: str | None = None
    risk_level: str
    status: str
    enforcement: str
    version: str
    owner_user_id: str | None = None

    applies_agents: int = 0
    requests_30d: int = 0
    approved_pct: int = 0
    last_triggered_at: dt.datetime | None = None

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class ApprovalRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=160)
    description: str | None = Field(None, max_length=2000)
    trigger: ApprovalTrigger
    approvers: list[str] = Field(
        ..., min_length=1, max_length=10, description="Approver groups, e.g. Finance Leads"
    )
    sla_minutes: int = Field(240, ge=5, le=20160)
    threshold_amount: float | None = Field(
        None, ge=0, description="Amount above which a financial trigger fires"
    )
    risk_level: RiskLevel = RiskLevel.MEDIUM
    scope: PolicyScope = PolicyScope.GLOBAL
    scope_ref: str | None = Field(None, max_length=64)
    scope_label: str | None = Field(None, max_length=80)
    status: PolicyStatus = PolicyStatus.ACTIVE

    @field_validator("approvers")
    @classmethod
    def _clean_approvers(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("At least one approver group is required.")
        if any(len(item) > 80 for item in cleaned):
            raise ValueError("Approver group names are limited to 80 characters.")
        return cleaned


class ApprovalRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=2, max_length=160)
    description: str | None = Field(None, max_length=2000)
    trigger: ApprovalTrigger | None = None
    approvers: list[str] | None = Field(None, min_length=1, max_length=10)
    sla_minutes: int | None = Field(None, ge=5, le=20160)
    threshold_amount: float | None = Field(None, ge=0)
    risk_level: RiskLevel | None = None
    scope: PolicyScope | None = None
    scope_ref: str | None = Field(None, max_length=64)
    scope_label: str | None = Field(None, max_length=80)
    status: PolicyStatus | None = None
    expected_updated_at: dt.datetime | None = Field(
        None,
        description="Optimistic concurrency: reject the edit if the row moved since this stamp",
    )

    @field_validator("approvers")
    @classmethod
    def _clean_approvers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("At least one approver group is required.")
        if any(len(item) > 80 for item in cleaned):
            raise ValueError("Approver group names are limited to 80 characters.")
        return cleaned


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class ApprovalsSummary(BaseModel):
    """The six KPI cards above the approval queue. Every number is a SQL aggregate."""

    pending: int = Field(..., description="Requests awaiting a first decision")
    approved_30d: int = Field(..., description="Approved in the last 30 days")
    rejected_30d: int = Field(..., description="Rejected in the last 30 days")
    escalated_30d: int = Field(..., description="Currently escalated, raised in the last 30 days")
    avg_time_to_approve_seconds: float | None = Field(
        None, description="Mean requested-to-approved time over the last 30 days; null when none"
    )
    sla_met_percent: float | None = Field(
        None, description="Share of decisions made inside the SLA window; null when none"
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class AuditEventRead(BaseModel):
    """One row of the append-only trail. There is no create, update or delete."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    occurred_at: dt.datetime
    actor: str
    actor_user_id: str | None = None
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    entity_label: str | None = None
    source_screen: str | None = None
    detail: str | None = None
    prev_value: str | None = None
    new_value: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    event_metadata: dict[str, Any] = Field(default_factory=dict)
    checksum: str | None = Field(None, description="SHA-256 linking this row to the previous one")


class AuditChainStatus(BaseModel):
    """Result of replaying the hash chain over a workspace's audit rows."""

    intact: bool
    checked: int = Field(..., description="Rows verified before the answer was returned")
    broken_at_event_id: str | None = None
    broken_at: dt.datetime | None = None
