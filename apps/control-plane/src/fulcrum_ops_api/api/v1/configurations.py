"""Configuration Center routes.

Nineteen operations back one screen: the KPI cards, the filtered table and its
CSV download, the type tabs, the New/Edit/Clone dialogs, the Import and Export
buttons, and the inspector's four tabs — Overview, Versions (with diff and
Rollback to Previous), Usage and Audit Trail.

Handlers here only parse, delegate and shape. Workspace scoping, role checks,
the lifecycle state machine, body validation and the audit trail all live in
``services.configurations``. The Audit Trail tab is served by the audit router
with ``entity_type=configuration``; nothing in this module duplicates it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.registry import (
    ConfigurationStatus,
    ConfigurationType,
    EnvironmentType,
    ImpactLevel,
)
from ...schemas.configurations import (
    CONFIGURATION_SCHEMAS,
    ConfigurationActionResponse,
    ConfigurationBundle,
    ConfigurationCloneRequest,
    ConfigurationCreate,
    ConfigurationDeprecateRequest,
    ConfigurationDiff,
    ConfigurationImportRequest,
    ConfigurationImportResult,
    ConfigurationRead,
    ConfigurationRollbackRequest,
    ConfigurationSchemaRead,
    ConfigurationsSummary,
    ConfigurationUpdate,
    ConfigurationUsage,
    ConfigurationValidateRequest,
    ConfigurationValidationReport,
    ConfigurationVersionCreate,
    ConfigurationVersionDetail,
    ConfigurationVersionRead,
    normalise_version,
)
from ...services import configurations as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/configurations", tags=["Configurations"])


@dataclasses.dataclass(frozen=True)
class ConfigurationFilters:
    """The Configuration Center's five dropdowns."""

    config_type: str | None = None
    status: str | None = None
    environment: str | None = None
    impact: str | None = None
    owner_user_id: str | None = None


def configuration_filters(
    config_type: Annotated[
        ConfigurationType | None,
        Query(alias="type", description="Type dropdown and the type tabs."),
    ] = None,
    status_filter: Annotated[
        ConfigurationStatus | None,
        Query(alias="status", description="Active, Draft, Deprecated or Archived."),
    ] = None,
    environment: Annotated[
        EnvironmentType | None, Query(alias="env", description="Environment dropdown.")
    ] = None,
    impact: Annotated[
        ImpactLevel | None, Query(description="Impact level dropdown.")
    ] = None,
    owner_user_id: Annotated[
        str | None, Query(alias="owner", description="Owner dropdown, by user id.")
    ] = None,
) -> ConfigurationFilters:
    """Read the table's dropdown filters off the query string."""
    return ConfigurationFilters(
        config_type=config_type.value if config_type else None,
        status=status_filter.value if status_filter else None,
        environment=environment.value if environment else None,
        impact=impact.value if impact else None,
        owner_user_id=owner_user_id,
    )


Filters = Annotated[ConfigurationFilters, Depends(configuration_filters)]
Params = Annotated[ListParams, Depends(list_params)]


def _csv_row(configuration: ConfigurationRead) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, _header in service.EXPORT_COLUMNS:
        value = getattr(configuration, key, None)
        if isinstance(value, enum.Enum):
            value = value.value
        elif isinstance(value, dt.datetime):
            value = value.isoformat()
        row[key] = value
    return row


def _action(
    configuration: ConfigurationRead,
    message: str,
    *,
    version: ConfigurationVersionRead | None = None,
    validation: ConfigurationValidationReport | None = None,
) -> ConfigurationActionResponse:
    return ConfigurationActionResponse(
        configuration=configuration, message=message, version=version, validation=validation
    )


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{configuration_id}.
# ---------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=ConfigurationsSummary,
    summary="Configuration KPI summary",
)
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[
        int, Query(ge=1, le=365, description="Rolling window for the change counts.")
    ] = service.DEFAULT_WINDOW_DAYS,
) -> ConfigurationsSummary:
    """Totals by status plus the windowed change counts.

    Every number is a SQL aggregate — the status counts over the workspace's
    configurations, the change count over the audit trail this domain writes —
    so the cards stay correct on a workspace with thousands of assets.
    """
    return await service.summarise(session, principal, window_days=window_days)


@router.get(
    "/schema",
    response_model=list[ConfigurationSchemaRead],
    summary="Declared body schema per configuration type",
)
async def get_schemas(
    principal: CurrentPrincipal,
    config_type: Annotated[
        ConfigurationType | None,
        Query(alias="type", description="Restrict to one type."),
    ] = None,
) -> list[ConfigurationSchemaRead]:
    """The field contract ``/validate`` checks bodies against.

    The console's editor renders its form from this, so a field added here
    appears in the UI without a frontend change.
    """
    wanted = [config_type] if config_type else list(ConfigurationType)
    return [
        ConfigurationSchemaRead(
            config_type=candidate,
            fields=list(CONFIGURATION_SCHEMAS.get(candidate.value, ())),
        )
        for candidate in wanted
    ]


@router.get("/export", summary="Export configurations as CSV")
async def export_configurations(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> StreamingResponse:
    """Download the filtered configuration list as CSV.

    Honours exactly the search, sort and dropdowns the table is showing, so the
    file matches what the operator sees on screen.
    """
    rows = await service.export_configurations(
        session,
        principal,
        params,
        config_type=filters.config_type,
        status=filters.status,
        environment=filters.environment,
        impact=filters.impact,
        owner_user_id=filters.owner_user_id,
    )
    body = to_csv([_csv_row(row) for row in rows], service.EXPORT_COLUMNS)
    filename = f"configurations-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/bundle",
    response_model=ConfigurationBundle,
    summary="Export configurations as a JSON bundle",
)
async def export_bundle(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> ConfigurationBundle:
    """The filtered configurations and their live bodies, ready to re-import.

    This is the file ``POST /configurations/import`` accepts, so a bundle can be
    moved between workspaces without hand-editing.
    """
    return await service.build_bundle(
        session,
        principal,
        params,
        config_type=filters.config_type,
        status=filters.status,
        environment=filters.environment,
        impact=filters.impact,
        owner_user_id=filters.owner_user_id,
    )


@router.post(
    "/import",
    response_model=ConfigurationImportResult,
    summary="Import a configuration bundle",
)
async def import_bundle(
    principal: CurrentPrincipal,
    session: Db,
    payload: ConfigurationImportRequest,
    request: Request,
) -> ConfigurationImportResult:
    """Load a bundle of configurations.

    Imported rows always land as Draft: a body that arrived from somewhere else
    has not been validated against this environment. Names that already exist
    are skipped or receive a new draft revision, never overwritten. Requires the
    admin role.
    """
    return await service.import_bundle(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[ConfigurationRead], summary="List configurations")
async def list_configurations(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> Page[ConfigurationRead]:
    """One page of the workspace's configurations, most recently changed first.

    Free-text search covers the name, the type, the description and the owner's
    name; `sort` accepts any column key the table renders.
    """
    rows, total = await service.list_configurations(
        session,
        principal,
        params,
        config_type=filters.config_type,
        status=filters.status,
        environment=filters.environment,
        impact=filters.impact,
        owner_user_id=filters.owner_user_id,
    )
    return Page[ConfigurationRead].build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=ConfigurationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a configuration",
)
async def create_configuration(
    principal: CurrentPrincipal,
    session: Db,
    payload: ConfigurationCreate,
    request: Request,
) -> ConfigurationRead:
    """Register a configuration and its first revision.

    Both start Draft on purpose: only an activated version may be Active, and
    activation validates the body first. Requires the operator role.
    """
    configuration = await service.create_configuration(
        session, principal, payload, request=request
    )
    return await service.read_configuration(session, principal, configuration.id)


# ---------------------------------------------------------------------------
# Single configuration
# ---------------------------------------------------------------------------


@router.get(
    "/{configuration_id}", response_model=ConfigurationRead, summary="Get a configuration"
)
async def get_configuration(
    principal: CurrentPrincipal, session: Db, configuration_id: str
) -> ConfigurationRead:
    """One configuration. A row in another workspace answers 404, not 403."""
    return await service.read_configuration(session, principal, configuration_id)


@router.patch(
    "/{configuration_id}",
    response_model=ConfigurationRead,
    summary="Update a configuration",
)
async def update_configuration(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationUpdate,
    request: Request,
) -> ConfigurationRead:
    """Edit identity fields — name, type, environment, impact, owner, description.

    Bodies never move through here: cut a version instead. Send
    `expected_updated_at` to make the write conditional. Requires the operator role.
    """
    configuration = await service.update_configuration(
        session, principal, configuration_id, payload, request=request
    )
    return await service.read_configuration(session, principal, configuration.id)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@router.get(
    "/{configuration_id}/versions",
    response_model=Page[ConfigurationVersionRead],
    summary="List versions",
)
async def list_versions(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    params: Params,
) -> Page[ConfigurationVersionRead]:
    """Revision history for one configuration, newest first.

    Rows carry the change note and the author's name, which is what the
    inspector's Versions pipe renders.
    """
    rows, total = await service.list_versions(session, principal, configuration_id, params)
    return Page[ConfigurationVersionRead].build(rows, total, params.page, params.page_size)


@router.post(
    "/{configuration_id}/versions",
    response_model=ConfigurationActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new version",
)
async def create_version(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationVersionCreate,
    request: Request,
) -> ConfigurationActionResponse:
    """Cut a revision — the New Version dialog.

    With `activate` set (the default) the body is validated first and the
    request is refused with 422 listing every finding if validation fails; the
    previous current revision is demoted rather than deleted. Requires the
    operator role.
    """
    version, report = await service.create_version(
        session, principal, configuration_id, payload, request=request
    )
    configuration = await service.read_configuration(session, principal, configuration_id)
    verb = "created and activated" if payload.activate else "drafted"
    return _action(
        configuration,
        f"{configuration.name} {version.version} {verb}"
        + (f" with {report.warning_count} warning(s)." if report.warning_count else "."),
        version=ConfigurationVersionRead.model_validate(version),
        validation=report,
    )


@router.get(
    "/{configuration_id}/versions/diff",
    response_model=ConfigurationDiff,
    summary="Diff two versions",
)
async def diff_versions(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    from_version: Annotated[str, Query(alias="from", description="Baseline version.")],
    to_version: Annotated[str, Query(alias="to", description="Version to compare.")],
) -> ConfigurationDiff:
    """Field-level differences between two revisions.

    Bodies are flattened to dot-paths so the answer names the exact settings
    that moved; a list is one leaf, because reordering a routing table is one
    change to the table rather than one per rule.
    """
    return await service.diff_versions(
        session,
        principal,
        configuration_id,
        from_version=normalise_version(from_version),
        to_version=normalise_version(to_version),
    )


@router.get(
    "/{configuration_id}/versions/{version}",
    response_model=ConfigurationVersionDetail,
    summary="Get one version",
)
async def get_version(
    principal: CurrentPrincipal, session: Db, configuration_id: str, version: str
) -> ConfigurationVersionDetail:
    """One revision, body included."""
    return await service.get_version(
        session, principal, configuration_id, normalise_version(version)
    )


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@router.post(
    "/{configuration_id}/clone",
    response_model=ConfigurationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Clone a configuration",
)
async def clone_configuration(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationCloneRequest,
    request: Request,
) -> ConfigurationRead:
    """Copy a configuration and its live body into a fresh Draft.

    The clone defaults to '<name> (Copy)' and carries none of the original's
    history. Requires the operator role.
    """
    clone = await service.clone_configuration(
        session, principal, configuration_id, payload, request=request
    )
    return await service.read_configuration(session, principal, clone.id)


@router.post(
    "/{configuration_id}/rollback",
    response_model=ConfigurationActionResponse,
    summary="Roll back to a previous version",
)
async def rollback(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationRollbackRequest,
    request: Request,
) -> ConfigurationActionResponse:
    """Republish an earlier body as a new revision.

    History is never rewritten: the old revision keeps its row and its body, and
    the rollback lands on top as the next version, validated before it goes
    live. With no version named, the revision published immediately before the
    current one is used. Requires the admin role.
    """
    version, report = await service.rollback(
        session, principal, configuration_id, payload, request=request
    )
    configuration = await service.read_configuration(session, principal, configuration_id)
    return _action(
        configuration,
        f"{configuration.name} restored to a previous body as {version.version}.",
        version=ConfigurationVersionRead.model_validate(version),
        validation=report,
    )


@router.post(
    "/{configuration_id}/deprecate",
    response_model=ConfigurationActionResponse,
    summary="Deprecate or archive a configuration",
)
async def deprecate(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationDeprecateRequest,
    request: Request,
) -> ConfigurationActionResponse:
    """Retire a configuration; agents migrate at their next deploy.

    Refused with 409 when the lifecycle does not allow the move. Requires the
    admin role.
    """
    configuration = await service.deprecate(
        session, principal, configuration_id, payload, request=request
    )
    read = await service.read_configuration(session, principal, configuration.id)
    return _action(read, f"{read.name} {read.current_version or ''} is now {read.status.value}.")


@router.post(
    "/{configuration_id}/restore",
    response_model=ConfigurationActionResponse,
    summary="Restore a retired configuration",
)
async def restore(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    request: Request,
) -> ConfigurationActionResponse:
    """Bring a deprecated or archived configuration back into service.

    The current body is validated first: a configuration whose body no longer
    passes its schema must not silently come back. Requires the admin role.
    """
    configuration = await service.restore(
        session, principal, configuration_id, request=request
    )
    read = await service.read_configuration(session, principal, configuration.id)
    return _action(read, f"{read.name} is active again.")


@router.post(
    "/{configuration_id}/validate",
    response_model=ConfigurationValidationReport,
    summary="Validate a configuration body",
)
async def validate_configuration(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    payload: ConfigurationValidateRequest,
) -> ConfigurationValidationReport:
    """Check a body against the declared schema for its type.

    Findings are per field with a stable code and a severity; only errors block
    activation. Passing a `payload` checks an unsaved body, which is what the
    editor does as you type. This endpoint changes nothing and writes no audit row.
    """
    return await service.validate_configuration(
        session,
        principal,
        configuration_id,
        version=payload.version,
        payload=payload.payload,
        environment=payload.environment,
    )


@router.get(
    "/{configuration_id}/usage",
    response_model=ConfigurationUsage,
    summary="Usage and linked configurations",
)
async def get_usage(
    principal: CurrentPrincipal,
    session: Db,
    configuration_id: str,
    window_days: Annotated[
        int, Query(ge=1, le=365, description="Rolling window for the run counts.")
    ] = service.DEFAULT_WINDOW_DAYS,
) -> ConfigurationUsage:
    """Impact & Usage plus Linked Configurations for the inspector.

    Bound agents come from the registry; run counts and success rate come from
    the telemetry engine for those agents' projects. When no agent is bound the
    counts are null rather than zero, and if the engine is unreachable the call
    fails rather than reporting an outage as no traffic.
    """
    return await service.usage(
        session, principal, configuration_id, window_days=window_days
    )
