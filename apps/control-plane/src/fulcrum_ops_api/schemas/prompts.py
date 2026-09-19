"""Wire contracts for the Prompt Manager.

Prompts live in the telemetry engine's prompt registry, which already owns
immutable commits, version listing, retrieval by commit and restore. This API
does not copy any of that into our database. What it adds is the two things the
engine has no opinion about:

* **Tenancy.** The engine runs single-tenant behind this service, so every
  prompt name is stored namespaced with the workspace's telemetry namespace.
  The namespace never reaches the client: ``name`` here is always the bare name
  an operator typed.
* **Governance.** Draft, In Review, Approved and Blocked are our states, and
  they are read back from our own append-only audit trail rather than stored in
  the registry — which means a prompt's approval history is covered by the same
  tamper-evident hash chain as every other governed change.

Token counts are labelled ``estimated_`` because they are derived from the
template text rather than measured by a tokenizer; nothing in this module ever
reports a number it did not compute from real content.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator

from ..models.registry import EnvironmentType

#: Mustache-style placeholder, which is what the engine's prompt templates use.
VARIABLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}"
)

#: Anything still wrapped in braces after rendering is an unresolved placeholder.
LEFTOVER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{\{[^}]*\}\}")

#: Separator between the workspace namespace and the operator-visible name.
NAMESPACE_SEPARATOR: Final[str] = "/"

#: Words to tokens. A tokenizer would be exact; this is explicitly an estimate
#: and every field carrying it says so.
TOKENS_PER_WORD: Final[float] = 1.3

MAX_TEST_CASES: Final[int] = 50


class PromptStatus(enum.StrEnum):
    """Governance state of a prompt. The state machine lives in the service."""

    DRAFT = "Draft"
    IN_REVIEW = "In Review"
    APPROVED = "Approved"
    BLOCKED = "Blocked"


def template_variables(template: str) -> list[str]:
    """Every ``{{variable}}`` the template declares, in first-seen order."""
    seen: dict[str, None] = {}
    for match in VARIABLE_PATTERN.finditer(template or ""):
        seen.setdefault(match.group(1), None)
    return list(seen)


def estimate_tokens(template: str) -> int:
    """Rough token count from the template's word count. Never a measurement."""
    words = len((template or "").split())
    return int(words * TOKENS_PER_WORD)


def render_template(template: str, variables: dict[str, Any]) -> str:
    """Substitute every declared placeholder present in ``variables``."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return str(variables[name])
        return match.group(0)

    return VARIABLE_PATTERN.sub(_replace, template or "")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class PromptRead(BaseModel):
    """One row of the Prompt Manager table.

    ``runs_30d`` and ``success_rate`` are the telemetry engine's trace figures
    for the project of the agent this prompt belongs to. They are null, not
    zero, for a prompt whose agent reports no telemetry — a screen must be able
    to tell "no runs" apart from "nothing measured".
    """

    id: str
    name: str = Field(description="Bare name; the workspace namespace is stripped.")
    description: str | None = None
    agent: str | None = Field(None, description="Agent this prompt is the system prompt for.")
    agent_id: str | None = None
    version: str | None = Field(None, description="Label of the head commit, e.g. 'v1.2.0'.")
    commit: str | None = Field(None, description="Engine commit id of the head version.")
    status: PromptStatus
    environment: EnvironmentType | None = None
    template: str | None = None

    estimated_tokens: int = Field(
        0, description="Estimated from the template's word count, not tokenizer-measured."
    )
    template_chars: int = 0
    variables: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    runs_30d: int | None = None
    success_rate: float | None = Field(
        None, description="Percentage of the window's runs that finished without error."
    )

    owner: str | None = Field(None, description="Actor who last changed the prompt's state.")
    version_count: int = 0
    created_at: dt.datetime | None = None
    modified_at: dt.datetime | None = None
    status_changed_at: dt.datetime | None = None
    status_changed_by: str | None = None


class PromptVersionRead(BaseModel):
    """One commit in the registry — a row of the inspector's version pipe."""

    commit: str
    version: str | None = Field(None, description="Human label carried in the commit metadata.")
    status: PromptStatus | None = Field(
        None, description="State recorded when the commit was cut."
    )
    change_note: str | None = None
    author: str | None = Field(
        None,
        description=(
            "Who cut the commit, as the audit trail names them. The registry's own "
            "author is used only for a commit this service has no record of making."
        ),
    )
    created_at: dt.datetime | None = None
    estimated_tokens: int = 0
    is_head: bool = False


class PromptVersionDetail(PromptVersionRead):
    """One commit with its template and declared variables."""

    template: str = ""
    variables: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptDiffLine(BaseModel):
    """One line of a unified diff between two commits."""

    kind: str = Field(description="context, added, removed or hunk")
    text: str


class PromptDiff(BaseModel):
    """What changed between two commits of one prompt."""

    prompt_id: str
    from_commit: str
    to_commit: str
    from_version: str | None = None
    to_version: str | None = None
    added_lines: int
    removed_lines: int
    identical: bool
    lines: list[PromptDiffLine] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class PromptCreate(BaseModel):
    """Author a prompt. It starts Draft and must be submitted for review."""

    name: str = Field(min_length=1, max_length=160)
    template: str = Field(min_length=1, max_length=200_000)
    description: str | None = Field(None, max_length=2000)
    agent: str | None = Field(None, max_length=160, description="Owning agent's name.")
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    version: str | None = Field(
        None, max_length=24, description="Human label for the first commit."
    )
    change_note: str | None = Field(None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name", "template")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("name")
    @classmethod
    def _no_namespace(cls, value: str) -> str:
        if NAMESPACE_SEPARATOR in value:
            raise ValueError(
                f"a prompt name may not contain '{NAMESPACE_SEPARATOR}'; the workspace "
                "namespace is added by the API"
            )
        return value


class PromptVersionCreate(BaseModel):
    """Commit a new body. The prompt returns to Draft and needs review again."""

    template: str = Field(min_length=1, max_length=200_000)
    change_note: str | None = Field(None, max_length=2000)
    version: str | None = Field(None, max_length=24, description="Human label for the commit.")
    environment: EnvironmentType | None = None
    agent: str | None = Field(None, max_length=160)

    @field_validator("template")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class PromptLifecycleRequest(BaseModel):
    """Body of the four lifecycle verbs; the note lands in the audit trail."""

    note: str | None = Field(None, max_length=2000)


class PromptTestRequest(BaseModel):
    """Run the prompt's template over sample variable sets.

    Each case is one mapping of variable name to value. With ``score`` set the
    rendered results are stored as a dataset in this workspace's evaluation
    namespace and an experiment is opened against them; running an evaluation
    against that dataset is what scores them.
    """

    cases: list[dict[str, Any]] = Field(
        default_factory=lambda: [{}],
        max_length=MAX_TEST_CASES,
        description="Variable sets to render. Defaults to a single empty case.",
    )
    commit: str | None = Field(
        None, description="Commit to test; defaults to the head version."
    )
    dataset_name: str | None = Field(
        None, max_length=160, description="Bare name; the API adds the workspace namespace."
    )
    experiment_name: str | None = Field(
        None, max_length=160, description="Bare name; the API adds the workspace namespace."
    )
    score: bool = Field(
        True,
        description=(
            "Persist the rendered cases as a dataset and open an experiment against "
            "them, ready to be evaluated. Set false to render only."
        ),
    )


class PromptTestCase(BaseModel):
    """What rendering one case produced, and what was wrong with it."""

    index: int
    rendered: str
    ok: bool
    missing_variables: list[str] = Field(
        default_factory=list, description="Declared by the template, absent from the case."
    )
    unused_variables: list[str] = Field(
        default_factory=list, description="Supplied by the case, not declared by the template."
    )
    unresolved_placeholders: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0


class PromptExecuteRequest(BaseModel):
    """Render one variable set and send it to a model.

    This is the Run button in Prompt Studio. It is a single interactive call,
    not an evaluation sweep — use ``/test`` when you want a scored dataset.
    """

    variables: dict[str, Any] = Field(
        default_factory=dict, description="One variable set to render the template with."
    )
    commit: str | None = Field(None, description="Commit to run; defaults to the head version.")
    model: str | None = Field(
        None,
        max_length=120,
        description="Override the deployment's configured model for this run only.",
    )
    system: str | None = Field(
        None, max_length=8000, description="Optional system message placed before the prompt."
    )
    max_output_tokens: int | None = Field(None, ge=1, le=8192)


class PromptExecuteResult(BaseModel):
    """What the model answered, and what it cost to ask."""

    prompt_id: str
    name: str
    commit: str | None = None
    version: str | None = None

    rendered: str = Field(description="The prompt exactly as it was sent.")
    missing_variables: list[str] = Field(default_factory=list)
    unresolved_placeholders: list[str] = Field(default_factory=list)

    output: str
    model: str = Field(description="The model that actually answered.")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None
    truncated: bool = Field(
        False, description="True when the model stopped on the token cap rather than finishing."
    )


class PromptTestResult(BaseModel):
    """Outcome of a prompt test run."""

    prompt_id: str
    name: str
    commit: str | None = None
    version: str | None = None
    variables: list[str] = Field(default_factory=list)
    cases: list[PromptTestCase] = Field(default_factory=list)
    passed: int
    failed: int

    recorded: bool = Field(
        False,
        description=(
            "True when the rendered cases were stored as a dataset with an experiment "
            "opened against the tested commit."
        ),
    )
    scored: bool = Field(
        False,
        description=(
            "True only when something has actually scored the run. Recording a test "
            "attaches no evaluator, so this is false until an evaluation is run "
            "against the dataset."
        ),
    )
    dataset_id: str | None = None
    dataset_name: str | None = Field(
        None,
        description=(
            "The dataset's name as the Evaluations screen lists it; the workspace "
            "namespace is stripped."
        ),
    )
    experiment_id: str | None = None
    experiment_name: str | None = Field(
        None, description="Bare name; the workspace namespace is stripped."
    )
    detail: str = Field(
        description="How the run was executed, including why it was not model-executed."
    )


class PromptActionResponse(BaseModel):
    """Uniform answer for the verbs: the prompt as it now stands, plus a message."""

    prompt: PromptRead
    message: str
    previous_status: PromptStatus | None = None
    version: PromptVersionRead | None = None


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class PromptsSummary(BaseModel):
    """The five KPI cards above the Prompt Manager table."""

    total: int
    approved: int
    in_review: int
    blocked: int
    draft: int
    avg_success_rate: float | None = Field(
        None,
        description=(
            "Mean success rate across prompts with telemetry. Null when nothing in the "
            "workspace has reported a run in the window."
        ),
    )
    runs_30d: int = Field(0, description="Runs attributed to prompt-bearing agents.")
    prompts_with_telemetry: int = Field(
        0, description="Prompts the average was computed from."
    )
    projects_sampled: int = Field(
        0, description="Engine projects queried for the average."
    )
    projects_total: int = Field(
        0, description="Engine projects the workspace has; larger means the sample was capped."
    )
    window_days: int
