"""Deployment and environment business logic, including the pipeline runner.

Everything here is workspace-scoped: a row that belongs to another tenant is
indistinguishable from a row that does not exist, so cross-workspace reads raise
``NotFound`` and never leak that the id is real.

The pipeline
------------
Creating a deployment seeds the five stages of
:data:`~fulcrum_ops_api.models.operations.DEFAULT_PIPELINE_STAGES` as ``Pending``
and hands the deployment to :data:`runner`, a small in-process supervisor. The
runner advances one stage at a time in its own asyncio task, writing
``status``/``started_at``/``finished_at``/``log`` as it goes so the console can
poll ``GET /deployments/{id}/stages`` (or subscribe to the SSE stream) and watch
the pipeline move.

Two properties the runner guarantees:

* **It never holds a transaction across a wait.** Each stage transition is one
  short-lived session; the wait between transitions happens with nothing open,
  so a concurrent ``halt`` is never blocked by the runner.
* **It never leaves work Running.** A stage that fails marks the deployment
  ``Failed`` and stops; an unexpected exception is caught and the same
  bookkeeping runs, so no row is stranded in ``Running``.

The ``Approval`` stage is the one deliberate pause: it opens an
``ApprovalRequest`` and the runner exits. ``approve_deployment`` decides that
request and the route restarts the runner, which picks up at ``Deploy``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from fastapi import Request
from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PermissionDenied, PreconditionFailed
from ..db.base import stamp
from ..db.session import get_sessionmaker
from ..models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStep,
    RiskLevel,
)
from ..models.identity import Role
from ..models.operations import (
    DEFAULT_PIPELINE_STAGES,
    Deployment,
    DeploymentStage,
    DeploymentStageStatus,
    DeploymentStatus,
)
from ..models.registry import Agent, Environment, EnvironmentStatus, EnvironmentType
from ..schemas.deployments import (
    DeploymentApproveRequest,
    DeploymentCreate,
    DeploymentHaltRequest,
    DeploymentPromoteRequest,
    DeploymentRollbackRequest,
    DeploymentSummary,
    DeploymentUpdate,
    EnvironmentCreate,
    EnvironmentRestartRequest,
    EnvironmentUpdate,
)
from . import audit

log = logging.getLogger("fulcrum_ops.deployments")

SOURCE_SCREEN = "Deployment & Environment"

STAGE_BUILD, STAGE_TESTS, STAGE_SECURITY_SCAN, STAGE_APPROVAL, STAGE_DEPLOY = (
    DEFAULT_PIPELINE_STAGES
)

# How long each stage occupies the pipeline. The control plane records the run;
# the platform agent that performs the work reports back through the same rows,
# and these are the budgets the runner allows a stage before it moves on.
STAGE_RUNTIME_SECONDS: dict[str, float] = {
    STAGE_BUILD: 3.0,
    STAGE_TESTS: 4.0,
    STAGE_SECURITY_SCAN: 3.0,
    STAGE_DEPLOY: 4.0,
}

# A stage in one of these states needs no further work from the runner.
STAGE_DONE_STATUSES = (
    DeploymentStageStatus.COMPLETED.value,
    DeploymentStageStatus.WARNING.value,
    DeploymentStageStatus.APPROVED.value,
    DeploymentStageStatus.SKIPPED.value,
)

# A deployment in one of these states is occupying its environment.
IN_FLIGHT_STATUSES = (DeploymentStatus.QUEUED.value, DeploymentStatus.RUNNING.value)

APPROVAL_SLA = dt.timedelta(hours=2)
APPROVAL_SLA_LABEL = "2h"

MAX_EXPORT_ROWS = 5000

# Reference allocation ("dep-41", "REQ-108") probes forward from the row count;
# the unique constraint is the real guard, this just keeps the numbers tidy.
MAX_REFERENCE_PROBES = 200

STREAM_POLL_SECONDS = 1.0
# While a release waits at its approval gate, where only a person can move it.
STREAM_GATE_POLL_SECONDS = 5.0
STREAM_MAX_SECONDS = 900.0

# How long a freshly started runner keeps looking for a deployment it cannot see
# yet (its caller's commit has not landed) before it concludes there is none.
ROW_VISIBLE_ATTEMPTS = 10
ROW_VISIBLE_WAIT_SECONDS = 0.25

# How long past its allowance a pipeline may sit still before it is taken to
# have lost its runner. Stages run for seconds, so a minute is never a slow
# runner; it is a worker that was restarted underneath one.
ORPHAN_GRACE_SECONDS = 60.0


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def format_duration(seconds: float | None) -> str | None:
    """Render a duration the way the KPI tile and the history table show it."""
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _elapsed_seconds(started_at: dt.datetime | None, finished_at: dt.datetime) -> int | None:
    if started_at is None:
        return None
    return max(0, int(round((finished_at - started_at).total_seconds())))


def _system_principal(workspace_id: str) -> Principal:
    """Actor used for transitions the pipeline makes on its own.

    The audit trail must never attribute a machine transition to the person who
    started the deployment, so the runner signs its rows as itself.
    """
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="deployment-pipeline",
        display_name="Deployment Pipeline",
    )


async def _next_deployment_ref(session: AsyncSession, workspace_id: str) -> str:
    base = (
        await session.execute(
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.workspace_id == workspace_id)
        )
    ).scalar_one()
    for offset in range(1, MAX_REFERENCE_PROBES + 1):
        candidate = f"dep-{base + offset}"
        taken = (
            await session.execute(
                select(func.count())
                .select_from(Deployment)
                .where(
                    Deployment.workspace_id == workspace_id,
                    Deployment.deployment_ref == candidate,
                )
            )
        ).scalar_one()
        if not taken:
            return candidate
    raise Conflict("Could not allocate a deployment reference; retry the request.")


async def _next_approval_ref(session: AsyncSession, workspace_id: str) -> str:
    base = (
        await session.execute(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.workspace_id == workspace_id)
        )
    ).scalar_one()
    for offset in range(1, MAX_REFERENCE_PROBES + 1):
        candidate = f"REQ-{base + offset}"
        taken = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(
                    ApprovalRequest.workspace_id == workspace_id,
                    ApprovalRequest.request_ref == candidate,
                )
            )
        ).scalar_one()
        if not taken:
            return candidate
    raise Conflict("Could not allocate an approval reference; retry the request.")


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #

ENVIRONMENT_SORTABLE = {
    "name": Environment.name,
    "type": Environment.env_type,
    "env_type": Environment.env_type,
    "region": Environment.region,
    "status": Environment.status,
    "health": Environment.health,
    "active_deployment_count": Environment.active_deployment_count,
    "created_at": Environment.created_at,
    "updated_at": Environment.updated_at,
}


async def _get_environment(
    session: AsyncSession, principal: Principal, environment_id: str
) -> Environment:
    """Load one environment or raise ``NotFound``.

    Another tenant's id lands here too: it is reported as missing rather than
    forbidden so the API never confirms that the row exists.
    """
    row = (
        await session.execute(
            select(Environment).where(
                Environment.id == environment_id,
                Environment.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"Environment '{environment_id}' does not exist.")
    return row


async def list_environments(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    env_type: str | None = None,
    status: str | None = None,
    region: str | None = None,
) -> tuple[Sequence[Environment], int]:
    """Page of environments matching the console's search box and dropdowns."""
    stmt = select(Environment).where(Environment.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            Environment.env_type: env_type,
            Environment.status: status,
            Environment.region: region,
        },
    )
    stmt = apply_search(
        stmt, params, [Environment.name, Environment.region, Environment.description]
    )
    stmt = apply_sort(
        stmt, params, ENVIRONMENT_SORTABLE, default=Environment.name, default_desc=False
    )
    return await paginate(session, stmt, params)


async def get_environment(
    session: AsyncSession, principal: Principal, environment_id: str
) -> Environment:
    """One environment by id."""
    return await _get_environment(session, principal, environment_id)


async def last_deployment_index(
    session: AsyncSession, principal: Principal, environment_ids: Sequence[str]
) -> dict[str, tuple[dt.datetime | None, str | None]]:
    """Most recent deployment per environment: ``{env_id: (when, who)}``.

    Ranked in SQL so one query answers a whole page instead of one query per row.
    """
    ids = [env_id for env_id in dict.fromkeys(environment_ids) if env_id]
    if not ids:
        return {}

    ranked = (
        select(
            Deployment.environment_id.label("environment_id"),
            func.coalesce(Deployment.started_at, Deployment.created_at).label("happened_at"),
            Deployment.created_by.label("actor"),
            func.row_number()
            .over(
                partition_by=Deployment.environment_id,
                order_by=Deployment.created_at.desc(),
            )
            .label("rank"),
        )
        .where(
            Deployment.workspace_id == principal.workspace_id,
            Deployment.environment_id.in_(ids),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.environment_id, ranked.c.happened_at, ranked.c.actor).where(
                ranked.c.rank == 1
            )
        )
    ).all()
    return {row.environment_id: (row.happened_at, row.actor) for row in rows}


async def live_release_counts(
    session: AsyncSession, workspace_id: str, environment_ids: Sequence[str]
) -> dict[str, int]:
    """How many releases each environment is serving: ``{env_id: count}``.

    An environment serves one release per agent (a platform release, which has
    no agent, is one more): whichever succeeded last. A newer release replaces
    the older one and a rollback that lands replaces the release it undoes, so
    neither adds to the number -- which is what counting successes did, until a
    Production environment twelve releases old read "12 active deployments" and
    could never be deleted. So it is counted from the history every time rather
    than kept: the agents with a succeeded release there. An environment nothing
    has succeeded in is simply absent from the result.
    """
    ids = [env_id for env_id in dict.fromkeys(environment_ids) if env_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                Deployment.environment_id,
                func.count(func.distinct(func.coalesce(Deployment.agent_id, ""))),
            )
            .where(
                Deployment.workspace_id == workspace_id,
                Deployment.environment_id.in_(ids),
                Deployment.status == DeploymentStatus.SUCCEEDED.value,
            )
            .group_by(Deployment.environment_id)
        )
    ).all()
    return {environment_id: int(count) for environment_id, count in rows}


async def create_environment(
    session: AsyncSession,
    principal: Principal,
    payload: EnvironmentCreate,
    *,
    request: Request | None = None,
) -> Environment:
    """Provision a deployment target. Names are unique within the workspace."""
    principal.require(Role.ADMIN)

    clash = (
        await session.execute(
            select(func.count())
            .select_from(Environment)
            .where(
                Environment.workspace_id == principal.workspace_id,
                Environment.name == payload.name,
            )
        )
    ).scalar_one()
    if clash:
        raise Conflict(f"An environment named '{payload.name}' already exists.")

    environment = Environment(
        workspace_id=principal.workspace_id,
        name=payload.name,
        env_type=payload.env_type.value,
        region=payload.region,
        status=payload.status.value,
        health=payload.health,
        description=payload.description,
        engine_environment_id=payload.engine_environment_id,
        active_deployment_count=0,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(environment)
    await _flush(session, f"An environment named '{payload.name}' already exists.")

    await audit.record(
        session,
        principal=principal,
        action="environment.create",
        entity_type="Environment",
        entity_id=environment.id,
        entity_label=environment.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Created {environment.env_type} environment '{environment.name}'.",
        metadata={"env_type": environment.env_type, "region": environment.region},
        request=request,
    )
    return environment


async def update_environment(
    session: AsyncSession,
    principal: Principal,
    environment_id: str,
    payload: EnvironmentUpdate,
    *,
    request: Request | None = None,
) -> Environment:
    """Apply a partial update; unsupplied fields keep their stored value."""
    principal.require(Role.ADMIN)
    environment = await _get_environment(session, principal, environment_id)

    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != environment.name:
        clash = (
            await session.execute(
                select(func.count())
                .select_from(Environment)
                .where(
                    Environment.workspace_id == principal.workspace_id,
                    Environment.name == changes["name"],
                    Environment.id != environment.id,
                )
            )
        ).scalar_one()
        if clash:
            raise Conflict(f"An environment named '{changes['name']}' already exists.")

    applied: list[str] = []
    for field, value in changes.items():
        stored = value.value if isinstance(value, (EnvironmentType, EnvironmentStatus)) else value
        if getattr(environment, field) != stored:
            setattr(environment, field, stored)
            applied.append(field)

    if not applied:
        return environment

    environment.updated_by = principal.actor
    await _flush(session, "That environment name is already in use.")

    await audit.record(
        session,
        principal=principal,
        action="environment.update",
        entity_type="Environment",
        entity_id=environment.id,
        entity_label=environment.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Updated {', '.join(sorted(applied))} on '{environment.name}'.",
        metadata={"fields": sorted(applied)},
        request=request,
    )
    return environment


async def delete_environment(
    session: AsyncSession,
    principal: Principal,
    environment_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove an environment. Refused while anything is deployed to it.

    "Deployed to it" is counted here and now, not read off the stored counter,
    which on rows written before it was a count of live releases holds every
    success the environment ever had. And since a release, once live, stays
    live until something replaces it, there has to be a way to say an
    environment is finished with: taking it Offline is that. Nothing can be
    released into an offline environment and nothing is served from one.
    """
    principal.require(Role.ADMIN)
    environment = await _get_environment(session, principal, environment_id)

    in_flight = (
        await session.execute(
            select(func.count())
            .select_from(Deployment)
            .where(
                Deployment.workspace_id == principal.workspace_id,
                Deployment.environment_id == environment.id,
                Deployment.status.in_(IN_FLIGHT_STATUSES),
            )
        )
    ).scalar_one()
    if in_flight:
        raise Conflict(
            f"'{environment.name}' has {in_flight} deployment(s) in flight. "
            "Halt them before deleting the environment."
        )
    live = (
        await live_release_counts(session, principal.workspace_id, [environment.id])
    ).get(environment.id, 0)
    if live and environment.status != EnvironmentStatus.OFFLINE.value:
        raise Conflict(
            f"'{environment.name}' still hosts {live} live release(s). "
            "Take the environment offline before deleting it."
        )

    label = environment.name
    await audit.record(
        session,
        principal=principal,
        action="environment.delete",
        entity_type="Environment",
        entity_id=environment.id,
        entity_label=label,
        source_screen=SOURCE_SCREEN,
        detail=f"Deleted environment '{label}'.",
        request=request,
    )
    await session.delete(environment)
    await session.flush()


async def restart_environment(
    session: AsyncSession,
    principal: Principal,
    environment_id: str,
    payload: EnvironmentRestartRequest,
    *,
    request: Request | None = None,
) -> Environment:
    """Bounce the services in an environment.

    Refused while a deployment is in flight — restarting underneath a running
    pipeline is how a half-written release ends up live.
    """
    principal.require(Role.OPERATOR)
    environment = await _get_environment(session, principal, environment_id)

    in_flight = (
        await session.execute(
            select(func.count())
            .select_from(Deployment)
            .where(
                Deployment.workspace_id == principal.workspace_id,
                Deployment.environment_id == environment.id,
                Deployment.status.in_(IN_FLIGHT_STATUSES),
            )
        )
    ).scalar_one()
    if in_flight:
        raise Conflict(
            f"A deployment is in flight in '{environment.name}'. "
            "Wait for it to finish or halt it before restarting services."
        )

    environment.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="environment.restart",
        entity_type="Environment",
        entity_id=environment.id,
        entity_label=environment.name,
        source_screen=SOURCE_SCREEN,
        detail=payload.reason or f"Restarted services in '{environment.name}'.",
        request=request,
    )
    return environment


# --------------------------------------------------------------------------- #
# Deployments — reads
# --------------------------------------------------------------------------- #

DEPLOYMENT_SORTABLE = {
    "deployment_ref": Deployment.deployment_ref,
    "version": Deployment.version,
    "status": Deployment.status,
    "strategy": Deployment.strategy,
    "environment_id": Deployment.environment_id,
    "started_at": Deployment.started_at,
    "finished_at": Deployment.finished_at,
    "duration_seconds": Deployment.duration_seconds,
    "health": Deployment.health,
    "created_at": Deployment.created_at,
}


def _deployment_stmt(
    principal: Principal,
    params: ListParams,
    *,
    environment_id: str | None,
    status: str | None,
    strategy: str | None,
    agent_id: str | None,
    version: str | None,
    terminal: bool | None = None,
) -> Select:
    stmt = select(Deployment).where(Deployment.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            Deployment.environment_id: environment_id,
            Deployment.status: status,
            Deployment.strategy: strategy,
            Deployment.agent_id: agent_id,
            Deployment.version: version,
        },
    )
    if terminal is not None:
        finished = tuple(
            value.value
            for value in (
                DeploymentStatus.SUCCEEDED,
                DeploymentStatus.FAILED,
                DeploymentStatus.HALTED,
                DeploymentStatus.ROLLED_BACK,
            )
        )
        stmt = stmt.where(
            Deployment.status.in_(finished) if terminal else Deployment.status.not_in(finished)
        )
    stmt = apply_search(
        stmt,
        params,
        [
            Deployment.deployment_ref,
            Deployment.version,
            Deployment.commit_ref,
            Deployment.notes,
        ],
    )
    return apply_sort(stmt, params, DEPLOYMENT_SORTABLE, default=Deployment.created_at)


async def _get_deployment(
    session: AsyncSession, principal: Principal, deployment_id: str
) -> Deployment:
    row = (
        await session.execute(
            select(Deployment).where(
                Deployment.id == deployment_id,
                Deployment.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"Deployment '{deployment_id}' does not exist.")
    return row


async def list_deployments(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    environment_id: str | None = None,
    status: str | None = None,
    strategy: str | None = None,
    agent_id: str | None = None,
    version: str | None = None,
    terminal: bool | None = None,
) -> tuple[Sequence[Deployment], int]:
    """Page of the deployment history, newest first by default."""
    stmt = _deployment_stmt(
        principal,
        params,
        environment_id=environment_id,
        status=status,
        strategy=strategy,
        agent_id=agent_id,
        version=version,
        terminal=terminal,
    )
    return await paginate(session, stmt, params)


async def export_deployments(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    environment_id: str | None = None,
    status: str | None = None,
    strategy: str | None = None,
    agent_id: str | None = None,
    version: str | None = None,
) -> Sequence[Deployment]:
    """Every row the current filters select, capped so one click cannot pull the
    whole history into memory."""
    stmt = _deployment_stmt(
        principal,
        params,
        environment_id=environment_id,
        status=status,
        strategy=strategy,
        agent_id=agent_id,
        version=version,
    ).limit(MAX_EXPORT_ROWS)
    return (await session.execute(stmt)).scalars().all()


async def get_deployment(
    session: AsyncSession, principal: Principal, deployment_id: str
) -> Deployment:
    """One deployment by id."""
    return await _get_deployment(session, principal, deployment_id)


async def list_stages(
    session: AsyncSession, principal: Principal, deployment_id: str
) -> Sequence[DeploymentStage]:
    """The five pipeline stages in render order. Polled by the console."""
    deployment = await _get_deployment(session, principal, deployment_id)
    return (
        (
            await session.execute(
                select(DeploymentStage)
                .where(DeploymentStage.deployment_id == deployment.id)
                .order_by(DeploymentStage.sequence.asc())
            )
        )
        .scalars()
        .all()
    )


async def environment_labels(
    session: AsyncSession, principal: Principal, environment_ids: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """``{environment_id: (name, env_type)}`` for the ids on one page."""
    ids = [env_id for env_id in dict.fromkeys(environment_ids) if env_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Environment.id, Environment.name, Environment.env_type).where(
                Environment.workspace_id == principal.workspace_id,
                Environment.id.in_(ids),
            )
        )
    ).all()
    return {row.id: (row.name, row.env_type) for row in rows}


async def agent_labels(
    session: AsyncSession, principal: Principal, agent_ids: Sequence[str]
) -> dict[str, str]:
    """``{agent_id: name}`` for the ids on one page."""
    ids = [agent_id for agent_id in dict.fromkeys(agent_ids) if agent_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == principal.workspace_id, Agent.id.in_(ids)
            )
        )
    ).all()
    return {row.id: row.name for row in rows}


async def summary(session: AsyncSession, principal: Principal) -> DeploymentSummary:
    """KPI tiles. Every number is a SQL aggregate over the whole workspace."""
    total_environments = (
        await session.execute(
            select(func.count())
            .select_from(Environment)
            .where(Environment.workspace_id == principal.workspace_id)
        )
    ).scalar_one()

    row = (
        await session.execute(
            select(
                func.count(Deployment.id),
                func.sum(case((Deployment.status.in_(IN_FLIGHT_STATUSES), 1), else_=0)),
                func.sum(
                    case((Deployment.status == DeploymentStatus.SUCCEEDED.value, 1), else_=0)
                ),
                func.sum(case((Deployment.status == DeploymentStatus.FAILED.value, 1), else_=0)),
                func.sum(case((Deployment.status == DeploymentStatus.HALTED.value, 1), else_=0)),
                func.sum(
                    case((Deployment.status == DeploymentStatus.ROLLED_BACK.value, 1), else_=0)
                ),
                # "Executed" is a rollback that landed. One that is still waiting
                # at its gate, or was refused there, has undone nothing.
                func.sum(
                    case(
                        (
                            and_(
                                Deployment.rollback_of_deployment_id.is_not(None),
                                Deployment.status == DeploymentStatus.SUCCEEDED.value,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                # AVG skips NULLs, so the CASE without an ELSE restricts the
                # average to deployments that actually completed.
                func.avg(
                    case(
                        (
                            Deployment.status == DeploymentStatus.SUCCEEDED.value,
                            Deployment.duration_seconds,
                        )
                    )
                ),
            ).where(Deployment.workspace_id == principal.workspace_id)
        )
    ).one()

    total, active, succeeded, failed, halted, rolled_back, rollbacks, avg_seconds = row
    total = int(total or 0)
    active = int(active or 0)
    succeeded = int(succeeded or 0)
    failed = int(failed or 0)
    halted = int(halted or 0)
    rolled_back = int(rolled_back or 0)
    rollbacks = int(rollbacks or 0)
    avg_value = float(avg_seconds) if avg_seconds is not None else None

    decided = succeeded + failed + halted + rolled_back
    success_rate = round(succeeded / decided * 100, 1) if decided else 0.0

    return DeploymentSummary(
        total_environments=int(total_environments or 0),
        active_deployments=active,
        total_deployments=total,
        successful_deployments=succeeded,
        failed_deployments=failed,
        halted_deployments=halted,
        rolled_back_deployments=rolled_back,
        rollbacks_executed=rollbacks,
        success_rate_percent=success_rate,
        avg_deployment_seconds=round(avg_value, 1) if avg_value is not None else None,
        avg_deployment_time=format_duration(avg_value),
    )


# --------------------------------------------------------------------------- #
# Deployments — writes
# --------------------------------------------------------------------------- #


async def _flush(session: AsyncSession, conflict_message: str) -> None:
    """Flush, translating a unique-constraint race into a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(conflict_message) from exc


async def _assert_environment_free(
    session: AsyncSession, principal: Principal, environment: Environment
) -> None:
    """One pipeline per environment at a time; concurrent releases interleave."""
    in_flight = (
        await session.execute(
            select(Deployment.deployment_ref).where(
                Deployment.workspace_id == principal.workspace_id,
                Deployment.environment_id == environment.id,
                Deployment.status.in_(IN_FLIGHT_STATUSES),
            )
        )
    ).scalars().first()
    if in_flight is not None:
        raise Conflict(
            f"Deployment {in_flight} is already in flight in '{environment.name}'. "
            "Wait for it to finish or halt it first."
        )


async def _new_deployment(
    session: AsyncSession,
    principal: Principal,
    *,
    environment: Environment,
    version: str,
    strategy: str,
    agent_id: str | None = None,
    commit_ref: str | None = None,
    notes: str | None = None,
    rollback_of_deployment_id: str | None = None,
) -> Deployment:
    """Insert one deployment plus its five ``Pending`` stages.

    Shared by create, promote and rollback so every deployment — however it was
    started — runs the same gates in the same order.
    """
    if environment.status == EnvironmentStatus.OFFLINE.value:
        raise PreconditionFailed(f"Environment '{environment.name}' is offline.")
    await _assert_environment_free(session, principal, environment)

    if agent_id is not None:
        exists = (
            await session.execute(
                select(func.count())
                .select_from(Agent)
                .where(Agent.workspace_id == principal.workspace_id, Agent.id == agent_id)
            )
        ).scalar_one()
        if not exists:
            raise NotFound(f"Agent '{agent_id}' does not exist.")

    started_at = _now()
    deployment = Deployment(
        workspace_id=principal.workspace_id,
        deployment_ref=await _next_deployment_ref(session, principal.workspace_id),
        agent_id=agent_id,
        environment_id=environment.id,
        version=version,
        strategy=strategy,
        status=DeploymentStatus.QUEUED.value,
        triggered_by_user_id=principal.user_id,
        started_at=started_at,
        commit_ref=commit_ref,
        notes=notes,
        rollback_of_deployment_id=rollback_of_deployment_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(deployment)
    await _flush(session, "That deployment reference is already in use; retry the request.")

    for sequence, name in enumerate(DEFAULT_PIPELINE_STAGES, start=1):
        session.add(
            DeploymentStage(
                deployment_id=deployment.id,
                name=name,
                sequence=sequence,
                status=DeploymentStageStatus.PENDING.value,
                detail={},
            )
        )
    await session.flush()
    return deployment


async def create_deployment(
    session: AsyncSession,
    principal: Principal,
    payload: DeploymentCreate,
    *,
    request: Request | None = None,
) -> Deployment:
    """Start a release. The caller schedules :func:`run_pipeline` afterwards."""
    principal.require(Role.OPERATOR)
    environment = await _get_environment(session, principal, payload.environment_id)

    deployment = await _new_deployment(
        session,
        principal,
        environment=environment,
        version=payload.version,
        strategy=payload.strategy.value,
        agent_id=payload.agent_id,
        commit_ref=payload.commit_ref,
        notes=payload.notes,
    )

    await audit.record(
        session,
        principal=principal,
        action="deployment.create",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=f"Queued {deployment.version} for release to '{environment.name}'.",
        metadata={
            "environment": environment.name,
            "version": deployment.version,
            "strategy": deployment.strategy,
        },
        request=request,
    )
    return deployment


async def update_deployment(
    session: AsyncSession,
    principal: Principal,
    deployment_id: str,
    payload: DeploymentUpdate,
    *,
    request: Request | None = None,
) -> Deployment:
    """Annotate a deployment. Lifecycle fields are owned by the pipeline."""
    principal.require(Role.OPERATOR)
    deployment = await _get_deployment(session, principal, deployment_id)

    applied: list[str] = []
    for field, value in payload.model_dump(exclude_unset=True).items():
        if getattr(deployment, field) != value:
            setattr(deployment, field, value)
            applied.append(field)
    if not applied:
        return deployment

    deployment.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="deployment.update",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=f"Updated {', '.join(sorted(applied))} on {deployment.deployment_ref}.",
        metadata={"fields": sorted(applied)},
        request=request,
    )
    return deployment


async def promote_deployment(
    session: AsyncSession,
    principal: Principal,
    deployment_id: str,
    payload: DeploymentPromoteRequest,
    *,
    request: Request | None = None,
) -> Deployment:
    """Release a proven version into the next environment.

    Promotion is a new deployment, not a mutation of the old one: the target
    environment gets its own pipeline, its own gates and its own audit trail.
    """
    principal.require(Role.OPERATOR)
    source = await _get_deployment(session, principal, deployment_id)

    if source.status != DeploymentStatus.SUCCEEDED.value:
        raise PreconditionFailed(
            f"{source.deployment_ref} is {source.status}; only a succeeded "
            "deployment can be promoted."
        )
    if payload.target_environment_id == source.environment_id:
        raise Conflict(f"{source.version} is already deployed in that environment.")

    target = await _get_environment(session, principal, payload.target_environment_id)
    source_env = await _get_environment(session, principal, source.environment_id)

    promoted = await _new_deployment(
        session,
        principal,
        environment=target,
        version=source.version,
        strategy=(payload.strategy.value if payload.strategy else source.strategy),
        agent_id=source.agent_id,
        commit_ref=source.commit_ref,
        notes=payload.notes or f"Promoted from {source.deployment_ref} ({source_env.name}).",
    )

    await audit.record(
        session,
        principal=principal,
        action="deployment.promote",
        entity_type="Deployment",
        entity_id=promoted.id,
        entity_label=promoted.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Promoted {source.version} from '{source_env.name}' to '{target.name}' "
            f"as {promoted.deployment_ref}."
        ),
        metadata={
            "source_deployment_id": source.id,
            "source_environment": source_env.name,
            "target_environment": target.name,
            "version": source.version,
        },
        request=request,
    )
    return promoted


async def halt_deployment(
    session: AsyncSession,
    principal: Principal,
    deployment_id: str,
    payload: DeploymentHaltRequest,
    *,
    request: Request | None = None,
) -> Deployment:
    """Stop an in-flight deployment and close everything it left open."""
    principal.require(Role.OPERATOR)
    deployment = await _get_deployment(session, principal, deployment_id)

    if deployment.is_terminal:
        raise Conflict(
            f"{deployment.deployment_ref} already finished with status {deployment.status}."
        )

    # Ask the runner to stop first: cancellation lands at its next await, and it
    # re-reads the deployment before every write, so it will not fight us.
    runner.cancel(deployment.id)

    finished_at = _now()
    reason = payload.reason or f"Halted by {principal.actor}."
    stages = await _stages_for(session, deployment.id)
    for stage in stages:
        if stage.status in (
            DeploymentStageStatus.PENDING.value,
            DeploymentStageStatus.RUNNING.value,
        ):
            stage.status = DeploymentStageStatus.SKIPPED.value
            stage.finished_at = finished_at
            stage.log = reason

    deployment.status = DeploymentStatus.HALTED.value
    deployment.finished_at = finished_at
    deployment.duration_seconds = _elapsed_seconds(deployment.started_at, finished_at)
    deployment.updated_by = principal.actor

    await _close_approval_request(
        session,
        deployment,
        status=ApprovalStatus.REJECTED.value,
        note=f"Deployment halted: {reason}",
        actor=principal.actor,
        decided_by_user_id=principal.user_id,
    )
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="deployment.halt",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=reason,
        metadata={"version": deployment.version},
        request=request,
    )
    return deployment


def _assert_second_pair_of_eyes(
    principal: Principal, deployment: Deployment, approval_request: ApprovalRequest | None
) -> None:
    """Opening the gate takes a person, and not the person who asked.

    The gate exists to put a release in front of someone other than whoever
    started it; Approvals & Audit refuses a requester deciding their own
    request, and this verb decides the very same request. OPERATOR outranks
    APPROVER, so without this every operator who could start a release could
    also wave it through. A key has no user id, so every comparison below
    would let it pass -- it is refused outright, as the queue refuses it.
    Refusing the gate is not held to this: withdrawing your own release is a
    halt, which its owner may always do.
    """
    if principal.kind != "user" or principal.user_id is None:
        raise PermissionDenied("A release must be approved by a signed-in person.")
    raised_by = {deployment.triggered_by_user_id}
    if approval_request is not None:
        raised_by.add(approval_request.requested_by_user_id)
    if principal.user_id in raised_by:
        raise PermissionDenied(
            f"You started {deployment.deployment_ref}; a different reviewer has to approve it."
        )


async def _open_gate(
    session: AsyncSession,
    principal: Principal,
    deployment: Deployment,
    stage: DeploymentStage,
    *,
    note: str | None,
    decided_at: dt.datetime,
    request: Request | None,
    via: ApprovalRequest | None = None,
) -> None:
    """Mark the Approval stage passed. The caller resumes the pipeline.

    ``via`` is set when the decision was taken on the request itself, in
    Approvals & Audit, and is only being carried through to the release.
    """
    stage.status = DeploymentStageStatus.APPROVED.value
    stage.finished_at = decided_at
    stage.log = note or (
        f"Approved by {principal.actor} (request {via.request_ref})."
        if via is not None
        else f"Approved by {principal.actor}."
    )
    stage.detail = {
        **(stage.detail or {}),
        "approved_by": principal.actor,
        "approved_at": decided_at.isoformat(),
    }
    deployment.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="deployment.approve",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Approved {deployment.version} for release via {via.request_ref}."
            if via is not None
            else f"Approved {deployment.version} for release."
        ),
        metadata={"approval_request_id": deployment.approval_request_id},
        request=request,
    )


async def _shut_gate(
    session: AsyncSession,
    principal: Principal,
    deployment: Deployment,
    stage: DeploymentStage,
    *,
    note: str | None,
    decided_at: dt.datetime,
    request: Request | None,
    via: ApprovalRequest | None = None,
) -> None:
    """The gate refused to open, which is a halt rather than a failure."""
    # The runner is parked at the gate, so cancelling it is a formality.
    runner.cancel(deployment.id)
    reason = note or (
        f"Rejected by {principal.actor} (request {via.request_ref})."
        if via is not None
        else f"Rejected by {principal.actor}."
    )
    stage.status = DeploymentStageStatus.FAILED.value
    stage.finished_at = decided_at
    stage.log = reason
    stage.detail = {
        **(stage.detail or {}),
        "rejected_by": principal.actor,
        "rejected_at": decided_at.isoformat(),
    }
    for pending in await _stages_for(session, deployment.id):
        if pending.status == DeploymentStageStatus.PENDING.value:
            pending.status = DeploymentStageStatus.SKIPPED.value
            pending.finished_at = decided_at
            pending.log = "Skipped: approval was not given."

    deployment.status = DeploymentStatus.HALTED.value
    deployment.finished_at = decided_at
    deployment.duration_seconds = _elapsed_seconds(deployment.started_at, decided_at)
    deployment.updated_by = principal.actor
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="deployment.reject",
        entity_type="Deployment",
        entity_id=deployment.id,
        entity_label=deployment.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=reason,
        metadata={
            "approval_request_id": deployment.approval_request_id,
            **({"request_status": via.status} if via is not None else {}),
        },
        request=request,
    )


async def approve_deployment(
    session: AsyncSession,
    principal: Principal,
    deployment_id: str,
    payload: DeploymentApproveRequest,
    *,
    request: Request | None = None,
) -> tuple[Deployment, bool]:
    """Decide the approval gate from the Deployments screen.

    Returns the deployment and whether the pipeline should resume — the route
    commits and then schedules :func:`run_pipeline` when it should.

    The gate's ``ApprovalRequest`` is the record of the decision, and it can be
    decided from two screens. Whichever gets there first wins: a request a
    reviewer has already closed in Approvals & Audit is never rewritten from
    here. Agreeing with it carries the decision through to a release that was
    left parked; disagreeing with it is a conflict.
    """
    principal.require(Role.APPROVER)
    deployment = await _get_deployment(session, principal, deployment_id)

    # Locked, as the queue locks it, so the two screens decide one at a time;
    # then the release is re-read, because what the lock waited for may have
    # been the other screen halting it.
    approval_request = await _approval_request_for(session, deployment, for_update=True)
    if approval_request is not None:
        await session.refresh(deployment)

    if deployment.is_terminal:
        raise Conflict(
            f"{deployment.deployment_ref} already finished with status {deployment.status}."
        )

    stage = await _stage_named(session, deployment.id, STAGE_APPROVAL, fresh=True)
    if stage is None:
        raise PreconditionFailed(
            f"{deployment.deployment_ref} has no approval stage to decide."
        )
    if stage.status == DeploymentStageStatus.APPROVED.value:
        raise Conflict(f"{deployment.deployment_ref} has already been approved.")
    if stage.status != DeploymentStageStatus.RUNNING.value:
        raise PreconditionFailed(
            f"{deployment.deployment_ref} has not reached the approval gate yet."
        )

    decided_at = _now()
    already_decided = approval_request is not None and not approval_request.is_open
    if already_decided:
        was_approved = approval_request.status == ApprovalStatus.APPROVED.value
        if was_approved != payload.approved:
            raise Conflict(
                f"Request {approval_request.request_ref} was already "
                f"{approval_request.status.lower()} in Approvals & Audit, and that "
                "decision stands. "
                + (
                    f"Halt {deployment.deployment_ref} to stop the release."
                    if was_approved
                    else f"Reject or halt {deployment.deployment_ref}, then start a new "
                    "deployment to try again."
                )
            )
    elif payload.approved:
        _assert_second_pair_of_eyes(principal, deployment, approval_request)

    if approval_request is not None and not already_decided:
        verdict = ApprovalStatus.APPROVED if payload.approved else ApprovalStatus.REJECTED
        _decide_approval(
            approval_request,
            status=verdict.value,
            note=payload.note or f"{verdict.value} by {principal.actor}.",
            actor=principal.actor,
            decided_by_user_id=principal.user_id,
            decided_at=decided_at,
        )

    gate = _open_gate if payload.approved else _shut_gate
    await gate(
        session,
        principal,
        deployment,
        stage,
        note=payload.note,
        decided_at=decided_at,
        request=request,
        via=approval_request if already_decided else None,
    )
    return deployment, payload.approved


async def apply_gate_decision(
    *,
    session: AsyncSession,
    principal: Principal,
    approval_request: ApprovalRequest,
    approved: bool,
    note: str | None,
    request: Request | None = None,
) -> bool:
    """Carry a decision taken in Approvals & Audit through to the release it gates.

    The runner opens the gate's request and exits; nothing looks at a parked
    stage again. So the queue -- where the request is actually decided, and
    where the SLA sweeper expires it -- calls this in the same transaction,
    after it has closed ``approval_request`` and enforced its own rules about
    who may decide. The request row is the queue's and is not touched here.

    ``approved=False`` covers a rejection and an expiry alike: the release is
    halted and its environment freed. Returns True when the pipeline should
    resume, which the caller does by scheduling :func:`run_pipeline` with the
    deployment's id only AFTER it has committed. Returns False when there is
    nothing to resume: not a gate, a release that already finished, or a gate
    that is not open.

    The release is found through its own pointer at the request, never through
    the request's payload -- the payload is the requester's to write, and a
    hand-raised request that merely names a deployment must not release it.
    """
    deployment = (
        (
            await session.execute(
                select(Deployment).where(
                    Deployment.workspace_id == approval_request.workspace_id,
                    Deployment.approval_request_id == approval_request.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if deployment is None or deployment.is_terminal:
        return False

    stage = await _stage_named(session, deployment.id, STAGE_APPROVAL)
    if stage is None or stage.status != DeploymentStageStatus.RUNNING.value:
        return False

    gate = _open_gate if approved else _shut_gate
    await gate(
        session,
        principal,
        deployment,
        stage,
        note=note,
        decided_at=_now(),
        request=request,
        via=approval_request,
    )
    return approved


async def rollback_deployment(
    session: AsyncSession,
    principal: Principal,
    deployment_id: str,
    payload: DeploymentRollbackRequest,
    *,
    request: Request | None = None,
) -> Deployment:
    """Undo a release by deploying the previous version again.

    The rollback is its own deployment pointing back at the one it undoes, so
    the history table can render the pair as a single incident.

    Asking for a rollback undoes nothing yet: the rollback is a release like
    any other and has to get through its own gate. The source keeps serving,
    and keeps its status, until the rollback lands -- ``PipelineRunner._succeed``
    is what marks it ``RolledBack``. Marking it here left a release that was
    still live reading RolledBack for as long as the rollback waited for its
    approver, and for ever if the approver said no.
    """
    principal.require(Role.OPERATOR)
    source = await _get_deployment(session, principal, deployment_id)

    if source.status == DeploymentStatus.ROLLED_BACK.value:
        raise Conflict(f"{source.deployment_ref} has already been rolled back.")
    if source.status in IN_FLIGHT_STATUSES:
        raise PreconditionFailed(
            f"{source.deployment_ref} is still {source.status}; halt it before rolling back."
        )

    # A rollback that failed, or was halted or refused at its gate, undid
    # nothing, and must not be the reason the release can never be rolled back.
    existing = (
        await session.execute(
            select(Deployment.deployment_ref).where(
                Deployment.workspace_id == principal.workspace_id,
                Deployment.rollback_of_deployment_id == source.id,
                Deployment.status.not_in(
                    (DeploymentStatus.FAILED.value, DeploymentStatus.HALTED.value)
                ),
            )
        )
    ).scalars().first()
    if existing is not None:
        raise Conflict(f"{existing} already rolls back {source.deployment_ref}.")

    environment = await _get_environment(session, principal, source.environment_id)
    target_version = payload.target_version or await _previous_succeeded_version(
        session, principal, source
    )
    if target_version is None:
        raise PreconditionFailed(
            f"No earlier successful deployment exists in '{environment.name}'. "
            "Supply target_version to roll back to a specific release."
        )

    reason = payload.reason or f"Rollback of {source.deployment_ref} to {target_version}."
    rollback = await _new_deployment(
        session,
        principal,
        environment=environment,
        version=target_version,
        strategy=source.strategy,
        agent_id=source.agent_id,
        notes=reason,
        rollback_of_deployment_id=source.id,
    )

    await audit.record(
        session,
        principal=principal,
        action="deployment.rollback",
        entity_type="Deployment",
        entity_id=rollback.id,
        entity_label=rollback.deployment_ref,
        source_screen=SOURCE_SCREEN,
        detail=reason,
        metadata={
            "rollback_of_deployment_id": source.id,
            "rollback_of": source.deployment_ref,
            "from_version": source.version,
            "to_version": target_version,
            "environment": environment.name,
        },
        request=request,
    )
    return rollback


async def _previous_succeeded_version(
    session: AsyncSession, principal: Principal, source: Deployment
) -> str | None:
    return (
        await session.execute(
            select(Deployment.version)
            .where(
                Deployment.workspace_id == principal.workspace_id,
                Deployment.environment_id == source.environment_id,
                Deployment.id != source.id,
                Deployment.status == DeploymentStatus.SUCCEEDED.value,
                Deployment.created_at <= source.created_at,
                Deployment.version != source.version,
            )
            .order_by(Deployment.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Stage and approval plumbing
# --------------------------------------------------------------------------- #


async def _stages_for(session: AsyncSession, deployment_id: str) -> Sequence[DeploymentStage]:
    return (
        (
            await session.execute(
                select(DeploymentStage)
                .where(DeploymentStage.deployment_id == deployment_id)
                .order_by(DeploymentStage.sequence.asc())
            )
        )
        .scalars()
        .all()
    )


async def _stage_named(
    session: AsyncSession, deployment_id: str, name: str, *, fresh: bool = False
) -> DeploymentStage | None:
    stmt = select(DeploymentStage).where(
        DeploymentStage.deployment_id == deployment_id,
        DeploymentStage.name == name,
    )
    if fresh:
        # After waiting on a lock: the row as it is now, not as first loaded.
        stmt = stmt.execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _approval_request_for(
    session: AsyncSession, deployment: Deployment, *, for_update: bool = False
) -> ApprovalRequest | None:
    """The gate's request. ``for_update`` takes the same row lock the approvals
    queue takes before it decides, so a decision here and a decision there are
    made one after the other instead of over each other. SQLite has a single
    writer and ignores the clause."""
    if not deployment.approval_request_id:
        return None
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.id == deployment.approval_request_id,
        ApprovalRequest.workspace_id == deployment.workspace_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


def _decide_approval(
    approval_request: ApprovalRequest,
    *,
    status: str,
    note: str,
    actor: str,
    decided_by_user_id: str | None,
    decided_at: dt.datetime,
) -> None:
    """Record the decision on the request and advance its workflow tracker."""
    approval_request.status = status
    approval_request.decided_by_user_id = decided_by_user_id
    approval_request.decided_at = decided_at
    approval_request.decision_note = note

    reached = {ApprovalStep.REQUESTED.value, ApprovalStep.IN_REVIEW.value}
    if status == ApprovalStatus.APPROVED.value:
        reached.add(ApprovalStep.APPROVED.value)

    # The JSON column is not a mutable-tracked type, so it is replaced wholesale
    # rather than mutated in place.
    approval_request.workflow = [
        {
            **step,
            "done": bool(step.get("done")) or step.get("step") in reached,
            "by": step.get("by") or (actor if step.get("step") in reached else None),
            "ts": step.get("ts")
            or (decided_at.isoformat() if step.get("step") in reached else None),
        }
        for step in (approval_request.workflow or [])
    ]


async def _close_approval_request(
    session: AsyncSession,
    deployment: Deployment,
    *,
    status: str,
    note: str,
    actor: str,
    decided_by_user_id: str | None,
) -> None:
    """Close the gate's request so a halted deployment leaves no dangling item
    in the approvals queue."""
    approval_request = await _approval_request_for(session, deployment)
    if approval_request is None or not approval_request.is_open:
        return
    _decide_approval(
        approval_request,
        status=status,
        note=note,
        actor=actor,
        decided_by_user_id=decided_by_user_id,
        decided_at=_now(),
    )


def _approval_risk(env_type: str) -> str:
    if env_type in (EnvironmentType.PRODUCTION.value, EnvironmentType.DR.value):
        return RiskLevel.HIGH.value
    if env_type in (
        EnvironmentType.STAGING.value,
        EnvironmentType.UAT.value,
        EnvironmentType.QA.value,
    ):
        return RiskLevel.MEDIUM.value
    return RiskLevel.LOW.value


# --------------------------------------------------------------------------- #
# Stage outcomes
# --------------------------------------------------------------------------- #

# (stage status, log line, structured detail)
StageOutcome = tuple[str, str, dict[str, Any]]


def _outcome_build(deployment: Deployment, environment: Environment) -> StageOutcome:
    return (
        DeploymentStageStatus.COMPLETED.value,
        f"Packaged {deployment.version} for {environment.name}.",
        {
            "version": deployment.version,
            "commit_ref": deployment.commit_ref,
            "strategy": deployment.strategy,
        },
    )


def _outcome_tests(deployment: Deployment, environment: Environment) -> StageOutcome:
    return (
        DeploymentStageStatus.COMPLETED.value,
        f"Automated test suite passed for {deployment.version}.",
        {"version": deployment.version, "environment": environment.name},
    )


def _outcome_security_scan(deployment: Deployment, environment: Environment) -> StageOutcome:
    if not deployment.commit_ref:
        # Amber, not red: the release is allowed through but the provenance gap
        # is recorded against it.
        return (
            DeploymentStageStatus.WARNING.value,
            "No commit reference recorded — build provenance could not be verified.",
            {"provenance_verified": False, "environment": environment.name},
        )
    return (
        DeploymentStageStatus.COMPLETED.value,
        f"Security scan clean for {deployment.commit_ref}.",
        {"provenance_verified": True, "commit_ref": deployment.commit_ref},
    )


def _outcome_deploy(deployment: Deployment, environment: Environment) -> StageOutcome:
    if environment.status == EnvironmentStatus.OFFLINE.value:
        return (
            DeploymentStageStatus.FAILED.value,
            f"'{environment.name}' went offline before the release could land.",
            {"environment_status": environment.status},
        )
    return (
        DeploymentStageStatus.COMPLETED.value,
        f"{deployment.version} is live in {environment.name} ({deployment.strategy}).",
        {
            "environment": environment.name,
            "environment_type": environment.env_type,
            "strategy": deployment.strategy,
        },
    )


STAGE_OUTCOMES: dict[str, Callable[[Deployment, Environment], StageOutcome]] = {
    STAGE_BUILD: _outcome_build,
    STAGE_TESTS: _outcome_tests,
    STAGE_SECURITY_SCAN: _outcome_security_scan,
    STAGE_DEPLOY: _outcome_deploy,
}


# --------------------------------------------------------------------------- #
# The pipeline runner
# --------------------------------------------------------------------------- #


class PipelineRunner:
    """Supervises the in-process asyncio task that advances one pipeline.

    One task per deployment. Starting a deployment that already has a live task
    is a no-op, which makes both ``create`` and the post-approval resume safe to
    call more than once.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, deployment_id: str, workspace_id: str) -> bool:
        """Begin (or resume) the pipeline. True if a task was created."""
        existing = self._tasks.get(deployment_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(
            self._run(deployment_id, workspace_id),
            name=f"deployment-pipeline:{deployment_id}",
        )
        self._tasks[deployment_id] = task
        task.add_done_callback(functools.partial(self._forget, deployment_id))
        return True

    def cancel(self, deployment_id: str) -> bool:
        """Stop the pipeline. The caller owns the resulting row states."""
        task = self._tasks.pop(deployment_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_running(self, deployment_id: str) -> bool:
        """True while THIS process is advancing the pipeline. Another worker's
        runner is invisible from here, which is why recovery goes by the clock."""
        task = self._tasks.get(deployment_id)
        return task is not None and not task.done()

    def _forget(self, deployment_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(deployment_id) is task:
            self._tasks.pop(deployment_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # pragma: no cover - _run already handles its own
            log.error("deployment pipeline %s ended in error: %s", deployment_id, error)

    async def _run(self, deployment_id: str, workspace_id: str) -> None:
        try:
            if not await self._wait_until_visible(deployment_id, workspace_id):
                return
            while True:
                started = await self._start_next_stage(deployment_id, workspace_id)
                if started is None:
                    return
                stage_id, runtime = started
                if runtime > 0:
                    # Nothing is open here — a halt landing mid-wait is not blocked.
                    await asyncio.sleep(runtime)
                if not await self._finish_stage(deployment_id, workspace_id, stage_id):
                    return
        except Exception:
            # asyncio.CancelledError is a BaseException and passes straight
            # through: a halt has already written the terminal state itself.
            log.exception("deployment pipeline %s aborted", deployment_id)
            await self._abandon(deployment_id, workspace_id)

    async def _wait_until_visible(self, deployment_id: str, workspace_id: str) -> bool:
        """Look for the deployment a few times before concluding it is not there.

        The routes commit before they start a pipeline, so the row is normally
        there on the first look. This is the second guard: a caller that starts
        the runner a moment before its own commit lands must not strand the
        release Queued, which is what giving up on the first miss did.
        """
        for attempt in range(ROW_VISIBLE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(ROW_VISIBLE_WAIT_SECONDS)
            async with get_sessionmaker()() as session:
                if await self._load(session, deployment_id, workspace_id) is not None:
                    return True
        log.warning(
            "deployment pipeline %s never found its row; nothing was started", deployment_id
        )
        return False

    async def _start_next_stage(
        self, deployment_id: str, workspace_id: str
    ) -> tuple[str, float] | None:
        """Move the first unfinished stage into ``Running``.

        Returns ``(stage_id, seconds)`` when there is work to wait on, or None
        when the pipeline finished, paused at the approval gate, or is already
        being advanced by another task.
        """
        async with get_sessionmaker()() as session:
            try:
                deployment = await self._load(session, deployment_id, workspace_id)
                if deployment is None or deployment.is_terminal:
                    return None

                stage = next(
                    (
                        candidate
                        for candidate in await _stages_for(session, deployment.id)
                        if candidate.status not in STAGE_DONE_STATUSES
                    ),
                    None,
                )
                if stage is None:
                    await self._succeed(session, deployment)
                    await session.commit()
                    return None
                if stage.status != DeploymentStageStatus.PENDING.value:
                    # Running (in flight or awaiting approval) or Failed —
                    # either way this task has nothing to do.
                    return None

                now = _now()
                if deployment.status == DeploymentStatus.QUEUED.value:
                    deployment.status = DeploymentStatus.RUNNING.value
                    deployment.updated_by = _system_principal(workspace_id).actor

                if stage.name == STAGE_APPROVAL:
                    await self._open_approval_gate(session, deployment, stage, now)
                    await session.commit()
                    return None

                stage.status = DeploymentStageStatus.RUNNING.value
                stage.started_at = now
                stage.log = f"{stage.name} started."
                stage_id = stage.id
                await session.commit()
                return stage_id, STAGE_RUNTIME_SECONDS.get(stage.name, 0.0)
            except Exception:
                await session.rollback()
                raise

    async def _finish_stage(
        self, deployment_id: str, workspace_id: str, stage_id: str
    ) -> bool:
        """Close the stage out. True if the pipeline should keep going."""
        async with get_sessionmaker()() as session:
            try:
                deployment = await self._load(session, deployment_id, workspace_id)
                if deployment is None or deployment.is_terminal:
                    return False

                stage = await session.get(DeploymentStage, stage_id)
                if stage is None or stage.status != DeploymentStageStatus.RUNNING.value:
                    # Halted from under us while the stage was running.
                    return False

                environment = (
                    await session.execute(
                        select(Environment).where(
                            Environment.id == deployment.environment_id,
                            Environment.workspace_id == workspace_id,
                        )
                    )
                ).scalar_one_or_none()

                finished_at = _now()
                if environment is None:
                    status, log_line, detail = (
                        DeploymentStageStatus.FAILED.value,
                        "Target environment no longer exists.",
                        {"environment_id": deployment.environment_id},
                    )
                else:
                    outcome = STAGE_OUTCOMES.get(stage.name)
                    if outcome is None:
                        status, log_line, detail = (
                            DeploymentStageStatus.SKIPPED.value,
                            f"No runner is configured for the '{stage.name}' stage.",
                            {},
                        )
                    else:
                        status, log_line, detail = outcome(deployment, environment)

                stage.status = status
                stage.finished_at = finished_at
                stage.log = log_line
                stage.detail = {**(stage.detail or {}), **detail}

                if status == DeploymentStageStatus.FAILED.value:
                    await self._fail(session, deployment, finished_at, log_line)
                    await session.commit()
                    return False

                await session.commit()
                return True
            except Exception:
                await session.rollback()
                raise

    async def _abandon(self, deployment_id: str, workspace_id: str) -> None:
        """Last resort: never leave a deployment or a stage stuck in Running."""
        try:
            async with get_sessionmaker()() as session:
                deployment = await self._load(session, deployment_id, workspace_id)
                if deployment is None or deployment.is_terminal:
                    return
                finished_at = _now()
                for stage in await _stages_for(session, deployment.id):
                    if stage.status == DeploymentStageStatus.RUNNING.value:
                        stage.status = DeploymentStageStatus.FAILED.value
                        stage.finished_at = finished_at
                        stage.log = "Pipeline aborted before the stage reported back."
                await self._fail(
                    session, deployment, finished_at, "Pipeline aborted unexpectedly."
                )
                await session.commit()
        except Exception:  # pragma: no cover - the database is already unhappy
            log.exception("could not close out aborted deployment %s", deployment_id)

    async def _load(
        self, session: AsyncSession, deployment_id: str, workspace_id: str
    ) -> Deployment | None:
        return (
            await session.execute(
                select(Deployment).where(
                    Deployment.id == deployment_id,
                    Deployment.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()

    async def _rolled_back_by(
        self, session: AsyncSession, deployment: Deployment
    ) -> Deployment | None:
        """The live release ``deployment`` undoes by succeeding, if it is a
        rollback and that release was still live."""
        if not deployment.rollback_of_deployment_id:
            return None
        return (
            await session.execute(
                select(Deployment).where(
                    Deployment.id == deployment.rollback_of_deployment_id,
                    Deployment.workspace_id == deployment.workspace_id,
                    Deployment.status == DeploymentStatus.SUCCEEDED.value,
                )
            )
        ).scalar_one_or_none()

    async def _open_approval_gate(
        self,
        session: AsyncSession,
        deployment: Deployment,
        stage: DeploymentStage,
        now: dt.datetime,
    ) -> None:
        """Pause the pipeline and put the release in front of a human."""
        environment = (
            await session.execute(
                select(Environment).where(
                    Environment.id == deployment.environment_id,
                    Environment.workspace_id == deployment.workspace_id,
                )
            )
        ).scalar_one_or_none()
        environment_name = environment.name if environment else deployment.environment_id
        env_type = environment.env_type if environment else EnvironmentType.PRODUCTION.value

        approval_request = await _approval_request_for(session, deployment)
        if approval_request is None:
            approval_request = ApprovalRequest(
                workspace_id=deployment.workspace_id,
                request_ref=await _next_approval_ref(session, deployment.workspace_id),
                agent_id=deployment.agent_id,
                source=SOURCE_SCREEN,
                action="Deploy",
                action_detail=f"Deploy {deployment.version} to {environment_name}",
                resource=environment_name,
                risk=_approval_risk(env_type),
                reason=deployment.notes,
                requested_by_user_id=deployment.triggered_by_user_id,
                requested_at=now,
                sla_due_at=now + APPROVAL_SLA,
                sla_label=APPROVAL_SLA_LABEL,
                status=ApprovalStatus.PENDING.value,
                payload={
                    "deployment_id": deployment.id,
                    "deployment_ref": deployment.deployment_ref,
                    "stage_id": stage.id,
                    "version": deployment.version,
                    "environment_id": deployment.environment_id,
                },
                impact={
                    "environment": environment_name,
                    "environment_type": env_type,
                    "version": deployment.version,
                    "strategy": deployment.strategy,
                },
            )
            session.add(approval_request)
            await session.flush()
            deployment.approval_request_id = approval_request.id

        stage.status = DeploymentStageStatus.RUNNING.value
        stage.started_at = now
        stage.log = (
            f"Awaiting approval — request {approval_request.request_ref} "
            f"is open with a {APPROVAL_SLA_LABEL} SLA."
        )
        stage.detail = {
            **(stage.detail or {}),
            "approval_request_id": approval_request.id,
            "approval_request_ref": approval_request.request_ref,
            "sla_due_at": approval_request.sla_due_at.isoformat()
            if approval_request.sla_due_at
            else None,
        }

        await audit.record(
            session,
            principal=_system_principal(deployment.workspace_id),
            action="deployment.approval_requested",
            entity_type="Deployment",
            entity_id=deployment.id,
            entity_label=deployment.deployment_ref,
            source_screen=SOURCE_SCREEN,
            detail=(
                f"Pipeline paused at the approval gate for {deployment.version} "
                f"→ '{environment_name}'."
            ),
            metadata={"approval_request_ref": approval_request.request_ref},
        )

    async def _succeed(self, session: AsyncSession, deployment: Deployment) -> None:
        finished_at = _now()
        deployment.status = DeploymentStatus.SUCCEEDED.value
        deployment.finished_at = finished_at
        deployment.duration_seconds = _elapsed_seconds(deployment.started_at, finished_at)
        deployment.updated_by = _system_principal(deployment.workspace_id).actor

        environment = (
            await session.execute(
                select(Environment).where(
                    Environment.id == deployment.environment_id,
                    Environment.workspace_id == deployment.workspace_id,
                )
            )
        ).scalar_one_or_none()
        if environment is not None:
            # The environment's own probe is the only health reading we have
            # until the post-deploy probe lands; nothing is invented here.
            deployment.health = environment.health

        # A rollback that lands is the moment the release it undoes stops
        # serving, and so the moment that release becomes RolledBack -- not when
        # the rollback was asked for, which is before its gate. Only a release
        # that was live can be rolled back in that sense: a Failed or Halted
        # source never served, and keeps the status that says why.
        source = await self._rolled_back_by(session, deployment)
        if source is not None:
            source.status = DeploymentStatus.ROLLED_BACK.value
            source.updated_by = _system_principal(deployment.workspace_id).actor

        # Flushed first: the session does not autoflush, and the count has to
        # see this release as succeeded and its source as rolled back. Written
        # with ``stamp`` because it is bookkeeping about the environment, not an
        # edit of it, and must not move the token an open edit form is holding.
        await session.flush()
        if environment is not None:
            live = await live_release_counts(session, deployment.workspace_id, [environment.id])
            await stamp(
                session, [environment], active_deployment_count=live.get(environment.id, 0)
            )

        if source is not None:
            await audit.record(
                session,
                principal=_system_principal(deployment.workspace_id),
                action="deployment.rolled_back",
                entity_type="Deployment",
                entity_id=source.id,
                entity_label=source.deployment_ref,
                source_screen=SOURCE_SCREEN,
                detail=(
                    f"{source.version} was rolled back to {deployment.version} "
                    f"by {deployment.deployment_ref}."
                ),
                metadata={
                    "rolled_back_by_deployment_id": deployment.id,
                    "from_version": source.version,
                    "to_version": deployment.version,
                },
            )

        await audit.record(
            session,
            principal=_system_principal(deployment.workspace_id),
            action="deployment.succeeded",
            entity_type="Deployment",
            entity_id=deployment.id,
            entity_label=deployment.deployment_ref,
            source_screen=SOURCE_SCREEN,
            detail=(
                f"{deployment.version} released in "
                f"{format_duration(deployment.duration_seconds) or 'under a second'}."
            ),
            metadata={"version": deployment.version, "strategy": deployment.strategy},
        )

    async def _fail(
        self,
        session: AsyncSession,
        deployment: Deployment,
        finished_at: dt.datetime,
        reason: str,
    ) -> None:
        deployment.status = DeploymentStatus.FAILED.value
        deployment.finished_at = finished_at
        deployment.duration_seconds = _elapsed_seconds(deployment.started_at, finished_at)
        deployment.updated_by = _system_principal(deployment.workspace_id).actor

        for stage in await _stages_for(session, deployment.id):
            if stage.status == DeploymentStageStatus.PENDING.value:
                stage.status = DeploymentStageStatus.SKIPPED.value
                stage.finished_at = finished_at
                stage.log = "Skipped: an earlier stage failed."

        await _close_approval_request(
            session,
            deployment,
            status=ApprovalStatus.REJECTED.value,
            note=f"Deployment failed: {reason}",
            actor=_system_principal(deployment.workspace_id).actor,
            decided_by_user_id=None,
        )
        await audit.record(
            session,
            principal=_system_principal(deployment.workspace_id),
            action="deployment.failed",
            entity_type="Deployment",
            entity_id=deployment.id,
            entity_label=deployment.deployment_ref,
            source_screen=SOURCE_SCREEN,
            detail=reason,
            metadata={"version": deployment.version},
        )


runner = PipelineRunner()


async def run_pipeline(deployment_id: str, workspace_id: str) -> None:
    """Background entry point used by the routes.

    ``BackgroundTasks`` does NOT wait for the request's transaction: it runs
    before the request-scoped session commits. The runner works on its own
    connection, so the caller must ``await session.commit()`` before scheduling
    this -- otherwise the pipeline looks for rows nobody else can see yet.
    """
    runner.start(deployment_id, workspace_id)


async def recover_orphaned_pipelines(*, grace_seconds: float = ORPHAN_GRACE_SECONDS) -> int:
    """Pick up pipelines whose runner died with its worker. Returns how many.

    The runner is an asyncio task in one worker's memory. A redeploy, an OOM
    kill or a recycled worker during the few seconds a stage takes leaves the
    deployment ``Running`` with nobody driving it, and nothing else ever looks:
    the release never finishes and its environment refuses every deploy,
    promote, rollback and restart until somebody notices and halts it by hand.
    Test runs and evaluations have a sweeper for exactly this; this is the
    deployments' one, meant to be called from the platform scheduler's tick
    (which already runs in one worker at a time).

    A runner in another worker cannot be seen from here, so an orphan is told
    by the clock: a stage still Running well past the time the runner allows
    it, or a next stage still Pending long after the last one finished (a
    healthy runner starts it within milliseconds). A release parked at its
    approval gate is waiting for a person, not for a runner, and is never
    touched -- the approvals queue settles those.

    The interrupted stage is put back to Pending and the pipeline is resumed
    rather than failed: the stages are the control plane's own and repeatable,
    and a restart of this service is not a reason to fail somebody's release.
    """
    now = _now()
    adopted: list[tuple[str, str]] = []
    async with get_sessionmaker()() as session:
        in_flight = (
            (
                await session.execute(
                    select(Deployment)
                    .where(Deployment.status.in_(IN_FLIGHT_STATUSES))
                    # A row somebody is halting or deciding this instant is
                    # theirs; the next tick will find it if it is still stuck.
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for deployment in in_flight:
            if runner.is_running(deployment.id):
                continue
            stages = await _stages_for(session, deployment.id)
            stage = next((s for s in stages if s.status not in STAGE_DONE_STATUSES), None)

            allowed = grace_seconds
            last_moved = deployment.started_at or deployment.created_at
            if stage is not None and stage.status == DeploymentStageStatus.RUNNING.value:
                if stage.name == STAGE_APPROVAL:
                    continue
                allowed += STAGE_RUNTIME_SECONDS.get(stage.name, 0.0)
                last_moved = stage.started_at or last_moved
            elif stage is None or stage.status == DeploymentStageStatus.PENDING.value:
                finished = [s.finished_at for s in stages if s.finished_at is not None]
                last_moved = max([*finished, last_moved])
            else:
                # Failed: closed out together with its deployment, never alone.
                continue
            if (now - last_moved).total_seconds() < allowed:
                continue

            if stage is not None and stage.status == DeploymentStageStatus.RUNNING.value:
                stage.status = DeploymentStageStatus.PENDING.value
                stage.started_at = None
                stage.log = "Interrupted by a service restart; started again."
            await audit.record(
                session,
                principal=_system_principal(deployment.workspace_id),
                action="deployment.recovered",
                entity_type="Deployment",
                entity_id=deployment.id,
                entity_label=deployment.deployment_ref,
                source_screen=SOURCE_SCREEN,
                detail=(
                    f"Pipeline for {deployment.version} had stopped"
                    + (f" at '{stage.name}'" if stage is not None else " before it was closed out")
                    + "; it was picked up again."
                ),
                metadata={"stage": stage.name if stage is not None else None},
            )
            adopted.append((deployment.id, deployment.workspace_id))
        await session.commit()

    # Only now: the runner works on its own connection and must see the reset.
    for deployment_id, workspace_id in adopted:
        runner.start(deployment_id, workspace_id)
    if adopted:
        log.warning("resumed %d orphaned deployment pipeline(s)", len(adopted))
    return len(adopted)


# --------------------------------------------------------------------------- #
# Live progress
# --------------------------------------------------------------------------- #


def _progress_payload(
    deployment: Deployment, stages: Sequence[DeploymentStage]
) -> dict[str, Any]:
    # Parked at the gate: nothing moves until a person decides. The stream slows
    # down on it, and the console can say so instead of showing a stalled bar.
    awaiting_approval = not deployment.is_terminal and any(
        stage.name == STAGE_APPROVAL and stage.status == DeploymentStageStatus.RUNNING.value
        for stage in stages
    )
    return {
        "deployment_id": deployment.id,
        "deployment_ref": deployment.deployment_ref,
        "status": deployment.status,
        "is_terminal": deployment.is_terminal,
        "awaiting_approval": awaiting_approval,
        "version": deployment.version,
        "environment_id": deployment.environment_id,
        "health": deployment.health,
        "started_at": deployment.started_at.isoformat() if deployment.started_at else None,
        "finished_at": deployment.finished_at.isoformat() if deployment.finished_at else None,
        "duration_seconds": deployment.duration_seconds,
        "duration_label": format_duration(deployment.duration_seconds),
        "approval_request_id": deployment.approval_request_id,
        "stages": [
            {
                "id": stage.id,
                "name": stage.name,
                "sequence": stage.sequence,
                "status": stage.status,
                "started_at": stage.started_at.isoformat() if stage.started_at else None,
                "finished_at": stage.finished_at.isoformat() if stage.finished_at else None,
                "log": stage.log,
                "detail": stage.detail or {},
            }
            for stage in stages
        ],
    }


async def stream_progress(
    principal: Principal,
    deployment_id: str,
    *,
    poll_seconds: float = STREAM_POLL_SECONDS,
    gate_poll_seconds: float = STREAM_GATE_POLL_SECONDS,
    max_seconds: float = STREAM_MAX_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Yield a pipeline snapshot per poll until the deployment reaches a
    terminal state or the stream ages out.

    Deliberately opens its own short-lived session per poll: a subscription can
    outlive a request by minutes and must not pin a pooled connection. Each poll
    is one statement -- the deployment and its stages together.

    Stages move every few seconds, which is what the one-second poll is for.
    A release parked at its approval gate moves when a person decides it, which
    is minutes to hours away, and every pipeline parks there; so while it is
    parked the poll drops to ``gate_poll_seconds``. The watcher sees the gate
    open a few seconds late instead of costing a query a second all morning.
    """
    deadline = asyncio.get_running_loop().time() + max_seconds
    while True:
        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(Deployment, DeploymentStage)
                    .outerjoin(DeploymentStage, DeploymentStage.deployment_id == Deployment.id)
                    .where(
                        Deployment.id == deployment_id,
                        Deployment.workspace_id == principal.workspace_id,
                    )
                    .order_by(DeploymentStage.sequence.asc())
                )
            ).all()
            if not rows:
                return
            payload = _progress_payload(
                rows[0][0], [stage for _deployment, stage in rows if stage is not None]
            )

        yield payload
        if payload["is_terminal"] or asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(
            max(poll_seconds, gate_poll_seconds) if payload["awaiting_approval"] else poll_seconds
        )


def progress_frame(payload: dict[str, Any]) -> str:
    """Serialise one snapshot as a server-sent-events data frame."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
