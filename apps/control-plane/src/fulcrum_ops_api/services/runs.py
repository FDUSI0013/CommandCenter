"""Live Runs, the run inspector, the execution trace and Replay Studio.

A run is a trace in the telemetry engine. Nothing about a run is stored here —
this module is the mapping layer between the engine's trace/span model and the
governance vocabulary the console renders (tenant, source, risk, policy).

**Tenancy.** The engine holds every workspace's telemetry in one namespace and
authenticates nobody, so isolation is entirely this module's job. Every read is
addressed by a project id taken from an ``agents`` row that was already loaded
with a ``workspace_id`` filter. A trace whose project belongs to no agent in the
caller's workspace answers 404, exactly as if it did not exist. There is no code
path here that queries the engine without a project id from that set.

**Why the filtered set is assembled here.** Tenant, risk and policy are control
plane concepts carried on trace metadata by the ingest contract; the engine
indexes traces, not our vocabulary. So a request scans a bounded window of the
workspace's projects through the engine's cursor search, maps the rows, and then
filters, sorts and paginates in process. The scan is capped
(:data:`MAX_SCAN_TRACES` rows over at most :data:`MAX_SCAN_AGENTS` projects) and
every response that depends on it carries a :class:`ScanInfo` saying how much
was actually read, so a partial window is never presented as a complete total.

**Nothing is invented.** Where a trace does not carry a value, the field is
null. When the engine cannot answer, the caller gets
:class:`TelemetryBackendUnavailable` with the adapter's error as the cause — not
a zero.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from typing import Any, Final, TypeVar

from fastapi import Request
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import (
    NotFound,
    RateLimited,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.base import new_id
from ..db.session import get_sessionmaker
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    EngineUnavailable,
    get_engine_client,
)
from ..models.identity import Role
from ..models.registry import Agent, Platform, RiskLevel
from ..schemas.runs import (
    FLAG_SCORE_NAME,
    PREVIEW_CHARS,
    SPAN_PREVIEW_CHARS,
    GuardrailVerdict,
    ReplaySession,
    ReplayStep,
    ReplayStepKind,
    RetrievedChunk,
    RunDetail,
    RunErrors,
    RunFlagRequest,
    RunFlagResult,
    RunGuardrails,
    RunHistoryPage,
    RunPolicy,
    RunRead,
    RunResponse,
    RunRetrieval,
    RunScore,
    RunSpan,
    RunsSummary,
    RunStatus,
    RunTrace,
    ScanInfo,
    SparkKpi,
    SparkPoint,
    TimeRange,
    ToolCall,
    TranscriptMessage,
    as_metadata,
)
from . import audit

T = TypeVar("T")

SOURCE_SCREEN: Final[str] = "Live Runs"
ENTITY_TYPE: Final[str] = "run"

#: Hard ceiling on rows pulled from the engine to answer one request.
MAX_SCAN_TRACES: Final[int] = 2_000

#: Rows the engine returns per cursor page.
ENGINE_PAGE_SIZE: Final[int] = 500

#: Projects scanned per request, most recently used first.
MAX_SCAN_AGENTS: Final[int] = 100

#: Rows always read per project, however many projects there are.
MIN_ROWS_PER_AGENT: Final[int] = 50

#: Concurrent engine calls one request may have in flight.
SCAN_CONCURRENCY: Final[int] = 8

#: Spans read for one trace when building the tree or the replay step list.
MAX_SPANS: Final[int] = 1_000

MAX_EXPORT_ROWS: Final[int] = 5_000

#: Buckets in each sparkline under the KPI row.
SERIES_BUCKETS: Final[int] = 24

#: Live stream tuning. The poll interval is the latency a new run waits before
#: it appears; the overlap covers ingest lag so a late-arriving trace with an
#: earlier start time is not missed by the next cursor.
STREAM_POLL_SECONDS: Final[float] = 3.0
STREAM_OVERLAP_SECONDS: Final[float] = 30.0
STREAM_HEARTBEAT_SECONDS: Final[float] = 15.0
STREAM_MAX_SECONDS: Final[float] = 30 * 60.0
STREAM_SCAN_BUDGET: Final[int] = 200
STREAM_SEEN_IDS: Final[int] = 2_000

#: Concurrent live streams this process will serve. Each one polls the engine on
#: its own schedule, so the bound is expressed against the adapter's connection
#: pool rather than an arbitrary number.
MAX_CONCURRENT_STREAMS: Final[int] = max(1, settings.engine_max_connections // 4)

_RISK_ORDER: Final[dict[str, int]] = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
}


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _instant(value: Any) -> dt.datetime | None:
    """Parse an engine timestamp; an unparseable value is dropped, not guessed."""
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def _truthy(value: Any) -> bool:
    """Interpret the several ways a flag reaches us: bool, 'Yes', 1, 'true'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"yes", "true", "1", "y", "used", "escalated"}
    return False


def _preview(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


_TEXT_KEYS: Final[tuple[str, ...]] = (
    "input",
    "question",
    "prompt",
    "query",
    "text",
    "content",
    "output",
    "answer",
    "response",
    "result",
    "value",
)


def _payload_text(value: Any) -> str:
    """Flatten a trace or span payload into the text a human reads.

    Payloads are free-form JSON. The well-known keys are preferred, a message
    list collapses to its last message with content, and anything else is shown
    as compact JSON rather than dropped — an operator debugging an incident
    needs to see what was actually there.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        for item in reversed(value):
            text = _payload_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if key in value:
                text = _payload_text(value[key])
                if text:
                    return text
        for key in ("messages", "choices", "documents", "chunks"):
            if key in value:
                text = _payload_text(value[key])
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _telemetry_error(exc: EngineError) -> Exception:
    """Translate an adapter failure into the API's error envelope, keeping the cause."""
    if isinstance(exc, EngineNotFound):
        return NotFound("That run does not exist.")
    if isinstance(exc, EngineUnavailable):
        return TelemetryBackendUnavailable()
    if isinstance(exc, EngineBadRequest):
        return TelemetryBackendUnavailable(
            f"The telemetry store rejected the request (status {exc.status}).",
            code="telemetry_rejected",
        )
    return TelemetryBackendUnavailable()


async def _call(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except EngineError as exc:
        raise _telemetry_error(exc) from exc


def _client() -> EngineClient:
    try:
        return get_engine_client()
    except EngineError as exc:
        raise _telemetry_error(exc) from exc


# --------------------------------------------------------------------------- #
# Filters and windows
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class RunFilters:
    """The six dropdowns above the table, plus the free-text box and agent link."""

    tenant: str | None = None
    source: Platform | None = None
    status: RunStatus | None = None
    risk: RiskLevel | None = None
    policy: RunPolicy | None = None
    agent_id: str | None = None
    q: str | None = None

    def matches(self, run: RunRead) -> bool:
        if self.tenant and run.tenant != self.tenant:
            return False
        if self.source and run.source is not self.source:
            return False
        if self.status and run.status is not self.status:
            return False
        if self.risk and run.risk is not self.risk:
            return False
        if self.policy and run.policy is not self.policy:
            return False
        if self.agent_id and run.agent_id != self.agent_id:
            return False
        if self.q:
            needle = self.q.strip().lower()
            haystack = " ".join(
                part
                for part in (
                    run.id,
                    run.agent,
                    run.input_preview,
                    run.model,
                    run.source.value if run.source else None,
                    run.status.value,
                    run.tenant,
                    run.user,
                )
                if part
            ).lower()
            if needle not in haystack:
                return False
        return True


def window_for(time_range: TimeRange, *, end: dt.datetime | None = None) -> tuple[
    dt.datetime, dt.datetime
]:
    """Start and end of the selected Time Range."""
    finish = end or _now()
    return finish - dt.timedelta(seconds=time_range.seconds), finish


# --------------------------------------------------------------------------- #
# Telemetry scanning
# --------------------------------------------------------------------------- #


def _provisioned_agents_stmt(principal: Principal) -> Select:
    """Workspace agents that own a telemetry project, most recently used first."""
    return (
        select(Agent)
        .where(
            Agent.workspace_id == principal.workspace_id,
            Agent.engine_project_id.is_not(None),
        )
        .order_by(
            # NULLS LAST, spelled portably for both SQLite and Postgres.
            case((Agent.last_used_at.is_(None), 1), else_=0),
            Agent.last_used_at.desc(),
            Agent.name.asc(),
        )
    )


async def _agents_for(
    session: AsyncSession, principal: Principal, agent_id: str | None
) -> list[Agent]:
    """The projects one request may read, already scoped to the workspace."""
    stmt = _provisioned_agents_stmt(principal)
    if agent_id:
        stmt = stmt.where(Agent.id == agent_id)
    return list((await session.execute(stmt.limit(MAX_SCAN_AGENTS))).scalars().all())


async def agent_count(session: AsyncSession, principal: Principal) -> int:
    """How many provisioned agents exist, so a capped scan can say so."""
    return int(
        (
            await session.execute(
                select(func.count(Agent.id)).where(
                    Agent.workspace_id == principal.workspace_id,
                    Agent.engine_project_id.is_not(None),
                )
            )
        ).scalar_one()
    )


async def _agent_for_project(
    session: AsyncSession, principal: Principal, project_id: str | None
) -> Agent | None:
    """Resolve a trace's project to the workspace agent that owns it, or None."""
    if not project_id:
        return None
    return (
        await session.execute(
            select(Agent).where(
                Agent.workspace_id == principal.workspace_id,
                Agent.engine_project_id == project_id,
            )
        )
    ).scalar_one_or_none()


async def _scan_project(
    client: EngineClient,
    agent: Agent,
    *,
    since: dt.datetime,
    until: dt.datetime,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Cursor through one project's traces in the window, up to ``limit`` rows."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    while len(rows) < limit:
        wanted = min(ENGINE_PAGE_SIZE, limit - len(rows))
        batch = await _call(
            client.search_traces(
                project_id=agent.engine_project_id,
                limit=wanted,
                last_retrieved_id=cursor,
                from_time=since,
                to_time=until,
                truncate=True,
                strip_attachments=True,
            )
        )
        usable = [row for row in batch if isinstance(row, dict) and row.get("id")]
        rows.extend(usable)
        if len(batch) < wanted or not usable:
            break
        cursor = str(usable[-1]["id"])
        if len(rows) >= limit:
            truncated = True
    return rows[:limit], truncated


async def _scan(
    client: EngineClient,
    agents: Sequence[Agent],
    *,
    since: dt.datetime,
    until: dt.datetime,
    budget: int = MAX_SCAN_TRACES,
) -> tuple[list[tuple[Agent, list[dict[str, Any]]]], bool]:
    """Read the window across every in-scope project, with bounded concurrency."""
    if not agents:
        return [], False
    per_agent = max(MIN_ROWS_PER_AGENT, budget // len(agents))
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(agent: Agent) -> tuple[Agent, list[dict[str, Any]], bool]:
        async with semaphore:
            rows, truncated = await _scan_project(
                client, agent, since=since, until=until, limit=per_agent
            )
        return agent, rows, truncated

    results = await asyncio.gather(*(one(agent) for agent in agents))
    truncated = any(flag for _, _, flag in results)
    return [(agent, rows) for agent, rows, _ in results], truncated


# --------------------------------------------------------------------------- #
# Trace mapping
# --------------------------------------------------------------------------- #


def _usage(trace: dict[str, Any]) -> tuple[int, int, int]:
    """Prompt, completion and total tokens, whichever keys the run recorded."""
    usage = trace.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _int(usage.get("total_tokens"))
    if not total:
        total = prompt + completion
    return prompt, completion, total


def _duration_seconds(trace: dict[str, Any]) -> float | None:
    """Engine durations are milliseconds; fall back to the recorded interval."""
    duration = _number(trace.get("duration"))
    if duration is not None:
        return round(duration / 1000.0, 3)
    start = _instant(trace.get("start_time"))
    end = _instant(trace.get("end_time"))
    if start and end:
        return round((end - start).total_seconds(), 3)
    return None


def _scores(trace: dict[str, Any]) -> tuple[dict[str, float], list[RunScore]]:
    raw = trace.get("feedback_scores")
    values: dict[str, float] = {}
    scores: list[RunScore] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = _number(item.get("value"))
            if not isinstance(name, str) or value is None:
                continue
            values[name.strip().lower()] = value
            scores.append(
                RunScore(
                    name=name,
                    value=value,
                    reason=item.get("reason"),
                    source=item.get("source"),
                )
            )
    return values, scores


def _guardrail_rows(payload: Any) -> list[GuardrailVerdict]:
    """Guardrail decisions recorded on a trace or span, in whichever shape."""
    verdicts: list[GuardrailVerdict] = []
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("guardrails_validations", "guardrails", "validations", "checks"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
    elif isinstance(payload, list):
        candidates = list(payload)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("type") or item.get("check")
        if not isinstance(name, str):
            continue
        raw_result = item.get("result") or item.get("status") or item.get("verdict")
        passed: bool | None = None
        if "passed" in item:
            passed = _truthy(item.get("passed"))
        elif isinstance(raw_result, str):
            passed = raw_result.strip().lower() in {"passed", "pass", "ok", "allowed"}
        verdicts.append(
            GuardrailVerdict(
                name=name,
                result=str(raw_result) if raw_result is not None else (
                    "Passed" if passed else "Failed"
                ),
                passed=passed,
                detail=item.get("detail") or item.get("message") or item.get("reason"),
            )
        )
    return verdicts


def _policy_of(trace: dict[str, Any], meta: dict[str, Any]) -> RunPolicy:
    """The enforcement verdict: recorded on the run, or read off its guardrails."""
    raw = meta.get("policy") or meta.get("policy_result") or meta.get("policy_status")
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return RunPolicy(raw.strip().title())
    verdicts = _guardrail_rows(trace) + _guardrail_rows(meta)
    if any(verdict.passed is False for verdict in verdicts):
        return RunPolicy.BLOCKED if _truthy(meta.get("blocked")) else RunPolicy.WARNED
    return RunPolicy.ALLOWED


def _status_of(
    trace: dict[str, Any], meta: dict[str, Any], policy: RunPolicy
) -> RunStatus:
    """Completed, Warned, Failed or Running — derived from what was recorded."""
    raw = meta.get("status")
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return RunStatus(raw.strip().title())
    if trace.get("error_info"):
        return RunStatus.FAILED
    if policy is RunPolicy.BLOCKED:
        return RunStatus.FAILED
    if _instant(trace.get("end_time")) is None:
        return RunStatus.RUNNING
    if policy is RunPolicy.WARNED:
        return RunStatus.WARNED
    return RunStatus.COMPLETED


def _risk_of(meta: dict[str, Any], agent: Agent) -> RiskLevel | None:
    """The run's own risk if it recorded one, else the agent's assessed risk."""
    raw = meta.get("risk") or meta.get("risk_level")
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return RiskLevel(raw.strip().title())
    with contextlib.suppress(ValueError):
        return RiskLevel(agent.risk)
    return None


def _platform_of(meta: dict[str, Any], agent: Agent) -> Platform | None:
    raw = meta.get("platform") or meta.get("source")
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return Platform(raw.strip())
    with contextlib.suppress(ValueError):
        return Platform(agent.platform)
    return None


def _tools_of(trace: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """Tool names the run called.

    The ingest contract records them on the trace metadata so the table can draw
    the tool chips without reading every run's spans. A run ingested without
    them reports an empty list here; the execution-trace and replay endpoints
    read the real span tree instead.
    """
    for source in (meta.get("tools"), meta.get("tool_calls"), trace.get("tools")):
        if isinstance(source, list):
            names: list[str] = []
            for item in source:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("tool")
                    if isinstance(name, str) and name.strip():
                        names.append(name.strip())
            if names:
                return names
    return []


def _error_flags(meta: dict[str, Any]) -> tuple[int, bool, bool]:
    """Retry count, fallback and escalation, as the ingest contract records them.

    They live under ``metadata.errors`` when written by our SDK and at the top
    level when written through OTLP, so both are read.
    """
    errors = meta.get("errors") if isinstance(meta.get("errors"), dict) else {}
    retries = _int(errors.get("retries") or meta.get("retry_count"))
    fallback = _truthy(errors.get("fallback") or meta.get("fallback_used"))
    escalated = _truthy(errors.get("escalated") or meta.get("escalated"))
    return retries, fallback, escalated


def _map_run(trace: dict[str, Any], agent: Agent, principal: Principal) -> RunRead:
    """One engine trace as one row of the Live Runs table."""
    meta = as_metadata(trace.get("metadata"))
    scores, _ = _scores(trace)
    policy = _policy_of(trace, meta)
    prompt_tokens, completion_tokens, total_tokens = _usage(trace)
    started = _instant(trace.get("start_time")) or _now()

    confidence = scores.get("confidence")
    if confidence is None:
        confidence = _number(meta.get("confidence"))

    tenant = meta.get("tenant")
    user = meta.get("user") or meta.get("user_email") or meta.get("user_id")

    return RunRead(
        id=str(trace.get("id")),
        tenant=str(tenant) if tenant else principal.workspace_slug,
        source=_platform_of(meta, agent),
        agent=agent.name,
        agent_id=agent.id,
        status=_status_of(trace, meta, policy),
        model=(
            str(meta.get("model")) if meta.get("model") else (agent.model or None)
        ),
        input_preview=_preview(_payload_text(trace.get("input")), PREVIEW_CHARS),
        tools=_tools_of(trace, meta),
        tokens=total_tokens,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cost=round(_number(trace.get("total_estimated_cost")) or 0.0, 6),
        duration_seconds=_duration_seconds(trace),
        confidence=confidence,
        risk=_risk_of(meta, agent),
        policy=policy,
        occurred_at=started,
        ended_at=_instant(trace.get("end_time")),
        session_id=(
            str(trace.get("thread_id"))
            if trace.get("thread_id")
            else (str(meta["session_id"]) if meta.get("session_id") else None)
        ),
        user=str(user) if user else None,
    )


@dataclasses.dataclass(frozen=True)
class _Scanned:
    """A mapped run plus the recorded flags the KPI row folds over.

    The flags are not table columns, so they do not belong on
    :class:`RunRead`; carrying them alongside means the summary counts real
    fallbacks and real escalations without a second read per run.
    """

    run: RunRead
    retry_count: int
    fallback_used: bool
    escalated: bool


def _scan_run(trace: dict[str, Any], agent: Agent, principal: Principal) -> _Scanned:
    retries, fallback, escalated = _error_flags(as_metadata(trace.get("metadata")))
    return _Scanned(
        run=_map_run(trace, agent, principal),
        retry_count=retries,
        fallback_used=fallback,
        escalated=escalated,
    )


def _map_detail(trace: dict[str, Any], agent: Agent, principal: Principal) -> RunDetail:
    """The inspector payload: the row, plus everything its seven sections show."""
    row = _map_run(trace, agent, principal)
    meta = as_metadata(trace.get("metadata"))
    score_values, scores = _scores(trace)
    verdicts = _guardrail_rows(trace) + _guardrail_rows(meta)

    retrieval = meta.get("retrieval") if isinstance(meta.get("retrieval"), dict) else {}
    retries, fallback_used, escalated = _error_flags(meta)
    error_info = trace.get("error_info")
    error_message = None
    if isinstance(error_info, dict):
        error_message = error_info.get("message") or error_info.get("exception_type")
    elif isinstance(error_info, str):
        error_message = error_info

    injection = next(
        (v.result for v in verdicts if "inject" in v.name.lower()),
        meta.get("prompt_injection_check"),
    )
    pii = next(
        (v.result for v in verdicts if "pii" in v.name.lower()),
        meta.get("pii_detection"),
    )

    tags = trace.get("tags")
    return RunDetail(
        **row.model_dump(),
        input=_payload_text(trace.get("input")),
        response=_payload_text(trace.get("output")),
        environment=str(meta["environment"]) if meta.get("environment") else agent.environment,
        guardrails=RunGuardrails(
            prompt_injection_check=str(injection) if injection else None,
            pii_detection=str(pii) if pii else None,
            final_policy=row.policy,
            verdicts=verdicts,
        ),
        retrieval=RunRetrieval(
            documents=_int(retrieval.get("docs") or retrieval.get("documents")),
            grounding_score=(
                _number(retrieval.get("grounding")) or score_values.get("grounding")
            ),
            citation_accuracy=_number(retrieval.get("citation"))
            or _number(retrieval.get("citation_accuracy")),
            source_freshness=_number(retrieval.get("freshness")),
        ),
        errors=RunErrors(
            retry_count=retries,
            fallback_used=fallback_used,
            escalated=escalated,
            message=str(error_message) if error_message else None,
        ),
        feedback_scores=scores,
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        span_count=_int(trace.get("span_count")),
        llm_span_count=_int(trace.get("llm_span_count")),
        flagged_for_review=FLAG_SCORE_NAME in score_values,
        trace_available=True,
        replay_supported=_int(trace.get("span_count")) > 0,
    )


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #

_SORT_KEYS: Final[dict[str, Callable[[RunRead], Any]]] = {
    "id": lambda r: r.id,
    "tenant": lambda r: (r.tenant or "").lower(),
    "source": lambda r: r.source.value if r.source else "",
    "platform": lambda r: r.source.value if r.source else "",
    "agent": lambda r: (r.agent or "").lower(),
    "status": lambda r: r.status.value,
    "model": lambda r: (r.model or "").lower(),
    "input": lambda r: r.input_preview.lower(),
    "input_preview": lambda r: r.input_preview.lower(),
    "tokens": lambda r: r.tokens,
    "cost": lambda r: r.cost,
    "duration": lambda r: r.duration_seconds if r.duration_seconds is not None else -1.0,
    "duration_seconds": lambda r: (
        r.duration_seconds if r.duration_seconds is not None else -1.0
    ),
    "confidence": lambda r: r.confidence if r.confidence is not None else -1.0,
    "risk": lambda r: _RISK_ORDER.get(r.risk.value, -1) if r.risk else -1,
    "policy": lambda r: r.policy.value,
    "time": lambda r: r.occurred_at,
    "ts": lambda r: r.occurred_at,
    "occurred_at": lambda r: r.occurred_at,
    "user": lambda r: (r.user or "").lower(),
}


def _sorted(runs: list[RunRead], params: ListParams) -> list[RunRead]:
    """Order the mapped rows by the table's column key."""
    if not params.sort_key:
        return sorted(runs, key=lambda r: r.occurred_at, reverse=True)
    key = _SORT_KEYS.get(params.sort_key)
    if key is None:
        raise ValidationFailed(
            f"Cannot sort by '{params.sort_key}'.",
            details={"sortable": sorted(_SORT_KEYS)},
        )
    return sorted(runs, key=key, reverse=params.descending)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _Scope:
    """Which projects a request may read, resolved once from the database.

    Kept separate from the scan because an :class:`AsyncSession` serves one
    operation at a time: the summary reads two windows concurrently, and both
    must share a scope resolved *before* the concurrency starts rather than
    query the same session from two tasks.
    """

    agents: list[Agent]
    total_agents: int


async def _scope(
    session: AsyncSession, principal: Principal, filters: RunFilters
) -> _Scope:
    return _Scope(
        agents=await _agents_for(session, principal, filters.agent_id),
        total_agents=await agent_count(session, principal),
    )


async def _gather(
    scope: _Scope,
    principal: Principal,
    *,
    filters: RunFilters,
    since: dt.datetime,
    until: dt.datetime,
    budget: int = MAX_SCAN_TRACES,
) -> tuple[list[_Scanned], ScanInfo]:
    """Scan the window, map every trace, keep the ones the filters select.

    Touches the telemetry engine only — no database work happens here, which is
    what makes it safe to run two of these at once.
    """
    scanned: list[_Scanned] = []
    truncated = False
    if scope.agents:
        batches, truncated = await _scan(
            _client(), scope.agents, since=since, until=until, budget=budget
        )
        for agent, traces in batches:
            for trace in traces:
                scanned.append(_scan_run(trace, agent, principal))

    info = ScanInfo(
        runs_scanned=len(scanned),
        agents_scanned=len(scope.agents),
        agents_total=scope.total_agents,
        truncated=truncated or scope.total_agents > len(scope.agents),
        window_start=since,
        window_end=until,
    )
    return [item for item in scanned if filters.matches(item.run)], info


async def _collect(
    session: AsyncSession,
    principal: Principal,
    *,
    filters: RunFilters,
    time_range: TimeRange,
    budget: int = MAX_SCAN_TRACES,
    end: dt.datetime | None = None,
) -> tuple[list[_Scanned], ScanInfo]:
    """Resolve the scope and scan one window."""
    since, until = window_for(time_range, end=end)
    scope = await _scope(session, principal, filters)
    return await _gather(
        scope, principal, filters=filters, since=since, until=until, budget=budget
    )


async def list_runs(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    filters: RunFilters,
    time_range: TimeRange = TimeRange.LAST_24_HOURS,
) -> tuple[list[RunRead], int, ScanInfo]:
    """One page of the run table: the filtered set, sorted, sliced."""
    scanned, info = await _collect(
        session, principal, filters=filters, time_range=time_range
    )
    ordered = _sorted([item.run for item in scanned], params)
    start = params.offset
    return ordered[start : start + params.page_size], len(ordered), info


async def run_history(
    session: AsyncSession,
    principal: Principal,
    agent_id: str,
    *,
    cursor: str | None = None,
    limit: int = 50,
) -> RunHistoryPage:
    """One agent's complete run history, newest first, one cursor page at a time.

    This is Replay Studio's browser: unlike the Live Runs window it has no
    time floor — every run the store still holds is reachable by paging. One
    project, one engine call per page; the cursor is the engine's own
    ``last_retrieved_id``, so a page is O(page) however deep the history goes.
    """
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.workspace_id == principal.workspace_id, Agent.id == agent_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound(f"Agent '{agent_id}' does not exist.")
    if not agent.engine_project_id:
        # Never provisioned means never a single run; an empty page says so
        # without pretending the store was asked.
        return RunHistoryPage(agent_id=agent.id, agent_name=agent.name, items=[])

    wanted = max(1, min(limit, ENGINE_PAGE_SIZE))
    batch = await _call(
        _client().search_traces(
            project_id=agent.engine_project_id,
            limit=wanted,
            last_retrieved_id=cursor,
            truncate=True,
            strip_attachments=True,
        )
    )
    rows = [row for row in batch if isinstance(row, dict) and row.get("id")]
    items = [_map_run(row, agent, principal) for row in rows]
    # A short page is the engine's end-of-stream signal — judged on the raw
    # batch, so a dropped unaddressable row can never silently end the history.
    next_cursor = str(rows[-1]["id"]) if len(batch) >= wanted and rows else None
    return RunHistoryPage(
        agent_id=agent.id, agent_name=agent.name, items=items, next_cursor=next_cursor
    )


async def export_runs(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    filters: RunFilters,
    time_range: TimeRange = TimeRange.LAST_24_HOURS,
) -> list[RunRead]:
    """Every run the current filters select, capped for one click."""
    scanned, _ = await _collect(
        session, principal, filters=filters, time_range=time_range
    )
    return _sorted([item.run for item in scanned], params)[:MAX_EXPORT_ROWS]


async def get_run(
    session: AsyncSession, principal: Principal, run_id: str
) -> tuple[RunDetail, Agent, dict[str, Any]]:
    """One run, its owning agent and the raw trace.

    The trace's project must belong to an agent in the caller's workspace; if it
    does not, the run answers 404 rather than 403, so an id from another tenant
    is indistinguishable from one that never existed.
    """
    trace = await _call(_client().get_trace(run_id, strip_attachments=True))
    if not isinstance(trace, dict) or not trace.get("id"):
        raise NotFound(f"Run '{run_id}' does not exist.")
    agent = await _agent_for_project(session, principal, trace.get("project_id"))
    if agent is None:
        raise NotFound(f"Run '{run_id}' does not exist.")
    return _map_detail(trace, agent, principal), agent, trace


async def get_run_inspector(
    session: AsyncSession, principal: Principal, run_id: str
) -> RunDetail:
    """The inspector payload, with the guardrail section always answered.

    Guardrail results may be recorded against the run or against the individual
    spans that ran the checks. The trace is read first because that is the cheap
    path; only when it carries no verdicts and the run actually has spans do we
    read them, so the Guardrails & Policy section reflects what happened rather
    than going blank on a perfectly well-instrumented run.
    """
    detail, agent, _ = await get_run(session, principal, run_id)
    if detail.guardrails.verdicts or not detail.span_count:
        return detail

    spans = await _fetch_spans(_client(), agent, run_id)
    verdicts = [
        verdict
        for span in spans
        for verdict in (
            _guardrail_rows(span) + _guardrail_rows(as_metadata(span.get("metadata")))
        )
    ]
    if not verdicts:
        return detail

    detail.guardrails.verdicts = verdicts
    detail.guardrails.prompt_injection_check = detail.guardrails.prompt_injection_check or next(
        (verdict.result for verdict in verdicts if "inject" in verdict.name.lower()), None
    )
    detail.guardrails.pii_detection = detail.guardrails.pii_detection or next(
        (verdict.result for verdict in verdicts if "pii" in verdict.name.lower()), None
    )
    return detail


async def get_response(
    session: AsyncSession, principal: Principal, run_id: str
) -> RunResponse:
    """The Full Response modal: the completion in full, not the preview."""
    detail, _, _ = await get_run(session, principal, run_id)
    return RunResponse(
        run_id=detail.id,
        agent=detail.agent,
        model=detail.model,
        occurred_at=detail.occurred_at,
        response=detail.response,
        output_tokens=detail.output_tokens,
        character_count=len(detail.response),
    )


# --------------------------------------------------------------------------- #
# Execution trace and replay
# --------------------------------------------------------------------------- #


async def _fetch_spans(
    client: EngineClient, agent: Agent, run_id: str
) -> list[dict[str, Any]]:
    """Every span of one trace, cursored, capped at :data:`MAX_SPANS`."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(rows) < MAX_SPANS:
        wanted = min(ENGINE_PAGE_SIZE, MAX_SPANS - len(rows))
        batch = await _call(
            client.search_spans(
                trace_id=run_id,
                project_id=agent.engine_project_id,
                limit=wanted,
                last_retrieved_id=cursor,
                truncate=False,
            )
        )
        usable = [row for row in batch if isinstance(row, dict) and row.get("id")]
        rows.extend(usable)
        if len(batch) < wanted or not usable:
            break
        cursor = str(usable[-1]["id"])
    return rows


def _span_duration_ms(span: dict[str, Any]) -> float | None:
    duration = _number(span.get("duration"))
    if duration is not None:
        return round(duration, 3)
    start = _instant(span.get("start_time"))
    end = _instant(span.get("end_time"))
    if start and end:
        return round((end - start).total_seconds() * 1000.0, 3)
    return None


def _map_span(span: dict[str, Any], trace_start: dt.datetime | None) -> RunSpan:
    meta = as_metadata(span.get("metadata"))
    _, _, tokens = _usage(span)
    started = _instant(span.get("start_time"))
    error_info = span.get("error_info")
    error = None
    if isinstance(error_info, dict):
        error = error_info.get("message") or error_info.get("exception_type")
    elif isinstance(error_info, str):
        error = error_info

    verdicts = _guardrail_rows(span) + _guardrail_rows(meta)
    failed = bool(error) or any(v.passed is False for v in verdicts)
    return RunSpan(
        id=str(span.get("id")),
        parent_span_id=(
            str(span["parent_span_id"]) if span.get("parent_span_id") else None
        ),
        name=str(span.get("name") or "span"),
        span_type=str(span["type"]) if span.get("type") else None,
        status="fail" if failed else "done",
        started_at=started,
        ended_at=_instant(span.get("end_time")),
        duration_ms=_span_duration_ms(span),
        offset_ms=(
            round((started - trace_start).total_seconds() * 1000.0, 3)
            if started and trace_start
            else None
        ),
        model=str(meta["model"]) if meta.get("model") else (span.get("model") or None),
        tokens=tokens,
        cost=round(_number(span.get("total_estimated_cost")) or 0.0, 6),
        input_preview=_preview(_payload_text(span.get("input")), SPAN_PREVIEW_CHARS) or None,
        output_preview=_preview(_payload_text(span.get("output")), SPAN_PREVIEW_CHARS)
        or None,
        error=str(error) if error else None,
        guardrails=verdicts,
    )


def _build_tree(spans: list[RunSpan]) -> list[RunSpan]:
    """Nest the flat span list. Orphans are promoted to roots, never dropped."""
    by_id = {span.id: span for span in spans}
    roots: list[RunSpan] = []
    for span in spans:
        parent = by_id.get(span.parent_span_id) if span.parent_span_id else None
        if parent is None or parent is span:
            roots.append(span)
        else:
            parent.children.append(span)

    def order(nodes: list[RunSpan]) -> None:
        nodes.sort(key=lambda s: (s.offset_ms if s.offset_ms is not None else 0.0, s.name))
        for node in nodes:
            order(node.children)

    order(roots)
    return roots


async def get_trace_tree(
    session: AsyncSession, principal: Principal, run_id: str
) -> RunTrace:
    """The Execution Trace modal: header facts plus the real span hierarchy."""
    detail, agent, trace = await get_run(session, principal, run_id)
    trace_start = _instant(trace.get("start_time"))
    rows = await _fetch_spans(_client(), agent, run_id)
    spans = [_map_span(row, trace_start) for row in rows]
    return RunTrace(
        run_id=detail.id,
        agent=detail.agent,
        agent_id=detail.agent_id,
        model=detail.model,
        status=detail.status,
        started_at=detail.occurred_at,
        duration_seconds=detail.duration_seconds,
        span_count=len(spans),
        total_tokens=sum(span.tokens for span in spans) or detail.tokens,
        total_cost=round(sum(span.cost for span in spans) or detail.cost, 6),
        spans=_build_tree(spans),
    )


def _flatten(nodes: Iterable[RunSpan]) -> list[RunSpan]:
    """Depth-first, start-ordered — the order the player steps through."""
    ordered: list[RunSpan] = []
    for node in nodes:
        ordered.append(node)
        ordered.extend(_flatten(node.children))
    return ordered


def _step_kind(span: RunSpan) -> ReplayStepKind:
    """Classify a span by its recorded type, then by what it is called."""
    span_type = (span.span_type or "").strip().lower()
    if span_type == "llm":
        return ReplayStepKind.MODEL
    if span_type == "tool":
        return ReplayStepKind.TOOL
    if span_type == "guardrail":
        return ReplayStepKind.GUARDRAIL
    name = span.name.lower()
    if any(token in name for token in ("retriev", "search", "rag", "vector", "index")):
        return ReplayStepKind.RETRIEVAL
    if any(token in name for token in ("guardrail", "policy", "moderation", "safety")):
        return ReplayStepKind.GUARDRAIL
    if any(token in name for token in ("prompt", "request", "input")):
        return ReplayStepKind.PROMPT
    if any(token in name for token in ("response", "answer", "output", "deliver")):
        return ReplayStepKind.RESPONSE
    return ReplayStepKind.STEP


def _chunks_from(payload: Any) -> list[RetrievedChunk]:
    """Documents a retrieval step returned, in whatever shape it recorded them."""
    items: list[Any] = []
    if isinstance(payload, dict):
        for key in ("documents", "chunks", "results", "matches", "output", "context"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
    elif isinstance(payload, list):
        items = payload

    chunks: list[RetrievedChunk] = []
    for item in items:
        if isinstance(item, str):
            chunks.append(RetrievedChunk(text=_preview(item, SPAN_PREVIEW_CHARS)))
        elif isinstance(item, dict):
            chunks.append(
                RetrievedChunk(
                    id=str(item["id"]) if item.get("id") else None,
                    source=(
                        str(item.get("source") or item.get("title") or item.get("url"))
                        if (item.get("source") or item.get("title") or item.get("url"))
                        else None
                    ),
                    score=_number(item.get("score") or item.get("relevance")),
                    text=_preview(_payload_text(item), SPAN_PREVIEW_CHARS),
                )
            )
    return chunks


def _replay_step(index: int, span: RunSpan, raw: dict[str, Any]) -> ReplayStep:
    kind = _step_kind(span)
    prompt = span.input_preview
    output = span.output_preview
    prompt_tokens, completion_tokens, _ = _usage(raw)

    tool_call = None
    if kind is ReplayStepKind.TOOL:
        tool_call = ToolCall(
            name=span.name,
            arguments=prompt,
            result=output,
            ok=span.status != "fail",
            duration_ms=span.duration_ms,
        )

    chunks = _chunks_from(raw.get("output")) if kind is ReplayStepKind.RETRIEVAL else []
    detail_bits = [bit for bit in (span.model, span.span_type) if bit]
    if chunks:
        detail_bits.append(f"{len(chunks)} document(s) retrieved")
    if span.tokens:
        detail_bits.append(f"{span.tokens} tokens")

    return ReplayStep(
        index=index,
        kind=kind,
        title=span.name,
        detail=" · ".join(detail_bits),
        span_id=span.id,
        started_at=span.started_at,
        offset_ms=span.offset_ms,
        duration_ms=span.duration_ms,
        prompt=prompt,
        response=output,
        model=span.model,
        tokens=span.tokens,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cost=span.cost,
        retrieved_chunks=chunks,
        tool_call=tool_call,
        guardrails=span.guardrails,
        status=span.status,
        error=span.error,
        has_full_payload=bool(prompt) and bool(output),
    )


async def get_replay(
    session: AsyncSession, principal: Principal, run_id: str
) -> ReplaySession:
    """The ordered, scrubbable step list Replay Studio plays.

    Steps are the run's span tree flattened depth-first in start order, each
    carrying its own prompt, retrieved chunks, tool call and result, guardrail
    verdicts, tokens, cost and offset from the run's start. Fidelity is the
    share of steps whose input *and* output were both captured — a run ingested
    without payloads replays as a timeline, and says so instead of pretending.
    """
    detail, agent, trace = await get_run(session, principal, run_id)
    trace_start = _instant(trace.get("start_time"))
    rows = await _fetch_spans(_client(), agent, run_id)
    by_id = {str(row["id"]): row for row in rows}
    spans = [_map_span(row, trace_start) for row in rows]
    ordered = _flatten(_build_tree(spans))

    steps = [
        _replay_step(index, span, by_id.get(span.id, {}))
        for index, span in enumerate(ordered, start=1)
    ]
    captured = sum(1 for step in steps if step.has_full_payload)

    transcript: list[TranscriptMessage] = []
    if detail.input:
        transcript.append(
            TranscriptMessage(
                role="user", author=detail.user, at=detail.occurred_at, text=detail.input
            )
        )
    for step in steps:
        if step.tool_call is not None:
            transcript.append(
                TranscriptMessage(
                    role="tool",
                    author=step.tool_call.name,
                    at=step.started_at,
                    text=step.tool_call.result or step.tool_call.arguments or "",
                )
            )
    if detail.response:
        transcript.append(
            TranscriptMessage(
                role="agent", author=detail.agent, at=detail.ended_at, text=detail.response
            )
        )

    total_ms = (detail.duration_seconds or 0.0) * 1000.0
    if not total_ms and steps:
        total_ms = max(
            (step.offset_ms or 0.0) + (step.duration_ms or 0.0) for step in steps
        )

    return ReplaySession(
        run=RunRead(**detail.model_dump(include=set(RunRead.model_fields))),
        steps=steps,
        transcript=transcript,
        total_duration_ms=round(total_ms, 3),
        step_count=len(steps),
        captured_steps=captured,
        fidelity=round(captured / len(steps) * 100, 1) if steps else 0.0,
        replayable=bool(steps),
    )


# --------------------------------------------------------------------------- #
# KPI summary
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class _Aggregate:
    """Counters folded over one window of runs."""

    runs: int = 0
    failed: int = 0
    duration_total: float = 0.0
    duration_count: int = 0
    tokens: int = 0
    cost: float = 0.0
    violations: int = 0
    fallbacks: int = 0
    escalations: int = 0

    @property
    def success_rate(self) -> float | None:
        if not self.runs:
            return None
        return round((self.runs - self.failed) / self.runs * 100, 1)

    @property
    def avg_latency(self) -> float | None:
        if not self.duration_count:
            return None
        return round(self.duration_total / self.duration_count, 3)


def _fold(scanned: Iterable[_Scanned]) -> _Aggregate:
    """Fold one window of scanned runs into the numbers the KPI cards show."""
    aggregate = _Aggregate()
    for item in scanned:
        run = item.run
        aggregate.runs += 1
        if run.status is RunStatus.FAILED:
            aggregate.failed += 1
        if run.duration_seconds is not None:
            aggregate.duration_total += run.duration_seconds
            aggregate.duration_count += 1
        aggregate.tokens += run.tokens
        aggregate.cost += run.cost
        if run.policy is not RunPolicy.ALLOWED:
            aggregate.violations += 1
        aggregate.fallbacks += int(item.fallback_used)
        aggregate.escalations += int(item.escalated)
    return aggregate


def _buckets(
    scanned: Sequence[_Scanned], since: dt.datetime, until: dt.datetime
) -> list[tuple[dt.datetime, list[_Scanned]]]:
    """Split the window into :data:`SERIES_BUCKETS` equal, ordered buckets."""
    span = max((until - since).total_seconds(), 1.0)
    width = span / SERIES_BUCKETS
    buckets: list[tuple[dt.datetime, list[_Scanned]]] = [
        (since + dt.timedelta(seconds=width * index), []) for index in range(SERIES_BUCKETS)
    ]
    for item in scanned:
        offset = (_as_utc(item.run.occurred_at) - since).total_seconds()
        index = min(SERIES_BUCKETS - 1, max(0, int(offset // width)))
        buckets[index][1].append(item)
    return buckets


def _percent_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


async def summarise(
    session: AsyncSession,
    principal: Principal,
    *,
    filters: RunFilters,
    time_range: TimeRange = TimeRange.LAST_24_HOURS,
) -> RunsSummary:
    """The four KPI cards and the four sparkline mini-KPIs.

    The selected window and the window immediately before it are scanned
    concurrently, so every "vs last 24h" delta on the screen is a measured
    change rather than a guess. Fallback and escalation counts come from the
    same scan: they are recorded on the run's metadata by the ingest contract.
    """
    since, until = window_for(time_range)
    previous_since, previous_until = window_for(time_range, end=since)

    # The scope is resolved once, before the concurrency starts: the two scans
    # below share this request's session and it serves one operation at a time.
    scope = await _scope(session, principal, filters)
    (current, info), (previous, _) = await asyncio.gather(
        _gather(scope, principal, filters=filters, since=since, until=until),
        _gather(
            scope,
            principal,
            filters=filters,
            since=previous_since,
            until=previous_until,
            budget=MAX_SCAN_TRACES // 2,
        ),
    )

    now_agg = _fold(current)
    was_agg = _fold(previous)

    buckets = _buckets(current, since, until)
    token_series = [
        SparkPoint(at=at, value=float(sum(item.run.tokens for item in rows)))
        for at, rows in buckets
    ]
    cost_series = [
        SparkPoint(at=at, value=round(sum(item.run.cost for item in rows), 6))
        for at, rows in buckets
    ]
    fallback_series = [
        SparkPoint(
            at=at,
            value=(
                round(sum(1 for item in rows if item.fallback_used) / len(rows) * 100, 2)
                if rows
                else 0.0
            ),
        )
        for at, rows in buckets
    ]
    escalation_series = [
        SparkPoint(at=at, value=float(sum(1 for item in rows if item.escalated)))
        for at, rows in buckets
    ]

    tenants = sorted({item.run.tenant for item in current if item.run.tenant})

    return RunsSummary(
        time_range=time_range,
        total_runs=now_agg.runs,
        total_runs_previous=was_agg.runs,
        total_runs_delta_percent=_percent_delta(now_agg.runs, was_agg.runs),
        success_rate=now_agg.success_rate,
        success_rate_previous=was_agg.success_rate,
        success_rate_delta_points=(
            round(now_agg.success_rate - was_agg.success_rate, 1)
            if now_agg.success_rate is not None and was_agg.success_rate is not None
            else None
        ),
        avg_latency_seconds=now_agg.avg_latency,
        avg_latency_previous_seconds=was_agg.avg_latency,
        avg_latency_delta_seconds=(
            round(now_agg.avg_latency - was_agg.avg_latency, 3)
            if now_agg.avg_latency is not None and was_agg.avg_latency is not None
            else None
        ),
        policy_violations=now_agg.violations,
        policy_violations_previous=was_agg.violations,
        policy_violations_delta_percent=_percent_delta(
            now_agg.violations, was_agg.violations
        ),
        tokens_used=SparkKpi(
            label="Tokens Used",
            value=float(now_agg.tokens),
            unit="tokens",
            series=token_series,
        ),
        estimated_cost=SparkKpi(
            label="Estimated Cost",
            value=round(now_agg.cost, 4),
            unit="usd",
            series=cost_series,
        ),
        fallback_rate=SparkKpi(
            label="Fallback Rate",
            value=(
                round(now_agg.fallbacks / now_agg.runs * 100, 2) if now_agg.runs else 0.0
            ),
            unit="percent",
            series=fallback_series,
        ),
        human_escalations=SparkKpi(
            label="Human Escalations",
            value=float(now_agg.escalations),
            unit="runs",
            series=escalation_series,
        ),
        tenants=tenants,
        scan=info,
    )


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def flag_run(
    session: AsyncSession,
    principal: Principal,
    run_id: str,
    payload: RunFlagRequest,
    *,
    request: Request | None = None,
) -> RunFlagResult:
    """Route a run to the review queue.

    The flag is written where reviewers look for it — as a feedback score on the
    run itself — and, when a reason was given, as a reviewer comment on the same
    run. The audit row is what governance reads later. Requires at least the
    member role: a viewer may read runs but not annotate them.
    """
    principal.require(Role.MEMBER)
    detail, agent, _ = await get_run(session, principal, run_id)

    client = _client()
    await _call(
        client.score_traces_batch(
            [
                {
                    "id": run_id,
                    "project_name": agent.engine_project_name,
                    "name": FLAG_SCORE_NAME,
                    "value": 1.0,
                    "source": "ui",
                    "reason": payload.reason,
                }
            ]
        )
    )

    comment_added = False
    if payload.comment and payload.reason:
        await _call(client.add_trace_comment(run_id, payload.reason))
        comment_added = True

    flagged_at = _now()
    await audit.record(
        session,
        principal=principal,
        action="run.flagged",
        entity_type=ENTITY_TYPE,
        entity_id=run_id,
        entity_label=f"{agent.name} run {run_id[:12]}",
        source_screen=SOURCE_SCREEN,
        detail=payload.reason or "Flagged for human review",
        metadata={"agent_id": agent.id, "status": detail.status.value},
        request=request,
    )
    await session.flush()

    return RunFlagResult(
        run_id=run_id,
        agent=agent.name,
        flagged_at=flagged_at,
        flagged_by=principal.actor,
        reason=payload.reason,
        comment_added=comment_added,
    )


# --------------------------------------------------------------------------- #
# Live stream
# --------------------------------------------------------------------------- #

_active_streams = 0
_stream_lock = asyncio.Lock()


class StreamSlot:
    """One reserved live-stream connection.

    Reserved before the response starts so an over-subscribed process can answer
    429 cleanly instead of opening a stream it cannot serve, and released in the
    generator's ``finally`` so a disconnect frees it immediately.
    """

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self._released = False

    async def release(self) -> None:
        global _active_streams
        if self._released:
            return
        self._released = True
        async with _stream_lock:
            _active_streams = max(0, _active_streams - 1)


async def acquire_stream_slot() -> StreamSlot:
    """Reserve a stream, or refuse with 429 when the process is at its bound."""
    global _active_streams
    async with _stream_lock:
        if _active_streams >= MAX_CONCURRENT_STREAMS:
            raise RateLimited(
                retry_after_seconds=15,
                message=(
                    "Too many live run streams are open. Close one or retry shortly."
                ),
            )
        _active_streams += 1
    return StreamSlot(new_id())


def active_stream_count() -> int:
    """Streams currently open in this process, for the stream's own handshake."""
    return _active_streams


async def stream_runs(
    principal: Principal,
    slot: StreamSlot,
    *,
    filters: RunFilters,
    poll_seconds: float = STREAM_POLL_SECONDS,
    max_seconds: float = STREAM_MAX_SECONDS,
) -> AsyncIterator[RunRead]:
    """Yield runs as they land, newest last, honouring the table's filters.

    Each poll re-reads the workspace's projects over a short window that
    overlaps the previous one, so a trace whose start time predates its arrival
    is still delivered. Ids already sent are remembered in a bounded ring, which
    is what stops the overlap producing duplicates.

    A short-lived database session is opened per poll: a subscription can outlive
    a request by half an hour and must not pin a pooled connection.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    seen: deque[str] = deque(maxlen=STREAM_SEEN_IDS)
    seen_ids: set[str] = set()
    since = _now()

    try:
        while True:
            async with get_sessionmaker()() as session:
                agents = await _agents_for(session, principal, filters.agent_id)
            if not agents:
                # A workspace with nothing registered yet still holds the
                # connection open. Returning here would end the response, and
                # the console's EventSource would reconnect immediately and
                # forever — a request storm, and a LIVE pill stuck on
                # "reconnecting" when the truth is simply "nothing to report".
                # Idling instead means an agent registered mid-stream starts
                # delivering on the next poll with no client involvement.
                if loop.time() >= deadline:
                    return
                await asyncio.sleep(poll_seconds)
                since = _now()
                continue

            until = _now()
            batches, _ = await _scan(
                _client(),
                agents,
                since=since - dt.timedelta(seconds=STREAM_OVERLAP_SECONDS),
                until=until,
                budget=STREAM_SCAN_BUDGET,
            )

            fresh: list[RunRead] = []
            for agent, traces in batches:
                for trace in traces:
                    run = _map_run(trace, agent, principal)
                    if run.id in seen_ids or not filters.matches(run):
                        continue
                    fresh.append(run)

            fresh.sort(key=lambda run: run.occurred_at)
            for run in fresh:
                if len(seen) == seen.maxlen:
                    seen_ids.discard(seen[0])
                seen.append(run.id)
                seen_ids.add(run.id)
                yield run

            since = until
            if loop.time() >= deadline:
                return
            await asyncio.sleep(poll_seconds)
    finally:
        await slot.release()
