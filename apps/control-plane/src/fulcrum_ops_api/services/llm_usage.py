"""LLM Usage: which models the workspace's agents run on, how much, and how well.

**Where every number comes from.** The per-model figures are folded from the
same bounded scan of the telemetry store that Live Runs reads
(:func:`services.runs._scan`): the same projects, the same row cap, the same
activity skip and the same shared per-project reads, so the two screens are
views of one read rather than two estimates. A run is attributed to one model
exactly as the Live Runs Model column attributes it -- the model recorded on the
run, else the model its agent is registered with -- so the counting unit is the
run, never the LLM call.

Beside the scan, two measurements are made that the scan cannot:

* the telemetry store's own count of runs in the window for the agents in
  scope (the statistics walk the Metrics screen shares), uncapped -- so a capped
  scan can say what share of the window it covers;
* the governance records that name a run -- violation records, guardrail events
  and rated feedback -- matched to the scanned runs by trace id in our own
  database. One statement per chunk of ids; never one read per run, and never a
  call to the telemetry store per run.

**What this module does not do.** It invents no figure: a model none of whose
runs recorded usage has null tokens, a model nobody rated has a null rating, and
"best results" is a measured leader per measure among models with a stated
minimum sample -- never a composite score.

**Tenancy.** Every project read comes from an ``agents`` row loaded with the
caller's ``workspace_id``; every governance query carries it too. A key bound to
one agent reads that agent and no other, as it does on Live Runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..core.errors import TelemetryBackendUnavailable
from ..engine import EngineError
from ..engine import deadline as engine_deadline
from ..models.governance import PolicyViolation
from ..models.quality import FeedbackItem, GuardrailAction, GuardrailEvent
from ..models.registry import Agent, EnvironmentType
from ..schemas.llm_usage import (
    LeaderMetric,
    LlmAgentModelRow,
    LlmAgentRef,
    LlmLeader,
    LlmModelSeries,
    LlmModelUsage,
    LlmSeriesBucket,
    LlmUsageReport,
    LlmUsageSeries,
    LlmUsageTotals,
    LlmUsageWindow,
)
from ..schemas.metrics import MetricInterval
from ..schemas.runs import RunPolicy, RunStatus, ScanInfo, as_metadata
from . import metrics, runs, telemetry_cache

# One definition of "a violation in the window" and of "a violation that put a
# human in the loop", shared with the Policy Center, the Agent Registry, Agent
# Detail and Live Runs -- so the in-window counts here are theirs, not a copy.
from .agents import ESCALATING_ACTIONS, is_human_escalation, violations_in_window

#: A model is ranked on success rate, cost per successful run or latency only
#: when it has at least this many runs measured on that axis. Below it, one
#: lucky run is a 100% success rate.
LEADER_MIN_RUNS: Final[int] = 20

#: The same floor for the feedback leader, counted in ratings.
LEADER_MIN_RATINGS: Final[int] = 10

#: Trace ids per governance lookup. Kept well under every database's bound on
#: bound parameters.
JOIN_CHUNK: Final[int] = 500

#: A governance record about a run is written when the run is, or after it. The
#: lookups are bounded below by the window start less this margin -- clock skew
#: between a reporter and this server -- so they read an index range rather than
#: every record the workspace ever wrote.
JOIN_LOOKBACK: Final[dt.timedelta] = dt.timedelta(hours=1)

#: What the console prints for a run that recorded no model on an agent that
#: names none.
NOT_RECORDED_LABEL: Final[str] = "Model not recorded"

#: Slice and line colours, in order of use. Grey is kept for "not recorded".
PALETTE: Final[tuple[str, ...]] = (
    "purple",
    "blue",
    "cyan",
    "amber",
    "green",
    "orange",
    "pink",
    "red",
)
NOT_RECORDED_COLOR: Final[str] = "gray"

#: Metadata keys a run may record its model's provider under.
_PROVIDER_KEYS: Final[tuple[str, ...]] = ("provider", "model_provider", "llm_provider")

_USAGE_KEYS: Final[tuple[str, ...]] = (
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "total_tokens",
)

_FINISHED: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.COMPLETED, RunStatus.WARNED, RunStatus.FAILED}
)
_SUCCESSFUL: Final[frozenset[RunStatus]] = frozenset({RunStatus.COMPLETED, RunStatus.WARNED})


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _Scope:
    """The agents one request may read, resolved once from the database.

    ``agents`` is what the scan reads: most recently used first, capped as the
    run screens cap it. ``project_ids`` is every matching project, for the
    uncapped count. ``conditions`` select the same agents again, for the
    governance counts; ``narrowed`` says whether any filter applied at all.
    """

    agents: list[Agent]
    total: int
    project_ids: list[str]
    conditions: tuple[ColumnElement[bool], ...]
    narrowed: bool
    empty: bool = False


async def _resolve_scope(
    session: AsyncSession,
    principal: Principal,
    agent_ids: Sequence[str] | None,
    environment: EnvironmentType | None,
) -> _Scope:
    wanted = list(dict.fromkeys(agent_id for agent_id in (agent_ids or []) if agent_id))
    bound = principal.api_key_agent_id
    conditions: list[ColumnElement[bool]] = [
        Agent.workspace_id == principal.workspace_id,
        Agent.engine_project_id.is_not(None),
    ]
    if bound:
        # A key bound to one agent reads that agent and no others -- asked for
        # a neighbour, it is answered as if the neighbour did not exist.
        if wanted and bound not in wanted:
            return _Scope(
                agents=[], total=0, project_ids=[], conditions=(), narrowed=True, empty=True
            )
        conditions.append(Agent.id == bound)
    elif wanted:
        conditions.append(Agent.id.in_(wanted))
    if environment is not None:
        conditions.append(Agent.environment == environment.value)

    ordered = (
        select(Agent)
        .where(*conditions)
        .order_by(
            # NULLS LAST, spelled portably -- the order the run screens scan in.
            case((Agent.last_used_at.is_(None), 1), else_=0),
            Agent.last_used_at.desc(),
            Agent.name.asc(),
        )
        .limit(runs.MAX_SCAN_AGENTS)
    )
    agents = list((await session.execute(ordered)).scalars().all())
    project_ids = [
        str(project_id)
        for project_id in (
            await session.execute(select(Agent.engine_project_id).where(*conditions))
        ).scalars()
        if project_id
    ]
    return _Scope(
        agents=agents,
        total=len(project_ids),
        project_ids=project_ids,
        conditions=tuple(conditions),
        narrowed=bool(bound or wanted or environment is not None),
    )


# --------------------------------------------------------------------------- #
# The telemetry read
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True, slots=True)
class _RunFact:
    """What one scanned run contributes, and nothing it did not record."""

    run_id: str
    agent_id: str
    agent_name: str
    model: str | None
    model_recorded: bool
    provider: str | None
    status: RunStatus
    started: dt.datetime
    duration: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost: float | None
    policy: RunPolicy
    handoff: bool


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _usage_recorded(trace: dict[str, Any]) -> bool:
    usage = trace.get("usage")
    if not isinstance(usage, dict):
        return False
    return any(runs._number(usage.get(key)) is not None for key in _USAGE_KEYS)


def _fact(trace: dict[str, Any], agent: Agent, principal: Principal) -> _RunFact:
    """One trace as the Live Runs row maps it, plus what the row leaves out."""
    scanned = runs._scan_run(trace, agent, principal)
    run = scanned.run
    meta = as_metadata(trace.get("metadata"))
    usage = _usage_recorded(trace)
    cost_recorded = runs._number(trace.get("total_estimated_cost")) is not None
    return _RunFact(
        run_id=run.id,
        agent_id=agent.id,
        agent_name=agent.name,
        # The Model column's own attribution, so this screen and Live Runs
        # cannot put one run under two models.
        model=run.model,
        # Read exactly as the Model column reads it: a blank name is no name,
        # so a run that recorded only whitespace is attributed through its
        # agent's registration and counted as such, not as recorded.
        model_recorded=bool(str(meta.get("model") or "").strip()),
        provider=next(
            (text for text in (_text(meta.get(key)) for key in _PROVIDER_KEYS) if text),
            None,
        ),
        status=run.status,
        started=run.occurred_at,
        duration=run.duration_seconds,
        input_tokens=run.input_tokens if usage else None,
        output_tokens=run.output_tokens if usage else None,
        total_tokens=run.tokens if usage else None,
        cost=run.cost if cost_recorded else None,
        policy=run.policy,
        handoff=scanned.escalated,
    )


@dataclasses.dataclass(frozen=True)
class _Measured:
    """One window's telemetry, as every request asking the same question shares it."""

    facts: tuple[_RunFact, ...]
    scan: ScanInfo
    runs_in_window: int | None


async def _store_count(scope: _Scope, since: dt.datetime, until: dt.datetime) -> int | None:
    """The store's own count of the window's runs for every project in scope.

    It is the denominator of the coverage figure and nothing else, so a failure
    here costs the screen that one figure -- it is reported as not measured --
    rather than the whole answer.
    """
    if not scope.project_ids:
        return 0
    try:
        async with engine_deadline(what="the LLM usage run count"):
            counts = await metrics.project_run_counts(
                runs._client(), set(scope.project_ids), since, until
            )
    except (TelemetryBackendUnavailable, EngineError):
        return None
    return sum(counts.values())


async def _read_window(
    scope: _Scope, principal: Principal, since: dt.datetime, until: dt.datetime
) -> _Measured:
    """Scan the window across the scope and map every run in it.

    Touches the telemetry store only; no database work happens in here, which
    is what lets requests share it.
    """

    async def scan() -> tuple[list[tuple[Agent, list[dict[str, Any]]]], bool, dt.datetime | None]:
        if not scope.agents:
            return [], False, None
        return await runs._scan(
            runs._client(),
            scope.agents,
            since=since,
            until=until,
            budget=runs.MAX_SCAN_TRACES,
        )

    # The count runs beside the scan. It answers for itself (a failure is "not
    # measured"), so only the scan can fail the read -- and when it does, the
    # count is stopped rather than left asking a store nobody is waiting on.
    counting = asyncio.ensure_future(_store_count(scope, since, until))
    try:
        batches, truncated, covered_from = await scan()
    except BaseException:
        counting.cancel()
        raise
    in_window = await counting

    facts = tuple(
        _fact(trace, agent, principal)
        for agent, traces in batches
        for trace in traces
        if runs._in_window(trace, since, until)
    )
    # Projects left unread have no edge to report; otherwise the edge is stated
    # inside the window that was asked about, exactly as Live Runs states it.
    unread = scope.total > len(scope.agents)
    if covered_from is not None:
        covered_from = None if unread else min(max(covered_from, since), until)
    info = ScanInfo(
        runs_scanned=len(facts),
        agents_scanned=len(scope.agents),
        agents_total=scope.total,
        truncated=truncated or unread,
        window_start=since,
        window_end=until,
        covered_from=covered_from,
    )
    return _Measured(facts=facts, scan=info, runs_in_window=in_window)


async def _measured(
    scope: _Scope, principal: Principal, since: dt.datetime, until: dt.datetime
) -> _Measured:
    """The window's telemetry, shared by everyone asking the same question.

    Held in the run screens' summary memory: short-lived, and forgotten the
    moment this process writes to a project.
    """
    key = (
        "llm-usage",
        principal.workspace_id,
        since,
        until,
        tuple(sorted(str(agent.id) for agent in scope.agents)),
        # Every project the uncapped count reads, not just the scanned ones:
        # two filters can share their newest hundred agents and differ beyond.
        tuple(sorted(scope.project_ids)),
    )
    return await telemetry_cache.summaries.get(
        key, lambda: _read_window(scope, principal, since, until)
    )


# --------------------------------------------------------------------------- #
# Governance records that name a run
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class _Attributed:
    """Governance records keyed by the trace id they name."""

    violations: dict[str, int] = dataclasses.field(default_factory=dict)
    escalations: dict[str, int] = dataclasses.field(default_factory=dict)
    guardrail_triggers: dict[str, int] = dataclasses.field(default_factory=dict)
    guardrail_blocks: dict[str, int] = dataclasses.field(default_factory=dict)
    ratings: dict[str, list[int]] = dataclasses.field(default_factory=dict)


def _chunks(values: Sequence[str], size: int = JOIN_CHUNK) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


async def _attributions(
    session: AsyncSession, principal: Principal, trace_ids: Sequence[str], since: dt.datetime
) -> _Attributed:
    """Violation records, guardrail events and ratings that name a scanned run."""
    found = _Attributed()
    ids = sorted(set(trace_ids))
    if not ids:
        return found
    floor = since - JOIN_LOOKBACK
    for chunk in _chunks(ids):
        rows = await session.execute(
            select(PolicyViolation.trace_id, PolicyViolation.action_taken).where(
                PolicyViolation.workspace_id == principal.workspace_id,
                PolicyViolation.occurred_at >= floor,
                PolicyViolation.trace_id.in_(chunk),
            )
        )
        for trace_id, action in rows.all():
            found.violations[trace_id] = found.violations.get(trace_id, 0) + 1
            if action in ESCALATING_ACTIONS:
                found.escalations[trace_id] = found.escalations.get(trace_id, 0) + 1

        rows = await session.execute(
            select(GuardrailEvent.trace_id, GuardrailEvent.action_taken).where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.occurred_at >= floor,
                GuardrailEvent.trace_id.in_(chunk),
            )
        )
        for trace_id, action in rows.all():
            found.guardrail_triggers[trace_id] = found.guardrail_triggers.get(trace_id, 0) + 1
            if action == GuardrailAction.BLOCK.value:
                found.guardrail_blocks[trace_id] = found.guardrail_blocks.get(trace_id, 0) + 1

        rows = await session.execute(
            select(FeedbackItem.trace_id, FeedbackItem.rating).where(
                FeedbackItem.workspace_id == principal.workspace_id,
                FeedbackItem.submitted_at >= floor,
                FeedbackItem.trace_id.in_(chunk),
                FeedbackItem.rating.is_not(None),
            )
        )
        for trace_id, rating in rows.all():
            found.ratings.setdefault(trace_id, []).append(int(rating))
    return found


@dataclasses.dataclass(frozen=True)
class _WindowCounts:
    violations: int = 0
    escalations: int = 0
    guardrail_triggers: int = 0


async def _window_counts(
    session: AsyncSession,
    principal: Principal,
    scope: _Scope,
    since: dt.datetime,
    until: dt.datetime,
) -> _WindowCounts:
    """Records that occurred in the window, attributed to a run or not.

    Unfiltered, the whole workspace, selected with the one definition of "a
    violation in the window" and "a human escalation" every governance screen
    counts with (:func:`~.agents.violations_in_window`,
    :func:`~.agents.is_human_escalation`) -- so over 30 days these are the
    Policy Center's cards. Filtered, the records naming one of the selected
    agents: the narrower question adds its own clause beside the definition.
    """
    if scope.empty:
        return _WindowCounts()

    def agents_clause(column: Any) -> list[ColumnElement[bool]]:
        if not scope.narrowed:
            return []
        return [column.in_(select(Agent.id).where(*scope.conditions))]

    violations, escalations = (
        await session.execute(
            select(
                func.count(PolicyViolation.id),
                func.coalesce(func.sum(case((is_human_escalation(), 1), else_=0)), 0),
            ).where(
                *violations_in_window(principal.workspace_id, since, until),
                *agents_clause(PolicyViolation.agent_id),
            )
        )
    ).one()
    triggers = (
        await session.execute(
            select(func.count(GuardrailEvent.id)).where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.occurred_at >= since,
                GuardrailEvent.occurred_at < until,
                *agents_clause(GuardrailEvent.agent_id),
            )
        )
    ).scalar_one()
    return _WindowCounts(
        violations=int(violations or 0),
        escalations=int(escalations or 0),
        guardrail_triggers=int(triggers or 0),
    )


# --------------------------------------------------------------------------- #
# Folding
# --------------------------------------------------------------------------- #


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear-interpolated percentile of the values; ``None`` for none at all."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return round(ordered[low], 3)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


@dataclasses.dataclass
class _Tally:
    """Counters folded over one model's runs (or one agent's runs on it)."""

    runs: int = 0
    recorded: int = 0
    from_agent: int = 0
    completed_or_warned: int = 0
    failed: int = 0
    running: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_runs: int = 0
    cost: float = 0.0
    cost_runs: int = 0
    finished_cost: float = 0.0
    finished_cost_runs: int = 0
    unpriced: int = 0
    finished_unpriced: int = 0
    durations: list[float] = dataclasses.field(default_factory=list)
    ratings: list[int] = dataclasses.field(default_factory=list)
    policy_flagged: int = 0
    violations: int = 0
    escalations: int = 0
    guardrail_triggers: int = 0
    guardrail_blocks: int = 0
    handoffs: int = 0
    providers: dict[str, int] = dataclasses.field(default_factory=dict)
    agents: dict[str, list[Any]] = dataclasses.field(default_factory=dict)

    @property
    def finished(self) -> int:
        return self.completed_or_warned + self.failed

    def add(self, fact: _RunFact, attributed: _Attributed) -> None:
        self.runs += 1
        if fact.model_recorded:
            self.recorded += 1
        elif fact.model is not None:
            self.from_agent += 1
        if fact.status in _SUCCESSFUL:
            self.completed_or_warned += 1
        elif fact.status is RunStatus.FAILED:
            self.failed += 1
        else:
            self.running += 1
        if fact.total_tokens is not None:
            self.token_runs += 1
            self.input_tokens += fact.input_tokens or 0
            self.output_tokens += fact.output_tokens or 0
            self.total_tokens += fact.total_tokens
        if fact.cost is not None:
            self.cost_runs += 1
            self.cost += fact.cost
            if fact.status in _FINISHED:
                self.finished_cost_runs += 1
                self.finished_cost += fact.cost
            if _unpriced(fact):
                self.unpriced += 1
                if fact.status in _FINISHED:
                    self.finished_unpriced += 1
        if fact.duration is not None:
            self.durations.append(fact.duration)
        self.ratings.extend(attributed.ratings.get(fact.run_id, ()))
        if fact.policy is not RunPolicy.ALLOWED:
            self.policy_flagged += 1
        self.violations += attributed.violations.get(fact.run_id, 0)
        self.escalations += attributed.escalations.get(fact.run_id, 0)
        self.guardrail_triggers += attributed.guardrail_triggers.get(fact.run_id, 0)
        self.guardrail_blocks += attributed.guardrail_blocks.get(fact.run_id, 0)
        self.handoffs += int(fact.handoff)
        if fact.provider:
            self.providers[fact.provider] = self.providers.get(fact.provider, 0) + 1
        entry = self.agents.setdefault(fact.agent_id, [fact.agent_name, 0])
        entry[1] += 1

    @property
    def success_rate(self) -> float | None:
        return _rate(self.completed_or_warned, self.finished)

    @property
    def cost_usd(self) -> float | None:
        return round(self.cost, 6) if self.cost_runs else None

    @property
    def cost_per_successful_run(self) -> float | None:
        # Every finished run's cost has to be known, or the numerator is a floor
        # and the quotient flatters the model. A run that used tokens at $0 was
        # not free: the store had no price for it.
        if (
            not self.completed_or_warned
            or self.finished_cost_runs < self.finished
            or self.finished_unpriced
        ):
            return None
        return round(self.finished_cost / self.completed_or_warned, 6)

    @property
    def avg_rating(self) -> float | None:
        return round(sum(self.ratings) / len(self.ratings), 2) if self.ratings else None


def _unpriced(fact: _RunFact) -> bool:
    """Tokens used, and a cost of exactly $0: the store had no price for the run.

    The store reports $0 rather than nothing when it cannot price a model, so a
    zero beside real token usage is a missing price, not a free run. A zero on
    a run that recorded no usage cannot be told apart from a free one and is
    taken as recorded.
    """
    return fact.cost == 0 and bool(fact.total_tokens)


def _label(model: str | None) -> str:
    return model if model is not None else NOT_RECORDED_LABEL


def _model_order(item: tuple[str | None, _Tally]) -> tuple[bool, int, str]:
    model, tally = item
    return (model is None, -tally.runs, (model or "").lower())


def _model_row(model: str | None, tally: _Tally, total_runs: int, color: str) -> LlmModelUsage:
    return LlmModelUsage(
        model=model,
        label=_label(model),
        color=color,
        providers=sorted(tally.providers, key=lambda name: (-tally.providers[name], name)),
        runs=tally.runs,
        share_percent=_rate(tally.runs, total_runs),
        runs_model_recorded=tally.recorded,
        runs_model_from_agent=tally.from_agent,
        finished_runs=tally.finished,
        successful_runs=tally.completed_or_warned,
        failed_runs=tally.failed,
        running_runs=tally.running,
        success_rate=tally.success_rate,
        input_tokens=tally.input_tokens if tally.token_runs else None,
        output_tokens=tally.output_tokens if tally.token_runs else None,
        total_tokens=tally.total_tokens if tally.token_runs else None,
        token_runs=tally.token_runs,
        cost_usd=tally.cost_usd,
        cost_runs=tally.cost_runs,
        unpriced_runs=tally.unpriced,
        cost_per_run=round(tally.cost / tally.cost_runs, 6) if tally.cost_runs else None,
        cost_per_successful_run=tally.cost_per_successful_run,
        latency_p50_seconds=percentile(tally.durations, 0.5),
        latency_p90_seconds=percentile(tally.durations, 0.9),
        latency_samples=len(tally.durations),
        feedback_avg_rating=tally.avg_rating,
        feedback_ratings=len(tally.ratings),
        policy_flagged_runs=tally.policy_flagged,
        policy_violations=tally.violations,
        human_escalations=tally.escalations,
        guardrail_triggers=tally.guardrail_triggers,
        guardrail_blocks=tally.guardrail_blocks,
        agent_handoffs=tally.handoffs,
        agents=[
            LlmAgentRef(id=agent_id, name=name, runs=count)
            for agent_id, (name, count) in sorted(
                tally.agents.items(), key=lambda item: (-item[1][1], item[1][0].lower())
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Best results
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _LeaderRule:
    metric: LeaderMetric
    label: str
    better: str
    unit: str
    sample_unit: str
    min_sample: int
    value: Any  # (LlmModelUsage) -> float | None
    sample: Any  # (LlmModelUsage) -> int
    #: Why a model with the minimum sample can still have no value to rank.
    unmeasured_reason: str = "the measure was not recorded for them"


_LEADER_RULES: Final[tuple[_LeaderRule, ...]] = (
    _LeaderRule(
        metric=LeaderMetric.SUCCESS_RATE,
        label="Highest success rate",
        better="higher",
        unit="percent",
        sample_unit="finished runs",
        min_sample=LEADER_MIN_RUNS,
        value=lambda row: row.successful_runs / row.finished_runs if row.finished_runs else None,
        sample=lambda row: row.finished_runs,
    ),
    _LeaderRule(
        metric=LeaderMetric.FEEDBACK,
        label="Highest feedback rating",
        better="higher",
        unit="rating",
        sample_unit="ratings",
        min_sample=LEADER_MIN_RATINGS,
        value=lambda row: row.feedback_avg_rating,
        sample=lambda row: row.feedback_ratings,
    ),
    _LeaderRule(
        metric=LeaderMetric.COST_PER_SUCCESS,
        label="Lowest cost per successful run",
        better="lower",
        unit="usd",
        sample_unit="successful runs",
        min_sample=LEADER_MIN_RUNS,
        value=lambda row: row.cost_per_successful_run,
        sample=lambda row: row.successful_runs,
        unmeasured_reason=(
            "some of their finished runs recorded no cost, or used tokens the store "
            "had no price for"
        ),
    ),
    _LeaderRule(
        metric=LeaderMetric.LATENCY_P50,
        label="Lowest p50 latency",
        better="lower",
        unit="seconds",
        sample_unit="timed runs",
        min_sample=LEADER_MIN_RUNS,
        value=lambda row: row.latency_p50_seconds,
        sample=lambda row: row.latency_samples,
    ),
)


def _published(rule: _LeaderRule, row: LlmModelUsage) -> float | None:
    """The leader's value as the table prints it."""
    if rule.metric is LeaderMetric.SUCCESS_RATE:
        return row.success_rate
    return rule.value(row)


def leaders(models: Sequence[LlmModelUsage]) -> list[LlmLeader]:
    """The best model on each measure, among models with the minimum sample.

    A run with no recorded model is not a model and is never ranked. A cost of
    exactly zero is not ranked either: the store records zero when it holds no
    price for a model, and "free" would win every time.
    """
    out: list[LlmLeader] = []
    named = [row for row in models if row.model is not None]
    for rule in _LEADER_RULES:
        eligible: list[tuple[float, LlmModelUsage]] = []
        unpriced = 0
        # Enough samples, but the measure itself was not taken: a model whose
        # finished runs did not all record a cost has no cost per success.
        # Said apart from "too few", which would be a different -- and false --
        # reason for it not to be ranked.
        unmeasured = 0
        for row in named:
            if rule.sample(row) < rule.min_sample:
                continue
            value = rule.value(row)
            if value is None:
                unmeasured += 1
                continue
            if rule.metric is LeaderMetric.COST_PER_SUCCESS and value == 0:
                unpriced += 1
                continue
            eligible.append((value, row))
        base = dict(
            metric=rule.metric,
            label=rule.label,
            better=rule.better,
            unit=rule.unit,
            sample_unit=rule.sample_unit,
            min_sample=rule.min_sample,
            eligible_models=len(eligible),
        )
        notes: list[str] = []
        if unmeasured:
            notes.append(
                f"{unmeasured} model(s) with at least {rule.min_sample} {rule.sample_unit} "
                f"were not ranked: {rule.unmeasured_reason}."
            )
        if unpriced:
            notes.append(
                f"{unpriced} model(s) with a recorded cost of $0 were not ranked: the store "
                "records zero when it has no price for a model."
            )
        note = " ".join(notes) or None
        if not eligible:
            out.append(
                LlmLeader(
                    **base,
                    note=note
                    or (
                        f"No model has at least {rule.min_sample} {rule.sample_unit} "
                        "in this window."
                    ),
                )
            )
            continue
        pick = max if rule.better == "higher" else min
        best_value = pick(value for value, _ in eligible)
        winners = sorted(
            (row for value, row in eligible if value == best_value),
            key=lambda row: (-rule.sample(row), (row.model or "").lower()),
        )
        leader = winners[0]
        out.append(
            LlmLeader(
                **base,
                model=leader.model,
                model_label=leader.label,
                value=_published(rule, leader),
                sample=rule.sample(leader),
                tied_with=[row.label for row in winners[1:]],
                note=note,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #


def _series(
    facts: Sequence[_RunFact],
    rows: Sequence[LlmModelUsage],
    scan: ScanInfo,
    window: LlmUsageWindow,
    since: dt.datetime,
    until: dt.datetime,
) -> LlmUsageSeries:
    interval = MetricInterval.HOURLY if window is LlmUsageWindow.LAST_24H else MetricInterval.DAILY
    starts = metrics.bucket_starts(since, until, interval)
    index = {start: position for position, start in enumerate(starts)}

    def position_of(moment: dt.datetime) -> int:
        floored = metrics.floor_to_bucket(moment, interval, starts[0])
        if floored < starts[0]:
            return 0
        return index.get(floored, len(starts) - 1)

    size = len(starts)
    per_model: dict[str | None, dict[str, list[Any]]] = {
        row.model: {
            "runs": [0] * size,
            "tokens": [0] * size,
            "token_runs": [0] * size,
            "cost": [0.0] * size,
            "cost_runs": [0] * size,
        }
        for row in rows
    }
    for fact in facts:
        lines = per_model.get(fact.model)
        if lines is None:
            continue
        at = position_of(fact.started)
        lines["runs"][at] += 1
        if fact.total_tokens is not None:
            lines["tokens"][at] += fact.total_tokens
            lines["token_runs"][at] += 1
        if fact.cost is not None:
            lines["cost"][at] += fact.cost
            lines["cost_runs"][at] += 1

    def partial(start: dt.datetime) -> bool:
        if not scan.truncated:
            return False
        if scan.covered_from is None:
            return True
        return start < scan.covered_from

    models_out: list[LlmModelSeries] = []
    for row in rows:
        lines = per_model[row.model]
        models_out.append(
            LlmModelSeries(
                model=row.model,
                label=row.label,
                color=row.color,
                runs=lines["runs"],
                # A bucket the model did not run in is a measured zero; one it ran
                # in without recording usage is not measured.
                tokens=[
                    lines["tokens"][i] if lines["token_runs"][i] or not lines["runs"][i] else None
                    for i in range(size)
                ],
                cost=[
                    round(lines["cost"][i], 6)
                    if lines["cost_runs"][i] or not lines["runs"][i]
                    else None
                    for i in range(size)
                ],
            )
        )
    return LlmUsageSeries(
        interval=interval.value,
        buckets=[
            LlmSeriesBucket(
                start=start, label=metrics.bucket_label(start, interval), partial=partial(start)
            )
            for start in starts
        ],
        models=models_out,
    )


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _agent_rows(
    facts: Sequence[_RunFact], scope: _Scope, attributed: _Attributed
) -> list[LlmAgentModelRow]:
    by_agent: dict[str, dict[str | None, _Tally]] = {}
    for fact in facts:
        by_agent.setdefault(fact.agent_id, {}).setdefault(fact.model, _Tally()).add(
            fact, attributed
        )
    agents = {agent.id: agent for agent in scope.agents}
    out: list[LlmAgentModelRow] = []
    for agent_id, tallies in by_agent.items():
        agent = agents.get(agent_id)
        agent_runs = sum(tally.runs for tally in tallies.values())
        # Normalised as the Model column normalises the fallback, or an agent
        # registered as "gpt-4o " is flagged for running "gpt-4o".
        configured = ((agent.model or "").strip() or None) if agent is not None else None
        for model, tally in sorted(tallies.items(), key=_model_order):
            name = next(iter(tally.agents.values()))[0]
            out.append(
                LlmAgentModelRow(
                    agent_id=agent_id,
                    agent_name=name,
                    environment=agent.environment if agent is not None else None,
                    configured_model=configured,
                    model=model,
                    model_label=_label(model),
                    matches_configuration=(
                        None if model is None or configured is None else model == configured
                    ),
                    runs=tally.runs,
                    share_of_agent_percent=_rate(tally.runs, agent_runs),
                    finished_runs=tally.finished,
                    success_rate=tally.success_rate,
                    total_tokens=tally.total_tokens if tally.token_runs else None,
                    cost_usd=tally.cost_usd,
                )
            )
    out.sort(key=lambda row: (row.agent_name.lower(), row.agent_id, -row.runs))
    return out


async def report(
    session: AsyncSession,
    principal: Principal,
    *,
    window: LlmUsageWindow = LlmUsageWindow.LAST_7D,
    agent_ids: Sequence[str] | None = None,
    environment: EnvironmentType | None = None,
) -> LlmUsageReport:
    """Everything the LLM Usage screen shows.

    Database first (the scope), then the telemetry read -- shared, engine only
    -- then the governance lookups on this request's own session, which the
    telemetry read never touches.
    """
    span = metrics.resolve_window_days(window.days)
    since, until = span.start, span.end
    scope = await _resolve_scope(session, principal, agent_ids, environment)
    measured = await _measured(scope, principal, since, until)
    # A capped scan holds each busy project's newest rows and each quiet
    # project's whole window. Folded as they are, a quiet model's week would be
    # set against a busy model's last hour, and the shares, rates and leaders
    # would measure the cap rather than the models. So when the edge from which
    # the window was read whole is known, every per-model figure covers that
    # stretch -- the same stretch for every project -- and says where it starts.
    measured_from = measured.scan.covered_from if measured.scan.truncated else None
    facts = measured.facts
    if measured_from is not None:
        facts = tuple(fact for fact in facts if fact.started >= measured_from)
    attributed = await _attributions(session, principal, [fact.run_id for fact in facts], since)
    in_window = await _window_counts(session, principal, scope, since, until)

    tallies: dict[str | None, _Tally] = {}
    overall = _Tally()
    for fact in facts:
        tallies.setdefault(fact.model, _Tally()).add(fact, attributed)
        overall.add(fact, attributed)

    rows: list[LlmModelUsage] = []
    colours = iter(PALETTE * (len(tallies) // len(PALETTE) + 1))
    for model, tally in sorted(tallies.items(), key=_model_order):
        colour = NOT_RECORDED_COLOR if model is None else next(colours)
        rows.append(_model_row(model, tally, overall.runs, colour))

    runs_in_window = measured.runs_in_window
    # No denominator, no share: a window the store counts as empty has nothing
    # to cover, and an unread count is not zero.
    coverage = round(min(100.0, overall.runs / runs_in_window * 100), 1) if runs_in_window else None

    totals = LlmUsageTotals(
        models_in_use=sum(1 for model in tallies if model is not None),
        runs=overall.runs,
        runs_in_window=runs_in_window,
        coverage_percent=coverage,
        runs_model_not_recorded=tallies[None].runs if None in tallies else 0,
        runs_model_from_agent=overall.from_agent,
        finished_runs=overall.finished,
        successful_runs=overall.completed_or_warned,
        success_rate=overall.success_rate,
        input_tokens=overall.input_tokens if overall.token_runs else None,
        output_tokens=overall.output_tokens if overall.token_runs else None,
        total_tokens=overall.total_tokens if overall.token_runs else None,
        cost_usd=overall.cost_usd,
        unpriced_runs=overall.unpriced,
        cost_per_successful_run=overall.cost_per_successful_run,
        agents_in_scope=scope.total,
        agents_with_runs=len({fact.agent_id for fact in facts}),
        feedback_ratings=len(overall.ratings),
        feedback_avg_rating=overall.avg_rating,
        policy_violations_attributed=overall.violations,
        policy_violations_in_window=in_window.violations,
        human_escalations_attributed=overall.escalations,
        human_escalations_in_window=in_window.escalations,
        guardrail_triggers_attributed=overall.guardrail_triggers,
        guardrail_triggers_in_window=in_window.guardrail_triggers,
    )

    return LlmUsageReport(
        window=window,
        window_label=window.label,
        period_start=since,
        period_end=until,
        agent_ids=list(dict.fromkeys(agent_id for agent_id in (agent_ids or []) if agent_id)),
        environment=environment.value if environment is not None else None,
        scan=measured.scan,
        scan_capped=measured.scan.truncated,
        measured_from=measured_from,
        totals=totals,
        models=rows,
        leaders=leaders(rows),
        leader_min_runs=LEADER_MIN_RUNS,
        leader_min_ratings=LEADER_MIN_RATINGS,
        agents=_agent_rows(facts, scope, attributed),
        series=_series(facts, rows, measured.scan, window, since, until),
    )
