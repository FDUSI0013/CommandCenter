"""Configuration Center business logic.

The rules this module enforces are the ones that make a configuration registry
worth having:

* **History is append-only.** A body change is a new ``ConfigurationVersion``.
  Rollback republishes an old body as a *new* revision, so the record of what
  was live and when survives the rollback.
* **Only one revision is current.** Activating a revision clears the flag from
  whichever revision held it and repoints ``Configuration.current_version``.
* **Nothing reaches Active unvalidated.** Activation runs the body through the
  declared schema for its type and refuses on any error-severity finding.
* **Lifecycle is a state machine.** Draft, Active, Deprecated and Archived move
  only along the edges in ``ALLOWED_TRANSITIONS``.

Two invariants hold in every function below: every statement filters on
``principal.workspace_id``, so a row belonging to another tenant is
indistinguishable from a row that does not exist; and every state change writes
an audit row naming the Configuration Center as its source screen.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

from fastapi import Request
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..engine import get_engine_client
from ..models.governance import AuditEvent
from ..models.identity import Role, User
from ..models.registry import (
    Agent,
    Configuration,
    ConfigurationStatus,
    ConfigurationType,
    ConfigurationVersion,
    EnvironmentType,
    ImpactLevel,
)
from ..schemas.configurations import (
    LINK_SLOTS,
    LINKS_KEY,
    ConfigurationBundle,
    ConfigurationBundleItem,
    ConfigurationCloneRequest,
    ConfigurationCreate,
    ConfigurationDeprecateRequest,
    ConfigurationDiff,
    ConfigurationFieldChange,
    ConfigurationFieldSpec,
    ConfigurationFinding,
    ConfigurationImportIssue,
    ConfigurationImportRequest,
    ConfigurationImportResult,
    ConfigurationLink,
    ConfigurationRead,
    ConfigurationRollbackRequest,
    ConfigurationsSummary,
    ConfigurationStatusSlice,
    ConfigurationUpdate,
    ConfigurationUsage,
    ConfigurationValidationReport,
    ConfigurationVersionCreate,
    ConfigurationVersionDetail,
    ConfigurationVersionRead,
    FieldKind,
    FindingSeverity,
    ImportConflictMode,
    is_absolute_http_url,
    next_version,
    schema_for,
)
from . import audit

SOURCE_SCREEN: Final[str] = "Configuration Center"
ENTITY_TYPE: Final[str] = "configuration"

#: Default rolling window for the KPI cards and the usage panel.
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: Lifecycle edges. Anything not listed here is refused with 409.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    ConfigurationStatus.DRAFT.value: frozenset(
        {
            ConfigurationStatus.ACTIVE.value,
            ConfigurationStatus.DEPRECATED.value,
            ConfigurationStatus.ARCHIVED.value,
        }
    ),
    ConfigurationStatus.ACTIVE.value: frozenset(
        {ConfigurationStatus.DEPRECATED.value, ConfigurationStatus.ARCHIVED.value}
    ),
    ConfigurationStatus.DEPRECATED.value: frozenset(
        {ConfigurationStatus.ACTIVE.value, ConfigurationStatus.ARCHIVED.value}
    ),
    ConfigurationStatus.ARCHIVED.value: frozenset({ConfigurationStatus.ACTIVE.value}),
}

SORTABLE: Final[dict[str, Any]] = {
    "name": Configuration.name,
    "type": Configuration.config_type,
    "config_type": Configuration.config_type,
    "env": Configuration.environment,
    "environment": Configuration.environment,
    "status": Configuration.status,
    "version": Configuration.current_version,
    "current_version": Configuration.current_version,
    "impact": Configuration.impact,
    "modified": Configuration.updated_at,
    "updated_at": Configuration.updated_at,
    "created_at": Configuration.created_at,
    "owner": User.full_name,
}

VERSION_SORTABLE: Final[dict[str, Any]] = {
    "version": ConfigurationVersion.version,
    "status": ConfigurationVersion.status,
    "created_at": ConfigurationVersion.created_at,
    "published_at": ConfigurationVersion.published_at,
}

#: Column order of the CSV export, matching the table left to right.
EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("id", "Configuration ID"),
    ("name", "Name"),
    ("description", "Description"),
    ("config_type", "Type"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("current_version", "Version"),
    ("updated_at", "Last Modified"),
    ("owner_name", "Owner"),
    ("owner_team", "Team"),
    ("impact", "Impact"),
    ("version_count", "Versions"),
    ("created_at", "Created On"),
    ("updated_by", "Modified By"),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive instant as UTC; clients are not required to send an offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _scoped(principal: Principal) -> Select:
    return select(Configuration).where(Configuration.workspace_id == principal.workspace_id)


def _versions_scoped(principal: Principal, configuration_id: str) -> Select:
    return select(ConfigurationVersion).where(
        ConfigurationVersion.workspace_id == principal.workspace_id,
        ConfigurationVersion.configuration_id == configuration_id,
    )


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a body to dot-paths so a diff can be reported per field.

    Lists are leaves: reordering a routing table is one change to that table,
    not one change per element.
    """
    if not isinstance(value, dict):
        return {prefix or "": value}
    flat: dict[str, Any] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict) and child:
            flat.update(_flatten(child, path))
        else:
            flat[path] = child
    return flat


# ---------------------------------------------------------------------------
# Validation against the declared schema
# ---------------------------------------------------------------------------


def _kind_matches(kind: FieldKind, value: Any) -> bool:
    if kind is FieldKind.STRING:
        return isinstance(value, str) and bool(value.strip())
    if kind is FieldKind.URL:
        return is_absolute_http_url(value)
    if kind is FieldKind.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is FieldKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is FieldKind.BOOLEAN:
        return isinstance(value, bool)
    if kind is FieldKind.OBJECT:
        return isinstance(value, dict)
    return isinstance(value, list)


def _check_field(
    spec: ConfigurationFieldSpec, value: Any, findings: list[ConfigurationFinding]
) -> None:
    if not _kind_matches(spec.kind, value):
        expected = "an absolute http(s) URL" if spec.kind is FieldKind.URL else spec.kind.value
        findings.append(
            ConfigurationFinding(
                field=spec.name,
                severity=FindingSeverity.ERROR,
                code="type_mismatch",
                message=f"'{spec.name}' must be {expected}.",
            )
        )
        return

    if spec.choices and value not in spec.choices:
        findings.append(
            ConfigurationFinding(
                field=spec.name,
                severity=FindingSeverity.ERROR,
                code="not_allowed",
                message=(
                    f"'{spec.name}' must be one of: {', '.join(spec.choices)}. "
                    f"Got '{value}'."
                ),
            )
        )

    if spec.kind in (FieldKind.NUMBER, FieldKind.INTEGER):
        if spec.minimum is not None and float(value) < spec.minimum:
            findings.append(
                ConfigurationFinding(
                    field=spec.name,
                    severity=FindingSeverity.ERROR,
                    code="below_minimum",
                    message=f"'{spec.name}' must be at least {spec.minimum}.",
                )
            )
        if spec.maximum is not None and float(value) > spec.maximum:
            findings.append(
                ConfigurationFinding(
                    field=spec.name,
                    severity=FindingSeverity.ERROR,
                    code="above_maximum",
                    message=f"'{spec.name}' must be at most {spec.maximum}.",
                )
            )

    if spec.kind is FieldKind.ARRAY and spec.min_items and len(value) < spec.min_items:
        findings.append(
            ConfigurationFinding(
                field=spec.name,
                severity=FindingSeverity.ERROR,
                code="too_few_items",
                message=f"'{spec.name}' needs at least {spec.min_items} entry(ies).",
            )
        )


def declared_links(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Read the body's link block as ``[(slot, name), …]``.

    A slot may name one configuration or a list of them; anything else is left
    for validation to report rather than raised here.
    """
    block = payload.get(LINKS_KEY)
    if not isinstance(block, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for slot, value in block.items():
        if isinstance(value, str) and value.strip():
            pairs.append((str(slot), value.strip()))
        elif isinstance(value, list):
            pairs.extend(
                (str(slot), entry.strip())
                for entry in value
                if isinstance(entry, str) and entry.strip()
            )
    return pairs


def _validate_body(
    *,
    config_type: str,
    environment: str,
    payload: dict[str, Any],
    link_states: dict[str, ConfigurationStatus | None],
) -> list[ConfigurationFinding]:
    """Check one body against its declared schema. Pure; no IO."""
    specs = schema_for(config_type)
    findings: list[ConfigurationFinding] = []
    is_production = environment == EnvironmentType.PRODUCTION.value

    if specs and not payload:
        findings.append(
            ConfigurationFinding(
                field="",
                severity=FindingSeverity.ERROR,
                code="empty_body",
                message=f"A {config_type} configuration needs a body.",
            )
        )

    for spec in specs:
        value = payload.get(spec.name)
        if value is None:
            if spec.required:
                findings.append(
                    ConfigurationFinding(
                        field=spec.name,
                        severity=FindingSeverity.ERROR,
                        code="required_missing",
                        message=f"'{spec.name}' is required. {spec.description}".strip(),
                    )
                )
            elif spec.required_in_production and is_production:
                findings.append(
                    ConfigurationFinding(
                        field=spec.name,
                        severity=FindingSeverity.ERROR,
                        code="required_in_production",
                        message=(
                            f"'{spec.name}' must be set before this runs in Production."
                        ),
                    )
                )
            continue
        _check_field(spec, value, findings)

    declared = {spec.name for spec in specs}
    if declared:
        for key in payload:
            if key in declared or key == LINKS_KEY:
                continue
            findings.append(
                ConfigurationFinding(
                    field=str(key),
                    severity=FindingSeverity.WARNING,
                    code="unknown_field",
                    message=(
                        f"'{key}' is not part of the declared {config_type} schema and "
                        "will be ignored by the runtime."
                    ),
                )
            )

    block = payload.get(LINKS_KEY)
    if block is not None and not isinstance(block, dict):
        findings.append(
            ConfigurationFinding(
                field=LINKS_KEY,
                severity=FindingSeverity.ERROR,
                code="type_mismatch",
                message=f"'{LINKS_KEY}' must be an object of slot to configuration name.",
            )
        )
    for slot, name in declared_links(payload):
        field = f"{LINKS_KEY}.{slot}"
        if slot not in LINK_SLOTS:
            findings.append(
                ConfigurationFinding(
                    field=field,
                    severity=FindingSeverity.WARNING,
                    code="unknown_link_slot",
                    message=(
                        f"'{slot}' is not a link slot the console renders "
                        f"({', '.join(LINK_SLOTS)})."
                    ),
                )
            )
        state = link_states.get(name)
        if state is None:
            findings.append(
                ConfigurationFinding(
                    field=field,
                    severity=FindingSeverity.ERROR,
                    code="link_unresolved",
                    message=f"'{name}' does not exist in this workspace.",
                )
            )
        elif state in (ConfigurationStatus.DEPRECATED, ConfigurationStatus.ARCHIVED):
            findings.append(
                ConfigurationFinding(
                    field=field,
                    severity=FindingSeverity.WARNING,
                    code="link_retired",
                    message=f"'{name}' is {state.value.lower()}; pick a live configuration.",
                )
            )

    return findings


async def _resolve_links(
    session: AsyncSession, principal: Principal, payload: dict[str, Any]
) -> dict[str, ConfigurationStatus | None]:
    """Look up every configuration a body links to, by name, in this workspace."""
    names = {name for _slot, name in declared_links(payload)}
    if not names:
        return {}
    rows = (
        await session.execute(
            select(Configuration.name, Configuration.status).where(
                Configuration.workspace_id == principal.workspace_id,
                Configuration.name.in_(names),
            )
        )
    ).all()
    found = {name: ConfigurationStatus(status) for name, status in rows}
    return {name: found.get(name) for name in names}


def _report(
    configuration: Configuration,
    *,
    version: str,
    environment: str,
    payload: dict[str, Any],
    findings: list[ConfigurationFinding],
) -> ConfigurationValidationReport:
    errors = sum(1 for f in findings if f.severity is FindingSeverity.ERROR)
    warnings = sum(1 for f in findings if f.severity is FindingSeverity.WARNING)
    return ConfigurationValidationReport(
        configuration_id=configuration.id,
        version=version,
        config_type=ConfigurationType(configuration.config_type),
        environment=EnvironmentType(environment),
        valid=errors == 0,
        error_count=errors,
        warning_count=warnings,
        declared_fields=len(schema_for(configuration.config_type)),
        checked_fields=len([key for key in payload if key != LINKS_KEY]),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _version_counts(
    session: AsyncSession, principal: Principal, configuration_ids: Sequence[str]
) -> dict[str, int]:
    if not configuration_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ConfigurationVersion.configuration_id, func.count(ConfigurationVersion.id)
            )
            .where(
                ConfigurationVersion.workspace_id == principal.workspace_id,
                ConfigurationVersion.configuration_id.in_(configuration_ids),
            )
            .group_by(ConfigurationVersion.configuration_id)
        )
    ).all()
    return {configuration_id: int(count) for configuration_id, count in rows}


async def _owner_rows(session: AsyncSession, user_ids: Sequence[str]) -> dict[str, Any]:
    ids = [user_id for user_id in user_ids if user_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.team).where(User.id.in_(ids))
        )
    ).all()
    return {row.id: row for row in rows}


async def hydrate(
    session: AsyncSession, principal: Principal, rows: Sequence[Configuration]
) -> list[ConfigurationRead]:
    """Attach the owner's name and the revision count to each row."""
    if not rows:
        return []
    owners = await _owner_rows(session, [row.owner_user_id for row in rows])
    counts = await _version_counts(session, principal, [row.id for row in rows])
    hydrated: list[ConfigurationRead] = []
    for row in rows:
        owner = owners.get(row.owner_user_id) if row.owner_user_id else None
        hydrated.append(
            ConfigurationRead.model_validate(row).model_copy(
                update={
                    "owner_name": owner.full_name if owner else None,
                    "owner_team": owner.team if owner else None,
                    "version_count": counts.get(row.id, 0),
                }
            )
        )
    return hydrated


def _list_statement(
    principal: Principal,
    params: ListParams,
    *,
    config_type: str | None,
    status: str | None,
    environment: str | None,
    impact: str | None,
    owner_user_id: str | None,
) -> Select:
    stmt = _scoped(principal).outerjoin(User, User.id == Configuration.owner_user_id)
    stmt = apply_search(
        stmt,
        params,
        [Configuration.name, Configuration.config_type, Configuration.description, User.full_name],
    )
    stmt = apply_filters(
        stmt,
        {
            Configuration.config_type: config_type,
            Configuration.status: status,
            Configuration.environment: environment,
            Configuration.impact: impact,
            Configuration.owner_user_id: owner_user_id,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=Configuration.updated_at)


async def list_configurations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    config_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    impact: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[list[ConfigurationRead], int]:
    """One page of the workspace's configurations, filtered the way the table is."""
    stmt = _list_statement(
        principal,
        params,
        config_type=config_type,
        status=status,
        environment=environment,
        impact=impact,
        owner_user_id=owner_user_id,
    )
    rows, total = await paginate(session, stmt, params)
    return await hydrate(session, principal, rows), total


async def export_configurations(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    config_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    impact: str | None = None,
    owner_user_id: str | None = None,
) -> list[ConfigurationRead]:
    """Every row the current filters select, unpaged, for the CSV download."""
    stmt = _list_statement(
        principal,
        params,
        config_type=config_type,
        status=status,
        environment=environment,
        impact=impact,
        owner_user_id=owner_user_id,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return await hydrate(session, principal, rows)


async def get_configuration(
    session: AsyncSession, principal: Principal, configuration_id: str
) -> Configuration:
    """Load one configuration, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as a row that never existed —
    a 403 would confirm the id is real.
    """
    configuration = (
        await session.execute(_scoped(principal).where(Configuration.id == configuration_id))
    ).scalar_one_or_none()
    if configuration is None:
        raise NotFound(f"Configuration '{configuration_id}' does not exist.")
    return configuration


async def read_configuration(
    session: AsyncSession, principal: Principal, configuration_id: str
) -> ConfigurationRead:
    configuration = await get_configuration(session, principal, configuration_id)
    return (await hydrate(session, principal, [configuration]))[0]


async def _current_version(
    session: AsyncSession, principal: Principal, configuration_id: str
) -> ConfigurationVersion | None:
    return (
        await session.execute(
            _versions_scoped(principal, configuration_id).where(
                ConfigurationVersion.is_current.is_(True)
            )
        )
    ).scalar_one_or_none()


async def _version_by_label(
    session: AsyncSession, principal: Principal, configuration_id: str, version: str
) -> ConfigurationVersion:
    row = (
        await session.execute(
            _versions_scoped(principal, configuration_id).where(
                ConfigurationVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"Version '{version}' does not exist for this configuration.")
    return row


async def _hydrate_versions(
    session: AsyncSession, rows: Sequence[ConfigurationVersion]
) -> list[ConfigurationVersionRead]:
    owners = await _owner_rows(session, [row.author_user_id for row in rows])
    return [
        ConfigurationVersionRead.model_validate(row).model_copy(
            update={
                "author_name": (
                    owners[row.author_user_id].full_name
                    if row.author_user_id and row.author_user_id in owners
                    else None
                )
            }
        )
        for row in rows
    ]


async def list_versions(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    params: ListParams,
) -> tuple[list[ConfigurationVersionRead], int]:
    """Revision history for one configuration, newest first."""
    await get_configuration(session, principal, configuration_id)
    stmt = _versions_scoped(principal, configuration_id)
    stmt = apply_search(
        stmt, params, [ConfigurationVersion.version, ConfigurationVersion.change_note]
    )
    stmt = apply_sort(
        stmt, params, VERSION_SORTABLE, default=ConfigurationVersion.created_at
    )
    rows, total = await paginate(session, stmt, params)
    return await _hydrate_versions(session, rows), total


async def get_version(
    session: AsyncSession, principal: Principal, configuration_id: str, version: str
) -> ConfigurationVersionDetail:
    """One revision, body included."""
    await get_configuration(session, principal, configuration_id)
    row = await _version_by_label(session, principal, configuration_id, version)
    owners = await _owner_rows(session, [row.author_user_id])
    owner = owners.get(row.author_user_id) if row.author_user_id else None
    return ConfigurationVersionDetail.model_validate(row).model_copy(
        update={"author_name": owner.full_name if owner else None}
    )


async def diff_versions(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    *,
    from_version: str,
    to_version: str,
) -> ConfigurationDiff:
    """Field-level differences between two revisions of one configuration."""
    await get_configuration(session, principal, configuration_id)
    left = await _version_by_label(session, principal, configuration_id, from_version)
    right = await _version_by_label(session, principal, configuration_id, to_version)

    before = _flatten(left.payload or {})
    after = _flatten(right.payload or {})
    changes: list[ConfigurationFieldChange] = []
    added = removed = changed = 0

    for field in sorted(set(before) | set(after)):
        in_before, in_after = field in before, field in after
        if in_before and in_after:
            if before[field] == after[field]:
                continue
            changed += 1
            changes.append(
                ConfigurationFieldChange(
                    field=field, change="changed", before=before[field], after=after[field]
                )
            )
        elif in_after:
            added += 1
            changes.append(
                ConfigurationFieldChange(field=field, change="added", after=after[field])
            )
        else:
            removed += 1
            changes.append(
                ConfigurationFieldChange(field=field, change="removed", before=before[field])
            )

    return ConfigurationDiff(
        configuration_id=configuration_id,
        from_version=left.version,
        to_version=right.version,
        from_status=ConfigurationStatus(left.status),
        to_status=ConfigurationStatus(right.status),
        added=added,
        removed=removed,
        changed=changed,
        identical=not changes,
        changes=changes,
    )


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> ConfigurationsSummary:
    """The six KPI cards, computed entirely in SQL."""
    workspace = Configuration.workspace_id == principal.workspace_id
    since = _now() - dt.timedelta(days=window_days)

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(Configuration.id).label("total"),
                _count_where(
                    Configuration.status == ConfigurationStatus.ACTIVE.value
                ).label("active"),
                _count_where(Configuration.status == ConfigurationStatus.DRAFT.value).label(
                    "draft"
                ),
                _count_where(
                    Configuration.status == ConfigurationStatus.DEPRECATED.value
                ).label("deprecated"),
                _count_where(
                    Configuration.status == ConfigurationStatus.ARCHIVED.value
                ).label("archived"),
                _count_where(Configuration.created_at >= since).label("created_recent"),
            ).where(workspace)
        )
    ).one()

    changes = (
        await session.execute(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.entity_type == ENTITY_TYPE,
                AuditEvent.occurred_at >= since,
            )
        )
    ).scalar_one()

    versions = (
        await session.execute(
            select(func.count(ConfigurationVersion.id)).where(
                ConfigurationVersion.workspace_id == principal.workspace_id,
                ConfigurationVersion.created_at >= since,
            )
        )
    ).scalar_one()

    total = int(totals.total or 0)
    counts = {
        ConfigurationStatus.ACTIVE: int(totals.active or 0),
        ConfigurationStatus.DRAFT: int(totals.draft or 0),
        ConfigurationStatus.DEPRECATED: int(totals.deprecated or 0),
        ConfigurationStatus.ARCHIVED: int(totals.archived or 0),
    }
    breakdown = [
        ConfigurationStatusSlice(
            status=state,
            count=count,
            percent=round(count / total * 100, 1) if total else 0.0,
        )
        for state, count in counts.items()
    ]

    return ConfigurationsSummary(
        total=total,
        active=counts[ConfigurationStatus.ACTIVE],
        draft=counts[ConfigurationStatus.DRAFT],
        deprecated=counts[ConfigurationStatus.DEPRECATED],
        archived=counts[ConfigurationStatus.ARCHIVED],
        changes_30d=int(changes or 0),
        created_30d=int(totals.created_recent or 0),
        versions_30d=int(versions or 0),
        window_days=window_days,
        status_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Usage: which agents run this, and what the telemetry engine saw
# ---------------------------------------------------------------------------


def _bound_agents_stmt(
    principal: Principal, configuration: Configuration, payload: dict[str, Any]
) -> Select | None:
    """Statement selecting the agents this configuration is bound to.

    Only two bindings are recorded on the ``agents`` table: an agent names the
    model it serves and the environment it runs in. Nothing on that table
    records which guardrail, tool, quota or routing table an agent picked up, so
    for those types this returns None and the usage panel reports no bound
    agents rather than guessing at a number.
    """
    scoped = select(Agent).where(Agent.workspace_id == principal.workspace_id)

    if configuration.config_type == ConfigurationType.MODEL.value:
        names = {configuration.name}
        for key in ("model", "deployment"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
        return scoped.where(or_(*[Agent.model == name for name in sorted(names)]))

    if configuration.config_type == ConfigurationType.ENVIRONMENT.value:
        return scoped.where(Agent.environment == configuration.name)

    return None


def _stat_value(payload: Any, names: tuple[str, ...]) -> float | None:
    """Pull one metric out of the engine's stats envelope.

    The envelope is either a mapping of metric name to value or a list of
    ``{"name": …, "value": …}`` entries depending on the metric family, so this
    accepts both and returns None when the metric is simply absent.
    """
    if isinstance(payload, dict):
        for key in ("stats", "metrics", "data"):
            if key in payload:
                found = _stat_value(payload[key], names)
                if found is not None:
                    return found
        for name in names:
            value = payload.get(name)
            # Count metrics arrive as {"count": N, "deviation": M}.
            if isinstance(value, dict):
                value = value.get("count")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None
    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name", "")).lower() in names:
                value = entry.get("value")
                if isinstance(value, dict):
                    value = value.get("count")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
    return None


_TRACE_COUNT_NAMES: Final[tuple[str, ...]] = (
    "trace_count",
    "count",
    "traces",
    "total",
)
_ERROR_COUNT_NAMES: Final[tuple[str, ...]] = (
    "error_count",
    "errors",
    "failed_count",
    "trace_error_count",
)


async def usage(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> ConfigurationUsage:
    """Impact & Usage plus Linked Configurations for one configuration.

    Run counts come from the telemetry engine for the bound agents' projects.
    If the engine is unreachable the adapter's error propagates: an outage must
    not be rendered as zero traffic.
    """
    configuration = await get_configuration(session, principal, configuration_id)
    current = await _current_version(session, principal, configuration_id)
    payload = (current.payload or {}) if current is not None else {}

    stmt = _bound_agents_stmt(principal, configuration, payload)
    agents: list[Agent] = []
    if stmt is not None:
        agents = list((await session.execute(stmt)).scalars().all())

    project_names = sorted(
        {agent.engine_project_name for agent in agents if agent.engine_project_name}
    )
    runs: int | None = None
    errors: int | None = None
    if project_names:
        now = _now()
        client = get_engine_client()
        payloads = await asyncio.gather(
            *(
                client.get_trace_stats(
                    project_name=name,
                    from_time=now - dt.timedelta(days=window_days),
                    to_time=now,
                )
                for name in project_names
            )
        )
        runs = 0
        for stats in payloads:
            count = _stat_value(stats, _TRACE_COUNT_NAMES)
            failures = _stat_value(stats, _ERROR_COUNT_NAMES)
            runs += int(count or 0)
            if failures is not None:
                errors = int(failures) + (errors or 0)

    success_rate: float | None = None
    if runs and errors is not None:
        success_rate = round(max(0.0, (runs - errors) / runs) * 100, 1)

    link_states = await _resolve_links(session, principal, payload)
    by_name: dict[str, Any] = {}
    if link_states:
        resolved_rows = (
            await session.execute(
                select(
                    Configuration.id, Configuration.name, Configuration.config_type
                ).where(
                    Configuration.workspace_id == principal.workspace_id,
                    Configuration.name.in_(list(link_states)),
                )
            )
        ).all()
        by_name = {row.name: row for row in resolved_rows}

    links = [
        ConfigurationLink(
            slot=slot,
            name=name,
            configuration_id=by_name[name].id if name in by_name else None,
            config_type=(
                ConfigurationType(by_name[name].config_type) if name in by_name else None
            ),
            status=link_states.get(name),
            resolved=name in by_name,
        )
        for slot, name in declared_links(payload)
    ]

    return ConfigurationUsage(
        configuration_id=configuration.id,
        impact=ImpactLevel(configuration.impact),
        used_by_agents=len(agents),
        agent_ids=[agent.id for agent in agents],
        runs_30d=runs,
        success_rate=success_rate,
        error_count_30d=errors,
        window_days=window_days,
        telemetry_available=bool(project_names),
        links=links,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _assert_name_free(
    session: AsyncSession, principal: Principal, name: str, *, exclude_id: str | None = None
) -> None:
    stmt = _scoped(principal).where(Configuration.name == name)
    if exclude_id:
        stmt = stmt.where(Configuration.id != exclude_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise Conflict(f"A configuration named '{name}' already exists.")


def _new_version_row(
    configuration: Configuration,
    *,
    version: str,
    payload: dict[str, Any],
    change_note: str | None,
    author_user_id: str | None,
    status: ConfigurationStatus,
    published_at: dt.datetime | None,
    is_current: bool,
) -> ConfigurationVersion:
    return ConfigurationVersion(
        workspace_id=configuration.workspace_id,
        configuration_id=configuration.id,
        version=version,
        status=status.value,
        payload=dict(payload),
        change_note=change_note,
        author_user_id=author_user_id,
        published_at=published_at,
        is_current=is_current,
    )


async def _publish(
    session: AsyncSession,
    principal: Principal,
    configuration: Configuration,
    version_row: ConfigurationVersion,
) -> None:
    """Make one revision the live one, demoting whichever held the flag."""
    previous = await _current_version(session, principal, configuration.id)
    if previous is not None and previous.id != version_row.id:
        previous.is_current = False
        if previous.status == ConfigurationStatus.ACTIVE.value:
            previous.status = ConfigurationStatus.DEPRECATED.value

    version_row.is_current = True
    version_row.status = ConfigurationStatus.ACTIVE.value
    version_row.published_at = version_row.published_at or _now()
    configuration.current_version = version_row.version
    configuration.status = ConfigurationStatus.ACTIVE.value


async def _validate_row(
    session: AsyncSession,
    principal: Principal,
    configuration: Configuration,
    *,
    version: str,
    payload: dict[str, Any],
    environment: str | None = None,
) -> ConfigurationValidationReport:
    target_env = environment or configuration.environment
    link_states = await _resolve_links(session, principal, payload)
    findings = _validate_body(
        config_type=configuration.config_type,
        environment=target_env,
        payload=payload,
        link_states=link_states,
    )
    return _report(
        configuration,
        version=version,
        environment=target_env,
        payload=payload,
        findings=findings,
    )


def _refuse_invalid(report: ConfigurationValidationReport, name: str) -> None:
    if report.valid:
        return
    raise ValidationFailed(
        f"'{name}' {report.version} failed validation and cannot be activated.",
        details={
            "findings": [finding.model_dump(mode="json") for finding in report.findings],
            "error_count": report.error_count,
            "warning_count": report.warning_count,
        },
    )


async def create_configuration(
    session: AsyncSession,
    principal: Principal,
    payload: ConfigurationCreate,
    *,
    request: Request | None = None,
) -> Configuration:
    """Register a configuration and its first revision, both Draft."""
    principal.require(Role.OPERATOR)
    await _assert_name_free(session, principal, payload.name)

    configuration = Configuration(
        workspace_id=principal.workspace_id,
        name=payload.name,
        config_type=payload.config_type.value,
        environment=payload.environment.value,
        status=ConfigurationStatus.DRAFT.value,
        current_version=payload.version,
        impact=payload.impact.value,
        owner_user_id=payload.owner_user_id or principal.user_id,
        description=payload.description,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(configuration)
    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(f"A configuration named '{payload.name}' already exists.") from exc

    session.add(
        _new_version_row(
            configuration,
            version=payload.version,
            payload=payload.payload,
            change_note=payload.change_note or "Initial draft",
            author_user_id=principal.user_id,
            status=ConfigurationStatus.DRAFT,
            published_at=None,
            is_current=True,
        )
    )
    await audit.record(
        session,
        principal=principal,
        action="configuration.created",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Created {configuration.config_type} draft {payload.version}",
        metadata={"config_type": configuration.config_type, "version": payload.version},
        request=request,
    )
    await session.flush()
    # Server-side timestamp defaults land on the row, not on the instance;
    # refresh so the caller never triggers implicit IO while serialising it.
    await session.refresh(configuration)
    return configuration


async def update_configuration(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    payload: ConfigurationUpdate,
    *,
    request: Request | None = None,
) -> Configuration:
    """Edit identity fields. Bodies never move through here — cut a version."""
    principal.require(Role.OPERATOR)
    configuration = await get_configuration(session, principal, configuration_id)

    if configuration.status == ConfigurationStatus.ARCHIVED.value:
        raise PreconditionFailed(
            f"'{configuration.name}' is archived and read-only. Restore it first."
        )

    if payload.expected_updated_at is not None:
        current = configuration.updated_at
        expected = _as_utc(payload.expected_updated_at)
        # One second of slack: clients round-trip the timestamp through JSON.
        if current is not None and abs((_as_utc(current) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{configuration.name}' was changed by someone else. Reload and try again."
            )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return configuration

    if "name" in changes and changes["name"] != configuration.name:
        await _assert_name_free(
            session, principal, changes["name"], exclude_id=configuration.id
        )

    for field in ("config_type", "environment", "impact"):
        value = changes.get(field)
        if value is not None and not isinstance(value, str):
            changes[field] = value.value

    for field, value in changes.items():
        setattr(configuration, field, value)
    configuration.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A configuration named '{changes.get('name')}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="configuration.updated",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    # ``updated_at`` is a server-side onupdate: the UPDATE expired it rather
    # than refetching it, so read it back before the caller serialises the row.
    await session.refresh(configuration)
    return configuration


async def clone_configuration(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    payload: ConfigurationCloneRequest,
    *,
    request: Request | None = None,
) -> Configuration:
    """Copy a configuration and its live body into a fresh Draft."""
    principal.require(Role.OPERATOR)
    source = await get_configuration(session, principal, configuration_id)
    current = await _current_version(session, principal, source.id)

    name = payload.name or f"{source.name} (Copy)"
    await _assert_name_free(session, principal, name)

    clone = Configuration(
        workspace_id=principal.workspace_id,
        name=name,
        config_type=source.config_type,
        environment=(payload.environment.value if payload.environment else source.environment),
        status=ConfigurationStatus.DRAFT.value,
        current_version=source.current_version or "v0.1.0",
        impact=source.impact,
        owner_user_id=payload.owner_user_id or principal.user_id or source.owner_user_id,
        description=source.description,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(clone)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A configuration named '{name}' already exists.") from exc

    session.add(
        _new_version_row(
            clone,
            version=clone.current_version or "v0.1.0",
            payload=(current.payload or {}) if current is not None else {},
            change_note=f"Cloned from {source.name} {source.current_version or ''}".strip(),
            author_user_id=principal.user_id,
            status=ConfigurationStatus.DRAFT,
            published_at=None,
            is_current=True,
        )
    )
    await audit.record(
        session,
        principal=principal,
        action="configuration.cloned",
        entity_type=ENTITY_TYPE,
        entity_id=clone.id,
        entity_label=clone.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Cloned from '{source.name}'",
        metadata={"source_configuration_id": source.id},
        request=request,
    )
    await session.flush()
    await session.refresh(clone)
    return clone


async def create_version(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    payload: ConfigurationVersionCreate,
    *,
    request: Request | None = None,
) -> tuple[ConfigurationVersion, ConfigurationValidationReport]:
    """Cut a new revision, validating it before it is allowed to go live."""
    principal.require(Role.OPERATOR)
    configuration = await get_configuration(session, principal, configuration_id)
    if configuration.status == ConfigurationStatus.ARCHIVED.value:
        raise PreconditionFailed(
            f"'{configuration.name}' is archived. Restore it before cutting a version."
        )

    current = await _current_version(session, principal, configuration.id)
    version = payload.version or next_version(configuration.current_version)
    body = (
        payload.payload
        if payload.payload is not None
        else dict((current.payload or {}) if current is not None else {})
    )

    existing = (
        await session.execute(
            _versions_scoped(principal, configuration.id).where(
                ConfigurationVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict(f"Version '{version}' already exists for '{configuration.name}'.")

    report = await _validate_row(
        session, principal, configuration, version=version, payload=body
    )
    if payload.activate:
        _refuse_invalid(report, configuration.name)

    row = _new_version_row(
        configuration,
        version=version,
        payload=body,
        change_note=payload.change_note,
        author_user_id=principal.user_id,
        status=ConfigurationStatus.DRAFT,
        published_at=None,
        is_current=False,
    )
    session.add(row)
    await session.flush()

    if payload.activate:
        await _publish(session, principal, configuration, row)
    configuration.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="configuration.version_created",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Version {version} created and activated"
            if payload.activate
            else f"Version {version} drafted"
        ),
        metadata={
            "version": version,
            "activated": payload.activate,
            "warnings": report.warning_count,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(row)
    return row, report


async def rollback(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    payload: ConfigurationRollbackRequest,
    *,
    request: Request | None = None,
) -> tuple[ConfigurationVersion, ConfigurationValidationReport]:
    """Republish an earlier body as a new revision.

    History is never rewritten: the old revision keeps its own row and the body
    it carried, and the rollback lands as the next version on top.
    """
    principal.require(Role.ADMIN)
    configuration = await get_configuration(session, principal, configuration_id)
    current = await _current_version(session, principal, configuration.id)

    if payload.version:
        target = await _version_by_label(
            session, principal, configuration.id, payload.version
        )
    else:
        stmt = _versions_scoped(principal, configuration.id).order_by(
            ConfigurationVersion.created_at.desc(), ConfigurationVersion.id.desc()
        )
        history = list((await session.execute(stmt)).scalars().all())
        previous = [row for row in history if current is None or row.id != current.id]
        if not previous:
            raise PreconditionFailed(
                f"'{configuration.name}' has no earlier version to roll back to."
            )
        target = previous[0]

    if current is not None and target.id == current.id:
        raise PreconditionFailed(
            f"Version {target.version} is already current for '{configuration.name}'."
        )

    version = next_version(configuration.current_version)
    body = dict(target.payload or {})
    report = await _validate_row(
        session, principal, configuration, version=version, payload=body
    )
    _refuse_invalid(report, configuration.name)

    note = payload.change_note or (
        f"Rolled back to {target.version}"
        + (f" from {current.version}" if current is not None else "")
    )
    row = _new_version_row(
        configuration,
        version=version,
        payload=body,
        change_note=note,
        author_user_id=principal.user_id,
        status=ConfigurationStatus.DRAFT,
        published_at=None,
        is_current=False,
    )
    session.add(row)
    await session.flush()
    await _publish(session, principal, configuration, row)
    configuration.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="configuration.rolled_back",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail=note,
        metadata={
            "restored_from": target.version,
            "new_version": version,
            "previous_version": current.version if current is not None else None,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(row)
    return row, report


def _assert_transition(configuration: Configuration, target: ConfigurationStatus) -> None:
    allowed = ALLOWED_TRANSITIONS.get(configuration.status, frozenset())
    if configuration.status == target.value:
        raise Conflict(f"'{configuration.name}' is already {target.value.lower()}.")
    if target.value not in allowed:
        raise Conflict(
            f"'{configuration.name}' cannot move from {configuration.status} to "
            f"{target.value}. Allowed: {', '.join(sorted(allowed)) or 'nothing'}."
        )


async def deprecate(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    payload: ConfigurationDeprecateRequest,
    *,
    request: Request | None = None,
) -> Configuration:
    """Retire a configuration. Agents migrate at their next deploy."""
    principal.require(Role.ADMIN)
    configuration = await get_configuration(session, principal, configuration_id)
    target = (
        ConfigurationStatus.ARCHIVED if payload.archive else ConfigurationStatus.DEPRECATED
    )
    _assert_transition(configuration, target)

    previous = configuration.status
    configuration.status = target.value
    configuration.updated_by = principal.actor

    current = await _current_version(session, principal, configuration.id)
    if current is not None:
        current.status = target.value

    await audit.record(
        session,
        principal=principal,
        action=f"configuration.{target.value.lower()}",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{previous} -> {target.value}"
            + (f": {payload.reason}" if payload.reason else "")
        ),
        metadata={
            "previous_status": previous,
            "version": configuration.current_version,
            "reason": payload.reason,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(configuration)
    return configuration


async def restore(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    *,
    request: Request | None = None,
) -> Configuration:
    """Bring a deprecated or archived configuration back into service."""
    principal.require(Role.ADMIN)
    configuration = await get_configuration(session, principal, configuration_id)
    _assert_transition(configuration, ConfigurationStatus.ACTIVE)

    current = await _current_version(session, principal, configuration.id)
    if current is None:
        raise PreconditionFailed(
            f"'{configuration.name}' has no current version to restore."
        )

    report = await _validate_row(
        session,
        principal,
        configuration,
        version=current.version,
        payload=current.payload or {},
    )
    _refuse_invalid(report, configuration.name)

    previous = configuration.status
    configuration.status = ConfigurationStatus.ACTIVE.value
    configuration.updated_by = principal.actor
    current.status = ConfigurationStatus.ACTIVE.value
    current.published_at = current.published_at or _now()

    await audit.record(
        session,
        principal=principal,
        action="configuration.restored",
        entity_type=ENTITY_TYPE,
        entity_id=configuration.id,
        entity_label=configuration.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{previous} -> {ConfigurationStatus.ACTIVE.value}",
        metadata={"previous_status": previous, "version": current.version},
        request=request,
    )
    await session.flush()
    await session.refresh(configuration)
    return configuration


async def validate_configuration(
    session: AsyncSession,
    principal: Principal,
    configuration_id: str,
    *,
    version: str | None = None,
    payload: dict[str, Any] | None = None,
    environment: EnvironmentType | None = None,
) -> ConfigurationValidationReport:
    """Check a body against its declared schema and report per-field findings."""
    principal.require(Role.MEMBER)
    configuration = await get_configuration(session, principal, configuration_id)

    if payload is not None:
        label = version or configuration.current_version or "unsaved"
        body = payload
    elif version is not None:
        row = await _version_by_label(session, principal, configuration.id, version)
        label, body = row.version, row.payload or {}
    else:
        current = await _current_version(session, principal, configuration.id)
        if current is None:
            raise PreconditionFailed(
                f"'{configuration.name}' has no version to validate."
            )
        label, body = current.version, current.payload or {}

    return await _validate_row(
        session,
        principal,
        configuration,
        version=label,
        payload=body,
        environment=environment.value if environment else None,
    )


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


async def build_bundle(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    config_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    impact: str | None = None,
    owner_user_id: str | None = None,
) -> ConfigurationBundle:
    """Serialise the filtered configurations and their live bodies as JSON."""
    stmt = _list_statement(
        principal,
        params,
        config_type=config_type,
        status=status,
        environment=environment,
        impact=impact,
        owner_user_id=owner_user_id,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    bodies: dict[str, ConfigurationVersion] = {}
    if rows:
        current_rows = (
            await session.execute(
                select(ConfigurationVersion).where(
                    ConfigurationVersion.workspace_id == principal.workspace_id,
                    ConfigurationVersion.configuration_id.in_([row.id for row in rows]),
                    ConfigurationVersion.is_current.is_(True),
                )
            )
        ).scalars()
        bodies = {row.configuration_id: row for row in current_rows}

    items = []
    for row in rows:
        body = bodies.get(row.id)
        items.append(
            ConfigurationBundleItem(
                name=row.name,
                config_type=ConfigurationType(row.config_type),
                environment=EnvironmentType(row.environment),
                impact=ImpactLevel(row.impact),
                status=ConfigurationStatus(row.status),
                description=row.description,
                version=row.current_version or "v0.1.0",
                payload=dict(body.payload or {}) if body is not None else {},
                change_note=body.change_note if body is not None else None,
            )
        )
    return ConfigurationBundle(
        exported_at=_now(), workspace=principal.workspace_slug, items=items
    )


async def import_bundle(
    session: AsyncSession,
    principal: Principal,
    payload: ConfigurationImportRequest,
    *,
    request: Request | None = None,
) -> ConfigurationImportResult:
    """Load a bundle. Existing names are skipped or versioned, never overwritten.

    Imported rows land as Draft whatever the bundle claims: a body that arrived
    from another environment has not been validated against this one.
    """
    principal.require(Role.ADMIN)

    existing_names = {
        name
        for (name,) in (
            await session.execute(
                select(Configuration.name).where(
                    Configuration.workspace_id == principal.workspace_id
                )
            )
        ).all()
    }

    created: list[str] = []
    versioned: list[str] = []
    skipped = 0
    issues: list[ConfigurationImportIssue] = []

    for index, item in enumerate(payload.items):
        if item.name in existing_names:
            if payload.on_conflict is ImportConflictMode.SKIP:
                skipped += 1
                issues.append(
                    ConfigurationImportIssue(
                        index=index,
                        name=item.name,
                        reason="A configuration of this name already exists.",
                    )
                )
                continue
            target = (
                await session.execute(
                    _scoped(principal).where(Configuration.name == item.name)
                )
            ).scalar_one()
            if target.status == ConfigurationStatus.ARCHIVED.value:
                skipped += 1
                issues.append(
                    ConfigurationImportIssue(
                        index=index, name=item.name, reason="Target is archived."
                    )
                )
                continue
            version = next_version(target.current_version)
            session.add(
                _new_version_row(
                    target,
                    version=version,
                    payload=item.payload,
                    change_note=item.change_note
                    or f"Imported from {payload.source or 'bundle'}",
                    author_user_id=principal.user_id,
                    status=ConfigurationStatus.DRAFT,
                    published_at=None,
                    is_current=False,
                )
            )
            target.updated_by = principal.actor
            versioned.append(target.id)
            continue

        configuration = Configuration(
            workspace_id=principal.workspace_id,
            name=item.name,
            config_type=item.config_type.value,
            environment=item.environment.value,
            status=ConfigurationStatus.DRAFT.value,
            current_version=item.version,
            impact=item.impact.value,
            owner_user_id=principal.user_id,
            description=item.description,
            created_by=principal.actor,
            updated_by=principal.actor,
        )
        session.add(configuration)
        try:
            await session.flush()
        except IntegrityError as exc:
            # The whole import shares one transaction, so a name that was taken
            # between the pre-check and this flush aborts the batch rather than
            # silently dropping the item out of a half-written bundle.
            await session.rollback()
            raise Conflict(
                f"'{item.name}' was created by someone else while the bundle was "
                "importing. Nothing was imported; retry the file."
            ) from exc

        session.add(
            _new_version_row(
                configuration,
                version=item.version,
                payload=item.payload,
                change_note=item.change_note
                or f"Imported from {payload.source or 'bundle'}",
                author_user_id=principal.user_id,
                status=ConfigurationStatus.DRAFT,
                published_at=None,
                is_current=True,
            )
        )
        existing_names.add(item.name)
        created.append(configuration.id)

    await audit.record(
        session,
        principal=principal,
        action="configuration.imported",
        entity_type=ENTITY_TYPE,
        entity_label=f"{len(payload.items)} configuration(s)",
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{len(created)} created, {len(versioned)} versioned, {skipped} skipped "
            f"from {payload.source or 'bundle'}"
        ),
        metadata={
            "created": len(created),
            "versioned": len(versioned),
            "skipped": skipped,
            "source": payload.source,
        },
        request=request,
    )
    await session.flush()

    return ConfigurationImportResult(
        submitted=len(payload.items),
        created=len(created),
        versioned=len(versioned),
        skipped=skipped,
        configuration_ids=[*created, *versioned],
        issues=issues,
    )


__all__ = [
    "EXPORT_COLUMNS",
    "build_bundle",
    "clone_configuration",
    "create_configuration",
    "create_version",
    "deprecate",
    "diff_versions",
    "export_configurations",
    "get_configuration",
    "get_version",
    "import_bundle",
    "list_configurations",
    "list_versions",
    "read_configuration",
    "restore",
    "rollback",
    "summarise",
    "update_configuration",
    "usage",
    "validate_configuration",
]
