"""Prompt Manager business logic.

The prompt registry itself is the telemetry engine's: commits, version history,
retrieval by commit and restore all exist there and are reached through the
adapter. Nothing here duplicates them into our database. This module supplies
the two things the registry has no opinion about.

**Tenancy.** The engine runs single-tenant behind this service, so a bare prompt
name would be visible to every workspace. Every name is therefore stored
prefixed with the workspace's telemetry namespace, every read re-checks that
prefix, and a prompt belonging to another workspace answers 404 exactly like a
prompt that does not exist. The prefix is never shown to a client.

**Governance.** Draft to In Review to Approved or Blocked is our state machine,
and it is derived from our own append-only audit trail rather than stored in the
registry: the transition that grants a prompt production status is then covered
by the same tamper-evident hash chain as every other governed change, and cannot
be edited out through the registry's own API.

Run counts come from the engine's trace statistics for the project of the agent
the prompt belongs to. Traces are attributed to a project, not to a prompt row,
so those figures are the agent's for the window — the schema says so on every
field that carries one, and a prompt with no agent reports null rather than zero.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import difflib
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Final, TypeVar

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams
from ..api.deps import Principal
from ..core.errors import (
    Conflict,
    ModelUnavailable,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineNotFound,
    EngineUnavailable,
    get_engine_client,
)
from ..models.governance import AuditEvent
from ..models.identity import Role
from ..models.registry import Agent, EnvironmentType
from ..schemas.prompts import (
    LEFTOVER_PATTERN,
    NAMESPACE_SEPARATOR,
    PromptActionResponse,
    PromptCreate,
    PromptDiff,
    PromptDiffLine,
    PromptExecuteRequest,
    PromptExecuteResult,
    PromptLifecycleRequest,
    PromptRead,
    PromptsSummary,
    PromptStatus,
    PromptTestCase,
    PromptTestRequest,
    PromptTestResult,
    PromptVersionCreate,
    PromptVersionDetail,
    PromptVersionRead,
    estimate_tokens,
    render_template,
    template_variables,
)
from . import audit, model_runner

T = TypeVar("T")

SOURCE_SCREEN: Final[str] = "Prompt Studio"
ENTITY_TYPE: Final[str] = "prompt"

DEFAULT_WINDOW_DAYS: Final[int] = 30

#: The registry pages; this is how much of it one listing will walk. A workspace
#: with more prompts than this pages through the engine's own ordering, and the
#: list reports the engine's total so the console can say the view is capped.
ENGINE_PAGE_SIZE: Final[int] = 100
MAX_ENGINE_PAGES: Final[int] = 20

#: Ceiling on how many engine projects one summary call will query for stats.
MAX_STATS_PROJECTS: Final[int] = 25

#: Audit actions that move the state machine, and the state each one lands in.
LIFECYCLE_ACTIONS: Final[dict[str, PromptStatus]] = {
    "prompt.created": PromptStatus.DRAFT,
    "prompt.version_created": PromptStatus.DRAFT,
    "prompt.restored": PromptStatus.DRAFT,
    "prompt.submitted_for_review": PromptStatus.IN_REVIEW,
    "prompt.approved": PromptStatus.APPROVED,
    "prompt.blocked": PromptStatus.BLOCKED,
}

#: Legal edges. A body change resets the prompt to Draft, which is why
#: ``version_created`` and ``restored`` land there rather than keeping approval.
ALLOWED_TRANSITIONS: Final[dict[PromptStatus, frozenset[PromptStatus]]] = {
    PromptStatus.DRAFT: frozenset({PromptStatus.IN_REVIEW}),
    PromptStatus.IN_REVIEW: frozenset({PromptStatus.APPROVED, PromptStatus.BLOCKED}),
    PromptStatus.APPROVED: frozenset({PromptStatus.BLOCKED, PromptStatus.IN_REVIEW}),
    PromptStatus.BLOCKED: frozenset({PromptStatus.IN_REVIEW}),
}

SORTABLE: Final[frozenset[str]] = frozenset(
    {
        "name",
        "agent",
        "version",
        "status",
        "env",
        "environment",
        "tokens",
        "estimated_tokens",
        "runs30",
        "runs_30d",
        "successRate",
        "success_rate",
        "owner",
        "modified",
        "modified_at",
        "created_at",
    }
)

#: Column order of the CSV export, matching the table left to right.
EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("id", "Prompt ID"),
    ("name", "Prompt"),
    ("agent", "Agent"),
    ("version", "Version"),
    ("commit", "Commit"),
    ("status", "Status"),
    ("environment", "Environment"),
    ("estimated_tokens", "Tokens (estimated)"),
    ("runs_30d", "Runs (30d)"),
    ("success_rate", "Success %"),
    ("owner", "Owner"),
    ("version_count", "Versions"),
    ("modified_at", "Modified"),
]

_TRACE_COUNT_NAMES: Final[tuple[str, ...]] = ("trace_count", "count", "traces", "total")
_ERROR_COUNT_NAMES: Final[tuple[str, ...]] = (
    "error_count",
    "errors",
    "failed_count",
    "trace_error_count",
)


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------


async def _call(awaitable: Awaitable[T], *, action: str) -> T:
    """Run one adapter call, translating its failures into API errors.

    An outage becomes 503 rather than an opaque 500, and a refusal from the
    engine becomes the status that describes it. No branch here invents a
    result: if the engine cannot answer, the request fails.
    """
    try:
        return await awaitable
    except EngineNotFound as exc:
        raise NotFound(f"The prompt registry has no such record ({action}).") from exc
    except EngineUnavailable as exc:
        raise TelemetryBackendUnavailable(
            f"The telemetry store could not be reached while {action}."
        ) from exc
    except EngineBadRequest as exc:
        if exc.status == 409:
            raise Conflict(
                f"The prompt registry refused the change while {action}: it conflicts "
                "with a record that already exists."
            ) from exc
        raise ValidationFailed(
            f"The prompt registry rejected the request while {action}.",
            details={"status": exc.status},
        ) from exc


def _first(payload: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    """First present, non-null key out of several spellings the engine may use."""
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap a paged engine envelope into its rows."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("content", "prompts", "versions", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _total(payload: Any, fallback: int) -> int:
    if isinstance(payload, dict):
        value = _first(payload, ("total", "total_count", "count"))
        if isinstance(value, int):
            return value
    return fallback


def _instant(value: Any) -> dt.datetime | None:
    """Parse an engine timestamp; unparseable values are dropped, not guessed."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _stat_value(payload: Any, names: tuple[str, ...]) -> float | None:
    """Pull one metric out of the engine's stats envelope.

    The envelope is either a mapping of metric name to value or a list of
    ``{"name": …, "value": …}`` entries, so this accepts both and returns None
    when the metric is simply absent rather than defaulting it to zero.
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
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() in names:
                value = entry.get("value")
                if isinstance(value, dict):
                    value = value.get("count")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
    return None


# ---------------------------------------------------------------------------
# Namespacing
# ---------------------------------------------------------------------------


def _namespace(principal: Principal) -> str:
    namespace = (principal.engine_workspace or "").strip()
    if not namespace:
        raise PreconditionFailed(
            "This workspace has no telemetry namespace configured, so its prompts "
            "cannot be addressed safely."
        )
    return namespace


def _prefix(principal: Principal) -> str:
    return f"{_namespace(principal)}{NAMESPACE_SEPARATOR}"


def _qualify(principal: Principal, name: str) -> str:
    return f"{_prefix(principal)}{name}"


def _bare(principal: Principal, qualified: str) -> str | None:
    """The operator-visible name, or None when the row is another tenant's."""
    prefix = _prefix(principal)
    if not qualified.startswith(prefix):
        return None
    return qualified[len(prefix) :]


# ---------------------------------------------------------------------------
# Lifecycle, read back from the audit trail
# ---------------------------------------------------------------------------


async def _lifecycle(
    session: AsyncSession, principal: Principal, prompt_ids: Sequence[str]
) -> dict[str, tuple[PromptStatus, dt.datetime, str]]:
    """Current governance state of each prompt, newest transition wins."""
    if not prompt_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AuditEvent.entity_id,
                AuditEvent.action,
                AuditEvent.occurred_at,
                AuditEvent.actor,
            )
            .where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.entity_type == ENTITY_TYPE,
                AuditEvent.entity_id.in_(list(prompt_ids)),
                AuditEvent.action.in_(list(LIFECYCLE_ACTIONS)),
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        )
    ).all()

    latest: dict[str, tuple[PromptStatus, dt.datetime, str]] = {}
    for entity_id, action, occurred_at, actor in rows:
        if entity_id in latest:
            continue
        state = LIFECYCLE_ACTIONS.get(action)
        if state is not None:
            latest[entity_id] = (state, occurred_at, actor)
    return latest


async def _version_states(
    session: AsyncSession, principal: Principal, prompt_id: str
) -> dict[str, PromptStatus]:
    """The state each commit was left in, rebuilt from the audit trail.

    Every lifecycle row records the commit it applied to, so the version pipe
    can show that v0.1.0 was approved and later superseded rather than showing
    the state it happened to be cut in. Commits with no lifecycle row against
    them are absent, and fall back to what the commit's own metadata recorded.
    """
    rows = (
        await session.execute(
            select(AuditEvent.action, AuditEvent.event_metadata)
            .where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.entity_type == ENTITY_TYPE,
                AuditEvent.entity_id == prompt_id,
                AuditEvent.action.in_(list(LIFECYCLE_ACTIONS)),
            )
            .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
        )
    ).all()

    states: dict[str, PromptStatus] = {}
    for action, metadata in rows:
        state = LIFECYCLE_ACTIONS.get(action)
        commit = (metadata or {}).get("commit")
        if state is not None and isinstance(commit, str) and commit:
            states[commit] = state
    return states


def _assert_transition(current: PromptStatus, target: PromptStatus, name: str) -> None:
    if current is target:
        raise Conflict(f"'{name}' is already {target.value.lower()}.")
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise Conflict(
            f"'{name}' cannot move from {current.value} to {target.value}. "
            f"Allowed from here: {', '.join(sorted(s.value for s in allowed)) or 'nothing'}."
        )


# ---------------------------------------------------------------------------
# Agents and their telemetry projects
# ---------------------------------------------------------------------------


async def _agents(
    session: AsyncSession, principal: Principal
) -> dict[str, tuple[str, str | None]]:
    """Agent name to ``(agent id, engine project name)`` for this workspace."""
    rows = (
        await session.execute(
            select(Agent.name, Agent.id, Agent.engine_project_name).where(
                Agent.workspace_id == principal.workspace_id
            )
        )
    ).all()
    return {name: (agent_id, project) for name, agent_id, project in rows}


async def _project_stats(
    client: EngineClient, projects: Sequence[str], *, window_days: int
) -> dict[str, tuple[int, int | None]]:
    """Trace count and error count per project over the window."""
    if not projects:
        return {}
    now = dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=window_days)
    payloads = await asyncio.gather(
        *(
            _call(
                client.get_trace_stats(project_name=project, from_time=since, to_time=now),
                action="reading run statistics",
            )
            for project in projects
        )
    )
    stats: dict[str, tuple[int, int | None]] = {}
    for project, payload in zip(projects, payloads, strict=True):
        runs = _stat_value(payload, _TRACE_COUNT_NAMES)
        errors = _stat_value(payload, _ERROR_COUNT_NAMES)
        stats[project] = (int(runs or 0), int(errors) if errors is not None else None)
    return stats


def _success_rate(runs: int, errors: int | None) -> float | None:
    if not runs or errors is None:
        return None
    return round(max(0.0, (runs - errors) / runs) * 100, 1)


# ---------------------------------------------------------------------------
# Shaping one registry row
# ---------------------------------------------------------------------------


def _environment(value: Any) -> EnvironmentType | None:
    """Coerce a stored environment label, dropping anything not in the vocabulary."""
    if not isinstance(value, str):
        return None
    try:
        return EnvironmentType(value)
    except ValueError:
        return None


def _head_version(prompt: Mapping[str, Any]) -> dict[str, Any]:
    head = _first(prompt, ("latest_version", "version", "head_version"), {})
    return head if isinstance(head, dict) else {}


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata")
    return value if isinstance(value, dict) else {}


def _shape(
    principal: Principal,
    prompt: Mapping[str, Any],
    *,
    lifecycle: dict[str, tuple[PromptStatus, dt.datetime, str]],
    agents: dict[str, tuple[str, str | None]],
    stats: dict[str, tuple[int, int | None]],
) -> PromptRead | None:
    """Turn one registry row into a table row, or None if it is another tenant's."""
    prompt_id = str(_first(prompt, ("id", "prompt_id"), ""))
    qualified = str(_first(prompt, ("name",), ""))
    name = _bare(principal, qualified)
    if not prompt_id or name is None:
        return None

    head = _head_version(prompt)
    meta = {**_metadata(prompt), **_metadata(head)}
    template = str(_first(head, ("template",), "") or "")

    state, changed_at, changed_by = lifecycle.get(
        prompt_id, (PromptStatus.DRAFT, None, None)
    )
    agent_name = meta.get("agent") if isinstance(meta.get("agent"), str) else None
    agent_id, project = agents.get(agent_name or "", (None, None))

    runs: int | None = None
    errors: int | None = None
    if project and project in stats:
        runs, errors = stats[project]

    environment = _environment(meta.get("environment"))
    tags = prompt.get("tags")

    return PromptRead(
        id=prompt_id,
        name=name,
        description=_first(prompt, ("description",)),
        agent=agent_name,
        agent_id=agent_id,
        version=meta.get("version") if isinstance(meta.get("version"), str) else None,
        commit=_first(head, ("commit", "id")),
        status=state,
        environment=environment,
        template=template or None,
        estimated_tokens=estimate_tokens(template),
        template_chars=len(template),
        variables=template_variables(template),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        runs_30d=runs,
        success_rate=_success_rate(runs or 0, errors) if runs is not None else None,
        owner=changed_by,
        version_count=int(_first(prompt, ("version_count", "versions_count"), 0) or 0),
        created_at=_instant(_first(prompt, ("created_at",))),
        modified_at=_instant(
            _first(prompt, ("last_updated_at", "updated_at", "created_at"))
        ),
        status_changed_at=changed_at,
        status_changed_by=changed_by,
    )


def _sort_key(row: PromptRead, key: str) -> Any:
    mapping: dict[str, Any] = {
        "name": row.name.lower(),
        "agent": (row.agent or "").lower(),
        "version": row.version or "",
        "status": row.status.value,
        "env": row.environment or "",
        "environment": row.environment or "",
        "tokens": row.estimated_tokens,
        "estimated_tokens": row.estimated_tokens,
        "runs30": row.runs_30d if row.runs_30d is not None else -1,
        "runs_30d": row.runs_30d if row.runs_30d is not None else -1,
        "successRate": row.success_rate if row.success_rate is not None else -1.0,
        "success_rate": row.success_rate if row.success_rate is not None else -1.0,
        "owner": (row.owner or "").lower(),
        "modified": row.modified_at or dt.datetime.min.replace(tzinfo=dt.UTC),
        "modified_at": row.modified_at or dt.datetime.min.replace(tzinfo=dt.UTC),
        "created_at": row.created_at or dt.datetime.min.replace(tzinfo=dt.UTC),
    }
    return mapping[key]


def _matches(row: PromptRead, needle: str) -> bool:
    haystack = " ".join(
        part
        for part in (row.name, row.agent, row.owner, row.description, row.environment)
        if part
    )
    return needle in haystack.lower()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _scan(client: EngineClient, principal: Principal) -> tuple[list[dict[str, Any]], int]:
    """Every prompt in this workspace's namespace, and what the engine reported.

    The registry is filtered by name prefix on the engine and again here, so a
    prompt from another namespace can never survive the walk even if the
    engine's own name filter is looser than an exact prefix match.
    """
    prefix = _prefix(principal)
    collected: list[dict[str, Any]] = []
    engine_total = 0
    for page in range(1, MAX_ENGINE_PAGES + 1):
        payload = await _call(
            client.list_prompts(page=page, size=ENGINE_PAGE_SIZE, name=prefix),
            action="listing prompts",
        )
        rows = _rows(payload)
        engine_total = _total(payload, engine_total + len(rows))
        collected.extend(
            row for row in rows if str(row.get("name", "")).startswith(prefix)
        )
        if len(rows) < ENGINE_PAGE_SIZE:
            break
    return collected, engine_total


async def _hydrate(
    session: AsyncSession,
    principal: Principal,
    rows: Sequence[Mapping[str, Any]],
    *,
    window_days: int,
    with_stats: bool = True,
    max_projects: int = MAX_STATS_PROJECTS,
) -> tuple[list[PromptRead], int]:
    """Attach governance state, the owning agent and its run statistics."""
    agents = await _agents(session, principal)
    lifecycle = await _lifecycle(
        session, principal, [str(row.get("id")) for row in rows if row.get("id")]
    )

    wanted: list[str] = []
    for row in rows:
        meta = {**_metadata(row), **_metadata(_head_version(row))}
        agent_name = meta.get("agent")
        if not isinstance(agent_name, str):
            continue
        project = agents.get(agent_name, (None, None))[1]
        if project and project not in wanted:
            wanted.append(project)

    stats: dict[str, tuple[int, int | None]] = {}
    sampled = wanted[:max_projects] if with_stats else []
    if sampled:
        stats = await _project_stats(
            get_engine_client(), sampled, window_days=window_days
        )

    shaped = [
        shaped_row
        for shaped_row in (
            _shape(principal, row, lifecycle=lifecycle, agents=agents, stats=stats)
            for row in rows
        )
        if shaped_row is not None
    ]
    return shaped, len(wanted)


def _filter_sort_page(
    rows: list[PromptRead],
    params: ListParams,
    *,
    status: PromptStatus | None,
    environment: str | None,
    agent: str | None,
) -> tuple[list[PromptRead], int]:
    """Apply the table's search, dropdowns, sort and paging.

    The engine pages the registry by its own ordering, so the console's ordering
    is applied here over the workspace's prompts rather than pushed down.
    """
    filtered = rows
    if status is not None:
        filtered = [row for row in filtered if row.status is status]
    if environment:
        filtered = [row for row in filtered if row.environment == environment]
    if agent:
        filtered = [row for row in filtered if row.agent == agent]
    if params.q:
        needle = params.q.strip().lower()
        filtered = [row for row in filtered if _matches(row, needle)]

    key = params.sort_key
    if key is not None:
        if key not in SORTABLE:
            raise ValidationFailed(
                f"Cannot sort by '{key}'.", details={"sortable": sorted(SORTABLE)}
            )
        filtered = sorted(
            filtered, key=lambda row: _sort_key(row, key), reverse=params.descending
        )
    else:
        filtered = sorted(
            filtered,
            key=lambda row: row.modified_at or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )

    total = len(filtered)
    start = params.offset
    return filtered[start : start + params.page_size], total


async def list_prompts(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: PromptStatus | None = None,
    environment: str | None = None,
    agent: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[list[PromptRead], int]:
    """One page of the workspace's prompts, joined with their run statistics."""
    rows, _engine_total = await _scan(get_engine_client(), principal)
    shaped, _projects = await _hydrate(
        session, principal, rows, window_days=window_days
    )
    return _filter_sort_page(
        shaped, params, status=status, environment=environment, agent=agent
    )


async def export_prompts(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: PromptStatus | None = None,
    environment: str | None = None,
    agent: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[PromptRead]:
    """Every prompt the current filters select, unpaged, for the CSV download."""
    rows, _engine_total = await _scan(get_engine_client(), principal)
    shaped, _projects = await _hydrate(
        session, principal, rows, window_days=window_days
    )
    unpaged = params.model_copy(update={"page": 1, "page_size": max(1, len(shaped) or 1)})
    page, _total_rows = _filter_sort_page(
        shaped, unpaged, status=status, environment=environment, agent=agent
    )
    return page


async def get_prompt(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    with_stats: bool = True,
) -> PromptRead:
    """One prompt. A prompt in another namespace answers 404, not 403."""
    client = get_engine_client()
    payload = await _call(client.get_prompt(prompt_id), action="reading a prompt")
    if not isinstance(payload, dict):
        raise NotFound(f"Prompt '{prompt_id}' does not exist.")
    if _bare(principal, str(payload.get("name", ""))) is None:
        # Another workspace's namespace: indistinguishable from absent.
        raise NotFound(f"Prompt '{prompt_id}' does not exist.")

    shaped, _projects = await _hydrate(
        session, principal, [payload], window_days=window_days, with_stats=with_stats
    )
    if not shaped:
        raise NotFound(f"Prompt '{prompt_id}' does not exist.")
    return shaped[0]


async def _raw_prompt(principal: Principal, prompt_id: str) -> dict[str, Any]:
    """The registry row itself, namespace-checked."""
    payload = await _call(
        get_engine_client().get_prompt(prompt_id), action="reading a prompt"
    )
    if not isinstance(payload, dict) or _bare(principal, str(payload.get("name", ""))) is None:
        raise NotFound(f"Prompt '{prompt_id}' does not exist.")
    return payload


async def _version_rows(prompt_id: str) -> list[dict[str, Any]]:
    """Every commit of one prompt, newest first as the registry returns them."""
    collected: list[dict[str, Any]] = []
    client = get_engine_client()
    for page in range(1, MAX_ENGINE_PAGES + 1):
        payload = await _call(
            client.list_versions(prompt_id, page=page, size=ENGINE_PAGE_SIZE),
            action="listing prompt versions",
        )
        rows = _rows(payload)
        collected.extend(rows)
        if len(rows) < ENGINE_PAGE_SIZE:
            break
    return collected


def _shape_version(
    row: Mapping[str, Any],
    *,
    head_commit: str | None,
    live_status: PromptStatus,
    states: Mapping[str, PromptStatus] | None = None,
) -> PromptVersionDetail:
    meta = _metadata(row)
    template = str(_first(row, ("template",), "") or "")
    commit = str(_first(row, ("commit", "id"), ""))
    is_head = bool(head_commit) and commit == head_commit

    recorded = meta.get("status")
    status: PromptStatus | None
    if is_head:
        # The head commit *is* the prompt, so it carries the live state.
        status = live_status
    elif states and commit in states:
        # What the audit trail says this commit was left in.
        status = states[commit]
    elif isinstance(recorded, str):
        try:
            status = PromptStatus(recorded)
        except ValueError:
            status = None
    else:
        status = None

    return PromptVersionDetail(
        commit=commit,
        version=meta.get("version") if isinstance(meta.get("version"), str) else None,
        status=status,
        change_note=_first(row, ("change_description", "change_note")),
        author=_first(row, ("created_by", "author")),
        created_at=_instant(_first(row, ("created_at",))),
        estimated_tokens=estimate_tokens(template),
        is_head=is_head,
        template=template,
        variables=template_variables(template),
        metadata=meta,
    )


async def list_versions(
    session: AsyncSession, principal: Principal, prompt_id: str, params: ListParams
) -> tuple[list[PromptVersionRead], int]:
    """Commit history for one prompt, newest first."""
    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)
    rows = await _version_rows(prompt_id)
    states = await _version_states(session, principal, prompt_id)
    shaped = [
        _shape_version(
            row, head_commit=prompt.commit, live_status=prompt.status, states=states
        )
        for row in rows
    ]
    if params.q:
        needle = params.q.strip().lower()
        shaped = [
            row
            for row in shaped
            if needle in f"{row.version or ''} {row.change_note or ''} {row.author or ''}".lower()
        ]
    total = len(shaped)
    start = params.offset
    page = shaped[start : start + params.page_size]
    return [PromptVersionRead.model_validate(row.model_dump()) for row in page], total


async def get_version(
    session: AsyncSession, principal: Principal, prompt_id: str, commit: str
) -> PromptVersionDetail:
    """One commit, template included."""
    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)
    row = await _find_version(prompt_id, commit)
    states = await _version_states(session, principal, prompt_id)
    return _shape_version(
        row, head_commit=prompt.commit, live_status=prompt.status, states=states
    )


async def _find_version(prompt_id: str, commit: str) -> dict[str, Any]:
    """Resolve a commit by its hash, its id or its human version label."""
    needle = commit.strip()
    rows = await _version_rows(prompt_id)
    for row in rows:
        candidates = {
            str(row.get("commit", "")),
            str(row.get("id", "")),
            str(_metadata(row).get("version", "")),
        }
        if needle in candidates and needle:
            return row
    raise NotFound(f"Version '{commit}' does not exist for this prompt.")


async def diff(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    *,
    from_commit: str,
    to_commit: str,
) -> PromptDiff:
    """Unified diff of two commits' templates."""
    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)
    left = await _find_version(prompt_id, from_commit)
    right = await _find_version(prompt_id, to_commit)

    left_text = str(_first(left, ("template",), "") or "")
    right_text = str(_first(right, ("template",), "") or "")

    lines: list[PromptDiffLine] = []
    added = removed = 0
    for line in difflib.unified_diff(
        left_text.splitlines(),
        right_text.splitlines(),
        lineterm="",
        n=3,
    ):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            lines.append(PromptDiffLine(kind="hunk", text=line))
        elif line.startswith("+"):
            added += 1
            lines.append(PromptDiffLine(kind="added", text=line[1:]))
        elif line.startswith("-"):
            removed += 1
            lines.append(PromptDiffLine(kind="removed", text=line[1:]))
        else:
            lines.append(PromptDiffLine(kind="context", text=line[1:] if line else ""))

    return PromptDiff(
        prompt_id=prompt.id,
        from_commit=str(_first(left, ("commit", "id"), from_commit)),
        to_commit=str(_first(right, ("commit", "id"), to_commit)),
        from_version=_metadata(left).get("version"),
        to_version=_metadata(right).get("version"),
        added_lines=added,
        removed_lines=removed,
        identical=left_text == right_text,
        lines=lines,
    )


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> PromptsSummary:
    """The five KPI cards above the table."""
    rows, _engine_total = await _scan(get_engine_client(), principal)
    shaped, projects_total = await _hydrate(
        session, principal, rows, window_days=window_days
    )

    counts = dict.fromkeys(PromptStatus, 0)
    for row in shaped:
        counts[row.status] += 1

    rated = [row.success_rate for row in shaped if row.success_rate is not None]
    runs = sum(row.runs_30d or 0 for row in shaped)

    return PromptsSummary(
        total=len(shaped),
        approved=counts[PromptStatus.APPROVED],
        in_review=counts[PromptStatus.IN_REVIEW],
        blocked=counts[PromptStatus.BLOCKED],
        draft=counts[PromptStatus.DRAFT],
        avg_success_rate=round(sum(rated) / len(rated), 1) if rated else None,
        runs_30d=runs,
        prompts_with_telemetry=len(rated),
        projects_sampled=min(projects_total, MAX_STATS_PROJECTS),
        projects_total=projects_total,
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _commit_metadata(
    principal: Principal,
    *,
    agent: str | None,
    environment: str | None,
    version: str | None,
    change_note: str | None,
) -> dict[str, Any]:
    """What we stamp on a commit so a later read can rebuild the table row."""
    metadata: dict[str, Any] = {
        "workspace": _namespace(principal),
        "status": PromptStatus.DRAFT.value,
    }
    if agent:
        metadata["agent"] = agent
    if environment:
        metadata["environment"] = environment
    if version:
        metadata["version"] = version
    if change_note:
        metadata["change_note"] = change_note
    return metadata


async def _assert_agent_exists(
    session: AsyncSession, principal: Principal, agent: str | None
) -> None:
    if not agent:
        return
    agents = await _agents(session, principal)
    if agent not in agents:
        raise ValidationFailed(
            f"'{agent}' is not an agent in this workspace.",
            details={"field": "agent"},
        )


async def create_prompt(
    session: AsyncSession,
    principal: Principal,
    payload: PromptCreate,
    *,
    request: Request | None = None,
) -> PromptRead:
    """Author a prompt. It lands Draft and needs review before it is approved."""
    principal.require(Role.MEMBER)
    await _assert_agent_exists(session, principal, payload.agent)

    qualified = _qualify(principal, payload.name)
    client = get_engine_client()

    existing, _engine_total = await _scan(client, principal)
    if any(str(row.get("name", "")) == qualified for row in existing):
        raise Conflict(f"A prompt named '{payload.name}' already exists.")

    version = payload.version or "v0.1.0"
    created = await _call(
        client.create_prompt(
            qualified,
            description=payload.description,
            template=payload.template,
            metadata=_commit_metadata(
                principal,
                agent=payload.agent,
                environment=payload.environment.value,
                version=version,
                change_note=payload.change_note or "Initial draft",
            ),
            tags=payload.tags or None,
        ),
        action="creating a prompt",
    )
    prompt_id = str(_first(created or {}, ("id", "prompt_id"), ""))
    if not prompt_id:
        # The registry accepted the create but will not name the row; without an
        # id there is nothing to audit against or to hand back.
        raise TelemetryBackendUnavailable(
            "The prompt registry accepted the prompt but did not return its id."
        )

    await audit.record(
        session,
        principal=principal,
        action="prompt.created",
        entity_type=ENTITY_TYPE,
        entity_id=prompt_id,
        entity_label=payload.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Drafted {version}" + (f" for {payload.agent}" if payload.agent else ""),
        metadata={
            "version": version,
            "commit": _first(_head_version(created or {}), ("commit", "id")),
            "agent": payload.agent,
            "environment": payload.environment.value,
        },
        request=request,
    )
    await session.flush()
    return await get_prompt(session, principal, prompt_id)


async def create_version(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    payload: PromptVersionCreate,
    *,
    request: Request | None = None,
) -> tuple[PromptRead, PromptVersionRead]:
    """Commit a new body. Approval does not survive a body change."""
    principal.require(Role.MEMBER)
    current = await get_prompt(session, principal, prompt_id, with_stats=False)
    await _assert_agent_exists(session, principal, payload.agent)

    agent = payload.agent or current.agent
    environment = (
        payload.environment.value if payload.environment else current.environment
    )
    version = payload.version or _bump(current.version)

    committed = await _call(
        get_engine_client().create_version(
            _qualify(principal, current.name),
            {
                "template": payload.template,
                "metadata": _commit_metadata(
                    principal,
                    agent=agent,
                    environment=environment,
                    version=version,
                    change_note=payload.change_note,
                ),
                "change_description": payload.change_note or f"Version {version}",
            },
        ),
        action="committing a prompt version",
    )
    row = committed if isinstance(committed, dict) else {}

    await audit.record(
        session,
        principal=principal,
        action="prompt.version_created",
        entity_type=ENTITY_TYPE,
        entity_id=current.id,
        entity_label=current.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Version {version} drafted; approval reset pending review",
        metadata={"version": version, "commit": _first(row, ("commit", "id"))},
        request=request,
    )
    await session.flush()

    refreshed = await get_prompt(session, principal, prompt_id)
    shaped = _shape_version(
        row, head_commit=refreshed.commit, live_status=refreshed.status
    )
    return refreshed, PromptVersionRead.model_validate(shaped.model_dump())


def _bump(version: str | None) -> str:
    """Next minor label; unlabelled prompts start at v0.1.0."""
    if not version:
        return "v0.1.0"
    parts = version.lstrip("vV").split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return "v0.1.0"
    return f"v{major}.{minor + 1}.0"


async def restore_version(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    version: str,
    *,
    request: Request | None = None,
) -> PromptRead:
    """Re-commit an earlier version as the head, sending the prompt back to Draft."""
    principal.require(Role.OPERATOR)
    current = await get_prompt(session, principal, prompt_id, with_stats=False)
    row = await _find_version(prompt_id, version)
    version_id = str(_first(row, ("id", "commit"), ""))
    if not version_id:
        raise PreconditionFailed("That version cannot be addressed for restore.")

    restored = await _call(
        get_engine_client().restore_version(prompt_id, version_id),
        action="restoring a prompt version",
    )
    head = restored if isinstance(restored, dict) else {}
    await audit.record(
        session,
        principal=principal,
        action="prompt.restored",
        entity_type=ENTITY_TYPE,
        entity_id=current.id,
        entity_label=current.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Restored {_metadata(row).get('version') or version} as the head version",
        metadata={
            "restored_from": _first(row, ("commit", "id")),
            "commit": _first(head, ("commit", "id")),
            "requested": version,
        },
        request=request,
    )
    await session.flush()
    return await get_prompt(session, principal, prompt_id)


async def transition(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    target: PromptStatus,
    payload: PromptLifecycleRequest,
    *,
    request: Request | None = None,
) -> PromptActionResponse:
    """Move one prompt along the governance state machine.

    Submitting for review is authoring work, so a member may do it; approving or
    blocking is a governance decision and needs the operator role.
    """
    if target is PromptStatus.IN_REVIEW:
        principal.require(Role.MEMBER)
    else:
        principal.require(Role.OPERATOR)

    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)
    _assert_transition(prompt.status, target, prompt.name)

    action = {
        PromptStatus.IN_REVIEW: "prompt.submitted_for_review",
        PromptStatus.APPROVED: "prompt.approved",
        PromptStatus.BLOCKED: "prompt.blocked",
    }[target]

    await audit.record(
        session,
        principal=principal,
        action=action,
        entity_type=ENTITY_TYPE,
        entity_id=prompt.id,
        entity_label=prompt.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"{prompt.status.value} -> {target.value}"
            + (f": {payload.note}" if payload.note else "")
        ),
        metadata={
            "previous_status": prompt.status.value,
            "status": target.value,
            "version": prompt.version,
            "commit": prompt.commit,
            "note": payload.note,
        },
        request=request,
    )
    await session.flush()

    updated = await get_prompt(session, principal, prompt_id)
    label = f"{updated.name} {updated.version}" if updated.version else updated.name
    messages = {
        PromptStatus.IN_REVIEW: f"{label} submitted for review.",
        PromptStatus.APPROVED: f"{label} approved for production use.",
        PromptStatus.BLOCKED: f"{label} blocked; it cannot be deployed.",
    }
    return PromptActionResponse(
        prompt=updated,
        message=messages[target],
        previous_status=prompt.status,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def execute_prompt(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    payload: PromptExecuteRequest,
    *,
    request: Request | None = None,
) -> PromptExecuteResult:
    """Render one variable set and actually send it to a model.

    The counterpart to :func:`test_prompt`, which renders and scores but never
    calls anything. This is the Run button: one prompt, one model, one answer,
    with the provider's own token counts rather than an estimate.

    A run is audited like any other governed action. It is deliberately not
    recorded as an agent run: no agent executed it, and inventing one would put
    a person's experiment into the same figures the platform reports as agent
    behaviour.
    """
    principal.require(Role.MEMBER)
    if not model_runner.available():
        raise ModelUnavailable(model_runner.requirement())

    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)

    if payload.commit:
        row = await _find_version(prompt_id, payload.commit)
    else:
        rows = await _version_rows(prompt_id)
        row = rows[0] if rows else {}
    template = str(_first(row, ("template",), prompt.template or "") or "")
    if not template:
        raise PreconditionFailed(f"'{prompt.name}' has no template to run.")

    declared = template_variables(template)
    supplied = dict(payload.variables or {})
    rendered = render_template(template, supplied)
    missing = [name for name in declared if name not in supplied]
    unresolved = LEFTOVER_PATTERN.findall(rendered)

    run = await model_runner.execute(
        rendered,
        model=payload.model,
        system=payload.system,
        max_output_tokens=payload.max_output_tokens,
    )

    await audit.record(
        session,
        principal=principal,
        action="prompt.executed",
        entity_type=ENTITY_TYPE,
        entity_id=prompt_id,
        entity_label=prompt.name,
        source_screen=SOURCE_SCREEN,
        detail=f"ran against {run.model} ({run.total_tokens or 0} tokens)",
        request=request,
    )
    await session.commit()

    return PromptExecuteResult(
        prompt_id=prompt_id,
        name=prompt.name,
        commit=str(_first(row, ("commit", "id"), None) or "") or None,
        version=str(_first(row, ("version",), "") or "") or None,
        rendered=rendered,
        missing_variables=missing,
        unresolved_placeholders=list(unresolved),
        output=run.output,
        model=run.model,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        total_tokens=run.total_tokens,
        latency_ms=run.latency_ms,
        finish_reason=run.finish_reason,
        truncated=run.finish_reason == "length",
    )


async def test_prompt(
    session: AsyncSession,
    principal: Principal,
    prompt_id: str,
    payload: PromptTestRequest,
    *,
    request: Request | None = None,
) -> PromptTestResult:
    """Render the prompt over sample inputs and hand the result to the engine.

    The adapter exposes no completion path, so this endpoint does not execute a
    model. What it does do is real: it renders the stored template against each
    supplied variable set, reports every missing variable and every placeholder
    that stayed unresolved, then persists the rendered cases as a dataset and
    opens an experiment against the tested commit so the engine's evaluation
    path scores them. ``scored`` says which of those two happened.
    """
    principal.require(Role.MEMBER)
    prompt = await get_prompt(session, principal, prompt_id, with_stats=False)

    if payload.commit:
        row = await _find_version(prompt_id, payload.commit)
    else:
        rows = await _version_rows(prompt_id)
        row = rows[0] if rows else {}
    template = str(_first(row, ("template",), prompt.template or "") or "")
    if not template:
        raise PreconditionFailed(f"'{prompt.name}' has no template to test.")

    declared = template_variables(template)
    cases: list[PromptTestCase] = []
    for index, variables in enumerate(payload.cases or [{}]):
        rendered = render_template(template, variables)
        leftovers = LEFTOVER_PATTERN.findall(rendered)
        missing = [name for name in declared if name not in variables]
        unused = [name for name in variables if name not in declared]
        cases.append(
            PromptTestCase(
                index=index,
                rendered=rendered,
                ok=not missing and not leftovers,
                missing_variables=missing,
                unused_variables=unused,
                unresolved_placeholders=leftovers,
                estimated_tokens=estimate_tokens(rendered),
            )
        )

    passed = sum(1 for case in cases if case.ok)
    commit = str(_first(row, ("commit", "id"), prompt.commit or "") or "") or None

    dataset_id = experiment_id = None
    dataset_name = experiment_name = None
    scored = False
    detail = (
        "Rendered locally. The telemetry adapter exposes no completion endpoint, so "
        "no model was executed."
    )

    if payload.score:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        dataset_name = payload.dataset_name or _qualify(
            principal, f"{prompt.name}-test-{stamp}"
        )
        experiment_name = payload.experiment_name or _qualify(
            principal, f"{prompt.name}-test-{stamp}"
        )
        client = get_engine_client()

        dataset = await _call(
            client.create_dataset(
                dataset_name,
                description=(
                    f"Prompt test for {prompt.name}"
                    + (f" at {commit}" if commit else "")
                ),
            ),
            action="creating the test dataset",
        )
        dataset_id = str(_first(dataset or {}, ("id", "dataset_id"), "")) or None

        await _call(
            client.create_dataset_items_batch(
                [
                    {
                        "data": {
                            "case": case.index,
                            "variables": payload.cases[case.index]
                            if case.index < len(payload.cases)
                            else {},
                            "rendered_prompt": case.rendered,
                            "missing_variables": case.missing_variables,
                            "unresolved_placeholders": case.unresolved_placeholders,
                        }
                    }
                    for case in cases
                ],
                dataset_name=dataset_name,
            ),
            action="storing the test cases",
        )

        experiment = await _call(
            client.create_experiment(
                experiment_name,
                dataset_name=dataset_name,
                metadata={
                    "prompt_id": prompt.id,
                    "prompt": prompt.name,
                    "commit": commit,
                    "version": prompt.version,
                    "source": SOURCE_SCREEN,
                },
            ),
            action="opening the test experiment",
        )
        experiment_id = str(_first(experiment or {}, ("id",), "")) or None
        scored = True
        detail = (
            "Rendered locally, then stored as a dataset with an experiment opened "
            "against it so the engine's evaluation path scores the run. The adapter "
            "exposes no completion endpoint, so no model was executed here."
        )

    await audit.record(
        session,
        principal=principal,
        action="prompt.tested",
        entity_type=ENTITY_TYPE,
        entity_id=prompt.id,
        entity_label=prompt.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{passed}/{len(cases)} case(s) rendered cleanly",
        metadata={
            "commit": commit,
            "cases": len(cases),
            "passed": passed,
            "dataset": dataset_name,
            "experiment": experiment_name,
            "scored": scored,
        },
        request=request,
    )
    await session.flush()

    return PromptTestResult(
        prompt_id=prompt.id,
        name=prompt.name,
        commit=commit,
        version=prompt.version,
        variables=declared,
        cases=cases,
        passed=passed,
        failed=len(cases) - passed,
        scored=scored,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        detail=detail,
    )


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "EXPORT_COLUMNS",
    "create_prompt",
    "create_version",
    "diff",
    "export_prompts",
    "get_prompt",
    "get_version",
    "list_prompts",
    "list_versions",
    "restore_version",
    "summarise",
    "test_prompt",
    "transition",
]
