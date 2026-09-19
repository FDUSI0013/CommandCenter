"""The platform's own clock.

Several screens promise time-driven behaviour: export schedules fire on a
cron, test suites run on a cadence, an approval whose SLA elapses expires, a
mute lapses after its duration, and a secret's status follows its rotation and
expiry dates. None of that can hang off a request — a workspace nobody is
looking at still owes its Monday-morning export — so this module runs a small
sweep on an interval from the application's lifespan.

Every sweep is written to be safe under more than one worker: the sweeps run
under a Postgres advisory lock (first worker in wins, the rest skip the tick),
and each individual action is a no-op when another pass already did it. On
SQLite there is one process, so the lock degrades to a pass-through.

The lock covers deciding and claiming -- short SQL -- and nothing else. Work a
sweep has claimed that then has to wait on the telemetry engine (rendering an
export) is carried out after the lock and its connection have been given back,
so one slow export cannot stop every worker's clock or sit on a pooled
connection, idle in a transaction, for as long as the engine takes.

The sweep never raises: a failing domain logs and leaves the others to run,
because the export that cannot render must not stop the approval that must
expire.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..core.config import settings
from ..db.session import get_sessionmaker
from ..models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    Secret,
    SecretStatus,
)
from ..models.identity import Role
from ..models.licensing import BillingPeriod, LicensePlan, LicenseStatus, TenantLicense
from ..models.operations import Alert, AlertStatus
from ..models.quality import RunTrigger, SuiteStatus, TestRun, TestSuite
from ..schemas.testing import TestRunRequest, next_fire

log = logging.getLogger(__name__)

#: One number, arbitrary but fixed: the advisory-lock key every worker contends
#: on. Whoever holds it runs the tick; everyone else skips it.
ADVISORY_LOCK_KEY = 0x46554C43  # "FULC"

#: Statuses the secret sweep may write — and therefore the only ones it may
#: overwrite. A Disabled or Revoked row is a human decision the clock must not
#: undo, and Warning is accepted on write as a console alias we leave alone.
DERIVED_SECRET_STATUSES = (
    SecretStatus.ACTIVE.value,
    SecretStatus.EXPIRING_SOON.value,
    SecretStatus.ROTATION_OVERDUE.value,
    SecretStatus.EXPIRED.value,
)

EXPIRING_WINDOW = dt.timedelta(days=30)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _system_principal(workspace_id: str) -> Principal:
    """How the sweep signs what it does, mirroring the evaluation supervisor."""
    return Principal(
        workspace_id=workspace_id,
        workspace_slug="",
        engine_workspace="",
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="platform-scheduler",
        display_name="Platform Scheduler",
    )


def _lock_statement() -> Select[Any]:
    """The tick's lock, *transaction*-scoped.

    It was ``pg_try_advisory_lock`` -- session-scoped -- on the assumption that
    closing the session closes the connection. It does not: the connection goes
    back to the pool after a ROLLBACK, and a session-level advisory lock
    survives a rollback. So the first connection to win kept the lock until
    ``pool_recycle`` closed it half an hour later; the other workers never won
    again, and the winner ran a tick only when the pool happened to hand it
    that same connection -- one tick in however many connections it had open.
    Everything on this clock fired late and erratically while the log said
    "running every 30s".

    The transaction-scoped form is released by the rollback (or commit) that
    ends ``lock_session``'s transaction, which is exactly the lifetime wanted,
    and needs no unlock that a cancelled task could skip. ``lock_session`` must
    therefore never commit while the sweeps run.
    """
    return select(func.pg_try_advisory_xact_lock(ADVISORY_LOCK_KEY))


async def _try_lock(session: AsyncSession) -> bool:
    if session.bind.dialect.name != "postgresql":
        return True
    result = await session.execute(_lock_statement())
    return bool(result.scalar())


#: Work a sweep claimed under the lock, to be carried out once it is released.
AfterLock = list[Callable[[], Awaitable[None]]]


async def _sweep_exports(counts: dict[str, int], after_lock: AfterLock) -> None:
    """Fire due export schedules; generate each queued job after the lock.

    Firing is the claim: it advances the schedule and queues the job in one
    commit, so no other worker will fire it again. Generating the file pages
    the telemetry engine and can take minutes, and needs no lock at all.
    """
    from . import exports

    async with get_sessionmaker()() as session:
        queued = await exports.run_due_schedules(session)
        await session.commit()

    async def generate(job_id: str, workspace_id: str) -> None:
        try:
            await exports.run_export_job(job_id, workspace_id)
        except Exception:  # noqa: BLE001 - one export must not stop the next
            log.exception("scheduled export %s failed", job_id)

    for job_id, workspace_id in queued:
        after_lock.append(functools.partial(generate, job_id, workspace_id))
    counts["exports_fired"] = len(queued)


#: Audit actions that set a suite's cadence going: a schedule bound or changed,
#: and a plain suite update that touched the cron or brought the suite back to
#: Active. (Creation needs no entry: ``created_at`` is already a candidate.)
_CADENCE_ACTIONS = (
    "test_suite.scheduled",
    "test_suite.schedule_updated",
    "test_suite.updated",
)
_CADENCE_FIELDS = frozenset({"schedule_cron", "status"})


async def _cadence_set_at(
    session: AsyncSession, suite_ids: Sequence[str]
) -> dict[str, dt.datetime]:
    """When each suite's current cadence was last set, read off the audit trail.

    A cron counts from an anchor, and the anchor was the suite's last run or its
    creation -- so binding ``0 2 * * *`` at three in the afternoon to a suite
    that last ran on Tuesday found a fire time already in the past, and the next
    tick ran the suite, against production, while the Schedules tab promised
    "tomorrow 02:00". Nothing on the suite row says when its cadence was set; the
    audit trail is the one place the platform writes that down, so it is read
    from there. A cadence starts counting from the moment it was set.
    """
    if not suite_ids:
        return {}
    events = (
        await session.execute(
            select(
                AuditEvent.entity_id,
                AuditEvent.occurred_at,
                AuditEvent.action,
                AuditEvent.event_metadata,
            )
            .where(
                AuditEvent.entity_type == "test_suite",
                AuditEvent.entity_id.in_(list(suite_ids)),
                AuditEvent.action.in_(_CADENCE_ACTIONS),
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
    ).all()
    latest: dict[str, dt.datetime] = {}
    for suite_id, occurred_at, action, metadata in events:
        if suite_id in latest:
            continue
        fields = (metadata or {}).get("fields") or ()
        if action == "test_suite.updated" and not _CADENCE_FIELDS.intersection(fields):
            continue  # a rename is not a new cadence
        latest[suite_id] = occurred_at
    return latest


async def _sweep_test_suites(counts: dict[str, int]) -> None:
    """Run every Active suite whose cron has ticked since its last run."""
    from . import testing

    now = _now()
    started = 0
    async with get_sessionmaker()() as session:
        suites = (
            (
                await session.execute(
                    select(TestSuite).where(
                        TestSuite.schedule_cron.is_not(None),
                        TestSuite.status == SuiteStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Anchor on the newest run we ever *started*, not only on last_run_at
        # (which the runner stamps at the finish): a suite whose runs die early
        # must not re-fire on every tick.
        latest_start: dict[str, dt.datetime] = {
            suite_id: started
            for suite_id, started in (
                await session.execute(
                    select(TestRun.suite_id, func.max(TestRun.created_at)).group_by(
                        TestRun.suite_id
                    )
                )
            ).all()
        }
        cadence_set = await _cadence_set_at(session, [suite.id for suite in suites])
        due: list[TestSuite] = []
        for suite in suites:
            candidates = [
                _as_utc(value)
                for value in (
                    suite.last_run_at,
                    latest_start.get(suite.id),
                    suite.created_at,
                    cadence_set.get(suite.id),
                )
                if value is not None
            ]
            anchor = max(candidates) if candidates else None
            fire_at = next_fire(suite.schedule_cron or "", anchor) if anchor else None
            if fire_at is not None and fire_at <= now:
                due.append(suite)

        for suite in due:
            principal = _system_principal(suite.workspace_id)
            try:
                run = await testing.start_run(
                    session,
                    principal,
                    suite.id,
                    TestRunRequest(trigger=RunTrigger.SCHEDULE),
                )
            except Exception as exc:  # noqa: BLE001 - one suite must not stop the rest
                log.info("scheduled suite %s not started: %s", suite.id, exc)
                continue
            await session.commit()
            asyncio.get_running_loop().create_task(
                testing.execute_run(run.id, suite.workspace_id),
                name=f"scheduled-test-run:{run.id}",
            )
            started += 1
        await session.commit()
    counts["test_suites_started"] = started


async def _sweep_approvals(counts: dict[str, int]) -> None:
    """Expire pending approvals whose SLA elapsed with nobody deciding."""
    from . import approvals

    now = _now()
    expired = 0
    async with get_sessionmaker()() as session:
        workspace_ids = (
            (
                await session.execute(
                    select(ApprovalRequest.workspace_id)
                    .where(
                        ApprovalRequest.status == ApprovalStatus.PENDING.value,
                        ApprovalRequest.sla_due_at.is_not(None),
                        ApprovalRequest.sla_due_at < now,
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for workspace_id in workspace_ids:
            rows = await approvals.expire_overdue(session, _system_principal(workspace_id))
            expired += len(rows)
        await session.commit()
    counts["approvals_expired"] = expired


async def _sweep_alert_mutes(counts: dict[str, int]) -> None:
    """Reopen muted alerts whose mute has lapsed."""
    now = _now()
    reopened = 0
    async with get_sessionmaker()() as session:
        # The deadline lives inside the JSON, so it cannot be a WHERE clause;
        # read the two columns that decide it rather than every muted row whole.
        muted = (
            await session.execute(
                select(Alert.id, Alert.event_metadata).where(
                    Alert.status == AlertStatus.MUTED.value
                )
            )
        ).all()
        for alert_id, event_metadata in muted:
            metadata: dict[str, Any] = dict(event_metadata or {})
            raw = metadata.get("muted_until")
            until = None
            if isinstance(raw, str):
                try:
                    until = dt.datetime.fromisoformat(raw)
                except ValueError:
                    until = None
            if until is None or _as_utc(until) > now:
                continue
            metadata["mute_expired_at"] = now.isoformat()
            # Still-muted is part of the write: an operator who unmuted or
            # resolved it a moment ago is not overruled by the clock.
            result = await session.execute(
                sa_update(Alert)
                .where(Alert.id == alert_id, Alert.status == AlertStatus.MUTED.value)
                .values(status=AlertStatus.OPEN.value, event_metadata=metadata)
                .execution_options(synchronize_session=False)
            )
            reopened += result.rowcount or 0
        await session.commit()
    counts["alerts_unmuted"] = reopened


async def _sweep_secret_statuses(counts: dict[str, int]) -> None:
    """Keep each secret's stored status in step with its own dates.

    Chips and KPIs compute freshness from the dates on read; the stored status
    also feeds the vault-overview donut, and without a sweep it fossilises at
    whatever it was last written as.
    """
    now = _now()
    changed = 0
    async with get_sessionmaker()() as session:
        # The four columns the decision needs, not the row: this runs on every
        # tick of every worker, and the row carries the ciphertext.
        rows = (
            await session.execute(
                select(
                    Secret.id, Secret.status, Secret.expires_at, Secret.next_rotation_at
                ).where(Secret.status.in_(DERIVED_SECRET_STATUSES))
            )
        ).all()
        moved: dict[str, list[str]] = {}
        for secret_id, status, raw_expires_at, raw_next_rotation in rows:
            expires_at = _as_utc(raw_expires_at)
            next_rotation = _as_utc(raw_next_rotation)
            if expires_at is not None and expires_at <= now:
                derived = SecretStatus.EXPIRED.value
            elif next_rotation is not None and next_rotation <= now:
                derived = SecretStatus.ROTATION_OVERDUE.value
            elif expires_at is not None and expires_at <= now + EXPIRING_WINDOW:
                derived = SecretStatus.EXPIRING_SOON.value
            else:
                derived = SecretStatus.ACTIVE.value
            if status != derived:
                moved.setdefault(derived, []).append(secret_id)
        for derived, secret_ids in moved.items():
            # Guarded on the status again: a secret somebody disabled or revoked
            # between the read and this write stays as they left it. And the
            # clock catching up with a date is not an edit, so it must not move
            # the concurrency token under whoever has the secret open.
            result = await session.execute(
                sa_update(Secret)
                .where(
                    Secret.id.in_(secret_ids),
                    Secret.status.in_(DERIVED_SECRET_STATUSES),
                )
                .values(status=derived, updated_at=Secret.updated_at)
                .execution_options(synchronize_session=False)
            )
            changed += result.rowcount or 0
        await session.commit()
    counts["secrets_restatused"] = changed


def _add_months(value: dt.datetime, months: int) -> dt.datetime:
    """``value`` moved on by whole calendar months, clamped to the month's end."""
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


async def _sweep_license_statuses(counts: dict[str, int]) -> None:
    """The renewal sweep the licensing model has always described.

    ``LicenseStatus`` says Expiring Soon and Expired are "written by the renewal
    sweep", the tenant grid and the status filter offer both, and entitlements
    resolve on the stored status alone -- but no sweep existed. A 14-day trial
    stayed Trial and fully entitled for ever, the grid counted "-12d" to a date
    long gone, the two filter options could never match a row, and a lapsed
    licence could not be replaced because it was still the current one (409).

    What the clock does, and no more than the row itself says:

    * a term that has ended with ``auto_renew`` on is rolled forward by the
      plan's billing period, as many periods as it takes to cover today;
    * a term that has ended with ``auto_renew`` off is Expired;
    * Active within the 30-day horizon becomes Expiring Soon, and Expiring Soon
      whose term was extended past it becomes Active again. Trial stays Trial
      until it ends: the grid shows the two apart.

    Suspended and Revoked are decisions somebody made, and are not touched.
    Every write is guarded on what was read, so a second worker's pass -- or an
    operator's edit between the read and the write -- makes it a no-op.
    """
    from . import audit
    from .licensing import EXPIRY_HORIZON_DAYS, LIVE_STATUSES, SOURCE_SCREEN

    now = _now()
    horizon = now + dt.timedelta(days=EXPIRY_HORIZON_DAYS)
    expiring_soon = LicenseStatus.EXPIRING_SOON.value
    renewed = expired = restatused = 0
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(
                    TenantLicense.id,
                    TenantLicense.tenant_workspace_id,
                    TenantLicense.status,
                    TenantLicense.expires_at,
                    TenantLicense.auto_renew,
                    TenantLicense.purchase_order_ref,
                    LicensePlan.billing_period,
                )
                .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
                .where(
                    TenantLicense.status.in_(LIVE_STATUSES),
                    # Everything near or past its date, plus whatever is marked
                    # Expiring Soon and may no longer be.
                    (TenantLicense.expires_at <= horizon)
                    | (TenantLicense.status == expiring_soon),
                )
            )
        ).all()
        for license_id, workspace_id, status, raw_expires, auto_renew, po_ref, period in rows:
            expires_at = _as_utc(raw_expires)
            seen = (TenantLicense.id == license_id, TenantLicense.status == status)

            if expires_at is not None and expires_at <= now:
                if auto_renew:
                    step = 1 if period == BillingPeriod.MONTHLY.value else 12
                    terms = 1
                    while _add_months(expires_at, terms * step) <= now:
                        terms += 1
                    new_expires = _add_months(expires_at, terms * step)
                    new_status = status
                    if status != LicenseStatus.TRIAL.value:
                        new_status = (
                            expiring_soon
                            if new_expires <= horizon
                            else LicenseStatus.ACTIVE.value
                        )
                    values: dict[str, Any] = {
                        "starts_at": _add_months(expires_at, (terms - 1) * step),
                        "expires_at": new_expires,
                        "status": new_status,
                    }
                    action, detail = (
                        "licensing.license.renewed",
                        f"Term renewed automatically to {new_expires.date().isoformat()}.",
                    )
                else:
                    values = {"status": LicenseStatus.EXPIRED.value}
                    action, detail = (
                        "licensing.license.expired",
                        "License term ended without renewal.",
                    )
                # The date is part of the guard: a term another worker already
                # rolled forward, or an operator just extended, is left alone.
                result = await session.execute(
                    sa_update(TenantLicense)
                    .where(*seen, TenantLicense.expires_at == raw_expires)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if not result.rowcount:
                    continue
                if auto_renew:
                    renewed += 1
                else:
                    expired += 1
                await audit.record(
                    session,
                    principal=_system_principal(workspace_id),
                    action=action,
                    entity_type="tenant_license",
                    entity_id=license_id,
                    entity_label=po_ref or license_id,
                    source_screen=SOURCE_SCREEN,
                    detail=detail,
                    metadata={"status": values["status"], "auto_renew": bool(auto_renew)},
                )
                continue

            within = expires_at is not None and expires_at <= horizon
            if status == LicenseStatus.ACTIVE.value and within:
                derived = expiring_soon
            elif status == expiring_soon and not within:
                derived = LicenseStatus.ACTIVE.value
            else:
                continue
            # The calendar reaching a date is not an edit; see the secrets sweep.
            result = await session.execute(
                sa_update(TenantLicense)
                .where(*seen)
                .values(status=derived, updated_at=TenantLicense.updated_at)
                .execution_options(synchronize_session=False)
            )
            restatused += result.rowcount or 0
        await session.commit()
    counts["licenses_renewed"] = renewed
    counts["licenses_expired"] = expired
    counts["licenses_restatused"] = restatused


#: Grace beyond a run's own deadline before the clock declares it abandoned.
#: A run genuinely in progress cannot outlive its deadline — the in-process
#: runner enforces it and writes a terminal state — so anything still
#: non-terminal past deadline+grace was orphaned by a restart (the runner lives
#: only in memory) or is wedged. Either way it must not block the suite forever.
STALE_RUN_GRACE_SECONDS = 120


async def _sweep_stale_runs(counts: dict[str, int]) -> None:
    """Fail-close test runs and evaluations a restart orphaned mid-flight.

    Without this, a deploy while a run is in flight leaves its row Queued or
    Running forever: the in-process runner is gone, and the one-live-run-per-
    suite guard then 409s every future run. Reaping by the run's own deadline
    is race-free — it never touches a run that is still within its lifetime.
    """
    from ..services.evaluations import EXECUTION_DEADLINE_SECONDS as EVAL_DEADLINE
    from ..services.testing import EXECUTION_DEADLINE_SECONDS as SUITE_DEADLINE

    now = _now()
    reaped = 0
    async with get_sessionmaker()() as session:
        from ..models.quality import (
            EvaluationRun,
            EvaluationStatus,
            TestRun,
            TestRunStatus,
        )

        suite_cutoff = now - dt.timedelta(seconds=SUITE_DEADLINE + STALE_RUN_GRACE_SECONDS)
        stale_runs = (
            (
                await session.execute(
                    select(TestRun).where(
                        TestRun.status.in_(
                            (TestRunStatus.QUEUED.value, TestRunStatus.RUNNING.value)
                        ),
                        func.coalesce(TestRun.started_at, TestRun.created_at) < suite_cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in stale_runs:
            run.status = TestRunStatus.ERROR.value
            run.finished_at = now
            summary = dict(run.summary or {})
            summary["error"] = "Interrupted before it finished (restart or timeout)."
            run.summary = summary
            reaped += 1

        eval_cutoff = now - dt.timedelta(seconds=EVAL_DEADLINE + STALE_RUN_GRACE_SECONDS)
        stale_evals = (
            (
                await session.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.status.in_(
                            (EvaluationStatus.QUEUED.value, EvaluationStatus.RUNNING.value)
                        ),
                        func.coalesce(EvaluationRun.started_at, EvaluationRun.created_at)
                        < eval_cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in stale_evals:
            run.status = EvaluationStatus.FAILED.value
            run.finished_at = now
            run.notes = "Interrupted before it finished (restart or timeout)."
            reaped += 1

        await session.commit()
    counts["stale_runs_reaped"] = reaped


async def run_once() -> dict[str, int]:
    """One tick of the platform clock. Returns what each sweep did."""
    counts: dict[str, int] = {}
    after_lock: AfterLock = []
    async with get_sessionmaker()() as lock_session:
        if not await _try_lock(lock_session):
            return counts
        for sweep in (
            functools.partial(_sweep_exports, after_lock=after_lock),
            _sweep_test_suites,
            _sweep_approvals,
            _sweep_alert_mutes,
            _sweep_secret_statuses,
            _sweep_license_statuses,
            _sweep_stale_runs,
        ):
            try:
                await sweep(counts)
            except Exception:  # noqa: BLE001 - one domain must not stop the others
                name = getattr(sweep, "func", sweep).__name__
                log.exception("scheduler sweep %s failed", name)
        # The advisory lock is transaction-scoped and lock_session never
        # commits: leaving this block rolls its transaction back, which is what
        # releases the lock and hands the connection back to the pool.
    for deferred in after_lock:
        await deferred()
    return counts


async def _loop() -> None:
    while True:
        try:
            counts = await run_once()
            if any(counts.values()):
                log.info("scheduler tick: %s", counts)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 - the loop itself must survive anything
            log.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_interval_seconds)


def start() -> asyncio.Task[None] | None:
    """Start the clock, if this deployment wants one."""
    if not settings.scheduler_enabled:
        return None
    task = asyncio.get_running_loop().create_task(_loop(), name="platform-scheduler")
    log.info(
        "platform scheduler running every %.0fs", settings.scheduler_interval_seconds
    )
    return task
