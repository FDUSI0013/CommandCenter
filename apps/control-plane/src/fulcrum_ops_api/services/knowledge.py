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
from ..core.errors import (
    Conflict,
    NotFound,
    PreconditionFailed,
    TelemetryBackendUnavailable,
    ValidationFailed,
)
from ..db.session import get_sessionmaker
from ..engine import (
    EngineBadRequest,
    EngineClient,
    EngineNotFound,
    EngineUnavailable,
    get_engine_client,
)
from ..models.identity import Role, User
from ..models.registry import (
    Agent,
    KnowledgeSource,
    KnowledgeSourceStatus,
    Sensitivity,
)
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

#: One search call per project; these bound what a scan will pull back so a
#: busy workspace cannot turn one console click into an unbounded read.
MAX_SCAN_PROJECTS: Final[int] = 10
MAX_SPANS_PER_PROJECT: Final[int] = 500

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


async def _agent_projects(
    session: AsyncSession, principal: Principal
) -> list[tuple[str, str]]:
    """``(agent name, engine project name)`` for every agent that reports telemetry."""
    rows = (
        await session.execute(
            select(Agent.name, Agent.engine_project_name).where(
                Agent.workspace_id == principal.workspace_id,
                Agent.engine_project_name.isnot(None),
            )
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


def _references(span: Mapping[str, Any], identifiers: set[str]) -> bool:
    """True when the span says it read from this source."""
    metadata = span.get("metadata")
    if isinstance(metadata, dict):
        for key in _SOURCE_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value in identifiers:
                return True
    tags = span.get("tags")
    return isinstance(tags, list) and any(
        isinstance(tag, str) and tag in identifiers for tag in tags
    )


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
                found.append((dimension, float(value)))
                break
    return found


def _absorb(scan: SpanScan, span: Mapping[str, Any], agent: str) -> None:
    """Fold one matching span into the accumulating scan."""
    scan.spans += 1
    seen_at = _instant(_first(span, ("end_time", "start_time", "created_at")))

    for entry in _document_entries(span):
        raw_id = _first(entry, _DOCUMENT_ID_KEYS)
        title = _first(entry, _TITLE_KEYS)
        document_id = str(raw_id) if raw_id is not None else (str(title) if title else None)
        if not document_id:
            continue
        document = scan.documents.setdefault(document_id, _Document(document_id=document_id))
        if title and not document.title:
            document.title = str(title)
        chunk = _first(entry, _CHUNK_KEYS)
        if chunk is not None:
            key = f"{document_id}:{chunk}"
            document.chunks.add(key)
            scan.chunk_ids.add(key)
        score = _first(entry, _SCORE_KEYS)
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            document.scores.append(float(score))
        if seen_at and (document.last_seen is None or seen_at > document.last_seen):
            document.last_seen = seen_at
        document.agents.add(agent)

    for dimension, value in _span_scores(span):
        scan.dimensions.setdefault(dimension, []).append(value)


async def scan_retrieval_spans(
    session: AsyncSession,
    principal: Principal,
    source: KnowledgeSource,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    client: EngineClient | None = None,
) -> SpanScan:
    """Read the retrieval spans that name this source, across our agents only.

    Spans are fetched per engine project, and the only projects addressed are
    those of agents in this workspace — a source can never be described using
    another tenant's telemetry. Both the number of projects and the number of
    spans per project are capped; the scan reports what it covered so a caller
    can say the sample was limited rather than imply it was exhaustive.
    """
    engine = client or get_engine_client()
    pairs = await _agent_projects(session, principal)
    scan = SpanScan(projects_total=len({project for _agent, project in pairs}))
    if not pairs:
        return scan

    now = _now()
    since = now - dt.timedelta(days=window_days)
    identifiers = _identifiers(source)

    selected: list[tuple[str, str]] = []
    seen_projects: set[str] = set()
    for agent, project in pairs:
        if project in seen_projects:
            continue
        seen_projects.add(project)
        selected.append((agent, project))
        if len(selected) >= MAX_SCAN_PROJECTS:
            break

    payloads = await asyncio.gather(
        *(
            _call(
                engine.search_spans(
                    project_name=project,
                    from_time=since,
                    to_time=now,
                    limit=MAX_SPANS_PER_PROJECT,
                    truncate=False,
                ),
                action="reading retrieval spans",
            )
            for _agent, project in selected
        )
    )

    scan.projects_scanned = len(selected)
    for (agent, _project), spans in zip(selected, payloads, strict=True):
        for span in spans or []:
            if isinstance(span, dict) and _references(span, identifiers):
                _absorb(scan, span, agent)
    return scan


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
        measured=any(dimension.sample_size for dimension in dimensions) or bool(overall),
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

    source = KnowledgeSource(
        workspace_id=principal.workspace_id,
        name=payload.name,
        source_type=payload.source_type.value,
        environment=payload.environment.value,
        status=(
            KnowledgeSourceStatus.SYNCING.value
            if payload.start_sync
            else KnowledgeSourceStatus.PAUSED.value
        ),
        document_count=payload.document_count,
        chunk_count=payload.chunk_count,
        grounding_score=None,
        sensitivity=payload.sensitivity.value,
        has_acl=payload.has_acl,
        acl_summary=payload.acl_summary,
        last_sync_at=None,
        sync_progress=0 if payload.start_sync else 100,
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
        sync=SyncState(state=SyncStage.QUEUED if payload.start_sync else None),
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


async def sync_status(
    session: AsyncSession, principal: Principal, source_id: str
) -> KnowledgeSyncStatus:
    """What the console's percentage bar polls."""
    source = await get_source(session, principal, source_id)
    return sync_status_of(source)


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
        async with get_sessionmaker()() as session:
            source = await self._load(session, source_id, workspace_id)
            if source is None:
                return "", False
            name = source.name
            settings = _settings(source)
            if not source.index_name and not settings.location:
                await session.rollback()
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
            principal = _system_principal(workspace_id)
            scan = await scan_retrieval_spans(session, principal, source)
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
                if source is None:
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

    Scheduled with ``BackgroundTasks`` so it fires after the request's
    transaction has committed and the job's own session sees the Syncing row.
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
    "EXPORT_COLUMNS",
    "SpanScan",
    "SyncRunner",
    "create_source",
    "delete_source",
    "export_sources",
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
