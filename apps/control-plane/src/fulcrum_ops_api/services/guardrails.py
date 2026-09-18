"""Guardrails business logic.

Guardrail definitions live here; enforcement runs in the telemetry engine's
inline content checker. Everything the console can do to a guardrail — create,
tune, enable, disable, test — goes through this module, and so does the runtime
path: :func:`evaluate_ingest_batch` is the hook the ingest pipeline calls on
every trace/span batch (ingest applies the verdicts and writes the evidence
rows), while :func:`evaluate_content` serves single-content callers and writes
its own ``GuardrailEvent`` rows.

The numbers the screen shows are counted, not remembered. Triggers, blocks and
masks come from ``GROUP BY`` over the event table for the requested window, so
a guardrail that stopped firing a fortnight ago shows a falling count without
anyone resetting a counter. The cached rollup columns on ``GuardrailConfig`` are
maintained alongside for consumers outside this screen.

Two things are deliberately never done here: a verdict is never simulated (a
test with no reachable checker fails loudly rather than reporting "Clear"), and
a test never writes an event, because a rehearsal must not move the 30-day
counters an operator is judged on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from fastapi import Request
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import Conflict, NotFound, PreconditionFailed
from ..engine import EngineError, get_engine_client
from ..models.identity import Role
from ..models.quality import (
    GuardrailAction,
    GuardrailConfig,
    GuardrailEvent,
    GuardrailScope,
    GuardrailStatus,
    GuardrailType,
)
from ..models.registry import Agent
from ..schemas.guardrails import (
    EVENT_SAMPLE_LIMIT,
    ContentEvaluation,
    GuardrailCreate,
    GuardrailEventRead,
    GuardrailHit,
    GuardrailRead,
    GuardrailsSummary,
    GuardrailTestResult,
    GuardrailThresholdUpdate,
    GuardrailUpdate,
    MatchedSpan,
    coverage_label,
    validation_name,
)
from . import audit
from .evaluations import owner_names, translate_engine_error

if TYPE_CHECKING:
    from ..schemas.ingest import GuardrailCandidate, GuardrailVerdict

log = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "Guardrails"
ENTITY_TYPE: Final[str] = "guardrail"

#: Window every counter on this screen is measured over.
WINDOW_DAYS: Final[int] = 30

#: Ceiling on the per-entity scan behind the PII card. Beyond it the card falls
#: back to the cached per-guardrail rollups and says so.
PII_SCAN_LIMIT: Final[int] = 20000

EXPORT_LIMIT: Final[int] = 5000

#: Weight of the newest measurement in the guardrail's added-latency average.
#: An exponential average keeps one slow check from rewriting the tile while
#: still tracking a checker that has genuinely got slower.
LATENCY_SMOOTHING: Final[float] = 0.2

#: Ordering used when more than one guardrail fires on the same content: the
#: strongest action wins, because that is the one actually applied.
ACTION_SEVERITY: Final[dict[str, int]] = {
    GuardrailAction.LOG.value: 0,
    GuardrailAction.WARN.value: 1,
    GuardrailAction.MASK.value: 2,
    GuardrailAction.BLOCK.value: 3,
}

#: Statuses whose guardrails run on live content. Tuning runs in shadow: the
#: check is evaluated and recorded, but nothing is enforced.
RUNTIME_STATUSES: Final[tuple[str, ...]] = (
    GuardrailStatus.ACTIVE.value,
    GuardrailStatus.TUNING.value,
)

SORTABLE: Final[dict[str, Any]] = {
    "name": GuardrailConfig.name,
    "guardrail_type": GuardrailConfig.guardrail_type,
    "status": GuardrailConfig.status,
    "action": GuardrailConfig.action,
    "threshold": GuardrailConfig.threshold,
    "scope": GuardrailConfig.scope,
    "triggers_30d": GuardrailConfig.triggers_30d,
    "blocked_30d": GuardrailConfig.blocked_30d,
    "masked_30d": GuardrailConfig.masked_30d,
    "effectiveness": GuardrailConfig.effectiveness,
    "added_latency_ms": GuardrailConfig.added_latency_ms,
    "last_triggered_at": GuardrailConfig.last_triggered_at,
    "created_at": GuardrailConfig.created_at,
    "updated_at": GuardrailConfig.updated_at,
}

EVENT_SORTABLE: Final[dict[str, Any]] = {
    "occurred_at": GuardrailEvent.occurred_at,
    "action_taken": GuardrailEvent.action_taken,
    "score": GuardrailEvent.score,
    "agent_id": GuardrailEvent.agent_id,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _window_start(window_days: int = WINDOW_DAYS) -> dt.datetime:
    return _now() - dt.timedelta(days=window_days)


# ---------------------------------------------------------------------------
# Reading the checker's answer
# ---------------------------------------------------------------------------


def _check_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten the checker's reply into one row per validation."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("validations", "results", "checks", "guardrails", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return [payload]


def _row_name(row: dict[str, Any]) -> str:
    for key in ("name", "validation", "type", "guardrail", "check"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _row_score(row: dict[str, Any]) -> float | None:
    for key in ("score", "confidence", "probability", "value"):
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return round(float(value), 4)
    return None


def _row_triggered(row: dict[str, Any], score: float | None, threshold: float) -> bool:
    """Did this validation fire?

    The checker states the verdict when it can; only when it reports nothing but
    a score is the threshold applied here, which is the same rule the engine
    would apply itself.
    """
    for key in ("triggered", "detected", "failed", "flagged", "blocked"):
        value = row.get(key)
        if isinstance(value, bool):
            return value
    for key in ("passed", "valid", "validation_passed"):
        value = row.get(key)
        if isinstance(value, bool):
            return not value
    if score is not None:
        return score >= threshold
    return False


def _row_matches(row: dict[str, Any]) -> list[MatchedSpan]:
    """Everything the checker pointed at inside the text."""
    raw: list[Any] = []
    for key in ("matches", "entities", "spans", "detections", "findings"):
        value = row.get(key)
        if isinstance(value, list):
            raw = value
            break
    if not raw:
        # The scanner answers with validation_details.detected_entities: a map
        # of entity label -> [{start, end, score, text}] rather than a flat
        # list; flatten it, carrying the label onto each hit.
        details = row.get("validation_details")
        if isinstance(details, dict):
            detected = details.get("detected_entities")
            if isinstance(detected, dict):
                for label, hits in detected.items():
                    for hit in hits if isinstance(hits, list) else []:
                        entry = dict(hit) if isinstance(hit, dict) else {}
                        entry.setdefault("label", str(label))
                        raw.append(entry)

    spans: list[MatchedSpan] = []
    for entry in raw:
        if isinstance(entry, str):
            spans.append(MatchedSpan(label=entry))
            continue
        if not isinstance(entry, dict):
            continue
        label = ""
        for key in ("label", "entity_type", "entity", "type", "name", "category"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                label = value.strip()
                break
        text = None
        for key in ("text", "value", "match", "excerpt"):
            value = entry.get(key)
            if isinstance(value, str):
                text = value
                break
        start = entry.get("start") if isinstance(entry.get("start"), int) else None
        end = entry.get("end") if isinstance(entry.get("end"), int) else None
        score = _row_score(entry)
        spans.append(
            MatchedSpan(label=label or "match", text=text, start=start, end=end, score=score)
        )
    return spans


def _mask(text: str, spans: Sequence[MatchedSpan]) -> str:
    """Blank out every matched span, longest offsets first so indexes hold.

    Spans the checker located by offset are masked in place; spans it only
    quoted are masked by replacing the quoted text. A span it neither located
    nor quoted cannot be masked, and the text is returned untouched rather than
    mangled.
    """
    masked = text
    positioned = sorted(
        (s for s in spans if s.start is not None and s.end is not None and s.end > s.start),
        key=lambda s: s.start or 0,
        reverse=True,
    )
    for span in positioned:
        start, end = max(0, span.start or 0), min(len(masked), span.end or 0)
        if start < end:
            masked = masked[:start] + "*" * (end - start) + masked[end:]
    for span in spans:
        if span.start is None and span.text:
            masked = masked.replace(span.text, "*" * len(span.text))
    return masked


def _pair_rows(
    guardrails: Sequence[GuardrailConfig], rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Match each guardrail to the validation row that answers for it.

    Names are matched first; a checker that answers positionally is honoured
    only when the counts line up exactly, so a mismatched reply is reported as
    "no verdict" rather than attributed to the wrong guardrail.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = _row_name(row)
        if name and name not in by_name:
            by_name[name] = row

    paired: dict[str, dict[str, Any]] = {}
    unmatched: list[GuardrailConfig] = []
    for guardrail in guardrails:
        wanted = validation_name(guardrail.guardrail_type, guardrail.config).upper()
        row = by_name.get(wanted)
        if row is None:
            unmatched.append(guardrail)
            continue
        paired[guardrail.id] = row

    leftovers = [row for row in rows if _row_name(row) not in by_name or not _row_name(row)]
    if unmatched and len(unmatched) == len(leftovers):
        for guardrail, row in zip(unmatched, leftovers, strict=True):
            paired[guardrail.id] = row
    return paired


#: The validations the inline scanner actually implements. Anything else sent
#: in the same request 400s the whole batch, so unsupported guardrails are
#: excluded from the call and simply produce no verdict — never a simulated one.
SUPPORTED_CHECKER_VALIDATIONS: Final[frozenset[str]] = frozenset(
    {"PII", "TOPIC", "PROMPT_INJECTION", "CUSTOM_CLASSIFIER"}
)


def checker_supported(guardrail: GuardrailConfig) -> bool:
    """Whether the inline scanner can answer for this guardrail at all."""
    return (
        validation_name(guardrail.guardrail_type, guardrail.config).upper()
        in SUPPORTED_CHECKER_VALIDATIONS
    )


def _checker_request(guardrail: GuardrailConfig) -> dict[str, Any]:
    """One validation entry in the scanner's own contract: {type, config}."""
    config = {
        "threshold": guardrail.threshold,
        **{
            key: value
            for key, value in (guardrail.config or {}).items()
            if key != "validation"
        },
    }
    name = validation_name(guardrail.guardrail_type, guardrail.config).upper()
    if name == "TOPIC":
        # The scanner requires a mode for topic checks; a guardrail listing
        # topics means those topics are restricted unless it says otherwise.
        config.setdefault("mode", "restrict")
    return {"type": name, "config": config}


#: Validation name -> monotonic instant until which the scanner is not asked
#: for it. Per process: the worst case is each worker discovering the same
#: broken validation once.
_SUSPENDED: dict[str, float] = {}

#: Scanner calls in flight at once for one ingest batch.
CHECK_CONCURRENCY: Final[int] = 8


def _validation(guardrail: GuardrailConfig) -> str:
    return validation_name(guardrail.guardrail_type, guardrail.config).upper()


def suspended_validations() -> list[str]:
    """Validations the scanner recently proved unable to run, for diagnostics."""
    now = time.monotonic()
    return sorted(name for name, until in _SUSPENDED.items() if until > now)


def _is_suspended(name: str) -> bool:
    until = _SUSPENDED.get(name)
    if until is None:
        return False
    if until <= time.monotonic():
        del _SUSPENDED[name]
        return False
    return True


def _suspend(name: str, exc: Exception) -> None:
    _SUSPENDED[name] = time.monotonic() + settings.guardrail_suspend_seconds
    log.error(
        "the content scanner cannot run %s checks (%s); guardrails of that type are "
        "NOT being enforced and will not be retried for %.0fs",
        name,
        exc,
        settings.guardrail_suspend_seconds,
    )


async def _run_checker(
    text: str, guardrails: Sequence[GuardrailConfig], *, isolate: bool = False
) -> tuple[dict[str, dict[str, Any]], int]:
    """Run the real checks for every supported guardrail, and time them.

    The scanner takes every validation in one request and answers 500 for the
    whole request if any one of them cannot run. One guardrail whose model is
    not installed therefore silenced every other guardrail in the workspace --
    a prompt-injection rule nobody could run switched PII scanning off.

    With ``isolate`` a refused request is asked again one validation at a time,
    so the healthy ones still answer, and whichever is broken is suspended
    rather than paid for on every batch. Without it the failure is raised,
    which is what the console's own "test this guardrail" wants to show.
    """
    client = get_engine_client()
    supported = [guardrail for guardrail in guardrails if checker_supported(guardrail)]
    if isolate:
        supported = [g for g in supported if not _is_suspended(_validation(g))]
    if not supported:
        return {}, 0
    timeout = settings.guardrail_check_timeout_seconds
    started = time.perf_counter()

    def elapsed() -> int:
        return int(round((time.perf_counter() - started) * 1000))

    try:
        payload = await client.evaluate_guardrails(
            text, [_checker_request(guardrail) for guardrail in supported], timeout_seconds=timeout
        )
        return _pair_rows(supported, _check_rows(payload)), elapsed()
    except EngineError as exc:
        if not isolate:
            raise translate_engine_error(exc) from exc
        if len(supported) == 1:
            _suspend(_validation(supported[0]), exc)
            return {}, elapsed()

    async def alone(guardrail: GuardrailConfig) -> dict[str, dict[str, Any]]:
        try:
            answer = await client.evaluate_guardrails(
                text, [_checker_request(guardrail)], timeout_seconds=timeout
            )
        except EngineError as exc:
            _suspend(_validation(guardrail), exc)
            return {}
        return _pair_rows([guardrail], _check_rows(answer))

    merged: dict[str, dict[str, Any]] = {}
    for paired in await asyncio.gather(*(alone(guardrail) for guardrail in supported)):
        merged.update(paired)
    return merged, elapsed()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(GuardrailConfig).where(
        GuardrailConfig.workspace_id == principal.workspace_id
    )


def _filtered_stmt(
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt,
        params,
        [
            GuardrailConfig.name,
            GuardrailConfig.guardrail_type,
            GuardrailConfig.action,
            GuardrailConfig.scope_ref,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            GuardrailConfig.status: status,
            GuardrailConfig.guardrail_type: guardrail_type,
            GuardrailConfig.action: action,
            GuardrailConfig.scope: scope,
            GuardrailConfig.owner_user_id: owner_user_id,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=GuardrailConfig.name, default_desc=False)


class _Rollup:
    """Counted activity for one guardrail over the window."""

    __slots__ = ("triggers", "blocked", "masked", "last_triggered_at")

    def __init__(
        self,
        triggers: int = 0,
        blocked: int = 0,
        masked: int = 0,
        last_triggered_at: dt.datetime | None = None,
    ) -> None:
        self.triggers = triggers
        self.blocked = blocked
        self.masked = masked
        self.last_triggered_at = last_triggered_at


def _events_where(condition: Any) -> Any:
    """SUM of a predicate over event rows, zero rather than NULL when empty."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


async def _rollups(
    session: AsyncSession,
    principal: Principal,
    guardrail_ids: Sequence[str],
    since: dt.datetime,
) -> dict[str, _Rollup]:
    """Triggers, blocks, masks and last-fired per guardrail, in one statement."""
    if not guardrail_ids:
        return {}
    rows = (
        await session.execute(
            select(
                GuardrailEvent.guardrail_id,
                func.count(GuardrailEvent.id),
                _events_where(GuardrailEvent.action_taken == GuardrailAction.BLOCK.value),
                _events_where(GuardrailEvent.action_taken == GuardrailAction.MASK.value),
                func.max(GuardrailEvent.occurred_at),
            )
            .where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.guardrail_id.in_(list(guardrail_ids)),
                GuardrailEvent.occurred_at >= since,
            )
            .group_by(GuardrailEvent.guardrail_id)
        )
    ).all()
    return {
        row[0]: _Rollup(int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), row[4])
        for row in rows
    }


async def _coverage_names(
    session: AsyncSession, principal: Principal, rows: Sequence[GuardrailConfig]
) -> dict[str, str]:
    """Agent names for the agent-scoped guardrails on the page."""
    agent_ids = {
        row.scope_ref
        for row in rows
        if row.scope == GuardrailScope.AGENT.value and row.scope_ref
    }
    if not agent_ids:
        return {}
    found = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == principal.workspace_id, Agent.id.in_(agent_ids)
            )
        )
    ).all()
    return {agent_id: name for agent_id, name in found}


def _read(
    guardrail: GuardrailConfig,
    *,
    rollup: _Rollup | None = None,
    coverage: str | None = None,
    owner: str | None = None,
) -> GuardrailRead:
    counted = rollup or _Rollup()
    return GuardrailRead(
        id=guardrail.id,
        name=guardrail.name,
        guardrail_type=GuardrailType(guardrail.guardrail_type),
        status=GuardrailStatus(guardrail.status),
        action=GuardrailAction(guardrail.action),
        threshold=guardrail.threshold,
        scope=GuardrailScope(guardrail.scope),
        scope_ref=guardrail.scope_ref,
        coverage=coverage_label(guardrail.scope, guardrail.scope_ref, coverage),
        triggers_30d=counted.triggers,
        blocked_30d=counted.blocked,
        masked_30d=counted.masked,
        effectiveness=guardrail.effectiveness,
        added_latency_ms=guardrail.added_latency_ms,
        last_triggered_at=counted.last_triggered_at or guardrail.last_triggered_at,
        config=dict(guardrail.config or {}),
        engine_guardrail_id=guardrail.engine_guardrail_id,
        owner_user_id=guardrail.owner_user_id,
        owner_name=owner,
        checker_supported=checker_supported(guardrail),
        created_at=guardrail.created_at,
        updated_at=guardrail.updated_at,
        created_by=guardrail.created_by,
        updated_by=guardrail.updated_by,
    )


async def read_many(
    session: AsyncSession,
    principal: Principal,
    rows: Sequence[GuardrailConfig],
    *,
    window_days: int = WINDOW_DAYS,
) -> list[GuardrailRead]:
    """Turn rows into table records, counting their activity in one statement."""
    rollups = await _rollups(
        session, principal, [row.id for row in rows], _window_start(window_days)
    )
    coverage = await _coverage_names(session, principal, rows)
    owners = await owner_names(session, principal, [row.owner_user_id for row in rows])
    return [
        _read(
            row,
            rollup=rollups.get(row.id),
            coverage=coverage.get(row.scope_ref or ""),
            owner=owners.get(row.owner_user_id or ""),
        )
        for row in rows
    ]


async def list_guardrails(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[Sequence[GuardrailConfig], int]:
    """One page of the workspace's guardrails, ordered by name by default."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        guardrail_type=guardrail_type,
        action=action,
        scope=scope,
        owner_user_id=owner_user_id,
    )
    return await paginate(session, stmt, params)


async def export_guardrails(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> Sequence[GuardrailConfig]:
    """Every guardrail matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        guardrail_type=guardrail_type,
        action=action,
        scope=scope,
        owner_user_id=owner_user_id,
    ).limit(EXPORT_LIMIT)
    return (await session.execute(stmt)).scalars().all()


async def get_guardrail(
    session: AsyncSession, principal: Principal, guardrail_id: str
) -> GuardrailConfig:
    """Load one guardrail, or raise :class:`NotFound`.

    A guardrail in another workspace answers 404, not 403: a 403 would confirm
    the id is real.
    """
    guardrail = (
        await session.execute(_scoped(principal).where(GuardrailConfig.id == guardrail_id))
    ).scalar_one_or_none()
    if guardrail is None:
        raise NotFound(f"Guardrail '{guardrail_id}' does not exist.")
    return guardrail


def _match_count(matched: dict[str, Any] | None) -> int:
    """How many things one event's ``matched`` payload points at.

    Two producers write this field in different shapes. The inline checker
    writes ``{"count": N, "labels": [...], "spans": [...]}``; the SDK's
    ``log_guardrail_event`` passes the reporter's own dict through verbatim,
    which by convention is a label→count map like ``{"CARD": 1, "PHONE": 1}``
    (a label may also carry the list of matched values). Both must total the
    same way — an event whose ``matched`` names real entities must never
    render as zero matches beside them.
    """
    if not matched:
        return 0
    count = matched.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    spans = matched.get("spans")
    if isinstance(spans, list):
        return len(spans)
    total = 0
    for value in matched.values():
        if not value:
            continue
        if isinstance(value, bool):
            total += 1
        elif isinstance(value, int):
            total += value
        elif isinstance(value, (list, tuple)):
            total += len(value)
        else:
            total += 1
    return total


async def list_events(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    guardrail_id: str | None = None,
    agent_id: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    window_days: int = WINDOW_DAYS,
) -> tuple[list[GuardrailEventRead], int]:
    """The recent-detections feed, newest first, with names attached.

    Filtering by guardrail type joins the definition table rather than
    denormalising the type onto every event row.
    """
    if guardrail_id is not None:
        await get_guardrail(session, principal, guardrail_id)

    stmt = select(GuardrailEvent).where(
        GuardrailEvent.workspace_id == principal.workspace_id,
        GuardrailEvent.occurred_at >= _window_start(window_days),
    )
    stmt = apply_search(stmt, params, [GuardrailEvent.sample, GuardrailEvent.trace_id])
    stmt = apply_filters(
        stmt,
        {
            GuardrailEvent.guardrail_id: guardrail_id,
            GuardrailEvent.agent_id: agent_id,
            GuardrailEvent.action_taken: action,
        },
    )
    if guardrail_type:
        stmt = stmt.where(
            GuardrailEvent.guardrail_id.in_(
                select(GuardrailConfig.id).where(
                    GuardrailConfig.workspace_id == principal.workspace_id,
                    GuardrailConfig.guardrail_type == guardrail_type,
                )
            )
        )
    stmt = apply_sort(
        stmt, params, EVENT_SORTABLE, default=GuardrailEvent.occurred_at, default_desc=True
    )

    rows, total = await paginate(session, stmt, params)
    if not rows:
        return [], total

    definitions = {
        row_id: (name, kind)
        for row_id, name, kind in (
            await session.execute(
                select(
                    GuardrailConfig.id, GuardrailConfig.name, GuardrailConfig.guardrail_type
                ).where(
                    GuardrailConfig.workspace_id == principal.workspace_id,
                    GuardrailConfig.id.in_({row.guardrail_id for row in rows}),
                )
            )
        ).all()
    }
    agent_ids = {row.agent_id for row in rows if row.agent_id}
    agents = (
        {
            agent_id: name
            for agent_id, name in (
                await session.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.workspace_id == principal.workspace_id, Agent.id.in_(agent_ids)
                    )
                )
            ).all()
        }
        if agent_ids
        else {}
    )

    payload: list[GuardrailEventRead] = []
    for row in rows:
        name, kind = definitions.get(row.guardrail_id, (None, None))
        payload.append(
            GuardrailEventRead(
                id=row.id,
                guardrail_id=row.guardrail_id,
                guardrail_name=name,
                guardrail_type=GuardrailType(kind) if kind else None,
                agent_id=row.agent_id,
                agent_name=agents.get(row.agent_id or ""),
                trace_id=row.trace_id,
                occurred_at=row.occurred_at,
                action_taken=GuardrailAction(row.action_taken),
                score=row.score,
                match_count=_match_count(row.matched),
                matched=dict(row.matched or {}),
                sample=row.sample,
            )
        )
    return payload, total


async def _pii_items_masked(
    session: AsyncSession, principal: Principal, since: dt.datetime
) -> int:
    """Entities masked by PII guardrails in the window.

    A single call can mask several entities, so this counts the matches carried
    by the events rather than the events themselves. The scan is bounded; past
    :data:`PII_SCAN_LIMIT` events it falls back to the cached per-guardrail
    rollups, which count the same thing more coarsely.
    """
    pii_ids = select(GuardrailConfig.id).where(
        GuardrailConfig.workspace_id == principal.workspace_id,
        GuardrailConfig.guardrail_type == GuardrailType.PII.value,
    )
    rows = (
        await session.execute(
            select(GuardrailEvent.matched)
            .where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.occurred_at >= since,
                GuardrailEvent.action_taken == GuardrailAction.MASK.value,
                GuardrailEvent.guardrail_id.in_(pii_ids),
            )
            .limit(PII_SCAN_LIMIT)
        )
    ).all()

    if len(rows) < PII_SCAN_LIMIT:
        return sum(_match_count(row[0]) for row in rows)

    cached = (
        await session.execute(
            select(func.coalesce(func.sum(GuardrailConfig.masked_30d), 0)).where(
                GuardrailConfig.workspace_id == principal.workspace_id,
                GuardrailConfig.guardrail_type == GuardrailType.PII.value,
            )
        )
    ).scalar_one()
    return int(cached or 0)


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = WINDOW_DAYS
) -> GuardrailsSummary:
    """The five KPI cards, computed with SQL aggregates over the event table."""
    workspace = GuardrailConfig.workspace_id == principal.workspace_id

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(GuardrailConfig.id),
                _count_where(GuardrailConfig.status == GuardrailStatus.ACTIVE.value),
                _count_where(GuardrailConfig.status == GuardrailStatus.DISABLED.value),
                _count_where(GuardrailConfig.status == GuardrailStatus.TUNING.value),
                func.avg(
                    case(
                        (
                            GuardrailConfig.status == GuardrailStatus.ACTIVE.value,
                            GuardrailConfig.added_latency_ms,
                        ),
                    )
                ),
            ).where(workspace)
        )
    ).one()

    now = _now()
    window = dt.timedelta(days=window_days)
    current_start, previous_start = now - window, now - 2 * window

    async def _counts(
        since: dt.datetime, until: dt.datetime
    ) -> tuple[int, int, dt.datetime | None]:
        row = (
            await session.execute(
                select(
                    func.count(GuardrailEvent.id),
                    _events_where(
                        GuardrailEvent.action_taken == GuardrailAction.BLOCK.value
                    ),
                    func.max(GuardrailEvent.occurred_at),
                ).where(
                    GuardrailEvent.workspace_id == principal.workspace_id,
                    GuardrailEvent.occurred_at >= since,
                    GuardrailEvent.occurred_at < until,
                )
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0), row[2]

    triggers, blocked, last_triggered = await _counts(current_start, now)
    previous_triggers, _, _ = await _counts(previous_start, current_start)

    masked_now = await _pii_items_masked(session, principal, current_start)
    masked_before = await _pii_items_masked(session, principal, previous_start)
    masked_before = max(0, masked_before - masked_now)

    def _delta(current: int, earlier: int) -> float | None:
        if earlier <= 0:
            return None
        return round((current - earlier) / earlier * 100, 1)

    latency = totals[4]
    return GuardrailsSummary(
        window_days=window_days,
        active=int(totals[1] or 0),
        configured=int(totals[0] or 0),
        disabled=int(totals[2] or 0),
        tuning=int(totals[3] or 0),
        triggers=triggers,
        triggers_delta_percent=_delta(triggers, previous_triggers),
        blocked=blocked,
        pii_items_masked=masked_now,
        pii_items_masked_delta_percent=_delta(masked_now, masked_before),
        avg_added_latency_ms=round(float(latency), 1) if latency is not None else None,
        last_triggered_at=last_triggered,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


#: What the mirrored evaluator actually scans for, per guardrail type. These
#: are heuristics run against every sampled trace by the engine's metric
#: runner — real checks, deliberately conservative ones.
_SCAN_PATTERNS: Final[dict[str, dict[str, str]]] = {
    "PII": {
        "EMAIL": r"[\w.+-]+@[\w-]+\.[\w.]{2,}",
        "PHONE": r"\+?\d[\d\s().-]{8,}\d",
        "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
        "CARD": r"\b(?:\d[ -]?){13,16}\b",
    },
    "SECRETS": {
        "API_KEY": r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}",
        "AWS_KEY": r"\bAKIA[0-9A-Z]{16}\b",
        "BEARER": r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}",
        "PRIVATE_KEY": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    },
    "PROMPT_INJECTION": {
        "OVERRIDE": r"ignore (all |any )?(previous|prior|above) (instructions|prompts)",
        "SYSTEM_SWAP": r"you are now|pretend to be|act as if you (are|were)",
        "EXFIL": r"(reveal|print|show).{0,30}(system prompt|instructions)",
    },
}

_METRIC_SOURCE_TEMPLATE: Final[str] = '''\
import re
from typing import Any

from {metric_library}.evaluation.metrics import base_metric, score_result

PATTERNS = {patterns!r}


class GuardrailScan(base_metric.BaseMetric):
    """Pattern scan mirrored from the control plane guardrail {guardrail_name!r}."""

    def __init__(self, name: str = {metric_name!r}):
        self.name = name

    def score(self, output: Any = "", **ignored: Any) -> score_result.ScoreResult:
        text = output if isinstance(output, str) else str(output)
        hits = sorted(
            label
            for label, pattern in PATTERNS.items()
            if re.search(pattern, text, re.IGNORECASE)
        )
        return score_result.ScoreResult(
            value=0.0 if hits else 1.0,
            name=self.name,
            reason="matched: " + ", ".join(hits) if hits else "clean",
        )
'''


def _metric_source(guardrail: GuardrailConfig) -> str:
    """Executable scan code for the engine's metric runner.

    Patterns come from the guardrail's type, and a ``patterns`` mapping in the
    config extends or replaces them — so a Custom guardrail with no patterns
    scores every trace 1.0 and says "clean", which is what a rule with nothing
    to look for honestly does.
    """
    validation = validation_name(guardrail.guardrail_type, guardrail.config).upper()
    patterns = dict(_SCAN_PATTERNS.get(validation, {}))
    configured = (guardrail.config or {}).get("patterns")
    if isinstance(configured, dict):
        patterns.update({str(key): str(value) for key, value in configured.items()})
    metric_name = validation.lower() + "_scan"
    return _METRIC_SOURCE_TEMPLATE.format(
        metric_library=settings.engine_metric_library,
        patterns=patterns,
        guardrail_name=guardrail.name,
        metric_name=metric_name,
    )


def _engine_definition(
    guardrail: GuardrailConfig, project_ids: Sequence[str]
) -> dict[str, Any]:
    """The guardrail as the engine's rule store wants it.

    The store holds automation-rule evaluators — a discriminated union keyed by
    ``type`` — so the guardrail is mirrored as a ``user_defined_metric_python``
    rule carrying real scan code, attached to every namespace the guardrail's
    scope covers. Action and threshold stay on our side: they drive the inline
    checker at ingest, which the engine's rule vocabulary does not model.
    """
    return {
        "name": guardrail.name[:150],
        "type": "user_defined_metric_python",
        "project_ids": list(project_ids),
        "sampling_rate": 1.0,
        "enabled": guardrail.status == GuardrailStatus.ACTIVE.value,
        "code": {
            "metric": _metric_source(guardrail),
            "arguments": {"output": "output"},
        },
    }


async def _scope_project_ids(
    session: AsyncSession, guardrail: GuardrailConfig
) -> list[str]:
    """Engine namespaces of every provisioned agent the guardrail covers."""
    stmt = select(Agent.engine_project_id).where(
        Agent.workspace_id == guardrail.workspace_id,
        Agent.engine_project_id.is_not(None),
    )
    if guardrail.scope == GuardrailScope.AGENT.value and guardrail.scope_ref:
        stmt = stmt.where(Agent.id == guardrail.scope_ref)
    elif guardrail.scope == GuardrailScope.ENVIRONMENT.value and guardrail.scope_ref:
        stmt = stmt.where(Agent.environment == guardrail.scope_ref)
    return list((await session.execute(stmt)).scalars().all())


async def _mirror_create(session: AsyncSession, guardrail: GuardrailConfig) -> None:
    """Register the rule with the engine so enforcement matches the record."""
    if not settings.engine_metric_library:
        # The rule store executes metric code that imports the engine's own
        # library, whose package name is deployment configuration (see
        # Settings.engine_metric_library). Without it there is no runnable
        # code to register; inline enforcement still applies at ingest.
        log.info(
            "guardrail %s not mirrored: engine metric library not configured",
            guardrail.id,
        )
        return
    project_ids = await _scope_project_ids(session, guardrail)
    if not project_ids:
        # An evaluator has to be attached to at least one namespace. With no
        # provisioned agents in scope there is nothing to attach to yet; the
        # next update re-mirrors once agents exist.
        log.info(
            "guardrail %s not mirrored to the engine: no provisioned agents in scope",
            guardrail.id,
        )
        return
    client = get_engine_client()
    try:
        guardrail.engine_guardrail_id = await client.create_guardrail(
            _engine_definition(guardrail, project_ids)
        )
    except EngineError as exc:
        raise translate_engine_error(exc) from exc


async def _mirror_update(session: AsyncSession, guardrail: GuardrailConfig) -> None:
    if not guardrail.engine_guardrail_id:
        await _mirror_create(session, guardrail)
        return
    if not settings.engine_metric_library:
        log.info(
            "guardrail %s mirror not updated: engine metric library not configured",
            guardrail.id,
        )
        return
    project_ids = await _scope_project_ids(session, guardrail)
    client = get_engine_client()
    try:
        await client.update_guardrail(
            guardrail.engine_guardrail_id, _engine_definition(guardrail, project_ids)
        )
    except EngineError as exc:
        raise translate_engine_error(exc) from exc


async def _validate_scope(
    session: AsyncSession, principal: Principal, scope: str, scope_ref: str | None
) -> None:
    """A scoped guardrail must point at something that exists in this workspace."""
    if scope == GuardrailScope.GLOBAL.value:
        return
    if not scope_ref:
        raise PreconditionFailed(f"A {scope}-scoped guardrail needs a scope reference.")
    if scope == GuardrailScope.AGENT.value:
        agent = (
            await session.execute(
                select(Agent.id).where(
                    Agent.workspace_id == principal.workspace_id, Agent.id == scope_ref
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise NotFound(f"Agent '{scope_ref}' does not exist.")


async def create_guardrail(
    session: AsyncSession,
    principal: Principal,
    payload: GuardrailCreate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Register a guardrail and mirror it into the engine's rule store."""
    principal.require(Role.ADMIN)
    await _validate_scope(session, principal, payload.scope.value, payload.scope_ref)

    clash = (
        await session.execute(_scoped(principal).where(GuardrailConfig.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A guardrail named '{payload.name}' already exists.")

    guardrail = GuardrailConfig(
        workspace_id=principal.workspace_id,
        name=payload.name,
        guardrail_type=payload.guardrail_type.value,
        status=payload.status.value,
        action=payload.action.value,
        threshold=payload.threshold,
        scope=payload.scope.value,
        scope_ref=payload.scope_ref,
        config=dict(payload.config),
        owner_user_id=payload.owner_user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(guardrail)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A guardrail named '{payload.name}' already exists.") from exc

    await _mirror_create(session, guardrail)
    await audit.record(
        session,
        principal=principal,
        action="guardrail.created",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{guardrail.guardrail_type} guardrail, action {guardrail.action}",
        metadata={"threshold": guardrail.threshold, "scope": guardrail.scope},
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def update_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    payload: GuardrailUpdate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if payload.expected_updated_at is not None and guardrail.updated_at is not None:
        expected = _as_utc(payload.expected_updated_at)
        if abs((_as_utc(guardrail.updated_at) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{guardrail.name}' was changed by someone else. Reload and try again."
            )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return guardrail

    if "name" in changes and changes["name"] != guardrail.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    GuardrailConfig.name == changes["name"],
                    GuardrailConfig.id != guardrail.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A guardrail named '{changes['name']}' already exists.")

    scope = changes.get("scope", GuardrailScope(guardrail.scope))
    scope_value = scope.value if isinstance(scope, GuardrailScope) else scope
    if "scope" in changes or "scope_ref" in changes:
        await _validate_scope(
            session,
            principal,
            scope_value,
            changes.get("scope_ref", guardrail.scope_ref),
        )

    vocabularies = (GuardrailType, GuardrailStatus, GuardrailAction, GuardrailScope)
    for field, value in changes.items():
        setattr(guardrail, field, value.value if isinstance(value, vocabularies) else value)
    guardrail.updated_by = principal.actor
    await session.flush()

    await _mirror_update(session, guardrail)
    await audit.record(
        session,
        principal=principal,
        action="guardrail.updated",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes)},
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def delete_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a guardrail here and in the engine. Its events go with it."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if guardrail.engine_guardrail_id:
        client = get_engine_client()
        try:
            await client.delete_guardrails([guardrail.engine_guardrail_id])
        except EngineError as exc:
            raise translate_engine_error(exc) from exc

    await audit.record(
        session,
        principal=principal,
        action="guardrail.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed {guardrail.guardrail_type} guardrail",
        request=request,
    )
    await session.delete(guardrail)
    await session.flush()


async def set_status(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    status: GuardrailStatus,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Enable or disable a guardrail, in the record and in the engine."""
    principal.require(Role.OPERATOR)
    guardrail = await get_guardrail(session, principal, guardrail_id)
    if guardrail.status == status.value:
        raise PreconditionFailed(f"'{guardrail.name}' is already {status.value}.")

    previous = guardrail.status
    guardrail.status = status.value
    guardrail.updated_by = principal.actor
    await session.flush()
    await _mirror_update(session, guardrail)

    await audit.record(
        session,
        principal=principal,
        action=(
            "guardrail.enabled" if status is GuardrailStatus.ACTIVE else "guardrail.disabled"
        ),
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{previous} to {status.value}",
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def set_threshold(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    payload: GuardrailThresholdUpdate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """The Tune action: move the threshold, and the action with it if asked."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    previous_threshold = guardrail.threshold
    previous_action = guardrail.action
    guardrail.threshold = payload.threshold
    if payload.action is not None:
        guardrail.action = payload.action.value
    guardrail.updated_by = principal.actor
    await session.flush()
    await _mirror_update(session, guardrail)

    detail = f"Threshold {previous_threshold:.2f} to {payload.threshold:.2f}"
    if payload.action is not None and payload.action.value != previous_action:
        detail += f"; action {previous_action} to {payload.action.value}"
    if payload.reason:
        detail += f" ({payload.reason})"

    await audit.record(
        session,
        principal=principal,
        action="guardrail.tuned",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"threshold": payload.threshold, "previous_threshold": previous_threshold},
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _hit_from(
    guardrail: GuardrailConfig, row: dict[str, Any] | None
) -> tuple[bool, float | None, list[MatchedSpan]]:
    if row is None:
        return False, None, []
    score = _row_score(row)
    triggered = _row_triggered(row, score, guardrail.threshold)
    return triggered, score, _row_matches(row)


async def test_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    text: str,
    *,
    request: Request | None = None,
) -> GuardrailTestResult:
    """Run one real check against a sample and report what it found.

    The check is executed by the engine's content checker, and the latency is
    the measured round trip. Nothing is written to the event table: a test must
    not move the counters the Guardrails screen reports.
    """
    principal.require(Role.OPERATOR)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if not checker_supported(guardrail):
        # Never simulate a verdict: a validation the scanner does not
        # implement cannot be tested, and saying "Clear" would be a lie.
        wanted = validation_name(guardrail.guardrail_type, guardrail.config)
        raise PreconditionFailed(
            f"The content checker on this deployment does not implement "
            f"'{wanted}'. It answers for: "
            + ", ".join(sorted(SUPPORTED_CHECKER_VALIDATIONS))
            + ".",
            details={"validation": wanted},
        )

    paired, latency_ms = await _run_checker(text, [guardrail])
    triggered, score, matches = _hit_from(guardrail, paired.get(guardrail.id))
    enforced = guardrail.status == GuardrailStatus.ACTIVE.value
    action = GuardrailAction(guardrail.action)

    masked_text = None
    if triggered and action is GuardrailAction.MASK and matches:
        masked_text = _mask(text, matches)

    if triggered:
        detail = (
            f"{guardrail.name} triggered"
            + (f" at {score:.2f}" if score is not None else "")
            + f" ({len(matches)} match(es)); "
            + (f"{action.value} would be applied" if enforced else "shadow mode, nothing enforced")
        )
    else:
        detail = f"{guardrail.name} did not trigger on this sample."

    await audit.record(
        session,
        principal=principal,
        action="guardrail.tested",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"triggered": triggered, "score": score, "latency_ms": latency_ms},
        request=request,
    )
    await session.flush()

    return GuardrailTestResult(
        guardrail_id=guardrail.id,
        name=guardrail.name,
        guardrail_type=GuardrailType(guardrail.guardrail_type),
        triggered=triggered,
        verdict="Triggered" if triggered else "Clear",
        score=score,
        threshold=guardrail.threshold,
        action=action,
        action_applied=enforced,
        matched=matches,
        masked_text=masked_text,
        added_latency_ms=latency_ms,
        detail=detail,
    )


async def _runtime_guardrails(
    session: AsyncSession, workspace_id: str, agent_id: str | None, environment: str | None
) -> Sequence[GuardrailConfig]:
    """Every guardrail that applies to this call, in severity order."""
    scopes = [GuardrailConfig.scope == GuardrailScope.GLOBAL.value]
    if agent_id:
        scopes.append(
            (GuardrailConfig.scope == GuardrailScope.AGENT.value)
            & (GuardrailConfig.scope_ref == agent_id)
        )
    if environment:
        scopes.append(
            (GuardrailConfig.scope == GuardrailScope.ENVIRONMENT.value)
            & (GuardrailConfig.scope_ref == environment)
        )
    stmt = (
        select(GuardrailConfig)
        .where(
            GuardrailConfig.workspace_id == workspace_id,
            GuardrailConfig.status.in_(RUNTIME_STATUSES),
            or_(*scopes),
        )
        .order_by(GuardrailConfig.name.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def evaluate_content(
    session: AsyncSession,
    *,
    workspace_id: str,
    text: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
    principal: Principal | None = None,
    request: Request | None = None,
) -> ContentEvaluation:
    """Run every applicable guardrail over one piece of content.

    This is the runtime entry point the ingest path calls. It runs one check
    covering every guardrail in scope, applies the strongest action that fired,
    records an event per trigger, and returns the content with masked spans
    already replaced so the caller never has to re-apply a rule.

    Guardrails in Tuning status are evaluated and recorded but never enforced,
    which is what makes it safe to try a new rule against production traffic.
    Only a block is audited: a mask or a warning is fully described by its
    event row, and auditing every guarded call would drown the trail.
    """
    environment = None
    if agent_id:
        environment = (
            await session.execute(
                select(Agent.environment).where(
                    Agent.workspace_id == workspace_id, Agent.id == agent_id
                )
            )
        ).scalar_one_or_none()

    guardrails = await _runtime_guardrails(session, workspace_id, agent_id, environment)
    if not guardrails:
        return ContentEvaluation(
            allowed=True, blocked=False, text=text, evaluated=0, detail="No guardrails in scope."
        )

    paired, latency_ms = await _run_checker(text, guardrails)
    now = _now()
    per_guardrail_latency = max(1, latency_ms // max(1, len(guardrails)))

    hits: list[GuardrailHit] = []
    applied: list[tuple[GuardrailConfig, list[MatchedSpan]]] = []
    masked_text = text
    blocked = False

    for guardrail in guardrails:
        triggered, score, matches = _hit_from(guardrail, paired.get(guardrail.id))
        # The measured cost is charged to every guardrail that ran, whether or
        # not it fired: the latency tile is about the checking, not the finding.
        guardrail.added_latency_ms = int(
            round(
                guardrail.added_latency_ms * (1 - LATENCY_SMOOTHING)
                + per_guardrail_latency * LATENCY_SMOOTHING
            )
        )
        if not triggered:
            continue

        enforced = guardrail.status == GuardrailStatus.ACTIVE.value
        action = GuardrailAction(guardrail.action)
        hits.append(
            GuardrailHit(
                guardrail_id=guardrail.id,
                name=guardrail.name,
                guardrail_type=GuardrailType(guardrail.guardrail_type),
                action=action,
                score=score,
                threshold=guardrail.threshold,
                matched=matches,
                enforced=enforced,
            )
        )

        action_taken = action if enforced else GuardrailAction.LOG
        if enforced:
            if action is GuardrailAction.BLOCK:
                blocked = True
            elif action is GuardrailAction.MASK and matches:
                masked_text = _mask(masked_text, matches)
            applied.append((guardrail, matches))

        guardrail.triggers_30d += 1
        if action_taken is GuardrailAction.BLOCK:
            guardrail.blocked_30d += 1
        elif action_taken is GuardrailAction.MASK:
            guardrail.masked_30d += len(matches) or 1
        guardrail.last_triggered_at = now

        session.add(
            GuardrailEvent(
                workspace_id=workspace_id,
                guardrail_id=guardrail.id,
                agent_id=agent_id,
                trace_id=trace_id,
                occurred_at=now,
                action_taken=action_taken.value,
                score=score,
                matched={
                    "count": len(matches),
                    "labels": sorted({span.label for span in matches}),
                    "spans": [
                        span.model_dump(exclude={"text"}, mode="json") for span in matches
                    ],
                    "enforced": enforced,
                },
                sample=text[:EVENT_SAMPLE_LIMIT],
            )
        )

    strongest = None
    if applied:
        strongest = max(
            (guardrail for guardrail, _ in applied),
            key=lambda g: ACTION_SEVERITY.get(g.action, 0),
        )

    if blocked and principal is not None:
        names = ", ".join(hit.name for hit in hits if hit.action is GuardrailAction.BLOCK)
        await audit.record(
            session,
            principal=principal,
            action="guardrail.blocked",
            entity_type=ENTITY_TYPE,
            entity_id=strongest.id if strongest else None,
            entity_label=names,
            source_screen=SOURCE_SCREEN,
            detail=f"Content blocked by {names}",
            metadata={"agent_id": agent_id, "trace_id": trace_id},
            request=request,
        )

    await session.flush()

    return ContentEvaluation(
        allowed=not blocked,
        blocked=blocked,
        action=GuardrailAction(strongest.action) if strongest else None,
        text="" if blocked else masked_text,
        masked=masked_text != text and not blocked,
        hits=hits,
        evaluated=len(guardrails),
        added_latency_ms=latency_ms,
        detail=(
            f"{len(hits)} of {len(guardrails)} guardrail(s) triggered"
            if hits
            else f"{len(guardrails)} guardrail(s) cleared the content"
        ),
    )


async def evaluate_ingest_batch(
    session: AsyncSession,
    principal: Principal,
    candidates: Sequence[GuardrailCandidate],
) -> list[GuardrailVerdict]:
    """Judge a batch of ingested items against the guardrails in scope.

    This is the hook :mod:`services.ingest` duck-types on every trace/span
    batch. The split of responsibilities is ingest's: ingest extracts the
    content, applies the verdicts (block, mask) and writes the evidence rows;
    this side owns the checking and the per-guardrail counters. Tuning
    guardrails run in shadow — their verdict is reported as ``Log`` so the
    trial is visible in the evidence without being enforced.

    An engine failure propagates: ingest treats any exception from this hook
    as "not evaluated" and lets the telemetry through, which is the documented
    degrade-open posture for a broken checker.
    """
    from ..schemas.ingest import MAX_SAMPLE_LENGTH, GuardrailVerdict

    verdicts: list[GuardrailVerdict] = []
    now = _now()
    # One guardrail load per (agent, environment) pair: batches are almost
    # always single-agent, so this is one query, not one per item.
    scoped: dict[tuple[str | None, str | None], Sequence[GuardrailConfig]] = {}

    # The database first and on its own -- a session serves one operation at a
    # time -- so that the checks themselves can then run side by side.
    work: list[tuple[GuardrailCandidate, str, Sequence[GuardrailConfig]]] = []
    for candidate in candidates:
        text = candidate.text.strip()
        if not text:
            continue
        key = (candidate.agent_id, candidate.environment)
        if key not in scoped:
            scoped[key] = await _runtime_guardrails(
                session, principal.workspace_id, candidate.agent_id, candidate.environment
            )
        if scoped[key]:
            work.append((candidate, text, scoped[key]))
    if not work:
        return verdicts

    # A batch used to be checked one item after another, each on the full engine
    # timeout, inside the ingest request. Side by side, under one budget for the
    # whole batch: past it the batch is stored unevaluated, which ingest records
    # as exactly that, instead of the agent's request hanging on a slow scanner.
    gate = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def check(
        text: str, guardrails: Sequence[GuardrailConfig]
    ) -> tuple[dict[str, dict[str, Any]], int]:
        async with gate:
            return await _run_checker(text, guardrails, isolate=True)

    checked = await asyncio.wait_for(
        asyncio.gather(*(check(text, guardrails) for _, text, guardrails in work)),
        timeout=settings.guardrail_batch_budget_seconds,
    )

    #: guardrail id -> [triggers, blocked, masked]. Counted here and written once
    #: as increments, so two workers ingesting at once do not lose each other's
    #: counts, and so a guardrail that is busy firing can still be edited.
    counts: dict[str, list[int]] = {}
    latency: dict[str, int] = {}
    rows: dict[str, GuardrailConfig] = {}

    for (candidate, text, guardrails), (paired, latency_ms) in zip(work, checked, strict=True):
        per_guardrail_latency = max(1, latency_ms // max(1, len(guardrails)))

        for guardrail in guardrails:
            if guardrail.id not in paired:
                continue  # not asked, or the scanner could not answer: no verdict
            rows[guardrail.id] = guardrail
            triggered, score, matches = _hit_from(guardrail, paired.get(guardrail.id))
            latency[guardrail.id] = int(
                round(
                    latency.get(guardrail.id, guardrail.added_latency_ms) * (1 - LATENCY_SMOOTHING)
                    + per_guardrail_latency * LATENCY_SMOOTHING
                )
            )
            if not triggered:
                continue

            enforced = guardrail.status == GuardrailStatus.ACTIVE.value
            action_taken = GuardrailAction(guardrail.action) if enforced else GuardrailAction.LOG
            tally = counts.setdefault(guardrail.id, [0, 0, 0])
            tally[0] += 1
            if action_taken is GuardrailAction.BLOCK:
                tally[1] += 1
            elif action_taken is GuardrailAction.MASK:
                tally[2] += len(matches) or 1

            verdicts.append(
                GuardrailVerdict(
                    index=candidate.index,
                    guardrail_id=guardrail.id,
                    guardrail_name=guardrail.name,
                    action=action_taken.value,
                    score=score,
                    matched={
                        "count": len(matches),
                        "labels": sorted({span.label for span in matches}),
                        "spans": [
                            span.model_dump(exclude={"text"}, mode="json") for span in matches
                        ],
                        "enforced": enforced,
                    },
                    sample=text[:MAX_SAMPLE_LENGTH],
                    # The literal text found, so a mask removes that and only
                    # that. Never serialised: see the schema.
                    redactions=[
                        text[span.start : span.end]
                        for span in matches
                        if span.start is not None
                        and span.end is not None
                        and 0 <= span.start < span.end <= len(text)
                    ],
                )
            )

    for guardrail_id, guardrail in rows.items():
        tally = counts.get(guardrail_id)
        smoothed = latency.get(guardrail_id, guardrail.added_latency_ms)
        if tally is None and smoothed == guardrail.added_latency_ms:
            continue  # nothing to record: do not take a row lock for nothing
        values: dict[str, Any] = {
            "added_latency_ms": smoothed,
            # Named, and set to itself: a guardrail firing is not an edit to it.
            "updated_at": GuardrailConfig.updated_at,
        }
        if tally is not None:
            values.update(
                triggers_30d=GuardrailConfig.triggers_30d + tally[0],
                blocked_30d=GuardrailConfig.blocked_30d + tally[1],
                masked_30d=GuardrailConfig.masked_30d + tally[2],
                last_triggered_at=now,
            )
        await session.execute(
            sa_update(GuardrailConfig)
            .where(GuardrailConfig.id == guardrail_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    return verdicts
