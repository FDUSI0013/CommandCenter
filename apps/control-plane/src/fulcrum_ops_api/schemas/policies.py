"""Wire contracts for the Policy Center.

A policy is two things at once: a row a governance reviewer reads in a table,
and a machine-readable rule the enforcement path evaluates before an agent
acts. The schemas below keep both halves honest.

* :class:`PolicyRules` is the small, closed schema every rule body is validated
  against on create and update — a set of **conditions**, one **action**, and
  the **severity** stamped on violations it raises. Anything else is rejected,
  so a rule body that reaches the enforcement path is always interpretable.
* Read models type their vocabulary fields as ``str`` rather than as enums. The
  columns are plain strings by design (see ``models.governance``), and a row
  written before a vocabulary was extended must still be readable: a list
  endpoint that raises because one row holds an unrecognised label is worse
  than one that renders the label as it was stored.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from ..models.governance import (
    PolicyCategory,
    PolicyEnforcement,
    PolicyScope,
    PolicyStatus,
    RiskLevel,
    ViolationSeverity,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; the wire layer stays ORM-free
    from ..models.governance import PolicyViolation


# ---------------------------------------------------------------------------
# The rule body
# ---------------------------------------------------------------------------

#: Comparison vocabulary a condition may use.
RuleOperator = Literal[
    "eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "contains", "matches", "exists"
]

#: How multiple conditions combine.
RuleMatch = Literal["all", "any"]

#: What the enforcement path does when a signal cannot be resolved.
RuleFailMode = Literal["closed", "open"]

#: Operators that impose an ordering, and therefore constrain the value's type.
ORDERED_OPERATORS: Final[tuple[str, ...]] = ("lt", "lte", "gt", "gte")

VERSION_PATTERN: Final[str] = r"^v\d+\.\d+\.\d+$"

MAX_IMPORT_ITEMS: Final[int] = 200

#: Every signal the enforcement path resolves for a trace or span. This is the
#: key set of ``services.ingest._signals`` — the suite pins the two together —
#: and it lives here so a rule body can be checked at write time without the
#: wire layer importing the ingest pipeline. A name outside it never resolves,
#: and a clause that never resolves either blocks everything (fail-closed) or
#: silently enforces nothing (fail-open); neither is what its author meant.
KNOWN_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "agent_id",
        "agent_name",
        "agent_slug",
        "agent_risk",
        "action_risk",
        "agent_status",
        "agent_tags",
        "team",
        "environment",
        "platform",
        "entity",
        "name",
        "span_type",
        "tool_name",
        "model",
        "provider",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "duration_ms",
        "tags",
        "has_error",
        "thread_id",
        "content",
        "content_length",
    }
)

#: A feedback score is not in the closed vocabulary — judges are named by the
#: workspace — so it is addressed explicitly, as ``score:<name>``. The prefix is
#: what tells a deliberate score reference apart from a mistyped signal.
SCORE_SIGNAL_PREFIX: Final[str] = "score:"


def is_resolvable_signal(signal: str) -> bool:
    """Whether the enforcement path can ever resolve ``signal``.

    Compared the way ingest compiles a rule: trimmed and case-folded.
    """
    name = signal.strip().lower()
    if name.startswith(SCORE_SIGNAL_PREFIX):
        return len(name) > len(SCORE_SIGNAL_PREFIX)
    return name in KNOWN_SIGNALS


def enforced_mode(rules: Any, fallback: str) -> str:
    """The enforcement a stored policy really applies.

    The enforcement path reads ``rules.action.mode`` and falls back to the
    ``enforcement`` column only when the body carries none. Every write keeps
    the two equal; this exists for rows written before that was true, so what a
    reviewer is shown is what is being enforced rather than a label beside it.
    """
    action = rules.get("action") if isinstance(rules, dict) else None
    mode = action.get("mode") if isinstance(action, dict) else None
    return mode if isinstance(mode, str) and mode else fallback


class PolicyCondition(BaseModel):
    """One clause evaluated against the trace or span an agent reports.

    ``signal`` names a value the enforcement path resolves at decision time:
    one of :data:`KNOWN_SIGNALS` (``action_risk``, ``total_tokens``,
    ``tool_name``, ``content`` ...) or a feedback score written as
    ``score:<name>``. The schema guarantees the clause is well formed; the
    service checks the signal against the vocabulary whenever a rule body is
    written or a policy is activated, so a stored body from before the
    vocabulary was enforced stays readable.
    """

    model_config = ConfigDict(extra="forbid")

    signal: str = Field(
        min_length=1,
        max_length=80,
        description="Value the enforcement path resolves at decision time.",
    )
    operator: RuleOperator = Field("eq", description="How the signal compares to the value.")
    value: Any = Field(None, description="Right-hand side of the comparison.")

    @field_validator("signal")
    @classmethod
    def _normalise_signal(cls, value: str) -> str:
        signal = value.strip()
        if not signal:
            raise ValueError("A condition needs a signal name.")
        return signal

    @model_validator(mode="after")
    def _value_fits_operator(self) -> PolicyCondition:
        operator, value = self.operator, self.value

        if operator == "exists":
            if value is not None:
                raise ValueError("The 'exists' operator takes no value.")
            return self

        if value is None:
            raise ValueError(f"The '{operator}' operator requires a value.")

        if operator in ("in", "not_in"):
            if not isinstance(value, list) or not value:
                raise ValueError(f"The '{operator}' operator requires a non-empty list.")
            return self

        if isinstance(value, (list, dict)):
            raise ValueError(f"The '{operator}' operator takes a single scalar value.")

        if operator in ("contains", "matches") and not isinstance(value, str):
            raise ValueError(f"The '{operator}' operator requires a string value.")

        # A number, or a member of an ordered vocabulary such as Low/Medium/High.
        if operator in ORDERED_OPERATORS and (
            isinstance(value, bool) or not isinstance(value, (int, float, str))
        ):
            raise ValueError(
                f"The '{operator}' operator requires a number or an ordered label."
            )

        return self


class PolicyAction(BaseModel):
    """What happens when the conditions match."""

    model_config = ConfigDict(extra="forbid")

    mode: PolicyEnforcement = Field(description="Enforcement applied on a match.")
    message: str | None = Field(
        None, max_length=500, description="Explanation surfaced to the caller when it fires."
    )
    notify: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Channels or addresses notified on a match.",
    )
    audit: bool = Field(True, description="Write an audit row for every match.")

    @field_validator("notify")
    @classmethod
    def _clean_notify(cls, value: list[str]) -> list[str]:
        targets = [item.strip() for item in value if item and item.strip()]
        if any(len(target) > 160 for target in targets):
            raise ValueError("Notification targets are limited to 160 characters.")
        return targets


class PolicyRules(BaseModel):
    """The complete rule body stored in ``policies.rules``.

    Deliberately small: conditions, one action, one severity. A control that
    cannot be expressed this way belongs in the enforcement path's own
    configuration, not in a row a reviewer is asked to sign off.
    """

    model_config = ConfigDict(extra="forbid")

    match: RuleMatch = Field("all", description="Whether all or any condition must hold.")
    conditions: list[PolicyCondition] = Field(
        min_length=1, max_length=25, description="Clauses combined according to 'match'."
    )
    action: PolicyAction = Field(description="What the enforcement path does on a match.")
    severity: ViolationSeverity = Field(
        ViolationSeverity.MEDIUM,
        description="Severity stamped on violations this rule raises.",
    )
    exceptions: list[str] = Field(
        default_factory=list,
        max_length=25,
        description="Agent ids, principals or tags exempt from this rule.",
    )
    # Open unless the author says otherwise. A signal that is absent is the
    # normal case, not an attack — ``tool_name`` is empty on every trace and on
    # every span that is not a tool call — so a body that merely omits this
    # field must not opt in to treating absence as a match.
    fail_mode: RuleFailMode = Field(
        "open", description="Behaviour when a signal cannot be resolved."
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


class PolicyCreate(BaseModel):
    """Body of ``POST /policies``.

    ``rules`` may be omitted. The create form collects category, risk and
    enforcement before the rule editor is opened, so the service derives one
    starter condition from those answers rather than storing an empty body that
    would silently match nothing. A policy created that way is saved
    ``Inactive`` whatever ``status`` asked for: nobody has read the derived
    rule yet, and a rule nobody has read must not start refusing telemetry.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(None, max_length=2000)
    category: PolicyCategory = PolicyCategory.GUARDRAILS
    scope: PolicyScope = PolicyScope.GLOBAL
    scope_ref: str | None = Field(
        None,
        max_length=64,
        description="Target of a non-global scope: agent id, connector id or environment name.",
    )
    scope_label: str | None = Field(
        None, max_length=80, description="Label the table renders; derived when omitted."
    )
    risk_level: RiskLevel = RiskLevel.MEDIUM
    status: PolicyStatus = PolicyStatus.ACTIVE
    enforcement: PolicyEnforcement = PolicyEnforcement.BLOCK
    rules: PolicyRules | None = None
    version: str = Field("v1.0.0", pattern=VERSION_PATTERN)
    owner_user_id: str | None = Field(None, max_length=36)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if len(name) < 2:
            raise ValueError("A policy name needs at least two characters.")
        return name

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _scope_ref_present(self) -> PolicyCreate:
        if self.scope is PolicyScope.GLOBAL:
            self.scope_ref = None
        elif not (self.scope_ref or "").strip():
            raise ValueError(f"A {self.scope.value} scope needs a scope_ref naming its target.")
        return self


class PolicyUpdate(BaseModel):
    """Body of ``PATCH /policies/{id}``; every field is optional.

    ``expected_updated_at`` is the optimistic-concurrency token: send the
    ``updated_at`` the client last read and the write is rejected with 409 if
    somebody else changed the policy in the meantime.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=2, max_length=160)
    description: str | None = Field(None, max_length=2000)
    category: PolicyCategory | None = None
    scope: PolicyScope | None = None
    scope_ref: str | None = Field(None, max_length=64)
    scope_label: str | None = Field(None, max_length=80)
    risk_level: RiskLevel | None = None
    status: PolicyStatus | None = None
    enforcement: PolicyEnforcement | None = None
    rules: PolicyRules | None = None
    owner_user_id: str | None = Field(None, max_length=36)
    expected_updated_at: dt.datetime | None = Field(
        None, description="Optimistic lock: the updated_at the client last read."
    )

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = " ".join(value.split())
        if len(name) < 2:
            raise ValueError("A policy name needs at least two characters.")
        return name

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class PolicyRead(BaseModel):
    """A policy as the Policy Center table and inspector render it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    category: str
    scope: str
    scope_ref: str | None = None
    scope_label: str | None = None
    risk_level: str
    status: str
    enforcement: str
    rules: dict[str, Any] = Field(default_factory=dict)
    version: str
    owner_user_id: str | None = None

    last_evaluated_at: dt.datetime | None = None
    last_triggered_at: dt.datetime | None = None

    # The windowed violation figures are counted from ``policy_violations`` when
    # the policy is read (``services.policies.window_counts``), with the same
    # definition as the KPI cards, so a page's rows add up to the cards. The
    # remaining counters are rollups recomputed in bulk by
    # ``services.policies.refresh_rollups`` on the scheduler's clock.
    # ``last_evaluated_at`` and ``applies_tools`` have no producer: render the
    # first as "—" when null and do not present the second as a measured count.
    violations_30d: int = Field(
        0, description="Violations of this policy in the last 30 days, counted when read."
    )
    blocked_30d: int = Field(
        0, description="Of those, the ones enforced as Block, counted when read."
    )
    escalations_30d: int = Field(
        0,
        description=(
            "Of those, the human escalations (enforced as Escalate or Require Approval), "
            "counted when read."
        ),
    )
    requests_30d: int = 0
    approved_pct: int = 0
    applies_agents: int = 0
    applies_tools: int = 0
    applies_connectors: int = 0
    applies_envs: int = 0

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    @model_validator(mode="after")
    def _show_what_is_enforced(self) -> PolicyRead:
        # Writes keep the column and ``rules.action.mode`` equal. A row saved
        # before they did can still disagree, and then the badge has to show the
        # mode ingest applies — a "Log Only" chip on a policy that is blocking
        # is the one thing this table must never say.
        self.enforcement = enforced_mode(self.rules, self.enforcement)
        return self


class PolicyViolationRead(BaseModel):
    """One breach, carrying the policy and agent names the console renders."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    policy_id: str
    policy_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    trace_id: str | None = None
    severity: str
    action_taken: str
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: dt.datetime
    resolved_at: dt.datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved(self) -> bool:
        """A violation is closed once the reviewer has recorded an outcome."""
        return self.resolved_at is not None

    @classmethod
    def from_row(
        cls,
        violation: PolicyViolation,
        *,
        policy_name: str | None = None,
        agent_name: str | None = None,
    ) -> PolicyViolationRead:
        """Build from the ORM row plus the labels resolved alongside the page."""
        return cls.model_validate(violation).model_copy(
            update={"policy_name": policy_name, "agent_name": agent_name}
        )


# ---------------------------------------------------------------------------
# Action bodies
# ---------------------------------------------------------------------------


class PolicyDeactivateRequest(BaseModel):
    """Body of ``POST /policies/{id}/deactivate``.

    The reason is optional because the console's confirm dialog does not always
    collect one; when it is supplied it is written verbatim into the audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class PolicyCloneRequest(BaseModel):
    """Body of ``POST /policies/{id}/clone``. An empty body is valid."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        None,
        min_length=2,
        max_length=160,
        description="Override for the copy's name; defaults to '<name> (Copy)'.",
    )

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = " ".join(value.split())
        if len(name) < 2:
            raise ValueError("A policy name needs at least two characters.")
        return name


class PolicyActionResponse(BaseModel):
    """Result of activate, deactivate or clone: the policy plus its blast radius."""

    policy: PolicyRead
    message: str
    agents_affected: int = Field(
        0, description="Agents that gain or lose enforcement because of this change."
    )
    bound_agents: int = Field(
        0, description="Agents explicitly bound to the policy, as opposed to matched by scope."
    )


class PolicyBindingRead(BaseModel):
    """One explicit policy-to-agent attachment, as the inspector's Scope tab lists it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    policy_id: str
    agent_id: str
    agent_name: str | None = Field(
        None, description="Null when the agent has since been deleted."
    )
    bound_at: dt.datetime
    bound_by: str | None = None


#: What to do when an imported definition collides with an existing name.
ImportConflictMode = Literal["skip", "replace", "fail"]


class PolicyImportRequest(BaseModel):
    """Body of ``POST /policies/import`` — the parsed contents of a policy file."""

    model_config = ConfigDict(extra="forbid")

    policies: list[PolicyCreate] = Field(min_length=1, max_length=MAX_IMPORT_ITEMS)
    on_conflict: ImportConflictMode = Field(
        "skip", description="What to do when a policy of the same name already exists."
    )
    activate: bool = Field(
        False, description="Force every imported policy Active, ignoring its own status."
    )
    source: str | None = Field(
        None, max_length=255, description="Filename or system the definitions came from."
    )


class PolicyImportIssue(BaseModel):
    """One definition the import could not apply as written.

    Usually it was skipped. It is also how the batch reports a definition that
    was imported but not activated, because its rule body had to be derived.
    Neither fails the batch.
    """

    index: int = Field(description="Zero-based position in the submitted list.")
    name: str
    reason: str


class PolicyImportResult(BaseModel):
    """Outcome of an import run."""

    submitted: int
    created: int
    replaced: int
    skipped: int
    policy_ids: list[str] = Field(default_factory=list)
    issues: list[PolicyImportIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class PolicySummary(BaseModel):
    """The KPI cards above the Policy Center table.

    Every number is a SQL aggregate: the status counts come from ``policies``,
    the windowed numbers from ``policy_violations``. The windowed numbers count
    every violation record of the workspace in the window -- the same records
    the Agent Registry's card and Live Runs count, including those naming an
    agent since deleted from the registry.
    """

    total: int
    active: int
    warning: int
    inactive: int
    pending_review: int
    active_pct: int = Field(description="Active policies as a whole percentage of total.")

    blocked_actions_30d: int = Field(description="Violations in the window enforced as Block.")
    policies_violated_30d: int = Field(
        description="Distinct policies with at least one breach in the window."
    )
    violations_30d: int = Field(description="Violation events in the window.")
    human_escalations_30d: int = Field(
        0,
        description=(
            "Violations in the window whose enforcement put a person in the loop: "
            "those enforced as one of ``escalating_actions``."
        ),
    )
    escalating_actions: list[str] = Field(
        default_factory=list,
        description="The enforcement outcomes counted as human escalations.",
    )
    window_days: int
