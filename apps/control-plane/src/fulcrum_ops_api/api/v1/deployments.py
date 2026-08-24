"""Deployment & Environment routes.

Two collections live behind one screen, so this module exposes two prefixed
routers — ``/environments`` and ``/deployments`` — and mounts both on the
module-level ``router`` that the v1 package includes.

Handlers stay thin: parse, delegate to
:mod:`fulcrum_ops_api.services.deployments`, shape the response. The only logic
here is response shaping (joining in environment and agent labels the tables
display) and the two streaming responses, which are HTTP concerns.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import AsyncIterator, Sequence
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.operations import Deployment, DeploymentStatus, DeploymentStrategy
from ...models.registry import Environment, EnvironmentStatus, EnvironmentType
from ...schemas.deployments import (
    DeploymentApproveRequest,
    DeploymentCreate,
    DeploymentHaltRequest,
    DeploymentPromoteRequest,
    DeploymentRead,
    DeploymentRollbackRequest,
    DeploymentStageRead,
    DeploymentSummary,
    DeploymentUpdate,
    EnvironmentCreate,
    EnvironmentRead,
    EnvironmentRestartRequest,
    EnvironmentUpdate,
)
from ...services import deployments as deployments_service
from ..common import ActionResult, ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db, Principal

environments_router = APIRouter(prefix="/environments", tags=["Environments"])
deployments_router = APIRouter(prefix="/deployments", tags=["Deployments"])

router = APIRouter()
router.include_router(environments_router)
router.include_router(deployments_router)

ListQuery = Annotated[ListParams, Depends(list_params)]

# Column order of the deployment history export, matching the on-screen table.
DEPLOYMENT_CSV_COLUMNS: list[tuple[str, str]] = [
    ("deployment_ref", "Deployment"),
    ("version", "Version"),
    ("environment", "Environment"),
    ("environment_type", "Environment Type"),
    ("agent", "Agent"),
    ("status", "Status"),
    ("strategy", "Strategy"),
    ("triggered_by", "Triggered By"),
    ("started_at", "Started"),
    ("finished_at", "Finished"),
    ("duration", "Duration"),
    ("commit_ref", "Commit"),
    ("health", "Health %"),
    ("rollback_of_deployment_id", "Rollback Of"),
    ("notes", "Notes"),
]

# Comment frame sent when a stream has been quiet, so proxies keep it open.
STREAM_HEARTBEAT_SECONDS = 15.0


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


async def _read_environments(
    session: Db, principal: Principal, rows: Sequence[Environment]
) -> list[EnvironmentRead]:
    """Attach the "last deployment" pair the environments table renders."""
    index = await deployments_service.last_deployment_index(
        session, principal, [row.id for row in rows]
    )
    out: list[EnvironmentRead] = []
    for environment in rows:
        read = EnvironmentRead.model_validate(environment)
        read.last_deployment_at, read.last_deployment_by = index.get(
            environment.id, (None, None)
        )
        out.append(read)
    return out


async def _read_deployments(
    session: Db, principal: Principal, rows: Sequence[Deployment]
) -> list[DeploymentRead]:
    """Attach environment and agent labels for a whole page in two queries."""
    environments = await deployments_service.environment_labels(
        session, principal, [row.environment_id for row in rows]
    )
    agents = await deployments_service.agent_labels(
        session, principal, [row.agent_id for row in rows if row.agent_id]
    )
    out: list[DeploymentRead] = []
    for deployment in rows:
        read = DeploymentRead.model_validate(deployment)
        label = environments.get(deployment.environment_id)
        if label is not None:
            read.environment_name, read.environment_type = label
        if deployment.agent_id:
            read.agent_name = agents.get(deployment.agent_id)
        read.duration_label = deployments_service.format_duration(deployment.duration_seconds)
        out.append(read)
    return out


async def _read_one(
    session: Db, principal: Principal, deployment: Deployment
) -> DeploymentRead:
    return (await _read_deployments(session, principal, [deployment]))[0]


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #


@environments_router.get("", response_model=Page[EnvironmentRead], summary="List environments")
async def list_environments(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    env_type: Annotated[EnvironmentType | None, Query(alias="type")] = None,
    env_status: Annotated[EnvironmentStatus | None, Query(alias="status")] = None,
    region: Annotated[str | None, Query()] = None,
) -> Page[EnvironmentRead]:
    """Page of deployment targets, filtered by the console's type, status and
    region dropdowns and searched across name, region and description."""
    rows, total = await deployments_service.list_environments(
        session,
        principal,
        params,
        env_type=env_type.value if env_type else None,
        status=env_status.value if env_status else None,
        region=region,
    )
    items = await _read_environments(session, principal, rows)
    return Page.build(items, total, params.page, params.page_size)


@environments_router.post(
    "",
    response_model=EnvironmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an environment",
)
async def create_environment(
    payload: EnvironmentCreate,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> EnvironmentRead:
    """Provision a new deployment target. Names are unique per workspace."""
    environment = await deployments_service.create_environment(
        session, principal, payload, request=request
    )
    return (await _read_environments(session, principal, [environment]))[0]


@environments_router.get(
    "/{environment_id}", response_model=EnvironmentRead, summary="Get an environment"
)
async def get_environment(
    environment_id: str, principal: CurrentPrincipal, session: Db
) -> EnvironmentRead:
    """One environment, including when it was last deployed to and by whom."""
    environment = await deployments_service.get_environment(session, principal, environment_id)
    return (await _read_environments(session, principal, [environment]))[0]


@environments_router.patch(
    "/{environment_id}", response_model=EnvironmentRead, summary="Update an environment"
)
async def update_environment(
    environment_id: str,
    payload: EnvironmentUpdate,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> EnvironmentRead:
    """Partial update; only the fields present in the body are written."""
    environment = await deployments_service.update_environment(
        session, principal, environment_id, payload, request=request
    )
    return (await _read_environments(session, principal, [environment]))[0]


@environments_router.delete(
    "/{environment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment",
)
async def delete_environment(
    environment_id: str,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> None:
    """Remove an environment. Refused while deployments are in flight or live
    in it, so history never loses the target it points at."""
    await deployments_service.delete_environment(
        session, principal, environment_id, request=request
    )


@environments_router.post(
    "/{environment_id}/restart",
    response_model=ActionResult,
    summary="Restart environment services",
)
async def restart_environment(
    environment_id: str,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    payload: EnvironmentRestartRequest | None = None,
) -> ActionResult:
    """Bounce the services in an environment. Refused while a deployment is in
    flight, because restarting under a running pipeline can strand a release."""
    environment = await deployments_service.restart_environment(
        session,
        principal,
        environment_id,
        payload or EnvironmentRestartRequest(),
        request=request,
    )
    return ActionResult(
        message=f"Services in '{environment.name}' are restarting.",
        entity_id=environment.id,
        data={"name": environment.name, "status": environment.status},
    )


# --------------------------------------------------------------------------- #
# Deployments
# --------------------------------------------------------------------------- #


@deployments_router.get("", response_model=Page[DeploymentRead], summary="List deployments")
async def list_deployments(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    environment_id: Annotated[str | None, Query()] = None,
    deployment_status: Annotated[DeploymentStatus | None, Query(alias="status")] = None,
    strategy: Annotated[DeploymentStrategy | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    version: Annotated[str | None, Query()] = None,
    terminal: Annotated[
        bool | None,
        Query(description="true: only finished deployments (the History tab)"),
    ] = None,
) -> Page[DeploymentRead]:
    """Deployment history, newest first, filtered by the console's environment,
    status, strategy, agent and version dropdowns."""
    rows, total = await deployments_service.list_deployments(
        session,
        principal,
        params,
        environment_id=environment_id,
        status=deployment_status.value if deployment_status else None,
        strategy=strategy.value if strategy else None,
        agent_id=agent_id,
        version=version,
        terminal=terminal,
    )
    items = await _read_deployments(session, principal, rows)
    return Page.build(items, total, params.page, params.page_size)


@deployments_router.get(
    "/summary", response_model=DeploymentSummary, summary="Deployment KPI summary"
)
async def deployment_summary(
    principal: CurrentPrincipal, session: Db
) -> DeploymentSummary:
    """The KPI row: environments, in-flight releases, success and failure
    counts, average release time and rollbacks executed."""
    return await deployments_service.summary(session, principal)


@deployments_router.get("/export", summary="Export deployments as CSV")
async def export_deployments(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    environment_id: Annotated[str | None, Query()] = None,
    deployment_status: Annotated[DeploymentStatus | None, Query(alias="status")] = None,
    strategy: Annotated[DeploymentStrategy | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    version: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The current view as CSV. Honours the same search and filters as the list
    endpoint so the file matches what the operator is looking at."""
    rows = await deployments_service.export_deployments(
        session,
        principal,
        params,
        environment_id=environment_id,
        status=deployment_status.value if deployment_status else None,
        strategy=strategy.value if strategy else None,
        agent_id=agent_id,
        version=version,
    )
    reads = await _read_deployments(session, principal, rows)
    records = [
        {
            "deployment_ref": read.deployment_ref,
            "version": read.version,
            "environment": read.environment_name or read.environment_id,
            "environment_type": read.environment_type,
            "agent": read.agent_name,
            "status": read.status,
            "strategy": read.strategy,
            "triggered_by": read.created_by,
            "started_at": read.started_at.isoformat() if read.started_at else None,
            "finished_at": read.finished_at.isoformat() if read.finished_at else None,
            "duration": read.duration_label,
            "commit_ref": read.commit_ref,
            "health": read.health,
            "rollback_of_deployment_id": read.rollback_of_deployment_id,
            "notes": read.notes,
        }
        for read in reads
    ]
    body = to_csv(records, DEPLOYMENT_CSV_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="deployments-{stamp}.csv"'},
    )


@deployments_router.post(
    "",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a deployment",
)
async def create_deployment(
    payload: DeploymentCreate,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> DeploymentRead:
    """Start a release.

    The five pipeline stages — Build, Automated Tests, Security Scan, Approval,
    Deploy — are created ``Pending`` with the deployment, and the runner is
    scheduled to advance them once this request has committed. Poll
    ``GET /deployments/{id}/stages`` or subscribe to ``/stream`` to follow it.
    """
    deployment = await deployments_service.create_deployment(
        session, principal, payload, request=request
    )
    background.add_task(
        deployments_service.run_pipeline, deployment.id, principal.workspace_id
    )
    return await _read_one(session, principal, deployment)


@deployments_router.get(
    "/{deployment_id}", response_model=DeploymentRead, summary="Get a deployment"
)
async def get_deployment(
    deployment_id: str, principal: CurrentPrincipal, session: Db
) -> DeploymentRead:
    """One deployment with its environment and agent labels resolved."""
    deployment = await deployments_service.get_deployment(session, principal, deployment_id)
    return await _read_one(session, principal, deployment)


@deployments_router.patch(
    "/{deployment_id}", response_model=DeploymentRead, summary="Annotate a deployment"
)
async def update_deployment(
    deployment_id: str,
    payload: DeploymentUpdate,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> DeploymentRead:
    """Record notes, a commit reference or a post-deploy health reading.
    Lifecycle fields belong to the pipeline and are not writable here."""
    deployment = await deployments_service.update_deployment(
        session, principal, deployment_id, payload, request=request
    )
    return await _read_one(session, principal, deployment)


@deployments_router.get(
    "/{deployment_id}/stages",
    response_model=list[DeploymentStageRead],
    summary="List pipeline stages",
)
async def list_stages(
    deployment_id: str, principal: CurrentPrincipal, session: Db
) -> list[DeploymentStageRead]:
    """The five stages in render order, with per-stage status, timings and log.
    This is what the console polls while a deployment is running."""
    stages = await deployments_service.list_stages(session, principal, deployment_id)
    return [DeploymentStageRead.model_validate(stage) for stage in stages]


@deployments_router.get("/{deployment_id}/stream", summary="Stream pipeline progress")
async def stream_deployment(
    deployment_id: str, principal: CurrentPrincipal, session: Db, request: Request
) -> StreamingResponse:
    """Server-sent events carrying a pipeline snapshot whenever it changes.

    The stream closes when the deployment reaches a terminal state, when the
    client disconnects, or when it ages out; a comment frame keeps intermediate
    proxies from closing an idle connection.
    """
    # Resolve first so a missing or cross-workspace id is a clean 404 rather
    # than an empty stream.
    await deployments_service.get_deployment(session, principal, deployment_id)

    async def frames() -> AsyncIterator[str]:
        previous: dict | None = None
        last_sent = time.monotonic()
        async for payload in deployments_service.stream_progress(principal, deployment_id):
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if payload != previous:
                previous = payload
                last_sent = now
                yield deployments_service.progress_frame(payload)
            elif now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                last_sent = now
                yield ": keep-alive\n\n"

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@deployments_router.post(
    "/{deployment_id}/promote", response_model=ActionResult, summary="Promote a deployment"
)
async def promote_deployment(
    deployment_id: str,
    payload: DeploymentPromoteRequest,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> ActionResult:
    """Release a succeeded version into the next environment.

    Promotion creates a new deployment with its own pipeline and gates; the
    source deployment is left untouched.
    """
    promoted = await deployments_service.promote_deployment(
        session, principal, deployment_id, payload, request=request
    )
    background.add_task(
        deployments_service.run_pipeline, promoted.id, principal.workspace_id
    )
    return ActionResult(
        message=f"{promoted.version} queued as {promoted.deployment_ref}.",
        entity_id=promoted.id,
        data={
            "deployment_id": promoted.id,
            "deployment_ref": promoted.deployment_ref,
            "environment_id": promoted.environment_id,
            "version": promoted.version,
            "status": promoted.status,
        },
    )


@deployments_router.post(
    "/{deployment_id}/halt", response_model=ActionResult, summary="Halt a deployment"
)
async def halt_deployment(
    deployment_id: str,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    payload: DeploymentHaltRequest | None = None,
) -> ActionResult:
    """Stop an in-flight deployment. The running stage and everything after it
    are marked skipped and any open approval request is closed."""
    deployment = await deployments_service.halt_deployment(
        session,
        principal,
        deployment_id,
        payload or DeploymentHaltRequest(),
        request=request,
    )
    return ActionResult(
        message=f"{deployment.deployment_ref} halted.",
        entity_id=deployment.id,
        data={"status": deployment.status, "version": deployment.version},
    )


@deployments_router.post(
    "/{deployment_id}/approve", response_model=ActionResult, summary="Decide the approval gate"
)
async def approve_deployment(
    deployment_id: str,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    payload: DeploymentApproveRequest | None = None,
) -> ActionResult:
    """Approve the paused deployment and let the pipeline continue to Deploy.

    Sending ``{"approved": false}`` rejects the gate instead, which halts the
    deployment and closes the approval request as rejected.
    """
    decision = payload or DeploymentApproveRequest()
    deployment, resumed = await deployments_service.approve_deployment(
        session, principal, deployment_id, decision, request=request
    )
    if resumed:
        background.add_task(
            deployments_service.run_pipeline, deployment.id, principal.workspace_id
        )
        message = f"{deployment.deployment_ref} approved; the pipeline is resuming."
    else:
        message = f"{deployment.deployment_ref} rejected and halted."
    return ActionResult(
        message=message,
        entity_id=deployment.id,
        data={
            "status": deployment.status,
            "approved": decision.approved,
            "approval_request_id": deployment.approval_request_id,
        },
    )


@deployments_router.post(
    "/{deployment_id}/rollback", response_model=ActionResult, summary="Roll back a deployment"
)
async def rollback_deployment(
    deployment_id: str,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    payload: DeploymentRollbackRequest | None = None,
) -> ActionResult:
    """Undo a release by deploying the previous version again.

    The rollback is a new deployment pointing back at the one it undoes, so the
    history table can render both halves of the incident together.
    """
    rollback = await deployments_service.rollback_deployment(
        session,
        principal,
        deployment_id,
        payload or DeploymentRollbackRequest(),
        request=request,
    )
    background.add_task(
        deployments_service.run_pipeline, rollback.id, principal.workspace_id
    )
    return ActionResult(
        message=f"Rolling back to {rollback.version} as {rollback.deployment_ref}.",
        entity_id=rollback.id,
        data={
            "deployment_id": rollback.id,
            "deployment_ref": rollback.deployment_ref,
            "version": rollback.version,
            "rollback_of_deployment_id": rollback.rollback_of_deployment_id,
            "status": rollback.status,
        },
    )
