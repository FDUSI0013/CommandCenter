"""Wire contracts for the Testing & Regression Suite screen.

A suite is a named set of cases held as a dataset in the telemetry engine; a
run of that suite is an experiment against it. The suite row carries the
workflow state we own — schedule, baseline, ownership, environment — and every
number the screen shows about results is derived from the runs themselves.

Two vocabularies meet on this screen and are deliberately kept apart:

* ``status`` is the *suite's* lifecycle — Active, Draft, Disabled;
* ``result`` is the verdict of its **last finished run** — Passed, Warning,
  Failed and the in-flight states. The table's Status column shows ``result``.

A suite that has never run reports ``result`` as ``Never Run`` rather than
borrowing a pass rate from somewhere else.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.quality import RunTrigger, SuiteStatus, SuiteType, TestRunStatus
from .evaluations import average_score, metric_values

#: Pass rate at or above which a finished run reads as Passed, and below which
#: it degrades to Warning before it is called Failed. These two numbers are the
#: whole of the verdict rule, and the console colours its pills on them.
PASS_THRESHOLD_PERCENT: Final[float] = 90.0
WARNING_THRESHOLD_PERCENT: Final[float] = 75.0

#: A case scores 0-1 in the engine; at or above this it counts as a pass.
CASE_PASS_SCORE: Final[float] = 0.5


def _numeric_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def case_score(raw: Any) -> float | None:
    """One case's 0-1 grade from the feedback scores its trace carries.

    The four published metrics are read first, through the same aliases (and the
    same inversion of hallucination-style scores) the Evaluations screen uses, so
    a case judged on them grades exactly as it does there. But a suite is graded
    by whatever scorers the team wrote, and the SDK names each score after the
    scorer function — ``contains_expected``, ``equals_metric`` — which is none of
    those aliases. Dropping them read a fully scored case as Unscored, and a run
    of nothing but such cases as "no verdicts". When none of the known metrics is
    present the grade is therefore the mean of every numeric score on the case,
    clamped to 0-1 (a boolean scorer counts as 0 or 1). ``None`` still means what
    it always did: nobody scored this case.
    """
    known = average_score(dict(metric_values(raw)))
    if known is not None:
        return known

    if isinstance(raw, dict):
        offered = list(raw.values())
    elif isinstance(raw, (list, tuple)):
        offered = [row.get("value") for row in raw if isinstance(row, dict)]
    else:
        return None
    values = [
        max(0.0, min(1.0, value))
        for value in (_numeric_score(item) for item in offered)
        if value is not None
    ]
    return round(sum(values) / len(values), 4) if values else None


#: How many finished runs the flaky-test detector looks back over.
FLAKY_WINDOW_RUNS: Final[int] = 10

#: Points kept on a suite's pass-rate trend line.
TREND_POINTS: Final[int] = 20

#: Columns of the test suite CSV export, in display order.
EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "Suite Name"),
    ("suite_type", "Type"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("result", "Result"),
    ("pass_rate", "Pass Rate"),
    ("case_count", "Tests"),
    ("last_run_at", "Last Run"),
    ("next_run_at", "Next Run"),
    ("baseline", "Baseline"),
    ("owner_name", "Owner"),
    ("team", "Team"),
    ("flaky_count", "Flaky"),
    ("regressions", "Regressions"),
    ("dataset", "Dataset"),
)

#: Columns of the test run CSV export.
RUN_EXPORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("run_ref", "Run ID"),
    ("suite_name", "Suite"),
    ("trigger", "Trigger"),
    ("result", "Result"),
    ("pass_rate", "Pass Rate"),
    ("total_cases", "Cases"),
    ("passed", "Passed"),
    ("failed", "Failed"),
    ("regression_count", "Regressions"),
    ("duration_label", "Duration"),
    ("started_at", "Started"),
    ("finished_at", "Finished"),
)


class SuiteResult(enum.StrEnum):
    """Verdict of a suite's last run, as the table's Status column renders it."""

    PASSED = "Passed"
    WARNING = "Warning"
    FAILED = "Failed"
    RUNNING = "Running"
    QUEUED = "Queued"
    ERROR = "Error"
    CANCELLED = "Cancelled"
    NEVER_RUN = "Never Run"


class CaseStatus(enum.StrEnum):
    """Per-case outcome inside a run."""

    PASSED = "Passed"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    UNSCORED = "Unscored"      # the engine has not judged this case


def verdict_for(status: str, pass_rate: float | None) -> SuiteResult:
    """Turn a finished run into the verdict the console colours on.

    A run that errored or was cancelled keeps its own state: those are not
    quality verdicts and must not be dressed up as a low pass rate.
    """
    if status == TestRunStatus.RUNNING.value:
        return SuiteResult.RUNNING
    if status == TestRunStatus.QUEUED.value:
        return SuiteResult.QUEUED
    if status == TestRunStatus.ERROR.value:
        return SuiteResult.ERROR
    if status == TestRunStatus.CANCELLED.value:
        return SuiteResult.CANCELLED
    if pass_rate is None:
        return SuiteResult.NEVER_RUN
    if pass_rate >= PASS_THRESHOLD_PERCENT:
        return SuiteResult.PASSED
    if pass_rate >= WARNING_THRESHOLD_PERCENT:
        return SuiteResult.WARNING
    return SuiteResult.FAILED


def validate_cron(expression: str) -> str:
    """Accept a five-field cron expression, or explain why it is not one."""
    trimmed = expression.strip()
    if not trimmed:
        raise ValueError("cron expression must not be blank")
    if not croniter.is_valid(trimmed):
        raise ValueError(f"'{trimmed}' is not a valid cron expression")
    return trimmed


def next_fire(expression: str, after: dt.datetime) -> dt.datetime | None:
    """Next fire time of a cron expression, or ``None`` if it cannot be read."""
    try:
        cron = croniter(expression, after)
    except (ValueError, KeyError):
        return None
    fires = cron.get_next(dt.datetime)
    if fires.tzinfo is None:
        fires = fires.replace(tzinfo=dt.UTC)
    return fires


def describe_cron(expression: str) -> str:
    """Plain-English cadence for the Schedules tab.

    Only the shapes an operator actually writes are spelled out; anything else
    is shown verbatim rather than described wrongly.
    """
    parts = expression.split()
    if len(parts) != 5:
        return expression
    minute, hour, day, month, weekday = parts
    if month != "*":
        return expression
    if minute.startswith("*/") and hour == "*":
        return f"Every {minute[2:]} minutes"
    if hour.startswith("*/") and minute.isdigit():
        return f"Every {hour[2:]} hours"
    if not (minute.isdigit() and hour.isdigit()):
        return expression
    clock = f"{int(hour):02d}:{int(minute):02d}"
    days = {
        "0": "Sundays",
        "1": "Mondays",
        "2": "Tuesdays",
        "3": "Wednesdays",
        "4": "Thursdays",
        "5": "Fridays",
        "6": "Saturdays",
        "7": "Sundays",
    }
    if day == "*" and weekday == "*":
        return f"Daily {clock}"
    if day == "*" and weekday in days:
        return f"{days[weekday]} {clock}"
    if day.isdigit() and weekday == "*":
        return f"Monthly on day {int(day)} at {clock}"
    return expression


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestSuiteRead(BaseModel):
    """One row of the Test Suites table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    suite_type: SuiteType
    environment: str
    status: SuiteStatus = Field(description="Suite lifecycle, not the run verdict")
    result: SuiteResult = Field(description="Verdict of the last run")
    agent_id: str | None = None
    agent_name: str | None = None
    dataset: str = Field(description="Dataset in the telemetry engine the cases come from")
    case_count: int = 0
    pass_rate: float | None = Field(None, description="Percentage, from the last finished run")
    last_run_at: dt.datetime | None = None
    last_run_id: str | None = Field(
        None, description="Newest run of any status, including one still in flight"
    )
    last_finished_run_id: str | None = Field(
        None,
        description=(
            "Newest run that finished with verdicts (Passed or Failed): the one to "
            "compare against the baseline. A queued, running, errored or cancelled "
            "run has no verdicts to diff."
        ),
    )
    next_run_at: dt.datetime | None = Field(None, description="Computed from schedule_cron")
    schedule_cron: str | None = None
    baseline_run_id: str | None = None
    baseline: str | None = Field(None, description="Run reference of the baseline")
    owner_user_id: str | None = None
    owner_name: str | None = None
    team: str | None = None
    flaky_count: int = 0
    regressions: int = Field(0, description="Regressions in the last finished run")
    avg_duration_seconds: float | None = None
    avg_duration_label: str | None = None
    trend: list[float] = Field(
        default_factory=list, description="Pass rate of recent finished runs, oldest first"
    )
    tags: list[str] = Field(default_factory=list)
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class TestCaseResult(BaseModel):
    """One case inside a run."""

    id: str
    name: str | None = None
    status: CaseStatus
    score: float | None = None
    trace_id: str | None = None


class TestRunRead(BaseModel):
    """One row of the Test Runs table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_ref: str
    suite_id: str
    suite_name: str | None = None
    environment: str | None = None
    status: TestRunStatus
    result: SuiteResult
    trigger: RunTrigger
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    pass_rate: float | None = None
    regression_count: int = 0
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    duration_seconds: float | None = None
    duration_label: str | None = None
    triggered_by_user_id: str | None = None
    engine_experiment_id: str | None = None
    baseline_run_id: str | None = None
    created_at: dt.datetime


class TestRunDetail(TestRunRead):
    """A run with the per-case results the compare view diffs."""

    cases: list[TestCaseResult] = Field(default_factory=list)
    unscored: int = Field(0, description="Cases the engine returned no judgement for")
    baseline_comparison: dict[str, Any] = Field(default_factory=dict)


class TestRunProgress(BaseModel):
    """Live progress of a suite run."""

    run_id: str
    run_ref: str
    suite_id: str
    status: TestRunStatus
    phase: str = Field(description="Preparing, Registering cases, Scoring, or Finished")
    total_cases: int = 0
    processed_cases: int = 0
    scored_cases: int = 0
    passed: int = 0
    failed: int = 0
    percent: float = 0.0
    is_terminal: bool = False
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    elapsed_seconds: float | None = None
    detail: str | None = None


class CaseDelta(BaseModel):
    """One case's movement between two runs."""

    id: str
    name: str | None = None
    baseline_status: CaseStatus | None = None
    candidate_status: CaseStatus | None = None
    baseline_score: float | None = None
    candidate_score: float | None = None
    change: str = Field(description="Regression, Fix, Unchanged, Added or Removed")
    is_regression: bool = False


class RunComparison(BaseModel):
    """Baseline against candidate, case by case."""

    baseline: TestRunRead
    candidate: TestRunRead
    pass_rate_delta: float | None = None
    regressions: int = 0
    fixes: int = 0
    added: int = 0
    removed: int = 0
    unchanged: int = 0
    verdict: str = Field(description="Improved, Regressed or Unchanged")
    cases: list[CaseDelta] = Field(default_factory=list)


class BaselineRead(BaseModel):
    """One row of the Baselines tab: what a suite would promote next."""

    suite_id: str
    suite_name: str
    baseline_run_id: str | None = None
    baseline: str | None = None
    baseline_pass_rate: float | None = None
    candidate_run_id: str | None = None
    candidate: str | None = None
    candidate_pass_rate: float | None = None
    pass_rate_delta: float | None = None
    regressions: int = 0
    promotable: bool = Field(
        description="True when a finished run newer than the baseline exists"
    )


class ScheduleRead(BaseModel):
    """One row of the Schedules tab. A schedule is a suite's cron binding."""

    id: str = Field(description="Same id as the suite the schedule belongs to")
    suite_id: str
    suite_name: str
    suite_type: SuiteType
    environment: str
    cron: str
    cadence: str = Field(description="Plain-English rendering of the cron expression")
    next_run_at: dt.datetime | None = None
    last_run_at: dt.datetime | None = None
    status: str = Field(description="Active while the suite is Active, otherwise Paused")
    case_count: int = 0
    owner_name: str | None = None


class EnvironmentBinding(BaseModel):
    """One row of the Environments tab."""

    environment: str
    suites: int = 0
    active_suites: int = 0
    runs_30d: int = 0
    pass_rate_30d: float | None = None
    failing_suites: int = 0
    last_full_pass_at: dt.datetime | None = Field(
        None, description="Most recent run in this environment with no failed case"
    )
    last_run_at: dt.datetime | None = None


class TestingSummary(BaseModel):
    """The six KPI cards, each with the movement against the previous window."""

    window_days: int = 30
    suites: int
    suites_delta: int = 0
    tests_executed: int = Field(description="Cases executed across all runs in the window")
    tests_executed_delta_percent: float | None = None
    pass_rate: float | None = Field(None, description="Cases passed / cases executed, percent")
    pass_rate_delta: float | None = Field(None, description="Percentage points")
    regressions_detected: int = 0
    regressions_delta_percent: float | None = None
    avg_duration_seconds: float | None = None
    avg_duration_label: str | None = None
    avg_duration_delta_percent: float | None = None
    flaky_tests: int = Field(0, description="Cases that flipped verdict within the window")
    flaky_tests_delta: int = 0
    running: int = 0


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestSuiteCreate(BaseModel):
    """Create a suite — the Create Test Suite modal's payload."""

    name: str = Field(min_length=1, max_length=160)
    suite_type: SuiteType = SuiteType.REGRESSION
    environment: str = Field(min_length=1, max_length=40)
    dataset: str = Field(min_length=1, max_length=160)
    status: SuiteStatus = SuiteStatus.ACTIVE
    agent_id: str | None = Field(None, max_length=36)
    schedule_cron: str | None = Field(None, max_length=120)
    owner_user_id: str | None = Field(None, max_length=36)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name", "environment", "dataset")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("schedule_cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else validate_cron(value)


class TestSuiteUpdate(BaseModel):
    """Partial update. Omitted fields are left alone."""

    name: str | None = Field(None, min_length=1, max_length=160)
    suite_type: SuiteType | None = None
    environment: str | None = Field(None, min_length=1, max_length=40)
    dataset: str | None = Field(None, min_length=1, max_length=160)
    status: SuiteStatus | None = None
    agent_id: str | None = Field(None, max_length=36)
    schedule_cron: str | None = Field(None, max_length=120)
    owner_user_id: str | None = Field(None, max_length=36)
    tags: list[str] | None = Field(None, max_length=20)
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name", "environment", "dataset")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("schedule_cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else validate_cron(value)


class TestRunRequest(BaseModel):
    """Start a run. The trigger records why it started."""

    trigger: RunTrigger = RunTrigger.MANUAL
    notes: str | None = Field(None, max_length=500)


class BaselinePromote(BaseModel):
    """Promote a run to the suite's baseline."""

    run_id: str | None = Field(
        None, description="Defaults to the suite's most recent finished run"
    )
    reason: str | None = Field(None, max_length=500)


class ScheduleCreate(BaseModel):
    """Bind a cron expression to a suite."""

    suite_id: str = Field(min_length=1, max_length=36)
    cron: str = Field(min_length=1, max_length=120)

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return validate_cron(value)


class ScheduleUpdate(BaseModel):
    """Change a schedule's cadence."""

    cron: str = Field(min_length=1, max_length=120)

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return validate_cron(value)
