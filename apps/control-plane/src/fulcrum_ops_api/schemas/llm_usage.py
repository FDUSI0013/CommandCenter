"""Wire contracts for the LLM Usage screen.

The screen answers three questions about the models a workspace's agents run
on: which models are in use, how much each is used, and which give the best
results. Everything in it is measured; this module only names the shapes.

Four rules hold for every field here:

* **The counting unit is the run.** A run is one agent execution -- one trace
  in the telemetry store, the same thing a row of Live Runs is. It is
  attributed to one model: the model recorded on the run (the ingest contract
  writes the model the run's last LLM span called), and, when the run recorded
  none, the model its agent is registered with. A run that calls two models is
  counted once, under the one it recorded. These are runs, not LLM calls.
* **A number that was not measured is null.** Tokens are null for a model none
  of whose runs recorded usage; a rating is null where nobody rated a run. A
  zero is a measured zero.
* **A figure read from a capped scan says so.** Per-model figures come from the
  same bounded scan Live Runs reads. ``scan`` reports how much was read and,
  when the cap was reached, ``measured_from`` the instant from which the window
  was read whole -- the stretch every per-model figure then covers, the same
  for every agent, so a busy agent's last hour is never set against a quiet
  agent's week.
* **An unpriced run is not a free one.** The store reports $0 when it has no
  price for a model; a run that used tokens at $0 is counted in
  ``unpriced_runs`` and keeps the model out of the cost-per-success figures.
* **"Best" is a measured leader, never a blend.** Each leader is the best model
  on one measure, among models with at least a stated number of samples, and
  carries the sample it was measured on. There is no composite score.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from pydantic import BaseModel, Field

from .runs import ScanInfo


class LlmUsageWindow(enum.StrEnum):
    """The windows the screen offers."""

    LAST_24H = "24h"
    LAST_7D = "7d"
    LAST_30D = "30d"

    @property
    def days(self) -> int:
        return _WINDOW_DAYS[self]

    @property
    def label(self) -> str:
        return _WINDOW_LABELS[self]


_WINDOW_DAYS: Final[dict[LlmUsageWindow, int]] = {
    LlmUsageWindow.LAST_24H: 1,
    LlmUsageWindow.LAST_7D: 7,
    LlmUsageWindow.LAST_30D: 30,
}

_WINDOW_LABELS: Final[dict[LlmUsageWindow, str]] = {
    LlmUsageWindow.LAST_24H: "Last 24 hours",
    LlmUsageWindow.LAST_7D: "Last 7 days",
    LlmUsageWindow.LAST_30D: "Last 30 days",
}


class LeaderMetric(enum.StrEnum):
    """The measures a model can lead on."""

    SUCCESS_RATE = "success_rate"
    FEEDBACK = "feedback_rating"
    COST_PER_SUCCESS = "cost_per_successful_run"
    LATENCY_P50 = "latency_p50"


# ---------------------------------------------------------------------------
# Per model
# ---------------------------------------------------------------------------


class LlmAgentRef(BaseModel):
    """One agent that ran on a model in the window."""

    id: str
    name: str
    runs: int = Field(description="This agent's runs attributed to the model")


class LlmModelUsage(BaseModel):
    """One model: how much it was used and how it did, over the scanned runs."""

    model: str | None = Field(
        description="The model name exactly as recorded. Null: no model was recorded "
        "on the run and its agent names none"
    )
    label: str = Field(description="What the console prints; 'Model not recorded' for null")
    color: str = Field(description="Colour key of this model's slice and line")
    providers: list[str] = Field(
        default_factory=list,
        description="Providers recorded on this model's runs. Empty: none recorded",
    )

    runs: int = Field(description="Runs attributed to this model (runs, not LLM calls)")
    share_percent: float | None = Field(
        None, description="This model's share of the scanned runs, 0-100"
    )
    runs_model_recorded: int = Field(0, description="Runs that recorded this model themselves")
    runs_model_from_agent: int = Field(
        0,
        description="Runs that recorded no model and were attributed to the model "
        "their agent is registered with",
    )

    finished_runs: int = Field(
        0, description="Runs with an outcome: Completed, Warned or Failed. Running runs are not"
    )
    successful_runs: int = Field(0, description="Completed or Warned runs")
    failed_runs: int = 0
    running_runs: int = 0
    success_rate: float | None = Field(
        None,
        description="successful_runs / finished_runs, 0-100. Its sample is finished_runs",
    )

    input_tokens: int | None = Field(None, description="Null when no run recorded usage")
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_runs: int = Field(0, description="Runs that recorded token usage")

    cost_usd: float | None = Field(None, description="Null when no run recorded a cost")
    cost_runs: int = Field(0, description="Runs that recorded a cost")
    unpriced_runs: int = Field(
        0,
        description=(
            "Runs that used tokens and recorded a cost of exactly $0: the store "
            "records zero when it has no price for a model. While this is above "
            "zero, cost_usd and cost_per_run are floors"
        ),
    )
    cost_per_run: float | None = Field(None, description="cost_usd / cost_runs")
    cost_per_successful_run: float | None = Field(
        None,
        description=(
            "The cost of every finished run -- failures included, since they are "
            "part of the price of a success -- divided by successful_runs. Null "
            "when a finished run recorded no cost or was unpriced, or nothing "
            "succeeded"
        ),
    )

    latency_p50_seconds: float | None = Field(
        None, description="Median run duration, computed from the runs themselves"
    )
    latency_p90_seconds: float | None = None
    latency_samples: int = Field(0, description="Runs with a recorded duration")

    feedback_avg_rating: float | None = Field(
        None,
        description=(
            "Mean 1-5 rating of the feedback records (Feedback & Quality Loop) that "
            "name one of this model's runs. Null when none does"
        ),
    )
    feedback_ratings: int = Field(0, description="Ratings the average is taken over")

    policy_flagged_runs: int = Field(
        0, description="Runs whose recorded enforcement verdict is Warned or Blocked"
    )
    policy_violations: int = Field(
        0,
        description=(
            "Violation records (the ones the Policy Center lists) whose trace id is "
            "one of this model's runs"
        ),
    )
    human_escalations: int = Field(
        0,
        description="Of those, the records whose action put a human in the loop",
    )
    guardrail_triggers: int = Field(
        0, description="Guardrail events whose trace id is one of this model's runs"
    )
    guardrail_blocks: int = Field(0, description="Of those, the ones that blocked")
    agent_handoffs: int = Field(
        0, description="Runs the agent itself reported as escalated to a person"
    )

    agents: list[LlmAgentRef] = Field(
        default_factory=list, description="Agents that ran on this model, most runs first"
    )


# ---------------------------------------------------------------------------
# Best results
# ---------------------------------------------------------------------------


class LlmLeader(BaseModel):
    """The measured leader on one measure, among models with enough samples."""

    metric: LeaderMetric
    label: str = Field(description="e.g. 'Highest success rate'")
    better: str = Field(description="'higher' or 'lower'")
    unit: str = Field(description="percent | rating | usd | seconds")
    model: str | None = Field(
        None, description="The leading model. Null when no model has the minimum sample"
    )
    model_label: str | None = None
    value: float | None = None
    sample: int | None = Field(None, description="What the leader's value was measured on")
    sample_unit: str = Field(description="e.g. 'finished runs' or 'ratings'")
    min_sample: int = Field(description="The fewest samples a model needs to be ranked")
    eligible_models: int = Field(description="Models that had the minimum sample")
    tied_with: list[str] = Field(
        default_factory=list, description="Other eligible models with exactly the same value"
    )
    note: str | None = Field(
        None, description="Why there is no leader, or what was left out of the ranking"
    )


# ---------------------------------------------------------------------------
# Per agent
# ---------------------------------------------------------------------------


class LlmAgentModelRow(BaseModel):
    """One agent on one model: which agents use which model."""

    agent_id: str
    agent_name: str
    environment: str | None = None
    configured_model: str | None = Field(None, description="The model the agent is registered with")
    model: str | None
    model_label: str
    matches_configuration: bool | None = Field(
        None,
        description="Whether the model run is the one registered. Null when either is unknown",
    )
    runs: int
    share_of_agent_percent: float | None = Field(
        None, description="This model's share of the agent's scanned runs"
    )
    finished_runs: int = 0
    success_rate: float | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


class LlmSeriesBucket(BaseModel):
    """One bucket of the series grid."""

    start: dt.datetime
    label: str
    partial: bool = Field(
        False,
        description=(
            "True when the scan was capped and did not read this bucket whole: its "
            "figures are a floor, not a measurement of the bucket"
        ),
    )


class LlmModelSeries(BaseModel):
    """One model's runs, tokens and cost per bucket."""

    model: str | None
    label: str
    color: str
    runs: list[int] = Field(default_factory=list)
    tokens: list[int | None] = Field(
        default_factory=list,
        description="Null in a bucket where the model ran and no run recorded usage",
    )
    cost: list[float | None] = Field(
        default_factory=list,
        description="Null in a bucket where the model ran and no run recorded a cost",
    )


class LlmUsageSeries(BaseModel):
    """Per-model runs, tokens and cost over the window: hourly for 24h, else daily."""

    interval: str = Field(description="'hourly' or 'daily'")
    buckets: list[LlmSeriesBucket] = Field(default_factory=list)
    models: list[LlmModelSeries] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Totals and the report
# ---------------------------------------------------------------------------


class LlmUsageTotals(BaseModel):
    """The KPI row, over every scanned run."""

    models_in_use: int = Field(description="Distinct recorded or registered models")
    runs: int = Field(
        description=(
            "Runs the per-model figures cover (runs, not LLM calls): every run in the "
            "window, or the runs from measured_from on when the scan was capped"
        )
    )
    runs_in_window: int | None = Field(
        None,
        description=(
            "Runs the telemetry store counts in the window for the agents in scope, "
            "uncapped. Null when that count could not be read"
        ),
    )
    coverage_percent: float | None = Field(
        None, description="runs / runs_in_window, 0-100: how much of the window the figures cover"
    )
    runs_model_not_recorded: int = Field(
        0, description="Runs with no recorded model whose agent names none either"
    )
    runs_model_from_agent: int = Field(
        0, description="Runs attributed to their agent's registered model"
    )

    finished_runs: int = 0
    successful_runs: int = 0
    success_rate: float | None = None

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    unpriced_runs: int = Field(
        0, description="Runs that used tokens at a recorded cost of $0 (no price)"
    )
    cost_per_successful_run: float | None = None

    agents_in_scope: int = Field(0, description="Provisioned agents the filters select")
    agents_with_runs: int = Field(0, description="Of those, the ones with a scanned run")

    feedback_ratings: int = 0
    feedback_avg_rating: float | None = None

    policy_violations_attributed: int = Field(
        0, description="Violation records attributed to a scanned run, and so to a model"
    )
    policy_violations_in_window: int = Field(
        0,
        description=(
            "Violation records that occurred in the window: the whole workspace, as the "
            "Policy Center counts them, or the selected agents when a filter is set. "
            "Records for blocked items, offline checks and runs outside the scan cannot "
            "be attributed to a model"
        ),
    )
    human_escalations_attributed: int = 0
    human_escalations_in_window: int = 0
    guardrail_triggers_attributed: int = 0
    guardrail_triggers_in_window: int = Field(
        0, description="Guardrail events in the window, scoped like the violations"
    )


class LlmUsageReport(BaseModel):
    """Everything the LLM Usage screen shows, from one scan of the window."""

    window: LlmUsageWindow
    window_label: str
    period_start: dt.datetime
    period_end: dt.datetime
    agent_ids: list[str] = Field(
        default_factory=list, description="The agent filter as applied; empty for all"
    )
    environment: str | None = Field(None, description="The environment filter as applied")

    scan: ScanInfo
    scan_capped: bool = Field(
        description=(
            "True when the window held more runs than one scan reads. Every per-model "
            "figure is then computed from the newest runs, not all of them"
        )
    )
    measured_from: dt.datetime | None = Field(
        None,
        description=(
            "Set when the scan was capped and the instant from which it read the window "
            "whole is known: every per-model figure, the totals folded from runs and the "
            "series cover the runs from this instant to period_end, the same stretch for "
            "every agent. Null when the whole window was read, or when the edge is "
            "unknown (agents left unread), in which case the figures are each agent's "
            "newest runs"
        ),
    )

    totals: LlmUsageTotals
    models: list[LlmModelUsage] = Field(
        default_factory=list, description="Most runs first; the unrecorded model last"
    )
    leaders: list[LlmLeader] = Field(default_factory=list)
    leader_min_runs: int = Field(
        description="Fewest runs a model needs to be ranked on success rate, cost or latency"
    )
    leader_min_ratings: int = Field(
        description="Fewest ratings a model needs to be ranked on feedback"
    )
    agents: list[LlmAgentModelRow] = Field(
        default_factory=list, description="Agent by model, most runs first within each agent"
    )
    series: LlmUsageSeries
