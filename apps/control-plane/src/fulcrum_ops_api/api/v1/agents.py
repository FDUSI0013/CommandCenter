"""Agent Registry and Agent Detail routes.

Fifteen endpoints back two screens: the registry's table, KPI row, filters and
CSV export; the detail screen's nine tabs, its Run Agent button, its version
history and Compare Versions modal, and Export Configuration.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
the status state machine, audit writes and every telemetry call live in
``services.agents``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.registry import (
    AgentStatus,
    EnvironmentType,
    Platform,
    PolicyStatus,
    RiskLevel,
)
from ...schemas.agents import (
    AgentClone,
    AgentConfiguration,
    AgentCreate,
    AgentDetail,
    AgentRead,
    AgentRunRequest,
    AgentsSummary,
    AgentStatusChange,
    AgentUpdate,
    AgentVersionCreate,
    AgentVersionDiff,
    AgentVersionRead,
)
from ...services import agents as service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/agents", tags=["Agents"])

ListQuery = Annotated[ListParams, Depends(list_params)]

# Column order of the registry export, matching the on-screen table.
AGENT_CSV_COLUMNS: list[tuple[str, str]] = [
    ("id", "Agent ID"),
    ("name", "Agent Name"),
    ("description", "Description"),
    ("platform", "Source"),
    ("agent_type", "Type"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("risk", "Risk"),
    ("policy_status", "Policy Status"),
    ("owner", "Owner"),
    ("team", "Team"),
    ("model", "Model"),
    ("prompt_version", "Prompt Version"),
    ("tools_enabled", "Tools"),
    ("policies_applied", "Policies"),
    ("runs_30d", "Runs (30d)"),
    ("success_rate_30d", "Success Rate (30d)"),
    ("last_used_at", "Last Used"),
    ("created_at", "Created"),
]


def _csv_record(agent: AgentRead) -> dict[str, object]:
    metrics = agent.metrics
    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "platform": agent.platform.value,
        "agent_type": agent.agent_type.value,
        "environment": agent.environment.value,
        "status": agent.status.value,
        "risk": agent.risk.value,
        "policy_status": agent.policy_status.value,
        "owner": agent.owner_name,
        "team": agent.team,
        "model": agent.model,
        "prompt_version": agent.prompt_version,
        "tools_enabled": agent.tools_enabled,
        "policies_applied": agent.policies_applied,
        "runs_30d": metrics.runs_30d if metrics else None,
        "success_rate_30d": metrics.success_rate_30d if metrics else None,
        "last_used_at": agent.last_used_at.isoformat() if agent.last_used_at else None,
        "created_at": agent.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{agent_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=AgentsSummary, summary="Agent KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> AgentsSummary:
    """The six KPI cards: Total, Active, High Risk, Policy Violations, Pending
    Approval and Inactive, plus the Owner filter's options.

    Every number is a SQL aggregate over the workspace, so the cards stay
    correct on a registry with thousands of agents.
    """
    return await service.summarise(session, principal)


@router.get("/export", summary="Export the registry as CSV")
async def export_agents(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    platform: Annotated[
        Platform | None, Query(alias="source", description="Source platform")
    ] = None,
    environment: Annotated[EnvironmentType | None, Query()] = None,
    agent_status: Annotated[
        AgentStatus | None, Query(alias="status", description="Active, Inactive or Pending Review")
    ] = None,
    risk: Annotated[RiskLevel | None, Query(alias="risk_level")] = None,
    policy_status: Annotated[PolicyStatus | None, Query()] = None,
    owner: Annotated[
        str | None, Query(description="Owner user id, email or display name")
    ] = None,
    team: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The current view as CSV. Honours the same search and filters as the list
    endpoint, so the file matches what the operator is looking at."""
    rows = await service.export_agents(
        session,
        principal,
        params,
        platform=platform,
        environment=environment,
        status=agent_status,
        risk=risk,
        policy_status=policy_status,
        owner=owner,
        team=team,
    )
    reads = await service.read_agents(session, rows)
    body = to_csv([_csv_record(read) for read in reads], AGENT_CSV_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="agent-registry-{stamp}.csv"'},
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[AgentRead], summary="List agents")
async def list_agents(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    platform: Annotated[
        Platform | None,
        Query(alias="source", description="Azure AI Foundry, Copilot Studio, M365 Copilot…"),
    ] = None,
    environment: Annotated[
        EnvironmentType | None, Query(description="Production, Staging, UAT, Development…")
    ] = None,
    agent_status: Annotated[
        AgentStatus | None, Query(alias="status", description="Active, Inactive or Pending Review")
    ] = None,
    risk: Annotated[
        RiskLevel | None, Query(alias="risk_level", description="Low, Medium or High")
    ] = None,
    policy_status: Annotated[
        PolicyStatus | None,
        Query(description="Allowed, Warned, Blocked or Approval Required"),
    ] = None,
    owner: Annotated[
        str | None, Query(description="Owner user id, email or display name")
    ] = None,
    team: Annotated[str | None, Query()] = None,
) -> Page[AgentRead]:
    """One page of the workspace's agents, ordered by name by default.

    Free-text search covers the name, description, source, model and team;
    `sort` accepts any column key the table renders.
    """
    rows, total = await service.list_agents(
        session,
        principal,
        params,
        platform=platform,
        environment=environment,
        status=agent_status,
        risk=risk,
        policy_status=policy_status,
        owner=owner,
        team=team,
    )
    return Page.build(
        await service.read_agents(session, rows), total, params.page, params.page_size
    )


@router.post(
    "", response_model=AgentRead, status_code=status.HTTP_201_CREATED, summary="Register an agent"
)
async def create_agent(
    principal: CurrentPrincipal, session: Db, payload: AgentCreate, request: Request
) -> AgentRead:
    """Register a new agent and provision the telemetry project its runs land in.

    The project is created first: an agent whose runs have nowhere to go is
    worse than a refused registration. The agent starts Pending Review — call
    `POST /agents/{id}/activate` once governance review passes. Requires admin.
    """
    agent = await service.create_agent(session, principal, payload, request=request)
    return (await service.read_agents(session, [agent]))[0]


# ---------------------------------------------------------------------------
# Single agent
# ---------------------------------------------------------------------------


@router.get("/{agent_id}", response_model=AgentDetail, summary="Get an agent")
async def get_agent(principal: CurrentPrincipal, session: Db, agent_id: str) -> AgentDetail:
    """Everything the nine detail tabs need in one round trip: identity and
    configuration, linked connectors, bound policies, run counters and the
    latency series from the telemetry engine, and the prompt version history.

    An agent in another workspace answers 404, not 403.
    """
    return await service.get_detail(session, principal, agent_id)


@router.patch("/{agent_id}", response_model=AgentRead, summary="Update an agent")
async def update_agent(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentUpdate,
    request: Request,
) -> AgentRead:
    """Partially update an agent.

    Send `expected_updated_at` to make the write conditional: if the row moved
    since you read it the request is refused with 409. Status is not settable
    here — use activate/deactivate. Requires admin.
    """
    agent = await service.update_agent(session, principal, agent_id, payload, request=request)
    return (await service.read_agents(session, [agent]))[0]


@router.delete(
    "/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an agent"
)
async def delete_agent(
    principal: CurrentPrincipal, session: Db, agent_id: str, request: Request
) -> None:
    """Remove an agent, its connector grants and its policy bindings.

    Refused with 412 while the agent is still Active. Telemetry already
    recorded stays in the engine, and the audit trail survives. Requires admin.
    """
    await service.delete_agent(session, principal, agent_id, request=request)


@router.get(
    "/{agent_id}/export",
    response_model=AgentConfiguration,
    summary="Export an agent's configuration",
)
async def export_configuration(
    principal: CurrentPrincipal, session: Db, agent_id: str
) -> AgentConfiguration:
    """The configuration manifest behind Export Configuration and the
    Configuration tab's Copy JSON button."""
    return await service.export_configuration(session, principal, agent_id)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/clone",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Clone an agent",
)
async def clone_agent(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentClone,
    request: Request,
) -> AgentRead:
    """Copy an agent into a new registration with its own telemetry project.

    The clone starts Pending Review in Development by default, and optionally
    carries the source's connector grants and policy bindings. Requires admin.
    """
    clone = await service.clone_agent(session, principal, agent_id, payload, request=request)
    return (await service.read_agents(session, [clone]))[0]


@router.post("/{agent_id}/activate", response_model=ActionResult, summary="Activate an agent")
async def activate_agent(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentStatusChange,
    request: Request,
) -> ActionResult:
    """Let the agent accept runs again.

    Refused with 412 when the agent has no telemetry project or its policy
    verdict is Blocked or Approval Required, and with 409 when the status move
    is not legal. Requires the operator role.
    """
    agent = await service.activate_agent(
        session, principal, agent_id, reason=payload.reason, request=request
    )
    return ActionResult(
        message=f"{agent.name} is now active.",
        entity_id=agent.id,
        data={"status": agent.status},
    )


@router.post(
    "/{agent_id}/deactivate", response_model=ActionResult, summary="Deactivate an agent"
)
async def deactivate_agent(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentStatusChange,
    request: Request,
) -> ActionResult:
    """Stop the agent accepting new runs; in-flight runs finish.

    The change is recorded in the audit trail with whatever reason was given.
    Requires the operator role.
    """
    agent = await service.deactivate_agent(
        session, principal, agent_id, reason=payload.reason, request=request
    )
    return ActionResult(
        message=f"{agent.name} is now inactive. Audit event recorded.",
        entity_id=agent.id,
        data={"status": agent.status},
    )


@router.post(
    "/{agent_id}/run",
    response_model=ActionResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an agent",
)
async def run_agent(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentRunRequest,
    request: Request,
) -> ActionResult:
    """Trigger one execution from the console.

    The invocation is recorded as an open run in the telemetry engine and
    appears on Live Runs immediately as `Running`; the agent's runtime completes
    it through the ingest API. Refused with 412 unless the agent is active,
    provisioned and permitted by policy. Requires the operator role.
    """
    accepted = await service.trigger_run(session, principal, agent_id, payload, request=request)
    return ActionResult(
        message=f"{accepted.agent_name} run started.",
        entity_id=accepted.run_id,
        data=accepted.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@router.get(
    "/{agent_id}/versions",
    response_model=list[AgentVersionRead],
    summary="List prompt versions",
)
async def list_versions(
    principal: CurrentPrincipal, session: Db, agent_id: str
) -> list[AgentVersionRead]:
    """The Version History pipe on the Model & Prompt tab, newest first.

    An agent with no committed prompt returns an empty list rather than an
    error: no history is a state, not a failure.
    """
    return await service.list_versions(session, principal, agent_id)


@router.post(
    "/{agent_id}/versions",
    response_model=AgentVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Commit a prompt version",
)
async def create_version(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    payload: AgentVersionCreate,
    request: Request,
) -> AgentVersionRead:
    """Append a commit to the agent's system prompt.

    With `make_current` the agent's prompt version moves to the new commit, so
    the registry row and the detail header agree with the history. Requires
    admin.
    """
    return await service.create_version(session, principal, agent_id, payload, request=request)


@router.get(
    "/{agent_id}/versions/diff",
    response_model=AgentVersionDiff,
    summary="Compare two prompt versions",
)
async def diff_versions(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: str,
    from_version: Annotated[
        str, Query(alias="from", description="Commit id or version label to compare from")
    ],
    to_version: Annotated[
        str, Query(alias="to", description="Commit id or version label to compare to")
    ],
) -> AgentVersionDiff:
    """What the Compare Versions modal renders: a real unified diff of the two
    prompt bodies plus the metadata keys that changed between them."""
    return await service.diff_versions(
        session, principal, agent_id, from_version=from_version, to_version=to_version
    )
