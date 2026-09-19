"""Wire contracts for the Evaluations screen.

An evaluation is one judged scoring pass over a dataset: the control plane
creates an experiment in the telemetry engine, links the cases an SDK experiment
already ran to it, and reads the judged scores back. Four metrics have columns
of their own — correctness, grounding, faithfulness and safety — because those
are the four the console renders. A score under any other name (an SDK scorer is
named after its function) is reported as ``extra_scores`` rather than dropped.

Nothing in this module invents a score. When the engine has not judged a case
or a metric yet the field is ``None`` and the table shows a dash; a missing
number is never rounded up to a plausible one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.quality import EvaluationStatus

#: The judged metrics, in the order the table shows them.
METRIC_KEYS: Final[tuple[str, ...]] = ("correctness", "grounding", "faithfulness", "safety")

METRIC_LABELS: Final[dict[str, str]] = {
    "correctness": "Correctness",
    "grounding": "Grounding",
    "faithfulness": "Faithfulness",
    "safety": "Safety",
}

#: Score names the engine's judges emit, mapped onto our four columns. A judge
#: that answers ``answer_correctness`` and one that answers ``correctness`` must
#: land in the same column, or the same evaluation would read differently
#: depending on which judge produced it.
METRIC_ALIASES: Final[dict[str, str]] = {
    "correctness": "correctness",
    "answer_correctness": "correctness",
    "answer_relevance": "correctness",
    "accuracy": "correctness",
    "exact_match": "correctness",
    # The comparisons an SDK scorer is most often written as — the SDK names a
    # score after the scorer function, and its README's example is the first.
    "contains_expected": "correctness",
    "contains": "correctness",
    "equals": "correctness",
    "levenshtein_ratio": "correctness",
    "grounding": "grounding",
    "groundedness": "grounding",
    "context_recall": "grounding",
    "context_precision": "grounding",
    "retrieval_relevance": "grounding",
    "faithfulness": "faithfulness",
    "hallucination": "faithfulness",
    "factuality": "faithfulness",
    "safety": "safety",
    "moderation": "safety",
    "toxicity": "safety",
    "harmfulness": "safety",
}

#: Metrics where a high engine score means a bad answer, so the column value is
#: ``1 - score``. Judges emit hallucination and toxicity this way round.
INVERTED_METRIC_SOURCES: Final[frozenset[str]] = frozenset(
    {"hallucination", "toxicity", "harmfulness"}
)

#: The judge the console offers first. Overridable per run.
DEFAULT_JUDGE_MODEL: Final[str] = "gpt-4o"

#: A candidate scores below its baseline by more than this before the run is
#: counted as a regression on the KPI card.
REGRESSION_THRESHOLD: Final[float] = 0.02

#: Columns of the Evaluations CSV export, in display order.
EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("agent_name", "Agent"),
    ("agent_model", "Model"),
    ("dataset", "Dataset"),
    ("cases", "Cases"),
    ("correctness", "Correctness"),
    ("grounding", "Grounding"),
    ("faithfulness", "Faithfulness"),
    ("safety", "Safety"),
    ("avg_score", "Avg Score"),
    ("baseline_delta", "Delta vs Baseline"),
    ("judge_model", "Judge Model"),
    ("status", "Status"),
    ("started_at", "Started"),
    ("finished_at", "Finished"),
)


#: Scores the platform itself writes onto a trace. They are observations about
#: a run — a thumbs-down, a review flag — not a judge's verdict on a case, so
#: they never count towards an evaluation.
NON_JUDGE_SCORES: Final[frozenset[str]] = frozenset({"user_feedback", "flagged_for_review"})


def canonical_metric(name: str) -> str:
    """One spelling per score name: lower snake case, without a ``_metric`` tail.

    The engine's built-in judges report under their class names
    (``hallucination_metric``, ``answer_relevance_metric``); the tail says
    nothing, and leaving it on kept every one of them off the alias list.
    """
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    return key.removesuffix("_metric") or key


def normalise_metric(name: str) -> str | None:
    """Map one engine score name onto a column key, or ``None`` if it is not ours."""
    return METRIC_ALIASES.get(canonical_metric(name))


def _coerce_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def metric_values(scores: Any) -> dict[str, float]:
    """Reduce any engine score payload to the judged scores we publish.

    Accepts both shapes the engine uses — a mapping of name to value, and a
    list of ``{"name": …, "value": …}`` rows — because experiments and traces
    answer with different ones. A name on the alias list lands in one of the
    four columns, clamped to 0-1. A name that is not is never guessed into a
    column, but it is not thrown away either: it is kept under its own name, as
    reported. It is a verdict somebody's scorer produced, and dropping it made a
    run judged only by such scorers read as not judged at all — Failed, every
    case Unscored. Duplicates of the same score are averaged; the scores the
    platform writes itself (:data:`NON_JUDGE_SCORES`) are not verdicts and are
    left out. Feeding the result back in returns it unchanged, which is what
    lets the row's cached ``scores`` be read through the same function.
    """
    collected: dict[str, list[float]] = {}

    def _offer(raw_name: Any, raw_value: Any) -> None:
        if not isinstance(raw_name, str) or not raw_name.strip():
            return
        source = canonical_metric(raw_name)
        value = _coerce_score(raw_value)
        if value is None or source in NON_JUDGE_SCORES:
            return
        key = METRIC_ALIASES.get(source)
        if key is None:
            collected.setdefault(source, []).append(value)
            return
        if source in INVERTED_METRIC_SOURCES:
            value = 1.0 - value
        collected.setdefault(key, []).append(max(0.0, min(1.0, value)))

    if isinstance(scores, dict):
        for name, value in scores.items():
            _offer(name, value)
    elif isinstance(scores, (list, tuple)):
        for row in scores:
            if isinstance(row, dict):
                _offer(row.get("name"), row.get("value"))

    return {key: round(sum(values) / len(values), 4) for key, values in collected.items()}


def extra_scores(values: dict[str, float | None] | None) -> dict[str, float]:
    """The judged scores that are not one of the four columns, by their own name."""
    return {
        key: value
        for key, value in (values or {}).items()
        if key not in METRIC_KEYS and isinstance(value, (int, float))
    }


def average_score(values: dict[str, float | None] | None) -> float | None:
    """Mean of the metrics that were actually judged, or ``None`` if none were.

    The four columns decide the average whenever any of them was judged. When
    none was — the run was scored only by scorers under their own names — the
    average is taken over those instead, restricted to the ones on the 0-1 scale
    every judged score here is read on: a scorer that reports a count or a
    latency is shown under its name but cannot be averaged with a ratio.
    """
    if not values:
        return None
    present = [v for key in METRIC_KEYS if (v := values.get(key)) is not None]
    if not present:
        present = [v for v in extra_scores(values).values() if 0.0 <= v <= 1.0]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class MetricScore(BaseModel):
    """One judged metric with its movement against the previous run."""

    key: str
    label: str
    value: float | None = Field(None, description="0-1; null when the judge produced none")
    baseline: float | None = None
    delta: float | None = Field(None, description="value - baseline, null when either is absent")


class EvaluationRead(BaseModel):
    """One row of the Evaluations table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    agent_id: str | None = None
    agent_name: str | None = Field(None, description="Resolved for the table's Agent column")
    agent_model: str | None = Field(None, description="Shown under the agent name")
    dataset: str
    judge_model: str
    status: EvaluationStatus
    cases: int = Field(
        0,
        description=(
            "Cases the run judged once it has completed (0 for a failed run). While "
            "the run is live this is the size of its dataset, for the progress bar"
        ),
    )

    correctness: float | None = None
    grounding: float | None = None
    faithfulness: float | None = None
    safety: float | None = None
    extra_scores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Judged scores under names that are not one of the four columns, "
            "e.g. an SDK scorer's own name. Averaged across the run's cases."
        ),
    )
    avg_score: float | None = Field(
        None,
        description=(
            "Mean of the judged columns; when none of the four was judged, the mean "
            "of the 0-1 scores in extra_scores"
        ),
    )

    baseline_run_id: str | None = Field(
        None, description="Previous completed run on the same agent and dataset"
    )
    baseline_avg_score: float | None = None
    baseline_delta: float | None = Field(
        None, description="avg_score - baseline_avg_score; negative means a regression"
    )
    is_regression: bool = False

    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    duration_seconds: float | None = None
    occurred_at: dt.datetime = Field(description="Finished, else started, else created")

    engine_experiment_id: str | None = None
    triggered_by_user_id: str | None = None
    notes: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None


class EvaluationItemRead(BaseModel):
    """One case inside an evaluation, as the detail view's breakdown lists it."""

    id: str
    index: int = Field(description="1-based position in the dataset")
    input: str | None = None
    expected_output: str | None = None
    actual_output: str | None = None
    scores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Judged scores for this case: the four columns where judged, and any "
            "other scorer under its own name. Empty when unscored"
        ),
    )
    avg_score: float | None = None
    passed: bool | None = Field(
        None, description="Null while the engine has not judged the case"
    )
    trace_id: str | None = None


class EvaluationTrendPoint(BaseModel):
    """One point on the score trend chart."""

    date: dt.date
    evaluations: int = 0
    cases: int = 0
    avg_score: float | None = None
    correctness: float | None = None
    grounding: float | None = None
    faithfulness: float | None = None
    safety: float | None = None


class EvaluationDetail(EvaluationRead):
    """The evaluation with its per-metric and per-case breakdown."""

    metrics: list[MetricScore] = Field(default_factory=list)
    items: list[EvaluationItemRead] = Field(default_factory=list)
    item_page: int = 1
    item_page_size: int = 25
    item_total: int = 0
    items_unavailable: bool = Field(
        False,
        description=(
            "True when the telemetry store could not be read for the case breakdown. "
            "items is then empty because it is unknown, not because there are no cases; "
            "every other field comes from the control plane's own database"
        ),
    )
    scored_items: int = Field(0, description="Cases that carry at least one judged score")
    trend: list[EvaluationTrendPoint] = Field(
        default_factory=list, description="This agent and dataset over time, oldest first"
    )


class EvaluationProgress(BaseModel):
    """Live progress of a running evaluation."""

    evaluation_id: str
    status: EvaluationStatus
    phase: str = Field(description="Preparing, Registering cases, Scoring, or Finished")
    total_cases: int = 0
    processed_cases: int = 0
    scored_cases: int = 0
    percent: float = Field(0.0, description="0-100, floored at the phase that is running")
    is_terminal: bool = False
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    elapsed_seconds: float | None = None
    detail: str | None = None


class EvaluationTrend(BaseModel):
    """The trend series plus the window it covers."""

    window_days: int
    points: list[EvaluationTrendPoint] = Field(default_factory=list)
    agent_id: str | None = None
    dataset: str | None = None


class EvaluationComparison(BaseModel):
    """Baseline against candidate, metric by metric."""

    baseline: EvaluationRead
    candidate: EvaluationRead
    metrics: list[MetricScore] = Field(default_factory=list)
    avg_score_delta: float | None = None
    verdict: str = Field(description="Improved, Regressed or Unchanged")
    regressed_metrics: list[str] = Field(default_factory=list)
    improved_metrics: list[str] = Field(default_factory=list)


class EvaluationsSummary(BaseModel):
    """The five KPI cards, each with the movement against the previous window."""

    window_days: int = 30
    evaluations: int = Field(description="Evaluations (30d)")
    evaluations_delta: int = Field(description="Against the preceding window, in runs")
    avg_score: float | None = None
    avg_score_delta: float | None = None
    cases_run: int = Field(0, description="Test Cases Run")
    cases_run_delta_percent: float | None = None
    regressions_caught: int = 0
    regressions_caught_delta: int = 0
    judge_model: str | None = Field(None, description="Most used judge in the window")
    judge_method: str = "LLM-as-judge + rules"
    running: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


class DatasetRead(BaseModel):
    """One dataset the console can evaluate or test against."""

    name: str
    engine_id: str | None = None
    description: str | None = None
    case_count: int = 0
    experiment_count: int = 0
    tags: list[str] = Field(default_factory=list)
    created_at: dt.datetime | None = None
    last_updated_at: dt.datetime | None = None
    created_by: str | None = None


class DatasetItemRead(BaseModel):
    """One case in a dataset."""

    id: str
    input: str | None = None
    expected_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime | None = None


class DatasetCreate(BaseModel):
    """Create a dataset in the telemetry engine, owned by this workspace."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        if "/" in trimmed:
            # The name is a path segment of /datasets/{name}/items. A slash in
            # it — even percent-encoded, which servers decode before routing —
            # names a different path, so the dataset could be created and
            # listed but never filled or read.
            raise ValueError("must not contain '/'")
        return trimmed


class DatasetItem(BaseModel):
    """One case to append. ``input`` is required; everything else is optional."""

    input: str = Field(min_length=1)
    expected_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetItemsCreate(BaseModel):
    """Append cases to a dataset. The engine upserts, so replays are safe."""

    items: list[DatasetItem] = Field(min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class EvaluationCreate(BaseModel):
    """Start an evaluation — the Run Evaluation modal's payload."""

    agent_id: str | None = Field(
        None, description="Null for a dataset-level run that is not tied to one agent"
    )
    dataset: str = Field(min_length=1, max_length=160)
    judge_model: str = Field(DEFAULT_JUDGE_MODEL, min_length=1, max_length=80)
    name: str | None = Field(None, max_length=160)
    notes: str | None = Field(None, max_length=2000)

    @field_validator("dataset", "judge_model")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class EvaluationRerun(BaseModel):
    """Re-run an evaluation. Omitted fields are inherited from the source run."""

    judge_model: str | None = Field(None, min_length=1, max_length=80)
    notes: str | None = Field(None, max_length=2000)
