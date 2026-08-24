"""The platform's own clock.

Several screens promise time-driven behaviour: export schedules fire on a
cron, test suites run on a cadence, an approval whose SLA elapses expires, a
mute lapses after its duration, and a secret's status follows its rotation and
expiry dates. None of that can hang off a request — a workspace nobody is
looking at still owes its Monday-morning export — so this module runs a small
sweep on an interval from the application's lifespan.

Every sweep is written to be safe under more than one worker: the whole cycle
runs under a Postgres advisory lock (first worker in wins, the rest skip the
tick), and each individual action is a no-op when another pass already did it.
On SQLite there is one process, so the lock degrades to a pass-through.

The sweep never raises: a failing domain logs and leaves the others to run,
because the export that cannot render must not stop the approval that must
expire.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import Principal
from ..core.config import settings
from ..db.session import get_sessionmaker
from ..models.governance import ApprovalRequest, ApprovalStatus, Secret, SecretStatus
from ..models.identity import Role
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


async def _try_lock(session: AsyncSession) -> bool:
    if session.bind.dialect.name != "postgresql":
        return True
    # Session-scoped: released automatically when the connection closes at the
    # end of the tick, so a crashed worker cannot wedge the clock.
    result = await session.execute(select(func.pg_try_advisory_lock(ADVISORY_LOCK_KEY)))
    return bool(result.scalar())


async def _sweep_exports(counts: dict[str, int]) -> None:
    """Fire due export schedules, then generate each queued job."""
    from . import exports

    async with get_sessionmaker()() as session:
        queued = await exports.run_due_schedules(session)
        await session.commit()
    for job_id, workspace_id in queued:
        await exports.run_export_job(job_id, workspace_id)
    counts["exports_fired"] = len(queued)


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
        due: list[TestSuite] = []
        for suite in suites:
            candidates = [
                _as_utc(value)
                for value in (
                    suite.last_run_at,
                    latest_start.get(suite.id),
                    suite.created_at,
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
        muted = (
            (
                await session.execute(
                    select(Alert).where(Alert.status == AlertStatus.MUTED.value)
                )
            )
            .scalars()
            .all()
        )
        for alert in muted:
            metadata: dict[str, Any] = dict(alert.event_metadata or {})
            raw = metadata.get("muted_until")
            until = None
            if isinstance(raw, str):
                try:
                    until = dt.datetime.fromisoformat(raw)
                except ValueError:
                    until = None
            if until is None or _as_utc(until) > now:
                continue
            alert.status = AlertStatus.OPEN.value
            metadata["mute_expired_at"] = now.isoformat()
            alert.event_metadata = metadata
            reopened += 1
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
        rows = (
            (
                await session.execute(
                    select(Secret).where(Secret.status.in_(DERIVED_SECRET_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        for secret in rows:
            expires_at = _as_utc(secret.expires_at)
            next_rotation = _as_utc(secret.next_rotation_at)
            if expires_at is not None and expires_at <= now:
                derived = SecretStatus.EXPIRED.value
            elif next_rotation is not None and next_rotation <= now:
                derived = SecretStatus.ROTATION_OVERDUE.value
            elif expires_at is not None and expires_at <= now + EXPIRING_WINDOW:
                derived = SecretStatus.EXPIRING_SOON.value
            else:
                derived = SecretStatus.ACTIVE.value
            if secret.status != derived:
                secret.status = derived
                changed += 1
        await session.commit()
    counts["secrets_restatused"] = changed


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
    async with get_sessionmaker()() as lock_session:
        if not await _try_lock(lock_session):
            return counts
        for sweep in (
            _sweep_exports,
            _sweep_test_suites,
            _sweep_approvals,
            _sweep_alert_mutes,
            _sweep_secret_statuses,
            _sweep_stale_runs,
        ):
            try:
                await sweep(counts)
            except Exception:  # noqa: BLE001 - one domain must not stop the others
                log.exception("scheduler sweep %s failed", sweep.__name__)
        # The advisory lock is session-scoped: closing the session's connection
        # releases it, so no explicit unlock is needed here.
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
