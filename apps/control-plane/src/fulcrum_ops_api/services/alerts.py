"""Alert triage: raising with flood control, acknowledgement, and closure.

Two things in here matter more than the CRUD around them.

**Dedupe.** A misbehaving integration does not raise one alert, it raises one a
second. :func:`raise_alert` collapses those onto a single row keyed by
``dedupe_key``, bumping ``occurrence_count`` and ``last_occurred_at`` instead of
inserting, so the Alerts screen stays readable during an incident. Every other
domain raises its alerts through that function.

**Timings.** Acknowledging stamps ``acknowledged_at`` once and never again — the
first human response is what MTTA measures. Resolving freezes ``mttr_seconds``
from ``raised_at`` so the KPI never re-derives from timestamps that later move.

**Rules.** The screens decide *when* something is wrong; a rule decides what the
workspace wants done about it. :func:`raise_alert` is the one place a rule is
read: an enabled rule for the alert's source and severity is recorded on the
alert it governs, and a rule an admin has switched off silences that source at
that severity — the condition is still written down, as a Muted alert, because a
row that was never written cannot be found again when the rule comes back. No
notification is delivered from here: a rule's channels are labels with no
address behind them, and the alert says nothing about having told anybody.

Nothing here imports FastAPI beyond the ``Request`` that the audit trail records
the caller's address from; HTTP concerns live in ``api.v1.alerts``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from sqlalchemy import Select, case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..db.base import new_id
from ..models.identity import Membership, Role, User
from ..models.operations import Alert, AlertRule, AlertSeverity, AlertStatus
from ..schemas.alerts import (
    AlertAcknowledgeAllRequest,
    AlertAcknowledgeRequest,
    AlertAssignRequest,
    AlertCreate,
    AlertMuteRequest,
    AlertResolveRequest,
    AlertRuleCreate,
    AlertRuleUpdate,
    AlertsSummary,
    AlertUpdate,
)
from . import audit

log = logging.getLogger(__name__)

SCREEN = "Alerts"

#: Statuses that still represent a live condition. A muted alert is included on
#: purpose: muting silences notifications, so recurrences must keep landing on
#: the muted row rather than escaping as new alerts.
LIVE_STATUSES: tuple[str, ...] = (
    AlertStatus.OPEN.value,
    AlertStatus.INVESTIGATING.value,
    AlertStatus.ACKNOWLEDGED.value,
    AlertStatus.MUTED.value,
)

#: Statuses ``acknowledge-all`` sweeps: the two that mean "nobody has taken this yet".
UNACKED_STATUSES: tuple[str, ...] = (
    AlertStatus.OPEN.value,
    AlertStatus.INVESTIGATING.value,
)

#: Only these two may be set through ``PATCH``; the rest carry timings that the
#: dedicated verbs are responsible for stamping.
PATCHABLE_STATUSES: tuple[str, ...] = (
    AlertStatus.OPEN.value,
    AlertStatus.INVESTIGATING.value,
)

#: Payload key that marks an alert a switched-off rule silenced. It is what tells
#: such a mute from an operator's, which carries a ``muted_until`` deadline.
RULE_MUTE_KEY = "silenced_by_rule_id"

#: Everything a rule's mute writes onto the payload, taken off again when it lifts.
_RULE_MUTE_KEYS: tuple[str, ...] = (RULE_MUTE_KEY, "muted_by", "mute_reason")

#: How many times a raise reads the next reference again after another raise
#: took it between the read and the insert, before settling for an opaque one.
REF_ALLOCATION_ATTEMPTS = 5

#: First key of the two-int advisory lock the raises of one condition take turns
#: on. The audit chain and the memory purge use namespaces of their own, and the
#: scheduler's tick lock is the single-bigint form, so none of them can collide.
CONDITION_LOCK_NAMESPACE = 0x414C5254  # "ALRT"

#: How long a raise waits for its turn before it goes ahead without one. The
#: lock is held until the holder commits, and the holder can be a screen's whole
#: edit; giving up costs at worst a second live row for the condition, which the
#: dedupe lookup tolerates. An alert that is late is worth more than none.
CONDITION_LOCK_WAIT_SECONDS = 3.0
CONDITION_LOCK_POLL_SECONDS = 0.025

#: Hard ceiling on a CSV export so one click cannot pull an unbounded table.
MAX_EXPORT_ROWS = 10_000

#: Column order of the Alerts CSV export: (attribute, header).
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("alert_ref", "Alert ID"),
    ("severity", "Severity"),
    ("title", "Alert"),
    ("description", "Description"),
    ("source", "Source"),
    ("status", "Status"),
    ("raised_at", "Raised At"),
    ("acknowledged_at", "Acknowledged At"),
    ("resolved_at", "Resolved At"),
    ("assigned_to_user_id", "Assigned To"),
    ("occurrence_count", "Occurrences"),
    ("last_occurred_at", "Last Occurred"),
    ("mttr_seconds", "MTTR (seconds)"),
    ("dedupe_key", "Dedupe Key"),
)

_SEARCH_COLUMNS = (Alert.title, Alert.description, Alert.source, Alert.alert_ref)

# Severity sorts by meaning, not alphabetically: Critical must lead and Info
# must trail, which "Critical, High, Info, Low, Medium" would not do.
_SEVERITY_RANK = case(
    (Alert.severity == AlertSeverity.CRITICAL.value, 0),
    (Alert.severity == AlertSeverity.HIGH.value, 1),
    (Alert.severity == AlertSeverity.MEDIUM.value, 2),
    (Alert.severity == AlertSeverity.LOW.value, 3),
    else_=4,
)

# Keys the console's table sends. The short forms are the column keys the table
# component uses; the long forms are the field names in the API contract.
_SORTABLE: dict[str, Any] = {
    "severity": _SEVERITY_RANK,
    "sev": _SEVERITY_RANK,
    "title": Alert.title,
    "source": Alert.source,
    "status": Alert.status,
    "raised_at": Alert.raised_at,
    "ts": Alert.raised_at,
    "last_occurred_at": Alert.last_occurred_at,
    "occurrence_count": Alert.occurrence_count,
    "acknowledged_at": Alert.acknowledged_at,
    "resolved_at": Alert.resolved_at,
    "mttr_seconds": Alert.mttr_seconds,
    "alert_ref": Alert.alert_ref,
}

_RULE_SORTABLE: dict[str, Any] = {
    "name": AlertRule.name,
    "source": AlertRule.source,
    "severity": AlertRule.severity,
    "enabled": AlertRule.enabled,
    "throttle_minutes": AlertRule.throttle_minutes,
    "created_at": AlertRule.created_at,
    "updated_at": AlertRule.updated_at,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _dialect(session: AsyncSession) -> str:
    bind = session.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "")


def _ack_latency_seconds(session: AsyncSession) -> ColumnElement[float]:
    """``acknowledged_at - raised_at`` in seconds, as a database expression.

    Timestamp arithmetic is the one place the two supported backends genuinely
    diverge: Postgres subtracts to an interval that ``EXTRACT(EPOCH …)`` turns
    into seconds, while SQLite has no interval type and needs Julian days scaled
    up. Both keep the work in the database, which is the point — the KPI must
    never load rows to average them. Rows that were never acknowledged evaluate
    to NULL and ``AVG`` skips them.
    """
    if _dialect(session) == "sqlite":
        return (
            func.julianday(Alert.acknowledged_at) - func.julianday(Alert.raised_at)
        ) * 86_400.0
    return func.extract("epoch", Alert.acknowledged_at) - func.extract(
        "epoch", Alert.raised_at
    )


async def _next_alert_ref(session: AsyncSession, workspace_id: str) -> str:
    """Allocate the next ``al-N`` reference for a workspace.

    Counting then probing keeps references dense and human-quotable. The probe
    walks past references that outlived a deleted alert; it does nothing for two
    raises that land at once, which read the same table and choose the same
    reference — :func:`raise_alert` inserts under a savepoint and asks again for
    that. After a stubborn run it falls back to an opaque suffix rather than
    blocking the raise. The suffix is the *tail* of the id: the head of a
    time-ordered id is its clock, and stays the same for a minute at a time.
    """
    used = (
        await session.execute(
            select(func.count()).select_from(Alert).where(Alert.workspace_id == workspace_id)
        )
    ).scalar_one()
    candidate = int(used) + 1
    for _ in range(50):
        ref = f"al-{candidate}"
        clash = (
            await session.execute(
                select(Alert.id)
                .where(Alert.workspace_id == workspace_id, Alert.alert_ref == ref)
                .limit(1)
            )
        ).scalar_one_or_none()
        if clash is None:
            return ref
        candidate += 1
    return _opaque_alert_ref()


def _opaque_alert_ref() -> str:
    return f"al-{new_id()[-8:]}"


async def _live_alert(
    session: AsyncSession,
    workspace_id: str,
    dedupe_key: str,
    *,
    other_than: str | None = None,
) -> Alert | None:
    """The alert a recurrence of ``dedupe_key`` lands on, if one is still live.

    ``other_than`` leaves one alert out: the row a raise has just inserted, when
    it looks again for a twin that got there first.
    """
    stmt = select(Alert).where(
        Alert.workspace_id == workspace_id,
        Alert.dedupe_key == dedupe_key,
        Alert.status.in_(LIVE_STATUSES),
    )
    if other_than is not None:
        stmt = stmt.where(Alert.id != other_than)
    return (
        await session.execute(stmt.order_by(Alert.raised_at.desc()).limit(1))
    ).scalar_one_or_none()


class _ConditionAlreadyLive(Exception):
    """Raised inside the insert's savepoint to take the insert back."""


def _condition_lock_key(workspace_id: str, dedupe_key: str) -> int:
    """The condition's half of the lock key: a stable signed 32-bit number.

    Derived here rather than by a database hash function so every worker, and
    every database version, agrees on it.
    """
    digest = hashlib.sha256(f"{workspace_id}\x00{dedupe_key}".encode()).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


async def _take_condition(session: AsyncSession, workspace_id: str, dedupe_key: str) -> None:
    """Make the raises of one condition take turns until the transaction ends.

    The dedupe lookup is a check and the insert an act, and between the two a
    second worker raising the same condition finds nothing either: two live rows
    for one condition, which is the flood the key exists to prevent. A unique
    index on the live key would refuse the second; until the schema has one,
    this lock is what stands in for it. It is keyed on the condition, not the
    workspace, so nothing waits here that the index would not also make wait.

    Transaction-scoped, so there is no unlock for a cancelled request to skip,
    and re-entrant. Under READ COMMITTED the lookup that follows takes a fresh
    snapshot, so it sees the row the previous holder has just committed.

    Polled with the try- form and bounded, the way the audit chain takes its
    turn and for the same reasons: a blocking wait needs ``lock_timeout`` to be
    bounded, and a statement that times out aborts the caller's transaction —
    the alert would be failing the edit that raised it. Polling also cannot
    deadlock two sweeps that raise the same conditions in a different order.

    SQLite has one writer at a time and no advisory locks; there, the second
    look :func:`raise_alert` takes after its insert is what finds the twin.
    """
    if _dialect(session) != "postgresql":
        return
    statement = select(
        func.pg_try_advisory_xact_lock(
            CONDITION_LOCK_NAMESPACE, _condition_lock_key(workspace_id, dedupe_key)
        )
    )
    deadline = time.monotonic() + CONDITION_LOCK_WAIT_SECONDS
    while True:
        if (await session.execute(statement)).scalar():
            return
        if time.monotonic() >= deadline:
            log.warning(
                "alert condition %r in workspace %s still being raised elsewhere after "
                "%.1fs; raising without the lock (a duplicate live row is tolerated)",
                dedupe_key,
                workspace_id,
                CONDITION_LOCK_WAIT_SECONDS,
            )
            return
        await asyncio.sleep(CONDITION_LOCK_POLL_SECONDS)


async def _absorb(
    session: AsyncSession, existing: Alert, occurred: dt.datetime, payload: dict[str, Any]
) -> Alert:
    """Fold one more occurrence onto the alert that is already live.

    The count is incremented by the database rather than read and written back:
    a flood is concurrent by nature, and two recurrences that both read 3 would
    both write 4. The instance is then brought into line with what was written,
    since the caller goes on to serialise it.
    """
    await session.execute(
        update(Alert)
        .where(Alert.id == existing.id)
        .values(
            occurrence_count=func.coalesce(Alert.occurrence_count, 1) + 1,
            last_occurred_at=occurred,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(
        existing, attribute_names=["occurrence_count", "last_occurred_at", "updated_at"]
    )
    if payload:
        _merge_metadata(existing, payload)
    await session.flush()
    return existing


async def _get_alert(session: AsyncSession, principal: Principal, alert_id: str) -> Alert:
    """Load one alert or raise :class:`NotFound`.

    The workspace predicate is part of the lookup rather than a check after it:
    an alert belonging to another tenant is indistinguishable from one that does
    not exist, which is the only answer that leaks nothing.
    """
    alert = (
        await session.execute(
            select(Alert).where(
                Alert.id == alert_id, Alert.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one_or_none()
    if alert is None:
        raise NotFound("That alert does not exist.")
    return alert


async def _get_rule(session: AsyncSession, principal: Principal, rule_id: str) -> AlertRule:
    rule = (
        await session.execute(
            select(AlertRule).where(
                AlertRule.id == rule_id, AlertRule.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotFound("That alert rule does not exist.")
    return rule


def _merge_metadata(alert: Alert, extra: dict[str, Any]) -> None:
    """Replace the JSON document wholesale so SQLAlchemy sees the change.

    Mutating a JSON column in place leaves the attribute unchanged as far as the
    unit of work is concerned, and the update is silently dropped.
    """
    alert.event_metadata = {**(alert.event_metadata or {}), **extra}


async def _rule_covering(
    session: AsyncSession, workspace_id: str, source: str, severity: str
) -> AlertRule | None:
    """The rule that speaks for alerts from ``source`` at ``severity``, if any.

    A rule and a raise are joined on the two things both of them state: the
    source screen and the severity. ``condition`` cannot be the join — it is an
    opaque document each screen words for itself — and the source alone is too
    coarse: a "Quota exceeded / Critical" rule would claim the Medium
    "approaching limit" warning as well, and switching it off would silence the
    whole screen. The label is compared without case because the rule editor
    takes it as free text.

    An enabled rule wins over a disabled one, then the name decides, so the
    answer is the same on every raise. ``None`` means nobody has written a rule
    for this pair and the screen's built-in raise stands on its own.
    """
    return (
        await session.execute(
            select(AlertRule)
            .where(
                AlertRule.workspace_id == workspace_id,
                func.lower(AlertRule.source) == source.strip().lower(),
                AlertRule.severity == severity,
            )
            .order_by(AlertRule.enabled.desc(), AlertRule.name.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _lift_rule_mutes(session: AsyncSession, workspace_id: str) -> int:
    """Reopen the alerts a switched-off rule silenced, once nothing silences them.

    Run after every rule write. A rule's mute has no deadline for the mute sweep
    to lapse, and the condition behind it may never be raised again — a quota
    alerts on the *change* to Exceeded, not on staying there — so an admin who
    switched a rule back on would otherwise be left with a queue that says
    nothing is wrong. Re-asking :func:`_rule_covering` rather than matching on
    the rule that was edited covers every way a pair stops being silenced:
    enabled, deleted, moved to another source, or outranked by a new rule.

    An operator's own mute carries ``muted_until`` and is left to run its course.
    """
    muted = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.workspace_id == workspace_id,
                    Alert.status == AlertStatus.MUTED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    now = _now().isoformat()
    still_silenced: dict[tuple[str, str], bool] = {}
    lifted = 0
    for alert in muted:
        payload = alert.event_metadata or {}
        if not payload.get(RULE_MUTE_KEY) or payload.get("muted_until"):
            continue
        pair = (alert.source.strip().lower(), alert.severity)
        if pair not in still_silenced:
            rule = await _rule_covering(session, workspace_id, alert.source, alert.severity)
            still_silenced[pair] = rule is not None and not rule.enabled
        if still_silenced[pair]:
            continue
        alert.status = AlertStatus.OPEN.value
        alert.event_metadata = {
            **{key: value for key, value in payload.items() if key not in _RULE_MUTE_KEYS},
            "rule_mute_lifted_at": now,
        }
        lifted += 1
    if lifted:
        await session.flush()
    return lifted


# --------------------------------------------------------------------------- #
# Raising
# --------------------------------------------------------------------------- #


async def raise_alert(
    session: AsyncSession,
    *,
    workspace_id: str,
    title: str,
    source: str,
    severity: AlertSeverity | str = AlertSeverity.MEDIUM,
    description: str | None = None,
    dedupe_key: str | None = None,
    source_entity_type: str | None = None,
    source_entity_id: str | None = None,
    engine_alert_id: str | None = None,
    assigned_to_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    raised_at: dt.datetime | None = None,
    principal: Principal | None = None,
    request: Request | None = None,
) -> tuple[Alert, bool]:
    """Raise an alert, collapsing recurrences onto the row that is already live.

    This is the entry point every other domain raises through. It takes
    ``workspace_id`` rather than a principal because most callers are background
    work with no user attached; pass ``principal`` when a person raised the
    alert and the action should appear in the audit trail. A machine-raised
    alert needs no audit row: the alert *is* the record.

    A new alert is put to the workspace's rules (:func:`_rule_covering`). An
    enabled rule is named on the alert it governs, which is how a triager — and
    whatever eventually delivers notifications — knows whose channels it falls
    under. A rule that is switched off silences what a *screen* raises: the
    alert is born Muted, out of the open count, and comes back when the rule
    does. What a person or an external monitor posts by hand is never silenced;
    a rule speaks for the screen it watches, not for them. A recurrence lands on
    the live row as it is, so a flood costs no rule lookups.

    Always returns an alert, and whether it was newly created; ``False`` means
    an existing alert absorbed this occurrence.
    """
    severity_value = severity.value if isinstance(severity, AlertSeverity) else str(severity)
    occurred = raised_at or _now()
    payload = metadata or {}

    if dedupe_key:
        await _take_condition(session, workspace_id, dedupe_key)
        existing = await _live_alert(session, workspace_id, dedupe_key)
        if existing is not None:
            return await _absorb(session, existing, occurred, payload), False

    # What the rule writes goes on the alert that is born here and nowhere else:
    # a raise that loses the race below lands on somebody else's row, which was
    # put to the rules when *it* was born and may be a person's, never silenced.
    status = AlertStatus.OPEN.value
    born_with = payload
    rule = await _rule_covering(session, workspace_id, source, severity_value)
    if rule is not None and rule.enabled:
        born_with = {**payload, "alert_rule": rule.name, "alert_rule_id": rule.id}
    elif rule is not None and principal is None:
        status = AlertStatus.MUTED.value
        born_with = {
            **payload,
            "alert_rule": rule.name,
            RULE_MUTE_KEY: rule.id,
            "muted_by": f"Alert rule '{rule.name}'",
            "mute_reason": (
                f"The alert rule '{rule.name}' is switched off for {severity_value} "
                f"alerts from {source}."
            ),
        }

    def _candidate(alert_ref: str) -> Alert:
        return Alert(
            workspace_id=workspace_id,
            alert_ref=alert_ref,
            severity=severity_value,
            title=title,
            description=description,
            source=source,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            status=status,
            raised_at=occurred,
            assigned_to_user_id=assigned_to_user_id,
            dedupe_key=dedupe_key,
            occurrence_count=1,
            last_occurred_at=occurred,
            engine_alert_id=engine_alert_id,
            event_metadata=born_with,
        )

    # The reference is read without a lock, so raises that land together — the
    # burst an incident is made of — choose the same one and the unique
    # constraint refuses all but the first. That used to surface as a 500, and
    # from a screen's evaluator it took the caller's whole transaction with it.
    # The insert runs in a savepoint, so losing the race costs a re-read: of the
    # reference, and of the dedupe key too, because the raise that won may have
    # been this very condition, and then this one is its second occurrence.
    #
    # Two raises of one key need not collide on the reference at all: the second
    # looks for the key before the first commits and reads the reference after,
    # so it takes the next one and inserts a second live row for the condition.
    # The turn taken above keeps them apart where the database has such a lock;
    # everywhere, the raise looks once more from inside the savepoint, and takes
    # its insert back if a twin has been committed in the meantime. What is left
    # is two raises that both went ahead without the lock and neither of which
    # has committed; only a unique index on the live key can refuse that, and
    # the lookup above tolerates it by landing on the newest.
    #
    # A screen's evaluator calls this with its own edit still pending (the quota
    # it has just restated, say). That is written first, in the caller's
    # transaction, so that nothing of the caller's is ever inside the savepoint:
    # opening one flushes whatever is pending, and from inside the ``try`` a
    # constraint the *caller's* row broke would be read as a lost race for the
    # reference and retried on a transaction that is already dead.
    await session.flush()
    alert: Alert | None = None
    for _attempt in range(REF_ALLOCATION_ATTEMPTS):
        candidate = _candidate(await _next_alert_ref(session, workspace_id))
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
                if dedupe_key and await _live_alert(
                    session, workspace_id, dedupe_key, other_than=candidate.id
                ):
                    raise _ConditionAlreadyLive
        except (IntegrityError, _ConditionAlreadyLive):
            if dedupe_key:
                existing = await _live_alert(session, workspace_id, dedupe_key)
                if existing is not None:
                    return await _absorb(session, existing, occurred, payload), False
            continue
        alert = candidate
        break
    if alert is None:
        # Outrun every time: an opaque reference rather than a lost alert. Not
        # caught — if this fails too, the reference was never the problem.
        alert = _candidate(_opaque_alert_ref())
        session.add(alert)
        await session.flush()

    if principal is not None:
        await audit.record(
            session,
            principal=principal,
            action="Alert raised",
            entity_type="Alert",
            entity_id=alert.id,
            entity_label=alert.title,
            source_screen=SCREEN,
            detail=f"{severity_value} alert raised from {source}.",
            metadata={"alert_ref": alert.alert_ref, "dedupe_key": dedupe_key},
            request=request,
        )
    return alert, True


async def create_alert(
    session: AsyncSession,
    principal: Principal,
    payload: AlertCreate,
    *,
    request: Request | None = None,
) -> tuple[Alert, bool]:
    """Raise an alert on behalf of a signed-in operator or an API key."""
    principal.require(Role.OPERATOR)
    if payload.assigned_to_user_id:
        await _assert_member(session, principal, payload.assigned_to_user_id)
    return await raise_alert(
        session,
        workspace_id=principal.workspace_id,
        title=payload.title,
        source=payload.source,
        severity=payload.severity,
        description=payload.description,
        dedupe_key=payload.dedupe_key,
        source_entity_type=payload.source_entity_type,
        source_entity_id=payload.source_entity_id,
        engine_alert_id=payload.engine_alert_id,
        assigned_to_user_id=payload.assigned_to_user_id,
        metadata=payload.event_metadata,
        raised_at=payload.raised_at,
        principal=principal,
        request=request,
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _alert_query(
    workspace_id: str,
    *,
    severity: Sequence[str] | None = None,
    status: Sequence[str] | None = None,
    source: Sequence[str] | None = None,
    assigned_to_user_id: str | None = None,
    raised_since: dt.datetime | None = None,
    open_only: bool = False,
) -> Select:
    stmt = select(Alert).where(Alert.workspace_id == workspace_id)
    stmt = apply_filters(
        stmt,
        {
            Alert.severity: list(severity) if severity else None,
            Alert.status: list(status) if status else None,
            Alert.source: list(source) if source else None,
            Alert.assigned_to_user_id: assigned_to_user_id,
        },
    )
    if raised_since is not None:
        stmt = stmt.where(Alert.raised_at >= raised_since)
    if open_only:
        stmt = stmt.where(Alert.status.in_(LIVE_STATUSES))
    return stmt


async def list_alerts(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    severity: Sequence[str] | None = None,
    status: Sequence[str] | None = None,
    source: Sequence[str] | None = None,
    assigned_to_user_id: str | None = None,
    raised_since: dt.datetime | None = None,
    open_only: bool = False,
) -> tuple[Sequence[Alert], int]:
    """One page of alerts plus the unpaged total, honouring the screen's filters."""
    stmt = _alert_query(
        principal.workspace_id,
        severity=severity,
        status=status,
        source=source,
        assigned_to_user_id=assigned_to_user_id,
        raised_since=raised_since,
        open_only=open_only,
    )
    stmt = apply_search(stmt, params, _SEARCH_COLUMNS)
    stmt = apply_sort(stmt, params, _SORTABLE, Alert.raised_at)
    # Ties on the sort column would otherwise shuffle between pages.
    stmt = stmt.order_by(Alert.id.desc())
    return await paginate(session, stmt, params)


async def get_alert(session: AsyncSession, principal: Principal, alert_id: str) -> Alert:
    """Fetch one alert within the caller's workspace."""
    return await _get_alert(session, principal, alert_id)


async def export_rows(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    severity: Sequence[str] | None = None,
    status: Sequence[str] | None = None,
    source: Sequence[str] | None = None,
    assigned_to_user_id: str | None = None,
    raised_since: dt.datetime | None = None,
    open_only: bool = False,
) -> list[dict[str, Any]]:
    """Rows for the CSV export, filtered exactly as the table was."""
    stmt = _alert_query(
        principal.workspace_id,
        severity=severity,
        status=status,
        source=source,
        assigned_to_user_id=assigned_to_user_id,
        raised_since=raised_since,
        open_only=open_only,
    )
    stmt = apply_search(stmt, params, _SEARCH_COLUMNS)
    stmt = apply_sort(stmt, params, _SORTABLE, Alert.raised_at)
    stmt = stmt.order_by(Alert.id.desc()).limit(MAX_EXPORT_ROWS)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            key: value.isoformat() if isinstance(value, dt.datetime) else value
            for key, value in ((k, getattr(alert, k)) for k, _ in EXPORT_COLUMNS)
        }
        for alert in rows
    ]


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #


async def _assert_member(
    session: AsyncSession, principal: Principal, user_id: str
) -> str | None:
    """Confirm a user belongs to this workspace; return their display name."""
    member = (
        await session.execute(
            select(Membership.user_id).where(
                Membership.user_id == user_id,
                Membership.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise ValidationFailed(
            "That user is not a member of this workspace.",
            details={"field": "assignee_user_id"},
        )
    return (
        await session.execute(select(User.full_name).where(User.id == user_id))
    ).scalar_one_or_none()


async def update_alert(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    payload: AlertUpdate,
    *,
    request: Request | None = None,
) -> Alert:
    """Edit an alert's descriptive fields, or move it to/from Investigating."""
    principal.require(Role.OPERATOR)
    alert = await _get_alert(session, principal, alert_id)
    changes = payload.model_dump(exclude_unset=True)
    touched = sorted(changes)

    # Status is handled here and removed from the generic assignment below, so a
    # partial update that mentions it explicitly as null cannot blank the column.
    requested_status = changes.pop("status", None)
    if requested_status is not None:
        new_status = AlertStatus(requested_status).value
        if new_status not in PATCHABLE_STATUSES:
            raise ValidationFailed(
                "Use the acknowledge, resolve or mute endpoints to move an alert "
                "into that state; they record the timings the KPIs depend on.",
                details={"allowed": list(PATCHABLE_STATUSES)},
            )
        if alert.status == AlertStatus.RESOLVED.value:
            raise Conflict("That alert is resolved and cannot be reopened by editing it.")
        alert.status = new_status

    if changes.get("assigned_to_user_id"):
        await _assert_member(session, principal, changes["assigned_to_user_id"])

    if "event_metadata" in changes and changes["event_metadata"] is not None:
        _merge_metadata(alert, changes.pop("event_metadata"))
    else:
        changes.pop("event_metadata", None)

    for field, value in changes.items():
        if value is None and field in ("title", "severity"):
            # Optional in the schema only because it is a partial update.
            continue
        setattr(alert, field, AlertSeverity(value).value if field == "severity" else value)

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert updated",
        entity_type="Alert",
        entity_id=alert.id,
        entity_label=alert.title,
        source_screen=SCREEN,
        detail=f"Updated {', '.join(touched)}.",
        request=request,
    )
    return alert


async def delete_alert(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove an alert. Reserved for admins: the row is incident evidence."""
    principal.require(Role.ADMIN)
    alert = await _get_alert(session, principal, alert_id)
    label, ref = alert.title, alert.alert_ref
    await session.delete(alert)
    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert deleted",
        entity_type="Alert",
        entity_id=alert_id,
        entity_label=label,
        source_screen=SCREEN,
        detail=f"Deleted alert {ref}.",
        request=request,
    )


# --------------------------------------------------------------------------- #
# Triage verbs
# --------------------------------------------------------------------------- #


async def acknowledge(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    payload: AlertAcknowledgeRequest | None = None,
    *,
    request: Request | None = None,
) -> tuple[Alert, bool]:
    """Take ownership of an alert.

    Idempotent: acknowledging twice is a double-click, not an error. The second
    call leaves ``acknowledged_at`` alone so MTTA keeps measuring the first
    human response. Returns the alert and whether this call changed it.
    """
    principal.require(Role.OPERATOR)
    alert = await _get_alert(session, principal, alert_id)

    if alert.status == AlertStatus.RESOLVED.value:
        raise Conflict("That alert is already resolved.")
    if alert.status == AlertStatus.ACKNOWLEDGED.value:
        return alert, False

    alert.status = AlertStatus.ACKNOWLEDGED.value
    if alert.acknowledged_at is None:
        alert.acknowledged_at = _now()
        alert.acknowledged_by_user_id = principal.user_id
    if payload is not None and payload.note:
        _merge_metadata(alert, {"acknowledgement_note": payload.note})

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert acknowledged",
        entity_type="Alert",
        entity_id=alert.id,
        entity_label=alert.title,
        source_screen=SCREEN,
        detail=payload.note if payload and payload.note else f"Acknowledged {alert.alert_ref}.",
        request=request,
    )
    return alert, True


async def acknowledge_all(
    session: AsyncSession,
    principal: Principal,
    payload: AlertAcknowledgeAllRequest | None = None,
    *,
    request: Request | None = None,
) -> int:
    """Acknowledge every unclaimed alert, optionally narrowed by severity or source.

    Two statements, in this order: stamp the timings only where they are still
    unset, then move the statuses. Doing it the other way round would lose the
    ability to tell which rows had never been acknowledged. One audit row
    records the sweep — a bulk action is one decision, not N.
    """
    principal.require(Role.OPERATOR)
    now = _now()

    predicates: list[Any] = [
        Alert.workspace_id == principal.workspace_id,
        Alert.status.in_(UNACKED_STATUSES),
    ]
    if payload is not None and payload.severity:
        predicates.append(Alert.severity.in_([s.value for s in payload.severity]))
    if payload is not None and payload.source:
        predicates.append(Alert.source.in_(list(payload.source)))

    await session.execute(
        update(Alert)
        .where(*predicates, Alert.acknowledged_at.is_(None))
        .values(acknowledged_at=now, acknowledged_by_user_id=principal.user_id)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(
        update(Alert)
        .where(*predicates)
        .values(status=AlertStatus.ACKNOWLEDGED.value)
        .execution_options(synchronize_session=False)
    )
    count = int(result.rowcount or 0)

    if count:
        await audit.record(
            session,
            principal=principal,
            action="Alerts acknowledged in bulk",
            entity_type="Alert",
            entity_label=f"{count} alerts",
            source_screen=SCREEN,
            detail=payload.note if payload and payload.note else f"Acknowledged {count} alerts.",
            metadata={
                "count": count,
                "severity": [s.value for s in payload.severity]
                if payload and payload.severity
                else None,
                "source": list(payload.source) if payload and payload.source else None,
            },
            request=request,
        )
    return count


async def resolve(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    payload: AlertResolveRequest | None = None,
    *,
    request: Request | None = None,
) -> Alert:
    """Close an alert and freeze its time-to-resolve.

    ``mttr_seconds`` is computed once, here, from ``raised_at``. Storing it
    rather than deriving it later means the KPI cannot drift if a timestamp is
    ever corrected, and it keeps the tile a single ``AVG`` over an integer.
    """
    principal.require(Role.OPERATOR)
    alert = await _get_alert(session, principal, alert_id)
    if alert.status == AlertStatus.RESOLVED.value:
        raise Conflict("That alert is already resolved.")

    resolved_at = _now()
    alert.status = AlertStatus.RESOLVED.value
    alert.resolved_at = resolved_at
    alert.resolved_by_user_id = principal.user_id
    alert.mttr_seconds = max(0, int((resolved_at - alert.raised_at).total_seconds()))
    if payload is not None and payload.resolution_note:
        _merge_metadata(alert, {"resolution_note": payload.resolution_note})

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert resolved",
        entity_type="Alert",
        entity_id=alert.id,
        entity_label=alert.title,
        source_screen=SCREEN,
        detail=(
            payload.resolution_note
            if payload and payload.resolution_note
            else f"Resolved {alert.alert_ref} after {alert.mttr_seconds}s."
        ),
        metadata={"mttr_seconds": alert.mttr_seconds},
        request=request,
    )
    return alert


async def assign(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    payload: AlertAssignRequest,
    *,
    request: Request | None = None,
) -> Alert:
    """Route an alert to a named member of this workspace."""
    principal.require(Role.OPERATOR)
    alert = await _get_alert(session, principal, alert_id)
    if alert.status == AlertStatus.RESOLVED.value:
        raise Conflict("That alert is resolved; reopen it before reassigning.")

    assignee_name = await _assert_member(session, principal, payload.assignee_user_id)
    alert.assigned_to_user_id = payload.assignee_user_id
    if payload.note:
        _merge_metadata(alert, {"assignment_note": payload.note})

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert assigned",
        entity_type="Alert",
        entity_id=alert.id,
        entity_label=alert.title,
        source_screen=SCREEN,
        detail=f"Assigned to {assignee_name or payload.assignee_user_id}.",
        metadata={"assignee_user_id": payload.assignee_user_id},
        request=request,
    )
    return alert


async def mute(
    session: AsyncSession,
    principal: Principal,
    alert_id: str,
    payload: AlertMuteRequest | None = None,
    *,
    request: Request | None = None,
) -> tuple[Alert, dt.datetime]:
    """Silence an alert for a window without closing it.

    The alert stays live for dedupe, so recurrences keep landing on this row
    instead of escaping as new alerts while the maintenance window runs. The
    deadline is kept on the alert's own payload, which is where the raiser's
    other context already lives.
    """
    principal.require(Role.OPERATOR)
    alert = await _get_alert(session, principal, alert_id)
    if alert.status == AlertStatus.RESOLVED.value:
        raise Conflict("That alert is already resolved; there is nothing to mute.")

    minutes = payload.duration_minutes if payload else 1440
    muted_until = _now() + dt.timedelta(minutes=minutes)
    alert.status = AlertStatus.MUTED.value
    _merge_metadata(
        alert,
        {
            "muted_until": muted_until.isoformat(),
            "muted_by": principal.actor,
            "mute_reason": payload.reason if payload else None,
        },
    )

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Alert muted",
        entity_type="Alert",
        entity_id=alert.id,
        entity_label=alert.title,
        source_screen=SCREEN,
        detail=f"Muted for {minutes} minutes, until {muted_until.isoformat()}.",
        metadata={"muted_until": muted_until.isoformat(), "duration_minutes": minutes},
        request=request,
    )
    return alert, muted_until


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


async def summary(session: AsyncSession, principal: Principal) -> AlertsSummary:
    """The KPI row, as one aggregate query plus one DISTINCT for the filter list.

    Conditional sums do all the counting in a single pass over the workspace's
    alerts; nothing is loaded into Python to be counted here.
    """
    workspace = principal.workspace_id
    day_ago = _now() - dt.timedelta(hours=24)

    def _count_if(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    row = (
        await session.execute(
            select(
                func.count(Alert.id).label("total"),
                _count_if(Alert.status == AlertStatus.OPEN.value).label("open"),
                _count_if(Alert.status == AlertStatus.INVESTIGATING.value).label(
                    "investigating"
                ),
                _count_if(Alert.status == AlertStatus.ACKNOWLEDGED.value).label("acknowledged"),
                _count_if(Alert.status == AlertStatus.MUTED.value).label("muted"),
                _count_if(
                    (Alert.severity == AlertSeverity.CRITICAL.value)
                    & (Alert.status != AlertStatus.RESOLVED.value)
                ).label("critical"),
                _count_if(
                    (Alert.status == AlertStatus.RESOLVED.value)
                    & (Alert.resolved_at >= day_ago)
                ).label("resolved_24h"),
                func.avg(_ack_latency_seconds(session)).label("mtta"),
                func.avg(Alert.mttr_seconds).label("mttr"),
            ).where(Alert.workspace_id == workspace)
        )
    ).one()

    sources = (
        (
            await session.execute(
                select(Alert.source)
                .where(Alert.workspace_id == workspace)
                .distinct()
                .order_by(Alert.source.asc())
            )
        )
        .scalars()
        .all()
    )

    return AlertsSummary(
        open=int(row.open or 0),
        critical=int(row.critical or 0),
        investigating=int(row.investigating or 0),
        acknowledged=int(row.acknowledged or 0),
        muted=int(row.muted or 0),
        resolved_24h=int(row.resolved_24h or 0),
        total=int(row.total or 0),
        mtta_seconds=round(float(row.mtta), 1) if row.mtta is not None else None,
        mttr_seconds=round(float(row.mttr), 1) if row.mttr is not None else None,
        sources=[s for s in sources if s],
    )


# --------------------------------------------------------------------------- #
# Alert rules
# --------------------------------------------------------------------------- #


async def list_rules(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    source: Sequence[str] | None = None,
    severity: Sequence[str] | None = None,
    enabled: bool | None = None,
) -> tuple[Sequence[AlertRule], int]:
    """One page of alert rules for the rules editor."""
    stmt = select(AlertRule).where(AlertRule.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            AlertRule.source: list(source) if source else None,
            AlertRule.severity: list(severity) if severity else None,
            AlertRule.enabled: enabled,
        },
    )
    stmt = apply_search(
        stmt, params, (AlertRule.name, AlertRule.description, AlertRule.source)
    )
    stmt = apply_sort(stmt, params, _RULE_SORTABLE, AlertRule.name, default_desc=False)
    stmt = stmt.order_by(AlertRule.id.desc())
    return await paginate(session, stmt, params)


async def get_rule(session: AsyncSession, principal: Principal, rule_id: str) -> AlertRule:
    """Fetch one alert rule within the caller's workspace."""
    return await _get_rule(session, principal, rule_id)


async def _assert_rule_name_free(
    session: AsyncSession, principal: Principal, name: str, *, exclude_id: str | None = None
) -> None:
    stmt = select(AlertRule.id).where(
        AlertRule.workspace_id == principal.workspace_id, AlertRule.name == name
    )
    if exclude_id:
        stmt = stmt.where(AlertRule.id != exclude_id)
    if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise Conflict(f"An alert rule named '{name}' already exists in this workspace.")


async def create_rule(
    session: AsyncSession,
    principal: Principal,
    payload: AlertRuleCreate,
    *,
    request: Request | None = None,
) -> AlertRule:
    """Define a new alert rule. Rule names are unique within a workspace."""
    principal.require(Role.ADMIN)
    await _assert_rule_name_free(session, principal, payload.name)

    rule = AlertRule(
        workspace_id=principal.workspace_id,
        name=payload.name,
        description=payload.description,
        source=payload.source,
        condition=dict(payload.condition),
        severity=payload.severity.value,
        enabled=payload.enabled,
        notify_channels=list(payload.notify_channels),
        throttle_minutes=payload.throttle_minutes,
        created_by_user_id=principal.user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(rule)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost the race with a concurrent create of the same name.
        await session.rollback()
        raise Conflict(
            f"An alert rule named '{payload.name}' already exists in this workspace."
        ) from exc

    # A new enabled rule can outrank the disabled one that was silencing its pair.
    reopened = await _lift_rule_mutes(session, principal.workspace_id)
    await audit.record(
        session,
        principal=principal,
        action="Alert rule created",
        entity_type="AlertRule",
        entity_id=rule.id,
        entity_label=rule.name,
        source_screen=SCREEN,
        detail=f"{rule.severity} rule watching {rule.source}.",
        metadata={"alerts_reopened": reopened} if reopened else None,
        request=request,
    )
    return rule


async def update_rule(
    session: AsyncSession,
    principal: Principal,
    rule_id: str,
    payload: AlertRuleUpdate,
    *,
    request: Request | None = None,
) -> AlertRule:
    """Partially update an alert rule."""
    principal.require(Role.ADMIN)
    rule = await _get_rule(session, principal, rule_id)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("name") and changes["name"] != rule.name:
        await _assert_rule_name_free(session, principal, changes["name"], exclude_id=rule.id)

    for field, value in changes.items():
        if value is None and field in ("name", "source", "condition", "severity"):
            continue
        if field == "severity":
            rule.severity = AlertSeverity(value).value
        elif field == "condition":
            rule.condition = dict(value)
        elif field == "notify_channels":
            rule.notify_channels = list(value)
        else:
            setattr(rule, field, value)
    rule.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Another alert rule in this workspace already uses that name.") from exc

    # Switching a rule back on, or moving it off the pair it was silencing,
    # brings back what it silenced while it was off.
    reopened = await _lift_rule_mutes(session, principal.workspace_id)
    await audit.record(
        session,
        principal=principal,
        action="Alert rule updated",
        entity_type="AlertRule",
        entity_id=rule.id,
        entity_label=rule.name,
        source_screen=SCREEN,
        detail=f"Updated {', '.join(sorted(changes))}.",
        metadata={"alerts_reopened": reopened} if reopened else None,
        request=request,
    )
    return rule


async def delete_rule(
    session: AsyncSession,
    principal: Principal,
    rule_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Delete an alert rule. Alerts it already raised are left untouched."""
    principal.require(Role.ADMIN)
    rule = await _get_rule(session, principal, rule_id)
    label = rule.name
    await session.delete(rule)
    await session.flush()
    # A deleted rule silences nothing: its pair is back on the built-in raise.
    reopened = await _lift_rule_mutes(session, principal.workspace_id)
    await audit.record(
        session,
        principal=principal,
        action="Alert rule deleted",
        entity_type="AlertRule",
        entity_id=rule_id,
        entity_label=label,
        source_screen=SCREEN,
        detail=f"Deleted alert rule '{label}'.",
        metadata={"alerts_reopened": reopened} if reopened else None,
        request=request,
    )
