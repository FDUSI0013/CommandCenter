"""LLM Usage route -- which models the agents run on, how much, and how well.

One read backs the whole screen: the KPI row, the runs-by-model donut, the
best-results panel, the per-model table, the per-agent breakdown and the trend
lines are all views of one scan of the window, so they cannot disagree.

Handlers parse, delegate and shape. Scoping, the telemetry scan and every
aggregation rule live in ``services.llm_usage``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...models.registry import EnvironmentType
from ...schemas.llm_usage import LlmUsageReport, LlmUsageWindow
from ...services import llm_usage as service
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/llm-usage", tags=["LLM Usage"])

WindowQuery = Annotated[
    LlmUsageWindow, Query(description="Window the figures cover: 24h, 7d or 30d")
]

#: Narrow to specific agents, as the Metrics screen does: repeat the parameter to
#: include several, omit it for the whole workspace. An agent the workspace does
#: not own is simply not read.
AgentFilter = Annotated[
    list[str] | None,
    Query(alias="agent_id", description="Repeat to include several agents"),
]

EnvironmentFilter = Annotated[
    EnvironmentType | None,
    Query(description="Only agents deployed to this environment"),
]


@router.get("", response_model=LlmUsageReport, summary="LLM usage by model")
async def get_llm_usage(
    principal: CurrentPrincipal,
    session: Db,
    window: WindowQuery = LlmUsageWindow.LAST_7D,
    agent_id: AgentFilter = None,
    environment: EnvironmentFilter = None,
) -> LlmUsageReport:
    """Every model the agents in scope ran on in the window, and how each did.

    The unit is the **run** -- one agent execution, one row of Live Runs --
    attributed to the model recorded on it, or to its agent's registered model
    when it recorded none. These are runs, not LLM calls.

    Per model: runs and share, tokens in and out, cost, cost per run and per
    successful run, success rate over finished runs, p50 and p90 duration, the
    agents using it, the mean feedback rating of the runs people rated, and the
    violation records and guardrail events that name its runs by trace id.

    `leaders` names the best model on each of four measures, only among models
    with at least `leader_min_runs` runs (`leader_min_ratings` ratings for
    feedback), each with the sample it was measured on. There is no blended
    score.

    The figures come from the same capped scan Live Runs reads. When
    `scan_capped` is true they describe the newest runs of the window, not all
    of them: `measured_from` is the instant from which the window was read
    whole for every agent, and the per-model figures cover the runs from there
    on; `totals.runs_in_window` is the store's uncapped count for the whole
    window. The window's violation, escalation and guardrail totals
    (`*_in_window`) are counted in full, whatever the cap.
    """
    return await service.report(
        session,
        principal,
        window=window,
        agent_ids=agent_id,
        environment=environment,
    )
