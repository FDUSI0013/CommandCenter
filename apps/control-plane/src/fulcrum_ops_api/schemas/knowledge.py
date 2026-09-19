"""Wire contracts for RAG & Knowledge Governance.

A knowledge source is one indexed corpus an agent may ground on. The row is
ours; the evidence about how well it grounds is the telemetry engine's, read
back from the feedback scores recorded against retrieval spans.

**Where the extra fields live.** ``knowledge_sources`` has no column for the
operator-facing description, the source location, the sync schedule or the
indexer name, and this API may not alter the model. Those attributes are
therefore persisted inside the row's one free-form JSON column,
``retrieval_policy``, under two reserved keys: ``source`` for the descriptive
and scheduling settings and ``sync`` for the state of the running sync job. The
retrieval settings themselves (top-k, minimum score, ACL enforcement, reranker,
filters) stay at the top level of that column where they belong. Both reserved
blocks are declared here so the shape is a contract rather than a convention.

**What the numbers mean.** ``document_count`` and ``chunk_count`` are the
inventory whoever runs the indexer reported. The ``observed_*`` fields are what
retrieval telemetry actually saw in the window, which is a different thing and
is named differently for that reason. Grounding scores are averaged from real
feedback scores; a source nothing has retrieved from reports null, never zero.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.registry import (
    EnvironmentType,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    Sensitivity,
)

#: Reserved keys inside ``KnowledgeSource.retrieval_policy``.
SOURCE_SETTINGS_KEY: Final[str] = "source"
SYNC_STATE_KEY: Final[str] = "sync"

#: Feedback score names this domain understands, mapped onto the four bars the
#: inspector's Grounding & Quality panel renders. Matching is case-insensitive
#: and ignores separators, so "Context Relevance" and "context_relevance" are
#: the same score. Every dimension reads high-is-good. "hallucination" is the
#: one alias a judge emits the other way round (1.0 = hallucinated); the
#: service turns it over before averaging, using the same list of inverted
#: judges the evaluations domain keeps. "grounded" is the name this product's
#: own user guide tells people to score with.
GROUNDING_DIMENSIONS: Final[dict[str, tuple[str, ...]]] = {
    "context_relevance": ("contextrelevance", "contextprecision", "retrievalrelevance"),
    "answer_relevance": ("answerrelevance", "responserelevance"),
    "citation_quality": ("citationquality", "citationaccuracy", "attribution"),
    "completeness": ("completeness", "answercompleteness", "coverage", "contextrecall"),
    "grounding": ("grounding", "grounded", "groundedness", "faithfulness", "hallucination"),
}

#: Stages a sync walks, with the progress percentage each one ends at. The
#: console's percentage bar polls ``/sync-status`` and reads these.
SYNC_STAGES: Final[tuple[tuple[str, int], ...]] = (
    ("Verifying source", 5),
    ("Reading retrieval telemetry", 40),
    ("Counting indexed chunks", 65),
    ("Scoring grounding quality", 90),
    ("Publishing index state", 100),
)

DEFAULT_WINDOW_DAYS: Final[int] = 30


class AclEnforcement(enum.StrEnum):
    """How the retriever treats the caller's access rights."""

    ENFORCED = "Enforced"
    ENFORCED_WITH_AUDIT = "Enforced + audit"
    NOT_REQUIRED = "Not required"


class IndexingStatus(enum.StrEnum):
    """Derived from the row's status and sync progress; never stored."""

    UP_TO_DATE = "Up to date"
    BUILDING = "Building"
    STALE = "Stale"
    FAILED = "Failed"


class SyncStage(enum.StrEnum):
    """Where a running sync has got to."""

    QUEUED = "Queued"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


#: Schemes a browser would run rather than fetch. The location is only ever
#: displayed, but the console may render it as a link.
_SCRIPT_SCHEMES: Final[frozenset[str]] = frozenset({"javascript", "data", "vbscript"})
_SCHEME: Final[re.Pattern[str]] = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
#: A browser drops tabs and line breaks from a URL before reading its scheme.
_IGNORED_IN_SCHEME: Final[re.Pattern[str]] = re.compile(r"[\t\r\n]+")


def _absolute_or_scheme_free(value: str) -> str:
    """Accept any URL or path; refuse only what a browser would execute.

    This used to allow a short list of URL schemes, and a URL parser calls
    whatever precedes the first colon a scheme. So it refused ``Z:\\Policies``
    (a File Share), ``wasbs://`` and ``gs://`` (Blob Storage),
    ``sharepoint://``, ``jdbc:`` and ``postgresql://`` (Database) and
    ``localhost:9200/idx`` (Vector Index) -- every source type the dialog
    offers beside the web -- with a message that ended "or a plain path".
    Stored blocks are validated again on every read, so this rule must never
    be made stricter than it is.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("must not be blank")
    scheme = _SCHEME.match(_IGNORED_IN_SCHEME.sub("", trimmed))
    if scheme and scheme.group(1).lower() in _SCRIPT_SCHEMES:
        raise ValueError("location must be a URL or a path, not a script")
    return trimmed


# ---------------------------------------------------------------------------
# The two blocks stored inside retrieval_policy
# ---------------------------------------------------------------------------


class RetrievalPolicy(BaseModel):
    """Retrieval settings — the Retrieval Policies tab renders these."""

    name: str | None = Field(None, max_length=160)
    top_k: int | None = Field(None, ge=1, le=200)
    min_score: float | None = Field(None, ge=0.0, le=1.0)
    acl_enforcement: AclEnforcement | None = None
    reranker: str | None = Field(None, max_length=120)
    filters: dict[str, Any] = Field(default_factory=dict)


class SourceSettings(BaseModel):
    """Descriptive and scheduling attributes the table and inspector show."""

    description: str | None = Field(None, max_length=2000)
    location: str | None = Field(
        None, max_length=1000, description="Where the corpus lives: URL or path."
    )
    indexer: str | None = Field(None, max_length=120, description="Service that indexes it.")
    sync_frequency_minutes: int | None = Field(
        None, ge=1, le=525_600, description="Interval the scheduler is meant to sync on."
    )
    indexing_errors: int = Field(0, ge=0, description="Errors seen by the last sync.")
    observed_documents: int | None = Field(
        None,
        description=(
            "Distinct documents retrieval telemetry saw for this index in the last "
            "sync's window. Not the corpus size."
        ),
    )
    observed_chunks: int | None = Field(
        None, description="Distinct chunks retrieval telemetry saw in the same window."
    )

    @field_validator("location")
    @classmethod
    def _location(cls, value: str | None) -> str | None:
        return None if value is None else _absolute_or_scheme_free(value)


class SyncState(BaseModel):
    """The persisted state of the most recent sync job."""

    job_id: str | None = None
    stage: str | None = Field(None, description="Human name of the stage in flight.")
    stage_index: int = 0
    stage_count: int = len(SYNC_STAGES)
    state: SyncStage | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error: str | None = None
    documents_seen: int | None = None
    chunks_seen: int | None = None
    projects_scanned: int = 0
    spans_sampled: int = 0
    started_by: str | None = None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class KnowledgeSourceRead(BaseModel):
    """One row of the RAG & Knowledge Governance table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_type: KnowledgeSourceType
    environment: EnvironmentType
    status: KnowledgeSourceStatus

    document_count: int = Field(
        0, description="Corpus size as reported by whoever runs the indexer."
    )
    chunk_count: int = Field(0, description="Chunk count as reported by the indexer.")
    grounding_score: float | None = Field(
        None, description="0-1, averaged from feedback scores on this index's retrieval spans."
    )

    sensitivity: Sensitivity
    has_acl: bool = False
    acl_summary: str | None = None

    last_sync_at: dt.datetime | None = None
    sync_progress: int = Field(100, ge=0, le=100)
    indexing_status: IndexingStatus = Field(
        IndexingStatus.UP_TO_DATE, description="Derived from status and sync progress."
    )
    next_sync_at: dt.datetime | None = Field(
        None, description="Last sync plus the configured interval; null when unscheduled."
    )

    owner_user_id: str | None = None
    owner_name: str | None = None
    owner_team: str | None = None

    index_name: str | None = None
    embedding_model: str | None = None
    chunk_size: int | None = Field(None, description="Average chunk size in tokens.")
    chunk_overlap: int | None = None

    retrieval_policy: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    settings: SourceSettings = Field(default_factory=SourceSettings)
    sync: SyncState = Field(default_factory=SyncState)

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class KnowledgeDocumentRead(BaseModel):
    """One document the View Documents modal lists.

    Rows are built from retrieval telemetry: these are the documents agents
    actually retrieved from this index in the window, with the chunk count and
    the last retrieval time observed for each. A document nobody has retrieved
    does not appear, because nothing in this system has seen it.
    """

    document_id: str
    title: str | None = None
    chunks: int = Field(0, description="Distinct chunks of this document seen retrieved.")
    last_retrieved_at: dt.datetime | None = None
    avg_retrieval_score: float | None = None
    retrieved_by: list[str] = Field(
        default_factory=list, description="Agents that retrieved it in the window."
    )
    sensitivity: Sensitivity
    status: str = Field("Indexed", description="Indexing state as far as retrieval shows.")


class GroundingDimension(BaseModel):
    """One bar of the Grounding & Quality panel."""

    name: str
    label: str
    score: float | None = Field(None, description="0-1 mean of the feedback scores seen.")
    sample_size: int = Field(0, description="Feedback scores the mean was taken over.")


class GroundingBreakdown(BaseModel):
    """Grounding quality for one source, from feedback scores on retrieval spans."""

    source_id: str
    overall: float | None = None
    dimensions: list[GroundingDimension] = Field(default_factory=list)
    spans_sampled: int = 0
    projects_scanned: int = 0
    projects_total: int = 0
    window_days: int
    measured: bool = Field(
        description="False when no retrieval span for this index carried a score."
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class KnowledgeSourceCreate(BaseModel):
    """Add a knowledge source. The initial sync starts unless it is suppressed."""

    name: str = Field(min_length=1, max_length=160)
    source_type: KnowledgeSourceType
    environment: EnvironmentType = EnvironmentType.PRODUCTION
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    has_acl: bool = Field(
        True, description="ACL-aware crawling; mandatory above Internal sensitivity."
    )
    acl_summary: str | None = Field(None, max_length=2000)

    document_count: int = Field(0, ge=0, description="Corpus size, if the indexer knows it.")
    chunk_count: int = Field(0, ge=0)
    index_name: str | None = Field(None, max_length=160)
    embedding_model: str | None = Field(None, max_length=80)
    chunk_size: int | None = Field(None, ge=1, le=100_000)
    chunk_overlap: int | None = Field(None, ge=0, le=100_000)
    owner_user_id: str | None = Field(None, max_length=36)

    retrieval_policy: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    settings: SourceSettings = Field(default_factory=SourceSettings)
    start_sync: bool = Field(True, description="Begin the first sync immediately.")

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class KnowledgeSourceUpdate(BaseModel):
    """Partial update. Every field is optional; omitted fields are left alone."""

    name: str | None = Field(None, min_length=1, max_length=160)
    source_type: KnowledgeSourceType | None = None
    environment: EnvironmentType | None = None
    sensitivity: Sensitivity | None = None
    has_acl: bool | None = None
    acl_summary: str | None = Field(None, max_length=2000)
    status: KnowledgeSourceStatus | None = Field(
        None, description="Pause or resume a source; Syncing is set by the sync job only."
    )

    document_count: int | None = Field(None, ge=0)
    chunk_count: int | None = Field(None, ge=0)
    index_name: str | None = Field(None, max_length=160)
    embedding_model: str | None = Field(None, max_length=80)
    chunk_size: int | None = Field(None, ge=1, le=100_000)
    chunk_overlap: int | None = Field(None, ge=0, le=100_000)
    owner_user_id: str | None = Field(None, max_length=36)

    retrieval_policy: RetrievalPolicy | None = None
    settings: SourceSettings | None = None
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class KnowledgeSyncStatus(BaseModel):
    """What ``GET /knowledge/{id}/sync-status`` reports while the bar is moving."""

    source_id: str
    name: str
    status: KnowledgeSourceStatus
    progress: int = Field(ge=0, le=100)
    stage: str | None = None
    stage_index: int = 0
    stage_count: int = len(SYNC_STAGES)
    state: SyncStage | None = None
    running: bool
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    last_sync_at: dt.datetime | None = None
    error: str | None = None
    documents_seen: int | None = None
    chunks_seen: int | None = None
    projects_scanned: int = 0
    spans_sampled: int = 0


class KnowledgeActionResponse(BaseModel):
    """Uniform answer for the verbs: the row as it now stands, plus a message."""

    source: KnowledgeSourceRead
    message: str
    sync: KnowledgeSyncStatus | None = None


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class KnowledgeSummary(BaseModel):
    """The six KPI cards above the table.

    Counts and sums are SQL aggregates over the workspace's sources; the average
    grounding score is the mean of the scores those sources last measured, and
    is null when none of them has been scored yet.
    """

    total_sources: int
    active_sources: int
    syncing_sources: int
    failed_sources: int
    paused_sources: int
    active_percent: float

    total_documents: int
    total_chunks: int
    avg_grounding_score: float | None = None
    sources_scored: int = Field(0, description="Sources the average was taken over.")

    sources_with_acl: int
    acl_percent: float
    created_30d: int
    window_days: int
