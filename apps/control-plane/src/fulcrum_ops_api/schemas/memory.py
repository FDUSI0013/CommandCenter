"""Wire contracts for Memory & State Management.

A memory store is our registry row: what kind of store it is, which environment
it serves, who owns it, how long its contents may legally be kept. The records
themselves are not ours — conversation and session stores are backed by the
telemetry engine's trace threads, and every other store type is held by the
backend named in ``backend``, which the server governs but does not
address.

That split is visible in this module: the *store* schemas are ordinary CRUD
contracts over columns we own, while the *record*, *session*, *conversation* and
*backup* schemas describe what the engine reported when it was asked. Fields the
engine does not report are typed optional and left null rather than filled in.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.registry import EnvironmentType, MemoryStoreStatus, MemoryStoreType

#: Retention periods the console's dropdown offers, label -> days. Any whole
#: number of days up to :data:`MAX_RETENTION_DAYS` is accepted; these are the
#: presets, and the label for a preset is reused verbatim so the table keeps
#: rendering the string operators recognise.
RETENTION_CHOICES: Final[dict[str, int]] = {
    "1 day": 1,
    "7 days": 7,
    "15 days": 15,
    "30 days": 30,
    "90 days": 90,
    "120 days": 120,
    "180 days": 180,
    "365 days": 365,
}

#: Ten years. Longer than this is a legal-hold decision, not a retention policy.
MAX_RETENTION_DAYS: Final[int] = 3650

#: Store types whose records live in the telemetry engine as trace threads.
#: Only these can be browsed, purged and backed up through this API; the rest
#: are governed here and operated by their own backend.
THREAD_BACKED_TYPES: Final[frozenset[str]] = frozenset(
    {MemoryStoreType.CONVERSATION.value, MemoryStoreType.SESSION.value}
)

#: A session counts as active while it has been touched inside this window.
ACTIVE_SESSION_WINDOW_HOURS: Final[int] = 24

#: Below this, a session is still live rather than merely recent.
LIVE_SESSION_WINDOW_MINUTES: Final[int] = 30


def retention_label(days: int) -> str:
    """The label a bare day count is displayed under."""
    for label, value in RETENTION_CHOICES.items():
        if value == days:
            return label
    return "1 day" if days == 1 else f"{days} days"


# ---------------------------------------------------------------------------
# Vocabularies owned by the API rather than by a column
# ---------------------------------------------------------------------------


class SessionState(enum.StrEnum):
    """Liveness of one conversation thread, as the Sessions tab renders it."""

    ACTIVE = "Active"
    IDLE = "Idle"
    CLOSED = "Closed"


class SyncState(enum.StrEnum):
    """Whether an agent's state store is keeping up, on the Agent State tab."""

    SYNCED = "Synced"
    PAUSED = "Paused"
    DEGRADED = "Degraded"
    NOT_PROVISIONED = "Not Provisioned"


#: What a backup's manifest holds, and therefore everything a restore can put
#: back. Conversation threads are not on this list: they are counted when the
#: snapshot is taken and never copied, so no restore can bring one back.
BACKUP_CAPTURES: Final[tuple[str, ...]] = ("retention_policy", "retention_days", "status")


class BackupKind(enum.StrEnum):
    """Every snapshot taken now is Full; Incremental survives on older ledger rows."""

    FULL = "Full"
    INCREMENTAL = "Incremental"


class BackupStatus(enum.StrEnum):
    """Partial means the export hit the per-run row cap before it ran out."""

    COMPLETED = "Completed"
    PARTIAL = "Partial"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class MemoryStoreRead(BaseModel):
    """One row of the Memory Stores table, plus what the inspector adds to it.

    Where the four measurements come from depends on who holds the records.

    * A thread-backed store's ``record_count`` and ``active_session_count`` are
      read live from the telemetry store, over the agents bound to it. They are
      null when that read failed — not measured, which is not zero. Its
      ``usage_percent`` is always null: the telemetry store has no notion of
      provisioned capacity, so there is no share of it to report.
    * Every other type reports what its own runtime last sent to
      ``PATCH /memory/{id}``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    store_type: MemoryStoreType
    environment: str
    status: MemoryStoreStatus

    usage_percent: int | None = Field(None, ge=0, le=100)
    record_count: int | None = None
    retention_policy: str | None = None
    retention_days: int | None = None

    last_updated_at: dt.datetime | None = None
    owner_user_id: str | None = None
    owner_name: str | None = Field(None, description="Resolved from the owning user")
    owner_team: str | None = None
    backend: str | None = Field(
        None, description="System holding the records, e.g. 'Azure AI Search'"
    )

    active_session_count: int | None = None
    avg_retrieval_latency_ms: float | None = None
    last_backup_at: dt.datetime | None = None
    backup_count: int = Field(0, description="Backups taken of this store, all time")
    thread_backed: bool = Field(
        False, description="True when records are engine trace threads"
    )
    bound_agents: int | None = Field(
        None,
        description=(
            "Agents whose memory policy names this store. They are the whole scope "
            "of its records, purge and backup; null for stores that are not thread-backed"
        ),
    )

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None

    @classmethod
    def from_model(
        cls,
        store: Any,
        *,
        owner_name: str | None = None,
        owner_team: str | None = None,
        backup_count: int = 0,
        live: tuple[int | None, int | None, int] | None = None,
    ) -> MemoryStoreRead:
        """Build a row, folding in what is not on the model.

        ``live`` is ``(records, active sessions, bound agents)`` for a
        thread-backed store. Its columns are never written by anything, so they
        are not consulted: the row says what the telemetry store said, or null.
        """
        thread_backed = store.store_type in THREAD_BACKED_TYPES
        records, sessions, bound = live if live is not None else (None, None, None)
        return cls(
            id=store.id,
            name=store.name,
            store_type=MemoryStoreType(store.store_type),
            environment=store.environment,
            status=MemoryStoreStatus(store.status),
            usage_percent=None if thread_backed else store.usage_percent,
            record_count=records if thread_backed else store.record_count,
            retention_policy=store.retention_policy,
            retention_days=store.retention_days,
            last_updated_at=store.last_updated_at,
            owner_user_id=store.owner_user_id,
            owner_name=owner_name,
            owner_team=owner_team,
            backend=store.backend,
            active_session_count=sessions if thread_backed else store.active_session_count,
            avg_retrieval_latency_ms=store.avg_retrieval_latency_ms,
            last_backup_at=store.last_backup_at,
            backup_count=backup_count,
            thread_backed=thread_backed,
            bound_agents=bound if thread_backed else None,
            created_at=store.created_at,
            updated_at=store.updated_at,
            created_by=store.created_by,
            updated_by=store.updated_by,
        )


class MemoryRecordRead(BaseModel):
    """One stored memory, as the View Records dialog lists it.

    ``size_bytes`` and ``context_tokens`` are only present when the engine
    reported usage for the thread; they are never estimated.
    """

    record_key: str = Field(description="Stable key of the record in its store")
    session_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    message_count: int = 0
    context_tokens: int | None = None
    size_bytes: int | None = None
    created_at: dt.datetime | None = None
    last_activity_at: dt.datetime | None = None
    expires_at: dt.datetime | None = Field(
        None, description="Last activity plus the store's retention window"
    )
    retention_policy: str | None = None
    state: SessionState


class MemorySessionRead(BaseModel):
    """One live session on the Sessions tab."""

    session_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    user: str | None = None
    turns: int = 0
    started_at: dt.datetime | None = None
    last_activity_at: dt.datetime | None = None
    duration_ms: int | None = None
    state: SessionState


class MemoryConversationRead(BaseModel):
    """One conversation on the Conversation State tab."""

    conversation_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    messages: int = 0
    context_tokens: int | None = None
    retention_policy: str | None = None
    last_activity_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None


class AgentStateRead(BaseModel):
    """One row of the Agent State tab: which store an agent checkpoints into."""

    agent_id: str
    agent_name: str
    environment: str
    state_store: str | None = Field(
        None, description="Memory store the agent's state policy names, when it resolves"
    )
    memory_policy: str | None = None
    session_count: int | None = Field(
        None, description="Engine threads in the agent's project; null when unprovisioned"
    )
    last_activity_at: dt.datetime | None = None
    sync_state: SyncState


class RetentionPolicyRead(BaseModel):
    """One row of the Retention Policies tab: a policy and everything under it."""

    policy: str
    retention_days: int | None = None
    applies_to: list[str] = Field(
        default_factory=list, description="Store types governed by this policy"
    )
    store_count: int = 0
    record_count: int | None = Field(
        None,
        description=(
            "Records under the policy: threads counted live for its conversation and "
            "session stores, plus what its other stores' runtimes reported. Null when "
            "the telemetry store could not be read — not measured, which is not zero"
        ),
    )
    stores: list[str] = Field(default_factory=list)
    status: str = Field("Active", description="Active while any store still uses it")


class MemoryBackupRead(BaseModel):
    """One backup on the Backups tab.

    Backups are recorded on the append-only audit trail rather than in a table
    of their own, so ``id`` is the audit event id and the record cannot be
    edited or deleted after the fact.

    A backup is a snapshot of the store's *governed state* — its retention
    policy and status — with a count of the conversation threads its agents held
    at that moment. The threads themselves are not copied anywhere, which is
    what ``threads_captured`` says on every row.
    """

    id: str
    store_id: str
    store_name: str | None = None
    created_at: dt.datetime
    created_by: str | None = None
    kind: BackupKind
    status: BackupStatus
    record_count: int = Field(
        0, description="Threads counted when the snapshot was taken. Counted, not copied"
    )
    payload_bytes: int | None = Field(
        None,
        description=(
            "Null: a snapshot exports nothing, so there is no payload to size. Older "
            "rows carry the size of a listing that was measured and then discarded"
        ),
    )
    window_from: dt.datetime | None = Field(
        None, description="Start of the counted window on older rows; null otherwise"
    )
    projects: int = Field(0, description="Engine projects the count spanned")
    threads_captured: bool = Field(
        False, description="Always false: no backup holds conversation threads"
    )
    captured: list[str] = Field(
        default_factory=lambda: list(BACKUP_CAPTURES),
        description="The store fields the snapshot holds, which is all a restore can put back",
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class MemoryStoreCreate(BaseModel):
    """Provision a store. It starts empty, so counters are not accepted."""

    name: str = Field(min_length=1, max_length=160)
    store_type: MemoryStoreType
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    backend: str | None = Field(None, max_length=80)
    retention_days: int = Field(90, ge=1, le=MAX_RETENTION_DAYS)
    retention_policy: str | None = Field(
        None, max_length=48, description="Display label; derived from the days when omitted"
    )
    owner_user_id: str | None = Field(None, max_length=36)

    @field_validator("name", "backend", "retention_policy")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class MemoryStoreUpdate(BaseModel):
    """Partial update.

    Retention is deliberately absent: it moves only through
    ``PUT /memory/{id}/retention``, so every change to a deletion SLA lands in
    the audit trail under one action.
    """

    name: str | None = Field(None, min_length=1, max_length=160)
    store_type: MemoryStoreType | None = None
    environment: EnvironmentType | None = None
    status: MemoryStoreStatus | None = None
    backend: str | None = Field(None, max_length=80)
    owner_user_id: str | None = Field(None, max_length=36)

    # Operational counters the store's runtime reports back.
    usage_percent: int | None = Field(None, ge=0, le=100)
    record_count: int | None = Field(None, ge=0)
    active_session_count: int | None = Field(None, ge=0)
    avg_retrieval_latency_ms: float | None = Field(None, ge=0)

    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name", "backend")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class RetentionPolicyUpdate(BaseModel):
    """Set the deletion SLA a store is audited against."""

    retention_days: int = Field(ge=1, le=MAX_RETENTION_DAYS)
    retention_policy: str | None = Field(
        None, max_length=48, description="Display label; derived from the days when omitted"
    )
    reason: str | None = Field(
        None, max_length=500, description="Recorded on the audit row for the change"
    )

    @model_validator(mode="after")
    def _label(self) -> RetentionPolicyUpdate:
        label = (self.retention_policy or "").strip()
        self.retention_policy = label or retention_label(self.retention_days)
        return self


class MemoryPurgeRequest(BaseModel):
    """Run the retention purge. The window comes from the policy, not the body.

    A purge cannot be undone, so an empty body does not start one: the caller
    names the store it means to purge in ``confirm``. The intended exchange is a
    ``dry_run`` first — which needs no confirmation and answers with what would
    go and whose it is — and then the real call carrying the store's name.
    """

    reason: str | None = Field(None, max_length=500)
    dry_run: bool = Field(
        False, description="Count what is past retention without deleting anything"
    )
    confirm: str | None = Field(
        None,
        max_length=160,
        description=(
            "The store's exact name. Required unless dry_run is set; a purge whose "
            "confirm does not match the store is refused with 422 and deletes nothing."
        ),
    )


class MemoryBackupRequest(BaseModel):
    """Take a snapshot of the store's governed state."""

    full: bool = Field(
        False,
        description="Accepted for compatibility and ignored: every snapshot is complete",
    )
    note: str | None = Field(None, max_length=500)


class MemoryRestoreRequest(BaseModel):
    """Restore the governed state captured by one backup."""

    backup_id: str = Field(min_length=1, max_length=36)
    reason: str | None = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Action results
# ---------------------------------------------------------------------------


class MemoryPurgeResult(BaseModel):
    """What the purge actually removed, and whose it was.

    A record is a conversation thread. One is past retention when its *last*
    activity is older than the cutoff, and it is removed whole; a thread that is
    still being written to keeps its early messages however old they are.
    """

    store_id: str
    name: str
    dry_run: bool
    cutoff: dt.datetime = Field(description="Records last active before this were in scope")
    retention_days: int
    candidate_records: int = Field(
        0, description="Records this run found past retention, deleted or not"
    )
    purged_records: int = Field(description="Records actually deleted; always 0 on a dry run")
    purged_traces: int = Field(0, description="Traces those records were made of")
    remaining_records: int | None = Field(
        None,
        description=(
            "Records the store's agents still hold, counted after the run; null when "
            "the telemetry store could not say"
        ),
    )
    usage_percent: int | None = Field(
        None, description="Always null here: the telemetry store reports no capacity figure"
    )
    projects: int = Field(0, description="Engine projects the purge swept")
    agents: list[str] = Field(
        default_factory=list,
        description="Agents bound to the store; only their records were in scope",
    )
    capped: bool = Field(
        False,
        description="True when the per-run cap or the time budget stopped the sweep; run it again",
    )
    partial: bool = Field(
        False,
        description=(
            "True when the telemetry store failed part way: what is reported here was "
            "deleted and audited, the rest was not reached"
        ),
    )
    message: str = Field("", description="The outcome in one sentence, for a toast")


class MemoryBackupResult(BaseModel):
    """What the snapshot captured — and, as plainly, what it did not."""

    backup_id: str
    store_id: str
    name: str
    kind: BackupKind
    status: BackupStatus
    record_count: int = Field(
        description="Threads the store's agents held at that moment. Counted, not copied"
    )
    payload_bytes: int | None = Field(
        None, description="Always null: a snapshot exports nothing, so there is nothing to size"
    )
    window_from: dt.datetime | None = None
    created_at: dt.datetime
    projects: int = 0
    agents: list[str] = Field(
        default_factory=list, description="Agents bound to the store, whose threads were counted"
    )
    threads_captured: bool = Field(
        False, description="Always false: conversation threads are not copied by a backup"
    )
    captured: list[str] = Field(
        default_factory=lambda: list(BACKUP_CAPTURES),
        description="The store fields the snapshot holds, which is all a restore can put back",
    )
    notice: str = Field("", description="What was and was not captured, in a sentence")


class MemoryRestoreResult(BaseModel):
    """What the restore put back — governed state, never conversation threads."""

    backup_id: str
    store_id: str
    name: str
    restored_at: dt.datetime
    record_count: int | None = Field(
        None,
        description=(
            "Threads the store's agents hold now, read live. A restore does not "
            "change it; null when the telemetry store could not say"
        ),
    )
    usage_percent: int | None = Field(
        None, description="Always null here: a restore does not touch measurements"
    )
    retention_policy: str | None = None
    status: MemoryStoreStatus
    fields_restored: list[str] = Field(default_factory=list)
    threads_restored: bool = Field(
        False,
        description=(
            "Always false: a backup holds no threads, so records deleted since it "
            "was taken stay deleted"
        ),
    )
    notice: str = Field("", description="What was and was not restored, in a sentence")


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------


class MemoryCountSlice(BaseModel):
    """One bucket of a grouped count on the summary."""

    label: str
    count: int
    percent: float = Field(description="Share of all stores, 0-100, one decimal")


class MemorySummary(BaseModel):
    """The six KPI cards above the table, plus the breakdowns beside them."""

    stores: int = Field(description="Total memory stores in the workspace")
    active_sessions: int = Field(
        description="Engine threads touched inside the active-session window"
    )
    active_session_window_hours: int = ACTIVE_SESSION_WINDOW_HOURS
    stored_memories: int = Field(
        description=(
            "Records across every store: conversation threads counted live for "
            "thread-backed stores, plus what the other stores' runtimes reported"
        )
    )
    avg_retrieval_latency_ms: float | None = None
    state_sync_success_percent: float = Field(
        description=(
            "Share of stores in the Active state; paused and degraded stores are not syncing"
        )
    )
    expired_purged_records: int = Field(
        description="Records removed by retention purges inside the purge window"
    )
    purge_window_days: int
    purge_runs: int = 0

    active_stores: int = 0
    paused_stores: int = 0
    degraded_stores: int = 0
    avg_usage_percent: float | None = None
    stores_over_capacity_threshold: int = Field(
        0, description="Stores above the usage level the table flags amber"
    )
    thread_backed_stores: int = 0
    last_backup_at: dt.datetime | None = None
    backups_in_window: int = 0

    by_type: list[MemoryCountSlice] = Field(default_factory=list)
    by_status: list[MemoryCountSlice] = Field(default_factory=list)
    by_environment: list[MemoryCountSlice] = Field(default_factory=list)
