"""RAG & Knowledge Governance business logic.

The source registry is ours; the evidence about how well a source grounds is the
telemetry engine's. A sync therefore does not crawl a corpus — this service owns
no crawler and will not pretend to — it re-derives what the control plane can
actually know about an index from the retrieval telemetry the agents produced:
which documents were retrieved, how many distinct chunks they came from, and
what the feedback scores on those retrieval spans say about grounding quality.

The job is real work with real intermediate state. It walks five stages, writes
its progress to ``sync_progress`` after each one and commits, which is what makes
``GET /knowledge/{id}/sync-status`` a genuine progress report rather than an
animation: the console polls a number this service persisted.

Two invariants hold in every function below: every statement filters on
``principal.workspace_id``, and the span scan only ever addresses engine
projects belonging to this workspace's agents, so no other tenant's telemetry
can reach a response. Every state change writes an audit row naming RAG &
Knowledge Governance as its source screen.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import functools
import logging
import re
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Final, TypeVar

from fastapi import Request
from sqlalchemy import Select, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings as app_settings
from ..core.errors import (
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..core.ttlcache import SingleFlightCache
from ..db.session import get_sessionmaker
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineNotFound,
    EngineUnavailable,
    deadline,
    get_engine_client,
)
from ..models.identity import Role, User
from ..models.registry import (
    Agent,
    KnowledgeSource,
    KnowledgeSourceStatus,
    Sensitivity,
)
from ..schemas.evaluations import INVERTED_METRIC_SOURCES
from ..schemas.knowledge import (
    DEFAULT_WINDOW_DAYS,
    GROUNDING_DIMENSIONS,
    SOURCE_SETTINGS_KEY,
    SYNC_STAGES,
    SYNC_STATE_KEY,
    GroundingBreakdown,
    GroundingDimension,
    IndexingStatus,
    KnowledgeActionResponse,
    KnowledgeDocumentRead,
    KnowledgeSourceCreate,
    KnowledgeSourceRead,
    KnowledgeSourceUpdate,
    KnowledgeSummary,
    KnowledgeSyncStatus,
    RetrievalPolicy,
    SourceSettings,
    SyncStage,
    SyncState,
)
from . import audit

log = logging.getLogger(__name__)

T = TypeVar("T")

SOURCE_SCREEN: Final[str] = "RAG & Knowledge Governance"
ENTITY_TYPE: Final[str] = "knowledge_source"

#: These bound what a scan will pull back so a busy workspace cannot turn one
#: console click into an unbounded read.
MAX_SCAN_PROJECTS: Final[int] = 10
#: Rows asked for per call, and the most one project contributes to a scan. The
#: store streams a project's spans newest first and knows nothing about
#: retrieval, so a single page of a busy agent was mostly model calls and the
#: "30-day window" the screen prints was really its last few dozen runs. The
#: scan follows the cursor back through the window until it reaches this cap.
SPAN_PAGE_SIZE: Final[int] = 500
MAX_SPANS_PER_PROJECT: Final[int] = 2000
#: Projects read side by side. Every page is an untruncated stream the adapter
#: parses in this process, and ten at once starved the requests beside it.
SCAN_CONCURRENCY: Final[int] = 5

#: Wall-clock ceiling on one sync, enforced by the job's own supervisor. A
#: healthy sync is a handful of store reads and takes seconds.
EXECUTION_DEADLINE_SECONDS: Final[float] = 120.0
#: A row still Syncing this long after its job's last committed progress, and
#: past the ceiling above, has no job: the task lives in one worker's memory,
#: and a restart, a deploy or an OOM kill takes it without a word. Counting from
#: the ceiling is what makes closing such a row out race-free -- a job that is
#: still within its lifetime is never touched.
STRANDED_GRACE_SECONDS: Final[float] = 120.0
INTERRUPTED: Final[str] = "Interrupted before it finished (restart or timeout)."
#: Times the job looks for its own row before concluding it is not there.
VERIFY_ATTEMPTS: Final[int] = 3

#: Sensitivity levels that may not have ACL trimming switched off.
ACL_MANDATORY: Final[frozenset[str]] = frozenset(
    {
        Sensitivity.CONFIDENTIAL.value,
        Sensitivity.HIGHLY_CONFIDENTIAL.value,
        Sensitivity.RESTRICTED.value,
    }
)

#: Statuses an operator may set directly. Syncing belongs to the sync job.
OPERATOR_SETTABLE_STATUS: Final[frozenset[str]] = frozenset(
    {
        KnowledgeSourceStatus.ACTIVE.value,
        KnowledgeSourceStatus.PAUSED.value,
    }
)

SORTABLE: Final[dict[str, Any]] = {
    "name": KnowledgeSource.name,
    "type": KnowledgeSource.source_type,
    "source_type": KnowledgeSource.source_type,
    "env": KnowledgeSource.environment,
    "environment": KnowledgeSource.environment,
    "status": KnowledgeSource.status,
    "documents": KnowledgeSource.document_count,
    "document_count": KnowledgeSource.document_count,
    "chunks": KnowledgeSource.chunk_count,
    "chunk_count": KnowledgeSource.chunk_count,
    "grounding": KnowledgeSource.grounding_score,
    "grounding_score": KnowledgeSource.grounding_score,
    "sensitivity": KnowledgeSource.sensitivity,
    "lastSync": KnowledgeSource.last_sync_at,
    "last_sync_at": KnowledgeSource.last_sync_at,
    "owner": User.full_name,
    "created_at": KnowledgeSource.created_at,
    "updated_at": KnowledgeSource.updated_at,
}

#: Column order of the CSV export, matching the table left to right.
EXPORT_COLUMNS: Final[list[tuple[str, str]]] = [
    ("id", "Source ID"),
    ("name", "Source Name"),
    ("description", "Description"),
    ("source_type", "Type"),
    ("environment", "Environment"),
    ("status", "Status"),
    ("document_count", "Documents"),
    ("chunk_count", "Chunks"),
    ("grounding_score", "Grounding Score"),
    ("sensitivity", "Sensitivity"),
    ("has_acl", "ACL Enforced"),
    ("last_sync_at", "Last Sync"),
    ("owner_name", "Owner"),
    ("index_name", "Vector Index"),
    ("embedding_model", "Embedding Model"),
    ("indexing_status", "Indexing Status"),
]

#: Keys a retrieval span may use to name the index it read from.
_SOURCE_KEYS: Final[tuple[str, ...]] = (
    "knowledge_source",
    "knowledge_source_id",
    "knowledge_source_name",
    "index",
    "index_name",
    "source",
    "datasource",
    "vector_index",
)
_DOCUMENT_CONTAINERS: Final[tuple[str, ...]] = (
    "documents",
    "context",
    "retrieved",
    "chunks",
    "results",
    "sources",
    "citations",
)
_DOCUMENT_ID_KEYS: Final[tuple[str, ...]] = (
    "document_id",
    "documentId",
    "doc_id",
    "uri",
    "url",
    "path",
    "id",
)
_TITLE_KEYS: Final[tuple[str, ...]] = ("title", "name", "document", "filename", "file_name")
_CHUNK_KEYS: Final[tuple[str, ...]] = ("chunk_id", "chunkId", "chunk", "id")
_SCORE_KEYS: Final[tuple[str, ...]] = ("score", "similarity", "relevance", "distance")

_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


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
    return select(KnowledgeSource).where(
        KnowledgeSource.workspace_id == principal.workspace_id
    )


async def _call(awaitable: Awaitable[T], *, action: str) -> T:
    """Run one adapter call, translating its failures into API errors.

    An outage becomes 503 rather than an opaque 500. No branch invents a result:
    if the engine cannot answer, the caller is told so.
    """
    try:
        return await awaitable
    except EngineNotFound as exc:
        raise NotFound(f"The telemetry store has no such record ({action}).") from exc
    except EngineUnavailable as exc:
        raise TelemetryBackendUnavailable(
            f"The telemetry store could not be reached while {action}."
        ) from exc
    except EngineBadRequest as exc:
        raise ValidationFailed(
            f"The telemetry store rejected the request while {action}.",
            details={"status": exc.status},
        ) from exc


def _first(payload: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _instant(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _normalise(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


def _policy_block(source: KnowledgeSource) -> dict[str, Any]:
    return dict(source.retrieval_policy or {})


def _retrieval_policy(source: KnowledgeSource) -> RetrievalPolicy:
    block = {
        key: value
        for key, value in _policy_block(source).items()
        if key not in (SOURCE_SETTINGS_KEY, SYNC_STATE_KEY)
    }
    return RetrievalPolicy.model_validate(block)


def _settings(source: KnowledgeSource) -> SourceSettings:
    block = _policy_block(source).get(SOURCE_SETTINGS_KEY)
    return SourceSettings.model_validate(block if isinstance(block, dict) else {})


def _sync_state(source: KnowledgeSource) -> SyncState:
    block = _policy_block(source).get(SYNC_STATE_KEY)
    return SyncState.model_validate(block if isinstance(block, dict) else {})


def _write_blocks(
    source: KnowledgeSource,
    *,
    policy: RetrievalPolicy | None = None,
    settings: SourceSettings | None = None,
    sync: SyncState | None = None,
) -> None:
    """Rewrite the JSON column, keeping the blocks this API does not own."""
    block = _policy_block(source)
    if policy is not None:
        preserved = {
            key: block[key] for key in (SOURCE_SETTINGS_KEY, SYNC_STATE_KEY) if key in block
        }
        block = {**policy.model_dump(mode="json", exclude_none=True), **preserved}
    if settings is not None:
        block[SOURCE_SETTINGS_KEY] = settings.model_dump(mode="json", exclude_none=True)
    if sync is not None:
        block[SYNC_STATE_KEY] = sync.model_dump(mode="json", exclude_none=True)
    # Reassign rather than mutate: SQLAlchemy tracks JSON columns by identity.
    source.retrieval_policy = block


def _indexing_status(source: KnowledgeSource, settings: SourceSettings) -> IndexingStatus:
    """Derived, never stored: what the index state is right now."""
    if source.status == KnowledgeSourceStatus.SYNCING.value:
        return IndexingStatus.BUILDING
    if source.status in (
        KnowledgeSourceStatus.FAILED.value,
        KnowledgeSourceStatus.ERROR.value,
    ):
        return IndexingStatus.FAILED
    if source.last_sync_at is None:
        return IndexingStatus.BUILDING
    interval = settings.sync_frequency_minutes
    if interval:
        due = _as_utc(source.last_sync_at) + dt.timedelta(minutes=interval)
        if due < _now():
            return IndexingStatus.STALE
    return IndexingStatus.UP_TO_DATE


def _next_sync_at(
    source: KnowledgeSource, settings: SourceSettings
) -> dt.datetime | None:
    if source.last_sync_at is None or not settings.sync_frequency_minutes:
        return None
    return _as_utc(source.last_sync_at) + dt.timedelta(
        minutes=settings.sync_frequency_minutes
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _owner_rows(session: AsyncSession, user_ids: Sequence[str | None]) -> dict[str, Any]:
    ids = [user_id for user_id in user_ids if user_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.team).where(User.id.in_(ids))
        )
    ).all()
    return {row.id: row for row in rows}


def _shape(source: KnowledgeSource, owner: Any | None) -> KnowledgeSourceRead:
    settings = _settings(source)
    return KnowledgeSourceRead.model_validate(source).model_copy(
        update={
            "owner_name": owner.full_name if owner else None,
            "owner_team": owner.team if owner else None,
            "retrieval_policy": _retrieval_policy(source),
            "settings": settings,
            "sync": _sync_state(source),
            "indexing_status": _indexing_status(source, settings),
            "next_sync_at": _next_sync_at(source, settings),
        }
    )


async def hydrate(
    session: AsyncSession, rows: Sequence[KnowledgeSource]
) -> list[KnowledgeSourceRead]:
    """Attach the owner's name and the derived index state to each row."""
    if not rows:
        return []
    owners = await _owner_rows(session, [row.owner_user_id for row in rows])
    return [
        _shape(row, owners.get(row.owner_user_id) if row.owner_user_id else None)
        for row in rows
    ]


def _list_statement(
    principal: Principal,
    params: ListParams,
    *,
    source_type: str | None,
    status: str | None,
    environment: str | None,
    sensitivity: str | None,
) -> Select:
    stmt = _scoped(principal).outerjoin(User, User.id == KnowledgeSource.owner_user_id)
    stmt = apply_search(
        stmt,
        params,
        [
            KnowledgeSource.name,
            KnowledgeSource.source_type,
            KnowledgeSource.index_name,
            KnowledgeSource.acl_summary,
            User.full_name,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            KnowledgeSource.source_type: source_type,
            KnowledgeSource.status: status,
            KnowledgeSource.environment: environment,
            KnowledgeSource.sensitivity: sensitivity,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=KnowledgeSource.updated_at)


async def list_sources(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    source_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    sensitivity: str | None = None,
) -> tuple[list[KnowledgeSourceRead], int]:
    """One page of the workspace's knowledge sources."""
    stmt = _list_statement(
        principal,
        params,
        source_type=source_type,
        status=status,
        environment=environment,
        sensitivity=sensitivity,
    )
    rows, total = await paginate(session, stmt, params)
    return await hydrate(session, rows), total


async def export_sources(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    source_type: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    sensitivity: str | None = None,
) -> list[KnowledgeSourceRead]:
    """Every row the current filters select, unpaged, for the CSV download."""
    stmt = _list_statement(
        principal,
        params,
        source_type=source_type,
        status=status,
        environment=environment,
        sensitivity=sensitivity,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return await hydrate(session, rows)


async def get_source(
    session: AsyncSession, principal: Principal, source_id: str
) -> KnowledgeSource:
    """Load one source, or raise :class:`NotFound`.

    A row in another workspace raises the same 404 as a row that never existed.
    """
    source = (
        await session.execute(_scoped(principal).where(KnowledgeSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise NotFound(f"Knowledge source '{source_id}' does not exist.")
    return source


async def read_source(
    session: AsyncSession, principal: Principal, source_id: str
) -> KnowledgeSourceRead:
    source = await get_source(session, principal, source_id)
    return (await hydrate(session, [source]))[0]


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> KnowledgeSummary:
    """The six KPI cards, computed entirely in SQL."""
    workspace = KnowledgeSource.workspace_id == principal.workspace_id
    since = _now() - dt.timedelta(days=window_days)

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(KnowledgeSource.id).label("total"),
                _count_where(
                    KnowledgeSource.status == KnowledgeSourceStatus.ACTIVE.value
                ).label("active"),
                _count_where(
                    KnowledgeSource.status == KnowledgeSourceStatus.SYNCING.value
                ).label("syncing"),
                _count_where(
                    KnowledgeSource.status.in_(
                        [
                            KnowledgeSourceStatus.FAILED.value,
                            KnowledgeSourceStatus.ERROR.value,
                        ]
                    )
                ).label("failed"),
                _count_where(
                    KnowledgeSource.status == KnowledgeSourceStatus.PAUSED.value
                ).label("paused"),
                _count_where(KnowledgeSource.has_acl.is_(True)).label("with_acl"),
                _count_where(KnowledgeSource.created_at >= since).label("created_recent"),
                func.coalesce(func.sum(KnowledgeSource.document_count), 0).label("documents"),
                func.coalesce(func.sum(KnowledgeSource.chunk_count), 0).label("chunks"),
                func.avg(KnowledgeSource.grounding_score).label("grounding"),
                _count_where(KnowledgeSource.grounding_score.isnot(None)).label("scored"),
            ).where(workspace)
        )
    ).one()

    total = int(totals.total or 0)
    with_acl = int(totals.with_acl or 0)
    active = int(totals.active or 0)

    return KnowledgeSummary(
        total_sources=total,
        active_sources=active,
        syncing_sources=int(totals.syncing or 0),
        failed_sources=int(totals.failed or 0),
        paused_sources=int(totals.paused or 0),
        active_percent=round(active / total * 100, 1) if total else 0.0,
        total_documents=int(totals.documents or 0),
        total_chunks=int(totals.chunks or 0),
        avg_grounding_score=(
            round(float(totals.grounding), 2) if totals.grounding is not None else None
        ),
        sources_scored=int(totals.scored or 0),
        sources_with_acl=with_acl,
        acl_percent=round(with_acl / total * 100, 1) if total else 0.0,
        created_30d=int(totals.created_recent or 0),
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# The retrieval-span scan
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Document:
    """What one scan learned about one retrieved document."""

    document_id: str
    title: str | None = None
    chunks: set[str] = dataclasses.field(default_factory=set)
    last_seen: dt.datetime | None = None
    scores: list[float] = dataclasses.field(default_factory=list)
    agents: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class SpanScan:
    """The result of reading one source's retrieval spans."""

    documents: dict[str, _Document] = dataclasses.field(default_factory=dict)
    chunk_ids: set[str] = dataclasses.field(default_factory=set)
    dimensions: dict[str, list[float]] = dataclasses.field(default_factory=dict)
    spans: int = 0
    projects_scanned: int = 0
    projects_total: int = 0


@dataclasses.dataclass(frozen=True)
class _Entry:
    """One retrieved-document record, reduced to what the screen shows."""

    document_id: str
    title: str | None
    chunk: str | None
    score: float | None


@dataclasses.dataclass(frozen=True)
class _RetrievalSpan:
    """What this domain reads off one span: what it says it read from, the
    documents it got back and how it was scored. Never the payload itself --
    a scan is remembered between requests and prompts are not ours to keep."""

    agent: str
    names: frozenset[str]
    entries: tuple[_Entry, ...]
    scores: tuple[tuple[str, float], ...]
    seen_at: dt.datetime | None


@dataclasses.dataclass
class _WorkspaceScan:
    """One workspace's retrieval telemetry for one window, for every source."""

    spans: list[_RetrievalSpan] = dataclasses.field(default_factory=list)
    projects_scanned: int = 0
    projects_total: int = 0


#: key: (workspace id, window in days). What the store is asked does not depend
#: on the source -- only the match made afterwards does -- so one scan answers
#: the grounding panel and the documents modal of every source in the
#: workspace, and every page of that modal. Single flight: the inspector and
#: the Citations & Grounding tab ask at the same moment and share one read.
_workspace_scans: SingleFlightCache[_WorkspaceScan] = SingleFlightCache(
    ttl=lambda: app_settings.knowledge_scan_cache_seconds, max_entries=64
)


def forget_scans(workspace_id: str | None = None) -> None:
    """Forget the remembered retrieval scans of one workspace, or of all."""
    if workspace_id is None:
        _workspace_scans.invalidate()
        return
    _workspace_scans.invalidate(
        lambda key: isinstance(key, tuple) and bool(key) and key[0] == workspace_id
    )


async def _agent_projects(
    session: AsyncSession, principal: Principal
) -> list[tuple[str, str]]:
    """``(agent name, engine project name)`` for every agent that reports telemetry.

    Most recently reporting first: the scan reads a bounded number of projects,
    and which ones must not be left to the order the database happens to
    return rows in. A quiet agent is the right one to leave out.
    """
    rows = (
        await session.execute(
            select(Agent.name, Agent.engine_project_name)
            .where(
                Agent.workspace_id == principal.workspace_id,
                Agent.engine_project_name.isnot(None),
            )
            .order_by(Agent.last_used_at.desc().nulls_last(), Agent.name)
        )
    ).all()
    return [(name, project) for name, project in rows if project]


def _identifiers(source: KnowledgeSource) -> set[str]:
    """Every string a span might use to name this source."""
    return {
        value
        for value in (source.index_name, source.name, source.id)
        if isinstance(value, str) and value
    }


def _named_sources(span: Mapping[str, Any]) -> frozenset[str]:
    """Every string this span uses to say what it read from."""
    names: set[str] = set()
    metadata = span.get("metadata")
    if isinstance(metadata, dict):
        for key in _SOURCE_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    tags = span.get("tags")
    if isinstance(tags, list):
        names.update(tag for tag in tags if isinstance(tag, str) and tag)
    return frozenset(names)


def _document_entries(span: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The retrieved-document records carried on a span's output."""
    output = span.get("output")
    entries: list[Any] = []
    if isinstance(output, dict):
        for key in _DOCUMENT_CONTAINERS:
            value = output.get(key)
            if isinstance(value, list):
                entries.extend(value)
    elif isinstance(output, list):
        entries.extend(output)
    return [entry for entry in entries if isinstance(entry, dict)]


def _span_scores(span: Mapping[str, Any]) -> list[tuple[str, float]]:
    """``(dimension, value)`` for every feedback score this domain recognises."""
    raw = span.get("feedback_scores")
    if not isinstance(raw, list):
        return []
    found: list[tuple[str, float]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if not isinstance(name, str) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, bool):
            continue
        needle = _normalise(name)
        for dimension, aliases in GROUNDING_DIMENSIONS.items():
            if needle in aliases:
                score = float(value)
                # Every bar reads high-is-good. A hallucination judge scores the
                # other way round, so averaged as it came a source that never
                # hallucinates showed a red 0.00 and one that always does a
                # green 1.00. Clamped, because a score outside 0-1 would leave
                # the scale once turned over.
                if needle in INVERTED_METRIC_SOURCES:
                    score = 1.0 - score
                found.append((dimension, max(0.0, min(1.0, score))))
                break
    return found


def _reduce(span: Mapping[str, Any], agent: str) -> _RetrievalSpan | None:
    """Keep what the screen reads off a span; ``None`` if it names no source."""
    names = _named_sources(span)
    if not names:
        return None

    entries: list[_Entry] = []
    for entry in _document_entries(span):
        raw_id = _first(entry, _DOCUMENT_ID_KEYS)
        title = _first(entry, _TITLE_KEYS)
        document_id = str(raw_id) if raw_id is not None else (str(title) if title else None)
        if not document_id:
            continue
        chunk = _first(entry, _CHUNK_KEYS)
        score = _first(entry, _SCORE_KEYS)
        entries.append(
            _Entry(
                document_id=document_id,
                title=str(title) if title else None,
                chunk=str(chunk) if chunk is not None else None,
                score=(
                    float(score)
                    if isinstance(score, (int, float)) and not isinstance(score, bool)
                    else None
                ),
            )
        )

    return _RetrievalSpan(
        agent=agent,
        names=names,
        entries=tuple(entries),
        scores=tuple(_span_scores(span)),
        seen_at=_instant(_first(span, ("end_time", "start_time", "created_at"))),
    )


def _absorb(scan: SpanScan, span: _RetrievalSpan) -> None:
    """Fold one matching span into the accumulating scan."""
    scan.spans += 1

    for entry in span.entries:
        document = scan.documents.setdefault(
            entry.document_id, _Document(document_id=entry.document_id)
        )
        if entry.title and not document.title:
            document.title = entry.title
        if entry.chunk is not None:
            key = f"{entry.document_id}:{entry.chunk}"
            document.chunks.add(key)
            scan.chunk_ids.add(key)
        if entry.score is not None:
            document.scores.append(entry.score)
        if span.seen_at and (document.last_seen is None or span.seen_at > document.last_seen):
            document.last_seen = span.seen_at
        document.agents.add(span.agent)

    for dimension, value in span.scores:
        scan.dimensions.setdefault(dimension, []).append(value)


def _is_error_envelope(row: Any) -> bool:
    """True for a stream row that reports a failure rather than a span.

    The store answers a search 200 and then streams; a query that dies half
    way is reported as a row in that stream. Such a row has no ``id`` and says
    what went wrong. Read as "not a span" it used to be skipped, and a store
    failure then looked like a source nothing had ever retrieved from.
    """
    return (
        isinstance(row, dict)
        and not row.get("id")
        and any(key in row for key in ("code", "errors", "message"))
    )


async def _project_spans(
    engine: EngineClient,
    agent: str,
    project: str,
    *,
    since: dt.datetime,
    until: dt.datetime,
    timeout_seconds: float | None,
) -> list[_RetrievalSpan]:
    """One project's retrieval spans in the window, cursored back to the cap."""
    found: list[_RetrievalSpan] = []
    fetched = 0
    cursor: str | None = None
    while fetched < MAX_SPANS_PER_PROJECT:
        wanted = min(SPAN_PAGE_SIZE, MAX_SPANS_PER_PROJECT - fetched)
        batch = await _call(
            engine.search_spans(
                project_name=project,
                from_time=since,
                to_time=until,
                limit=wanted,
                last_retrieved_id=cursor,
                # Documents are parsed from ``output``; a truncated one is not JSON.
                truncate=False,
                timeout_seconds=timeout_seconds,
            ),
            action="reading retrieval spans",
        )
        rows = batch or []
        if any(_is_error_envelope(row) for row in rows):
            raise TelemetryBackendUnavailable(
                "The telemetry store failed part way through reading retrieval spans."
            )
        usable = [row for row in rows if isinstance(row, dict) and row.get("id")]
        fetched += len(usable)

        left_window = False
        for row in usable:
            started = _instant(_first(row, ("start_time", "created_at")))
            if started is not None and started < since:
                # Newest first: everything after this row is older still.
                left_window = True
                continue
            reduced = _reduce(row, agent)
            if reduced is not None:
                found.append(reduced)

        if len(rows) < wanted or not usable or left_window:
            break
        cursor = str(usable[-1]["id"])
    return found


async def _read_workspace(
    engine: EngineClient,
    pairs: Sequence[tuple[str, str]],
    *,
    window_days: int,
    timeout_seconds: float | None,
) -> _WorkspaceScan:
    """Ask the store, once, for everything any source in the workspace needs."""
    scan = _WorkspaceScan(projects_total=len({project for _agent, project in pairs}))

    selected: list[tuple[str, str]] = []
    seen_projects: set[str] = set()
    for agent, project in pairs:
        if project in seen_projects:
            continue
        seen_projects.add(project)
        selected.append((agent, project))
        if len(selected) >= MAX_SCAN_PROJECTS:
            break
    if not selected:
        return scan

    until = _now()
    since = until - dt.timedelta(days=window_days)
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def read(agent: str, project: str) -> list[_RetrievalSpan]:
        async with gate:
            return await _project_spans(
                engine,
                agent,
                project,
                since=since,
                until=until,
                timeout_seconds=timeout_seconds,
            )

    per_project = await asyncio.gather(*(read(agent, project) for agent, project in selected))
    scan.projects_scanned = len(selected)
    for spans in per_project:
        scan.spans.extend(spans)
    return scan


async def _source_scan(
    workspace_id: str,
    pairs: Sequence[tuple[str, str]],
    identifiers: set[str],
    *,
    window_days: int,
    client: EngineClient | None = None,
    fresh: bool = False,
) -> SpanScan:
    """One source's view of the workspace scan. Touches no database.

    A screen read is served from the remembered scan when there is one, and
    waits under the fan-out deadline so this API answers before the console
    stops listening; a shared read it gave up on finishes on its own and is
    there for the retry. ``fresh`` is the sync job: it always reads the store,
    takes the time it needs, and leaves what it read for the screen's next look.
    """
    engine = client or get_engine_client()
    key = (workspace_id, window_days)

    async def compute() -> _WorkspaceScan:
        return await _read_workspace(
            engine,
            pairs,
            window_days=window_days,
            timeout_seconds=None if fresh else app_settings.engine_read_timeout_seconds,
        )

    if fresh:
        forget_scans(workspace_id)
        workspace_scan = await _workspace_scans.get(key, compute)
    else:
        try:
            async with deadline(what="reading retrieval spans"):
                workspace_scan = await _workspace_scans.get(key, compute)
        except EngineUnavailable as exc:
            raise TelemetryBackendUnavailable(
                "The telemetry store could not be reached while reading retrieval spans."
            ) from exc

    scan = SpanScan(
        projects_scanned=workspace_scan.projects_scanned,
        projects_total=workspace_scan.projects_total,
    )
    for span in workspace_scan.spans:
        if span.names & identifiers:
            _absorb(scan, span)
    return scan


async def scan_retrieval_spans(
    session: AsyncSession,
    principal: Principal,
    source: KnowledgeSource,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    client: EngineClient | None = None,
    fresh: bool = False,
) -> SpanScan:
    """Read the retrieval spans that name this source, across our agents only.

    Spans are fetched per engine project, and the only projects addressed are
    those of agents in this workspace — a source can never be described using
    another tenant's telemetry. Both the number of projects and the number of
    spans per project are capped; the scan reports what it covered so a caller
    can say the sample was limited rather than imply it was exhaustive.

    The session's transaction is ended before the store is asked anything. The
    scan can take as long as the store does, and a request that holds a pooled
    database connection for that long is how a slow store empties the pool for
    requests that never touch it. Rows already loaded stay readable.
    """
    pairs = await _agent_projects(session, principal)
    identifiers = _identifiers(source)
    await session.commit()
    return await _source_scan(
        principal.workspace_id,
        pairs,
        identifiers,
        window_days=window_days,
        client=client,
        fresh=fresh,
    )


def _grounding(scan: SpanScan) -> tuple[float | None, list[GroundingDimension]]:
    """Mean of each dimension's scores, and the overall grounding score."""
    labels = {
        "context_relevance": "Context Relevance",
        "answer_relevance": "Answer Relevance",
        "citation_quality": "Citation Quality",
        "completeness": "Completeness",
    }
    dimensions = [
        GroundingDimension(
            name=name,
            label=label,
            score=(
                round(sum(scan.dimensions[name]) / len(scan.dimensions[name]), 2)
                if scan.dimensions.get(name)
                else None
            ),
            sample_size=len(scan.dimensions.get(name, [])),
        )
        for name, label in labels.items()
    ]

    explicit = scan.dimensions.get("grounding") or []
    if explicit:
        overall = round(sum(explicit) / len(explicit), 2)
    else:
        measured = [d.score for d in dimensions if d.score is not None]
        overall = round(sum(measured) / len(measured), 2) if measured else None
    return overall, dimensions


async def grounding_breakdown(
    session: AsyncSession,
    principal: Principal,
    source_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> GroundingBreakdown:
    """Grounding quality for one source, from feedback scores on its spans."""
    source = await get_source(session, principal, source_id)
    scan = await scan_retrieval_spans(
        session, principal, source, window_days=window_days
    )
    overall, dimensions = _grounding(scan)
    return GroundingBreakdown(
        source_id=source.id,
        overall=overall,
        dimensions=dimensions,
        spans_sampled=scan.spans,
        projects_scanned=scan.projects_scanned,
        projects_total=scan.projects_total,
        window_days=window_days,
        # ``overall`` is None exactly when no recognised score was seen. Its
        # truthiness is not the test: 0.0 is a measurement, and the worst one.
        measured=overall is not None,
    )


async def list_documents(
    session: AsyncSession,
    principal: Principal,
    source_id: str,
    params: ListParams,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[list[KnowledgeDocumentRead], int]:
    """The documents retrieval telemetry saw agents read from this source.

    This is deliberately not "every document in the corpus": the control plane
    does not index the corpus, so the only documents it can name are the ones it
    watched an agent retrieve. The modal says so, and an unretrieved corpus
    answers with an empty page rather than a fabricated file list.
    """
    source = await get_source(session, principal, source_id)
    scan = await scan_retrieval_spans(
        session, principal, source, window_days=window_days
    )
    sensitivity = Sensitivity(source.sensitivity)

    rows = [
        KnowledgeDocumentRead(
            document_id=document.document_id,
            title=document.title,
            chunks=len(document.chunks),
            last_retrieved_at=document.last_seen,
            avg_retrieval_score=(
                round(sum(document.scores) / len(document.scores), 3)
                if document.scores
                else None
            ),
            retrieved_by=sorted(document.agents),
            sensitivity=sensitivity,
            status="Indexed",
        )
        for document in scan.documents.values()
    ]

    if params.q:
        needle = params.q.strip().lower()
        rows = [
            row
            for row in rows
            if needle in f"{row.document_id} {row.title or ''}".lower()
        ]

    key = params.sort_key or "last_retrieved_at"
    keys = {
        "document_id": lambda row: row.document_id.lower(),
        "title": lambda row: (row.title or row.document_id).lower(),
        "chunks": lambda row: row.chunks,
        "last_retrieved_at": lambda row: row.last_retrieved_at
        or dt.datetime.min.replace(tzinfo=dt.UTC),
        "avg_retrieval_score": lambda row: (
            row.avg_retrieval_score if row.avg_retrieval_score is not None else -1.0
        ),
    }
    if key not in keys:
        raise ValidationFailed(
            f"Cannot sort documents by '{key}'.", details={"sortable": sorted(keys)}
        )
    descending = params.descending if params.sort_key else True
    rows.sort(key=keys[key], reverse=descending)

    total = len(rows)
    start = params.offset
    return rows[start : start + params.page_size], total


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _check_acl(sensitivity: str, has_acl: bool, name: str) -> None:
    if sensitivity in ACL_MANDATORY and not has_acl:
        raise ValidationFailed(
            f"'{name}' is {sensitivity.lower()}; ACL-aware retrieval cannot be "
            "switched off for it.",
            details={"field": "has_acl"},
        )


async def create_source(
    session: AsyncSession,
    principal: Principal,
    payload: KnowledgeSourceCreate,
    *,
    request: Request | None = None,
) -> KnowledgeSource:
    """Register a knowledge source and, unless suppressed, start its first sync."""
    principal.require(Role.OPERATOR)
    _check_acl(payload.sensitivity.value, payload.has_acl, payload.name)

    clash = (
        await session.execute(_scoped(principal).where(KnowledgeSource.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A knowledge source named '{payload.name}' already exists.")

    # The row is always born at rest, whatever ``start_sync`` says. Queuing the
    # first sync is start_sync()'s job and nobody else's: it is the one place
    # that flips a row to Syncing, and its first guard refuses a row that
    # already is. A row inserted as Syncing met that guard on the very next
    # line of the route, so the default "start the first sync now" answered
    # 409 "already syncing" and rolled the creation back with it.
    source = KnowledgeSource(
        workspace_id=principal.workspace_id,
        name=payload.name,
        source_type=payload.source_type.value,
        environment=payload.environment.value,
        status=KnowledgeSourceStatus.PAUSED.value,
        document_count=payload.document_count,
        chunk_count=payload.chunk_count,
        grounding_score=None,
        sensitivity=payload.sensitivity.value,
        has_acl=payload.has_acl,
        acl_summary=payload.acl_summary,
        last_sync_at=None,
        sync_progress=100,
        owner_user_id=payload.owner_user_id or principal.user_id,
        index_name=payload.index_name,
        embedding_model=payload.embedding_model,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
        retrieval_policy={},
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    _write_blocks(
        source,
        policy=payload.retrieval_policy,
        settings=payload.settings,
        sync=SyncState(),
    )
    session.add(source)

    try:
        await session.flush()
    except IntegrityError as exc:  # someone claimed the name between check and flush
        await session.rollback()
        raise Conflict(
            f"A knowledge source named '{payload.name}' already exists."
        ) from exc

    await audit.record(
        session,
        principal=principal,
        action="knowledge_source.created",
        entity_type=ENTITY_TYPE,
        entity_id=source.id,
        entity_label=source.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Registered {source.source_type} source"
            + (" and queued the first sync" if payload.start_sync else "")
        ),
        metadata={
            "source_type": source.source_type,
            "sensitivity": source.sensitivity,
            "has_acl": source.has_acl,
        },
        request=request,
    )
    await session.flush()
    # Server-side timestamp defaults land on the row, not the instance.
    await session.refresh(source)
    return source


async def update_source(
    session: AsyncSession,
    principal: Principal,
    source_id: str,
    payload: KnowledgeSourceUpdate,
    *,
    request: Request | None = None,
) -> KnowledgeSource:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.OPERATOR)
    source = await get_source(session, principal, source_id)

    if payload.expected_updated_at is not None:
        current = source.updated_at
        expected = _as_utc(payload.expected_updated_at)
        # One second of slack: clients round-trip the timestamp through JSON.
        if current is not None and abs((_as_utc(current) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{source.name}' was changed by someone else. Reload and try again."
            )

    if payload.status is not None and payload.status.value not in OPERATOR_SETTABLE_STATUS:
        raise ValidationFailed(
            f"'{payload.status.value}' is set by the sync job, not by hand. "
            f"Settable statuses: {', '.join(sorted(OPERATOR_SETTABLE_STATUS))}.",
            details={"field": "status"},
        )
    # Pause is the third exit a sync that died with its worker used to bar,
    # beside Sync Now and Delete.
    if payload.status is not None and _stranded(source):
        await _close_out_stranded(session, source)
    if source.status == KnowledgeSourceStatus.SYNCING.value and payload.status is not None:
        raise PreconditionFailed(
            f"'{source.name}' is syncing. Wait for the job to finish before changing "
            "its status."
        )

    changes = payload.model_dump(
        exclude_unset=True,
        exclude={"expected_updated_at", "retrieval_policy", "settings"},
    )
    sensitivity = changes.get("sensitivity")
    sensitivity_value = (
        sensitivity.value if hasattr(sensitivity, "value") else sensitivity
    ) or source.sensitivity
    has_acl = changes.get("has_acl", source.has_acl)
    _check_acl(sensitivity_value, bool(has_acl), payload.name or source.name)

    if "name" in changes and changes["name"] != source.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    KnowledgeSource.name == changes["name"],
                    KnowledgeSource.id != source.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(
                f"A knowledge source named '{changes['name']}' already exists."
            )

    for field, value in changes.items():
        setattr(source, field, value.value if hasattr(value, "value") else value)

    touched = set(changes)
    if payload.retrieval_policy is not None or payload.settings is not None:
        _write_blocks(
            source, policy=payload.retrieval_policy, settings=payload.settings
        )
        touched.update(
            name
            for name, supplied in (
                ("retrieval_policy", payload.retrieval_policy),
                ("settings", payload.settings),
            )
            if supplied is not None
        )

    if not touched:
        return source

    source.updated_by = principal.actor
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A knowledge source named '{source.name}' already exists.") from exc

    await audit.record(
        session,
        principal=principal,
        action="knowledge_source.updated",
        entity_type=ENTITY_TYPE,
        entity_id=source.id,
        entity_label=source.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(touched)),
        metadata={"fields": sorted(touched)},
        request=request,
    )
    await session.flush()
    # ``updated_at`` is a server-side onupdate: read it back before serialising.
    await session.refresh(source)
    return source


async def delete_source(
    session: AsyncSession,
    principal: Principal,
    source_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a source. The audit trail survives the deletion."""
    principal.require(Role.ADMIN)
    source = await get_source(session, principal, source_id)

    if _stranded(source):
        await _close_out_stranded(session, source)
    if source.status == KnowledgeSourceStatus.SYNCING.value:
        raise PreconditionFailed(
            f"'{source.name}' is syncing. Wait for the job to finish before deleting it."
        )

    await audit.record(
        session,
        principal=principal,
        action="knowledge_source.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=source.id,
        entity_label=source.name,
        source_screen=SOURCE_SCREEN,
        detail=(
            f"Removed {source.source_type} source with {source.chunk_count} reported chunk(s)"
        ),
        metadata={
            "documents": source.document_count,
            "chunks": source.chunk_count,
            "index_name": source.index_name,
        },
        request=request,
    )
    await session.delete(source)
    await session.flush()


# ---------------------------------------------------------------------------
# The sync job
# ---------------------------------------------------------------------------


def _system_principal(workspace_id: str) -> Principal:
    """Actor the sync job signs its own audit rows with.

    The trail must not attribute a machine transition to the person who pressed
    Sync Now, so the runner signs as itself.
    """
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="knowledge-sync",
        display_name="Knowledge Sync",
    )


def sync_status_of(source: KnowledgeSource) -> KnowledgeSyncStatus:
    """Read the persisted job state off the row."""
    state = _sync_state(source)
    return KnowledgeSyncStatus(
        source_id=source.id,
        name=source.name,
        status=KnowledgeSourceStatus(source.status),
        progress=source.sync_progress,
        stage=state.stage,
        stage_index=state.stage_index,
        stage_count=len(SYNC_STAGES),
        state=state.state,
        running=source.status == KnowledgeSourceStatus.SYNCING.value,
        started_at=state.started_at,
        finished_at=state.finished_at,
        last_sync_at=source.last_sync_at,
        error=state.error,
        documents_seen=state.documents_seen,
        chunks_seen=state.chunks_seen,
        projects_scanned=state.projects_scanned,
        spans_sampled=state.spans_sampled,
    )


def _stranded(source: KnowledgeSource) -> bool:
    """True when the row says Syncing and no job can still be behind it.

    Measured from the job's last committed progress -- every stage commits, and
    each commit moves ``updated_at`` -- so a job that is alive in another worker
    is never mistaken for a dead one: it would have had to sit silent for longer
    than its own supervisor lets it live.
    """
    if source.status != KnowledgeSourceStatus.SYNCING.value or runner.is_running(source.id):
        return False
    marks = [
        _as_utc(mark)
        for mark in (_sync_state(source).started_at, source.updated_at)
        if mark is not None
    ]
    if not marks:
        return True
    cutoff = _now() - dt.timedelta(
        seconds=EXECUTION_DEADLINE_SECONDS + STRANDED_GRACE_SECONDS
    )
    return max(marks) < cutoff


async def _close_out_stranded(session: AsyncSession, source: KnowledgeSource) -> None:
    """Fail a Syncing row whose job is gone, which gives the row its exits back.

    While a row is Syncing, Sync Now answers 409 and Delete answers 412, and
    only the job ever moves it on. With the job gone that was for ever, short
    of an UPDATE by hand.
    """
    state = _sync_state(source)
    current = _settings(source)
    _write_blocks(
        source,
        settings=current.model_copy(
            update={"indexing_errors": current.indexing_errors + 1}
        ),
        sync=state.model_copy(
            update={"state": SyncStage.FAILED, "finished_at": _now(), "error": INTERRUPTED}
        ),
    )
    source.status = KnowledgeSourceStatus.FAILED.value
    await audit.record(
        session,
        principal=_system_principal(source.workspace_id),
        action="knowledge_source.sync_failed",
        entity_type=ENTITY_TYPE,
        entity_id=source.id,
        entity_label=source.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Sync failed: {INTERRUPTED}",
        metadata={"job_id": state.job_id, "stranded": True},
    )
    await session.flush()


async def fail_stranded_syncs() -> int:
    """Close out every stranded sync, in every workspace. The scheduler's sweep.

    The screen heals a stranded row the moment anyone looks at it (the progress
    poll below); this is for the rows nobody is looking at, whose Syncing count
    sits on the KPI card all the same.
    """
    closed = 0
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.status == KnowledgeSourceStatus.SYNCING.value
                    )
                )
            )
            .scalars()
            .all()
        )
        for source in rows:
            if _stranded(source):
                await _close_out_stranded(session, source)
                closed += 1
        await session.commit()
    return closed


async def sync_status(
    session: AsyncSession, principal: Principal, source_id: str
) -> KnowledgeSyncStatus:
    """What the console's percentage bar polls.

    The console polls this for every row it shows as Syncing, which makes it
    the one call certain to arrive for a stranded row. It is closed out here
    rather than reported as "Syncing... 40%" every two seconds for ever.
    """
    source = await get_source(session, principal, source_id)
    if _stranded(source):
        await _close_out_stranded(session, source)
    return sync_status_of(source)


class _Superseded(Exception):
    """The row is no longer this job's to write: it stands down without a word."""


def _still_owned(source: KnowledgeSource, job_id: str) -> bool:
    """True while the row is Syncing under this job's id.

    A job that outlives its welcome -- closed out as stranded, then restarted
    by an operator -- must not write its progress over its successor's.
    """
    return source.status == KnowledgeSourceStatus.SYNCING.value and _sync_state(
        source
    ).job_id in (None, job_id)


class SyncRunner:
    """Supervises the in-process asyncio task that runs one source's sync.

    One task per source. Starting a sync for a source that already has a live
    task is refused by the caller, which is what keeps the row's Syncing status
    and the task set in step with each other.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, source_id: str, workspace_id: str, *, job_id: str) -> bool:
        """Begin the sync. True if a task was created."""
        existing = self._tasks.get(source_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(
            self._run(source_id, workspace_id, job_id),
            name=f"knowledge-sync:{source_id}",
        )
        self._tasks[source_id] = task
        task.add_done_callback(functools.partial(self._forget, source_id))
        return True

    def is_running(self, source_id: str) -> bool:
        task = self._tasks.get(source_id)
        return task is not None and not task.done()

    def _forget(self, source_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(source_id) is task:
            self._tasks.pop(source_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # pragma: no cover - _run handles its own failures
            log.error("knowledge sync %s ended in error: %s", source_id, error)

    async def _run(self, source_id: str, workspace_id: str, job_id: str) -> None:
        principal = _system_principal(workspace_id)
        try:
            source_name, identifiers_present = await self._stage_verify(
                source_id, workspace_id, job_id
            )
            if not identifiers_present:
                return

            scan = await self._stage_scan(source_id, workspace_id, job_id)
            await self._stage_chunks(source_id, workspace_id, job_id, scan)
            overall = await self._stage_grounding(source_id, workspace_id, job_id, scan)
            await self._stage_publish(
                source_id, workspace_id, job_id, scan, overall, principal, source_name
            )
        except _Superseded:
            log.info("knowledge sync %s (job %s) was superseded", source_id, job_id)
        except TimeoutError:
            await self._fail(
                source_id,
                workspace_id,
                job_id,
                "The telemetry store did not answer within "
                f"{EXECUTION_DEADLINE_SECONDS:g} s.",
            )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            await self._fail(source_id, workspace_id, job_id, "Sync was cancelled.")
            raise
        except Exception as exc:
            log.exception("knowledge sync %s aborted", source_id)
            await self._fail(source_id, workspace_id, job_id, str(exc))

    # -- stages ------------------------------------------------------------

    async def _load(
        self, session: AsyncSession, source_id: str, workspace_id: str
    ) -> KnowledgeSource | None:
        return (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.id == source_id,
                    KnowledgeSource.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()

    async def _mark(
        self,
        source_id: str,
        workspace_id: str,
        job_id: str,
        stage_index: int,
        **fields: Any,
    ) -> None:
        """Persist the progress of one finished stage and commit it."""
        stage_name, progress = SYNC_STAGES[stage_index]
        async with get_sessionmaker()() as session:
            try:
                source = await self._load(session, source_id, workspace_id)
                if source is None:
                    return
                if not _still_owned(source, job_id):
                    raise _Superseded
                state = _sync_state(source)
                updated = state.model_copy(
                    update={
                        "job_id": job_id,
                        "stage": stage_name,
                        "stage_index": stage_index + 1,
                        "state": SyncStage.RUNNING,
                        **fields,
                    }
                )
                _write_blocks(source, sync=updated)
                source.sync_progress = progress
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _stage_verify(
        self, source_id: str, workspace_id: str, job_id: str
    ) -> tuple[str, bool]:
        """Refuse to sync a source that has nothing to identify its index by."""
        # The routes commit before they schedule the job, so the row is there.
        # Should it not be -- a replica a moment behind, a route that forgot --
        # look again before giving up: a job that walks away from a row it
        # could not see yet leaves that row Syncing with nothing working on it.
        source: KnowledgeSource | None = None
        for attempt in range(VERIFY_ATTEMPTS):
            async with get_sessionmaker()() as session:
                source = await self._load(session, source_id, workspace_id)
            if source is not None:
                break
            await asyncio.sleep(0.25 * (attempt + 1))
        if source is None:
            log.warning("knowledge sync %s: the source row never appeared", source_id)
            return "", False
        if not _still_owned(source, job_id):
            raise _Superseded

        name = source.name
        if not source.index_name and not _settings(source).location:
            await self._fail(
                source_id,
                workspace_id,
                job_id,
                "No vector index or location is configured, so there is nothing "
                "to synchronise.",
            )
            return name, False
        await self._mark(source_id, workspace_id, job_id, 0)
        return name, True

    async def _stage_scan(self, source_id: str, workspace_id: str, job_id: str) -> SpanScan:
        async with get_sessionmaker()() as session:
            source = await self._load(session, source_id, workspace_id)
            if source is None:
                return SpanScan()
            identifiers = _identifiers(source)
            pairs = await _agent_projects(session, _system_principal(workspace_id))
        # The store is read with no session open, and always afresh: a sync that
        # republished the scan the screen made a minute ago would measure nothing.
        # It is the one stage that waits on somebody else, so it is the one the
        # job's ceiling is applied to; no SQL runs inside the block.
        async with asyncio.timeout(EXECUTION_DEADLINE_SECONDS):
            scan = await _source_scan(
                workspace_id,
                pairs,
                identifiers,
                window_days=DEFAULT_WINDOW_DAYS,
                fresh=True,
            )
        await self._mark(
            source_id,
            workspace_id,
            job_id,
            1,
            documents_seen=len(scan.documents),
            projects_scanned=scan.projects_scanned,
            spans_sampled=scan.spans,
        )
        return scan

    async def _stage_chunks(
        self, source_id: str, workspace_id: str, job_id: str, scan: SpanScan
    ) -> None:
        await self._mark(
            source_id, workspace_id, job_id, 2, chunks_seen=len(scan.chunk_ids)
        )

    async def _stage_grounding(
        self, source_id: str, workspace_id: str, job_id: str, scan: SpanScan
    ) -> float | None:
        overall, _dimensions = _grounding(scan)
        await self._mark(source_id, workspace_id, job_id, 3)
        return overall

    async def _stage_publish(
        self,
        source_id: str,
        workspace_id: str,
        job_id: str,
        scan: SpanScan,
        overall: float | None,
        principal: Principal,
        source_name: str,
    ) -> None:
        finished_at = _now()
        async with get_sessionmaker()() as session:
            try:
                source = await self._load(session, source_id, workspace_id)
                if source is None:
                    return
                if not _still_owned(source, job_id):
                    raise _Superseded
                settings = _settings(source).model_copy(
                    update={
                        "observed_documents": len(scan.documents),
                        "observed_chunks": len(scan.chunk_ids),
                        "indexing_errors": 0,
                    }
                )
                state = _sync_state(source).model_copy(
                    update={
                        "job_id": job_id,
                        "stage": SYNC_STAGES[-1][0],
                        "stage_index": len(SYNC_STAGES),
                        "state": SyncStage.COMPLETED,
                        "finished_at": finished_at,
                        "error": None,
                        "documents_seen": len(scan.documents),
                        "chunks_seen": len(scan.chunk_ids),
                        "projects_scanned": scan.projects_scanned,
                        "spans_sampled": scan.spans,
                    }
                )
                _write_blocks(source, settings=settings, sync=state)
                # Only overwrite the cached score when the scan actually measured
                # one; an unscored window must not erase the last real figure.
                if overall is not None:
                    source.grounding_score = overall
                source.sync_progress = 100
                source.status = KnowledgeSourceStatus.ACTIVE.value
                source.last_sync_at = finished_at
                source.updated_by = principal.actor

                await audit.record(
                    session,
                    principal=principal,
                    action="knowledge_source.synced",
                    entity_type=ENTITY_TYPE,
                    entity_id=source.id,
                    entity_label=source_name or source.name,
                    source_screen=SOURCE_SCREEN,
                    detail=(
                        f"Sync completed: {len(scan.documents)} document(s) and "
                        f"{len(scan.chunk_ids)} chunk(s) observed across "
                        f"{scan.projects_scanned} project(s)"
                    ),
                    metadata={
                        "job_id": job_id,
                        "documents_seen": len(scan.documents),
                        "chunks_seen": len(scan.chunk_ids),
                        "spans_sampled": scan.spans,
                        "grounding_score": overall,
                    },
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _fail(
        self, source_id: str, workspace_id: str, job_id: str, reason: str
    ) -> None:
        """Never leave a source stuck in Syncing."""
        finished_at = _now()
        try:
            async with get_sessionmaker()() as session:
                source = await self._load(session, source_id, workspace_id)
                if source is None or not _still_owned(source, job_id):
                    return
                settings = _settings(source).model_copy(
                    update={"indexing_errors": _settings(source).indexing_errors + 1}
                )
                state = _sync_state(source).model_copy(
                    update={
                        "job_id": job_id,
                        "state": SyncStage.FAILED,
                        "finished_at": finished_at,
                        "error": reason[:500],
                    }
                )
                _write_blocks(source, settings=settings, sync=state)
                source.status = KnowledgeSourceStatus.FAILED.value
                await audit.record(
                    session,
                    principal=_system_principal(workspace_id),
                    action="knowledge_source.sync_failed",
                    entity_type=ENTITY_TYPE,
                    entity_id=source.id,
                    entity_label=source.name,
                    source_screen=SOURCE_SCREEN,
                    detail=f"Sync failed: {reason[:200]}",
                    metadata={"job_id": job_id},
                )
                await session.commit()
        except Exception:  # pragma: no cover - the database is already unhappy
            log.exception("could not close out failed knowledge sync %s", source_id)


#: One runner per process, mirroring how the deployment pipeline is supervised.
runner = SyncRunner()


async def run_sync(source_id: str, workspace_id: str, job_id: str) -> None:
    """Background entry point used by the routes.

    Scheduled with ``BackgroundTasks``, which run BEFORE the session dependency
    commits. The routes therefore commit the request's transaction themselves
    before scheduling this, so the job's own session sees the Syncing row.
    """
    runner.start(source_id, workspace_id, job_id=job_id)


async def start_sync(
    session: AsyncSession,
    principal: Principal,
    source_id: str,
    *,
    request: Request | None = None,
) -> tuple[KnowledgeActionResponse, str]:
    """Queue a sync and hand back the state the progress bar starts from.

    The task itself is scheduled by the route with ``BackgroundTasks``; the job
    id returned here is what the route hands to :func:`run_sync`.
    """
    principal.require(Role.OPERATOR)
    source = await get_source(session, principal, source_id)

    # Sync Now is the operator's way out of a sync that died with its worker.
    if _stranded(source):
        await _close_out_stranded(session, source)
    if source.status == KnowledgeSourceStatus.SYNCING.value or runner.is_running(source.id):
        raise Conflict(f"'{source.name}' is already syncing.")

    job_id = uuid.uuid4().hex
    started_at = _now()
    _write_blocks(
        source,
        sync=SyncState(
            job_id=job_id,
            stage=SYNC_STAGES[0][0],
            stage_index=0,
            state=SyncStage.QUEUED,
            started_at=started_at,
            finished_at=None,
            error=None,
            started_by=principal.actor,
        ),
    )
    source.status = KnowledgeSourceStatus.SYNCING.value
    source.sync_progress = 0
    source.updated_by = principal.actor

    await audit.record(
        session,
        principal=principal,
        action="knowledge_source.sync_started",
        entity_type=ENTITY_TYPE,
        entity_id=source.id,
        entity_label=source.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Sync requested for {source.index_name or source.name}",
        metadata={"job_id": job_id},
        request=request,
    )
    await session.flush()
    await session.refresh(source)

    read = (await hydrate(session, [source]))[0]
    return (
        KnowledgeActionResponse(
            source=read,
            message=f"Sync started for {source.name}.",
            sync=sync_status_of(source),
        ),
        job_id,
    )


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "EXECUTION_DEADLINE_SECONDS",
    "EXPORT_COLUMNS",
    "SpanScan",
    "SyncRunner",
    "create_source",
    "delete_source",
    "export_sources",
    "fail_stranded_syncs",
    "forget_scans",
    "get_source",
    "grounding_breakdown",
    "hydrate",
    "list_documents",
    "list_sources",
    "read_source",
    "run_sync",
    "runner",
    "scan_retrieval_spans",
    "start_sync",
    "summarise",
    "sync_status",
    "update_source",
]
