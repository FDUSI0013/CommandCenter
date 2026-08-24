"""The platform clock: what time alone is supposed to do, done on a sweep.

Every promise a screen makes about cadence — a cron that fires an export, a
suite that runs nightly, an SLA that expires an unattended approval, a mute
that lapses, a secret status that follows its dates — resolves to one function,
``scheduler.run_once``. These tests call it directly: the loop around it is
plumbing, the tick is the behaviour.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from fulcrum_ops_api.models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    Secret,
    SecretStatus,
)
from fulcrum_ops_api.models.operations import Alert, AlertStatus, ExportJob, ExportSchedule
from fulcrum_ops_api.models.quality import RunTrigger
from fulcrum_ops_api.models.quality import TestRun as TestRunRow
from fulcrum_ops_api.services import scheduler


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def test_a_due_export_schedule_fires_and_produces_a_file(
    admin_client, db, workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("FULCRUM_OPS_EXPORT_SPOOL_DIR", str(tmp_path))

    created = await admin_client.post(
        "/api/v1/exports/schedules",
        json={
            "name": "Nightly audit",
            "source_screen": "Audit Trail",
            "cron": "0 2 * * *",
            "export_format": "CSV",
            "filters": {"days": 7},
        },
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    await db.execute(
        update(ExportSchedule)
        .where(ExportSchedule.id == schedule_id)
        .values(next_run_at=_now() - dt.timedelta(minutes=5))
    )

    counts = await scheduler.run_once()
    assert counts["exports_fired"] == 1

    jobs = await db.scalars(select(ExportJob))
    assert len(jobs) == 1
    assert jobs[0].status == "Ready", jobs[0].error
    assert jobs[0].requested_by_user_id is None, "a cadence firing is the system's"

    schedule = await db.get(ExportSchedule, schedule_id)
    assert schedule.last_run_at is not None
    next_run = schedule.next_run_at
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=dt.UTC)
    assert next_run > _now(), "the cadence advanced"


async def test_an_unattended_approval_expires_when_its_sla_elapses(db, factory, workspace):
    overdue = await factory.approval(workspace, sla_due_at=_now() - dt.timedelta(hours=1))
    fresh = await factory.approval(
        workspace, request_ref="REQ-FRESH", sla_due_at=_now() + dt.timedelta(hours=4)
    )

    counts = await scheduler.run_once()
    assert counts["approvals_expired"] == 1

    assert (await db.get(ApprovalRequest, overdue.id)).status == ApprovalStatus.EXPIRED.value
    assert (await db.get(ApprovalRequest, fresh.id)).status == ApprovalStatus.PENDING.value


async def test_a_lapsed_mute_reopens_the_alert(db, factory, workspace):
    lapsed = await factory.alert(
        workspace,
        status=AlertStatus.MUTED.value,
        event_metadata={"muted_until": (_now() - dt.timedelta(minutes=1)).isoformat()},
    )
    still_muted = await factory.alert(
        workspace,
        alert_ref="ALT-STILL",
        status=AlertStatus.MUTED.value,
        event_metadata={"muted_until": (_now() + dt.timedelta(hours=1)).isoformat()},
    )

    counts = await scheduler.run_once()
    assert counts["alerts_unmuted"] == 1

    assert (await db.get(Alert, lapsed.id)).status == AlertStatus.OPEN.value
    assert (await db.get(Alert, still_muted.id)).status == AlertStatus.MUTED.value


async def test_secret_statuses_follow_their_own_dates(db, factory, workspace):
    expired = await factory.secret(
        workspace, name="Old cert", expires_at=_now() - dt.timedelta(days=1)
    )
    overdue = await factory.secret(workspace, name="Stale key")
    await db.execute(
        update(Secret)
        .where(Secret.id == overdue.id)
        .values(next_rotation_at=_now() - dt.timedelta(days=2))
    )
    revoked = await factory.secret(
        workspace, name="Burned key", status=SecretStatus.REVOKED.value
    )

    await scheduler.run_once()

    assert (await db.get(Secret, expired.id)).status == SecretStatus.EXPIRED.value
    assert (await db.get(Secret, overdue.id)).status == SecretStatus.ROTATION_OVERDUE.value
    assert (await db.get(Secret, revoked.id)).status == SecretStatus.REVOKED.value, (
        "a human decision is not the clock's to undo"
    )


async def test_a_scheduled_suite_starts_a_run_with_the_schedule_trigger(
    db, factory, workspace, monkeypatch
):
    from fulcrum_ops_api.services import testing

    async def swallowed(_run_id: str, _workspace_id: str) -> None:
        return None

    monkeypatch.setattr(testing, "execute_run", swallowed)

    suite = await factory.test_suite(
        workspace,
        name="Nightly regression",
        schedule_cron="*/5 * * * *",
    )
    # A fresh suite anchors its cadence at creation; age it so a tick is due.
    from fulcrum_ops_api.models.quality import TestSuite

    await db.execute(
        update(TestSuite)
        .where(TestSuite.id == suite.id)
        .values(
            created_at=_now() - dt.timedelta(hours=2),
            last_run_at=_now() - dt.timedelta(hours=2),
        )
    )

    counts = await scheduler.run_once()
    assert counts["test_suites_started"] == 1

    runs = await db.scalars(select(TestRunRow))
    assert len(runs) == 1
    assert runs[0].trigger == RunTrigger.SCHEDULE.value

    again = await scheduler.run_once()
    assert again["test_suites_started"] == 0, "one tick per cadence, not one per sweep"


async def test_a_run_orphaned_by_a_restart_is_reaped(db, factory, workspace):
    """A run left non-terminal past its deadline is failed closed, not stuck."""
    from fulcrum_ops_api.models.quality import TestRun, TestRunStatus

    suite = await factory.test_suite(workspace, name="Orphan suite")
    orphan = await factory.add(TestRun(
        workspace_id=workspace.id, suite_id=suite.id, run_ref="tr-orphan",
        status=TestRunStatus.RUNNING.value, total_cases=4, trigger="Manual",
        summary={}, baseline_comparison={},
    ))
    # Age it well past the suite deadline so it is unambiguously abandoned.
    await db.execute(
        update(TestRun).where(TestRun.id == orphan.id).values(
            started_at=_now() - dt.timedelta(seconds=3000),
            created_at=_now() - dt.timedelta(seconds=3000),
        )
    )
    # A fresh run, still within its lifetime, must be left alone.
    fresh = await factory.add(TestRun(
        workspace_id=workspace.id, suite_id=suite.id, run_ref="tr-fresh",
        status=TestRunStatus.RUNNING.value, total_cases=4, trigger="Manual",
        summary={}, baseline_comparison={},
    ))

    counts = await scheduler.run_once()
    assert counts["stale_runs_reaped"] == 1

    assert (await db.get(TestRun, orphan.id)).status == TestRunStatus.ERROR.value
    assert (await db.get(TestRun, fresh.id)).status == TestRunStatus.RUNNING.value
