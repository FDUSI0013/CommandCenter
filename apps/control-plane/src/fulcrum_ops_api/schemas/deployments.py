"""Request and response models for the Deployment & Environment screen.

Two entities share this module because they share a screen: an ``Environment``
is a deployment target and a ``Deployment`` is one release into one target, and
the console renders them side by side (environments table, deployment history,
pipeline inspector, KPI row).

Read models mirror the stored row plus the handful of labels the table needs
(``environment_name``, ``agent_name``, ``last_deployment_at``) which are joined
in by the service rather than denormalised onto the table. Write models are
strict: enum-typed vocabularies, trimmed strings, bounded lengths — the database
columns are sized and the API refuses anything that would not fit.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..models.operations import DeploymentStrategy
from ..models.registry import EnvironmentStatus, EnvironmentType

# Reusable constrained strings. Every bound matches the column it lands in, so a
# value that validates here can always be stored.
EntityId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
EnvName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Region = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
EngineRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Version = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
CommitRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]

HealthPercent = Annotated[float, Field(ge=0, le=100)]


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #


class EnvironmentRead(BaseModel):
    """One deployment target, shaped for the environments table and inspector."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    env_type: str
    region: str | None = None
    status: str
    health: float | None = None
    description: str | None = None
    engine_environment_id: str | None = None
    active_deployment_count: int = 0

    # Joined in from the deployment history; not stored on the environment row.
    last_deployment_at: dt.datetime | None = None
    last_deployment_by: str | None = None

    created_by: str | None = None
    updated_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class EnvironmentCreate(BaseModel):
    """Provision a new deployment target."""

    name: EnvName
    env_type: EnvironmentType
    region: Region | None = None
    status: EnvironmentStatus = EnvironmentStatus.HEALTHY
    health: HealthPercent | None = Field(
        default=None, description="Reported uptime percentage; null until the first probe."
    )
    description: Description | None = None
    engine_environment_id: EngineRef | None = Field(
        default=None, description="Matching environment in the telemetry engine."
    )


class EnvironmentUpdate(BaseModel):
    """Partial update; every field is optional and only the supplied ones move."""

    name: EnvName | None = None
    env_type: EnvironmentType | None = None
    region: Region | None = None
    status: EnvironmentStatus | None = None
    health: HealthPercent | None = None
    description: Description | None = None
    engine_environment_id: EngineRef | None = None


class EnvironmentRestartRequest(BaseModel):
    """Body for ``POST /environments/{id}/restart``; the console sends ``{}``."""

    reason: Reason | None = None


# --------------------------------------------------------------------------- #
# Deployments
# --------------------------------------------------------------------------- #


class DeploymentStageRead(BaseModel):
    """One rung of the Build → Tests → Scan → Approval → Deploy pipeline."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    deployment_id: str
    name: str
    sequence: int
    status: str
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    log: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
    updated_at: dt.datetime


class DeploymentRead(BaseModel):
    """One release of one version into one environment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    deployment_ref: str
    agent_id: str | None = None
    agent_name: str | None = None
    environment_id: str
    environment_name: str | None = None
    environment_type: str | None = None
    version: str
    strategy: str
    status: str
    is_terminal: bool = False
    triggered_by_user_id: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    duration_seconds: int | None = None
    duration_label: str | None = None
    commit_ref: str | None = None
    notes: str | None = None
    approval_request_id: str | None = None
    rollback_of_deployment_id: str | None = None
    health: float | None = None

    created_by: str | None = None
    updated_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class DeploymentCreate(BaseModel):
    """Start a release. The five pipeline stages are created with it."""

    environment_id: EntityId
    version: Version
    agent_id: EntityId | None = Field(
        default=None, description="Null for a platform release not tied to one agent."
    )
    strategy: DeploymentStrategy = DeploymentStrategy.ROLLING
    commit_ref: CommitRef | None = None
    notes: Notes | None = None


class DeploymentUpdate(BaseModel):
    """Annotate a deployment after the fact. Lifecycle fields are not writable —
    status moves only through the pipeline and the halt/approve/rollback verbs."""

    commit_ref: CommitRef | None = None
    notes: Notes | None = None
    health: HealthPercent | None = None


class DeploymentPromoteRequest(BaseModel):
    """Promote a succeeded release into the next environment."""

    target_environment_id: EntityId
    strategy: DeploymentStrategy | None = Field(
        default=None, description="Defaults to the source deployment's strategy."
    )
    notes: Notes | None = None


class DeploymentHaltRequest(BaseModel):
    """Stop an in-flight deployment."""

    reason: Reason | None = None


class DeploymentApproveRequest(BaseModel):
    """Decide the approval gate. ``approved=false`` halts the deployment."""

    approved: bool = True
    note: Reason | None = None


class DeploymentRollbackRequest(BaseModel):
    """Undo a release by deploying the previous version again."""

    reason: Reason | None = None
    target_version: Version | None = Field(
        default=None,
        description="Defaults to the version of the last succeeded deployment in the environment.",
    )


class DeploymentSummary(BaseModel):
    """KPI row above the Deployment & Environment screen."""

    total_environments: int
    active_deployments: int
    total_deployments: int
    successful_deployments: int
    failed_deployments: int
    halted_deployments: int
    rolled_back_deployments: int
    rollbacks_executed: int
    success_rate_percent: float
    avg_deployment_seconds: float | None = None
    avg_deployment_time: str | None = None
