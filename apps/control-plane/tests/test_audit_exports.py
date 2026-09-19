"""Exports: retention, the time window, interrupted jobs and schedule inputs.

Defects from the September audit, each pinned by driving the API the way the
console does -- and the platform clock the way the scheduler does -- and reading
the answer back off the database and the spool.

* Retention was promised by three docstrings and carried out by nothing. Files
  stayed on the spool for ever, no job ever became Expired, the lazy expiry on
  download was rolled back by its own 404, and the KPI row called files
  downloadable that the download route refused.
* The "last N days" window was applied to every dataset, and for an inventory
  the only date to window on is the day the record was created. A default
  export of Secrets Compliance held only the secrets created that month.
* Generation lives only in the process that started it. A deploy or an OOM kill
  left the row Queued or Generating for ever, with nothing to move it on.
* A schedule collected recipients, showed them and audited them, and nothing
  ever sent them anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import select, update

from conftest import utcnow
from fulcrum_ops_api.models.governance import Secret
from fulcrum_ops_api.models.operations import Alert, ExportJob, ExportSchedule, ExportStatus
from fulcrum_ops_api.services import exports as exports_service
from fulcrum_ops_api.services import scheduler


@pytest.fixture(autouse=True)
def spool(tmp_path, monkeypatch) -> Path:
    """Every test writes to its own spool, and the hourly orphan sweep is due."""
    root = tmp_path / "spool"
    monkeypatch.setenv("FULCRUM_OPS_EXPORT_SPOOL_DIR", str(root))
    monkeypatch.setattr(exports_service, "_last_orphan_sweep", None)
    return root


async def generate(http, *, source_screen: str = "Audit Trail", **fields) -> dict:
    """Request an export and hand back the job once its file exists.

    The ASGI transport runs the route's background task before it returns the
    response, so the job is already terminal by the time the status is read.
    """
    body = {"name": f"{source_screen} extract", "source_screen": source_screen, **fields}
    queued = await http.post("/api/v1/exports", json=body)
    assert queued.status_code == 202, queued.text
    job = (await http.get(f"/api/v1/exports/{queued.json()['id']}/status")).json()
    assert job["status"] == "Ready", job
    return job


async def lapse(db, job_id: str) -> None:
    """Close a job's retention window, as a week going by would."""
    await db.execute(
        update(ExportJob)
        .where(ExportJob.id == job_id)
        .values(expires_at=utcnow() - dt.timedelta(minutes=1))
    )


def spooled(root: Path) -> list[str]:
    return sorted(path.name for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def test_the_clock_expires_a_lapsed_export_and_drops_its_file(admin_client, db, spool):
    kept = await generate(admin_client, name="Still in retention")
    lapsed = await generate(admin_client, name="A week old")
    assert len(spooled(spool)) == 2
    await lapse(db, lapsed["id"])

    await scheduler.run_once()

    row = await db.get(ExportJob, lapsed["id"])
    assert row.status == ExportStatus.EXPIRED.value, "nothing ever flipped a job to Expired"
    assert row.storage_key is None
    assert (await db.get(ExportJob, kept["id"])).status == ExportStatus.READY.value
    assert len(spooled(spool)) == 1, "the lapsed file stayed on the spool volume for ever"

    expired = await admin_client.get("/api/v1/exports", params={"status": "Expired"})
    assert [item["id"] for item in expired.json()["items"]] == [lapsed["id"]]
    ready = await admin_client.get("/api/v1/exports", params={"status": "Ready"})
    assert [item["id"] for item in ready.json()["items"]] == [kept["id"]]


async def test_a_refused_download_leaves_the_job_expired(admin_client, db, spool):
    """The lazy expiry ran, deleted the file, and was rolled back by its own 404."""
    job = await generate(admin_client)
    await lapse(db, job["id"])

    refused = await admin_client.get(f"/api/v1/exports/{job['id']}/download")
    assert refused.status_code == 404, refused.text

    row = await db.get(ExportJob, job["id"])
    assert row.status == ExportStatus.EXPIRED.value
    assert row.storage_key is None
    assert spooled(spool) == []


async def test_a_ready_row_whose_file_is_gone_stops_offering_the_download(
    admin_client, db, spool
):
    job = await generate(admin_client)
    for path in spool.rglob("*"):
        if path.is_file():
            path.unlink()

    refused = await admin_client.get(f"/api/v1/exports/{job['id']}/download")
    assert refused.status_code == 404, refused.text

    shown = (await admin_client.get(f"/api/v1/exports/{job['id']}")).json()
    assert shown["status"] == "Expired"
    assert shown["is_downloadable"] is False, "every Download click was a fresh 404"


async def test_downloadable_now_stops_counting_a_file_past_retention(admin_client, db):
    await generate(admin_client, name="Downloadable")
    lapsed = await generate(admin_client, name="Past retention")
    await lapse(db, lapsed["id"])

    summary = (await admin_client.get("/api/v1/exports/summary")).json()
    assert summary["ready"] == 1, "the card counted a file the download route refuses"


async def test_the_orphan_sweep_removes_only_files_no_job_points_at(
    admin_client, db, workspace, spool
):
    job = await generate(admin_client)
    folder = spool / workspace.id
    orphan = folder / "exp-7-0198c1de-0000-7000-8000-000000000001.csv"
    partial = folder / "exp-8-0198c1de-0000-7000-8000-000000000002.json.part"
    fresh = folder / "exp-9-0198c1de-0000-7000-8000-000000000003.csv"
    foreign = folder / "notes.txt"
    for path in (orphan, partial, fresh, foreign):
        path.write_bytes(b"x")
    long_ago = (utcnow() - dt.timedelta(hours=3)).timestamp()
    for path in (orphan, partial, foreign, *[p for p in folder.iterdir() if job["id"] in p.name]):
        os.utime(path, (long_ago, long_ago))

    await scheduler.run_once()

    left = spooled(spool)
    assert orphan.name not in left, "a file with no row is kept for ever"
    assert partial.name not in left, "a killed write leaves its .part behind"
    assert fresh.name in left, "a file this young may still be waiting for its row"
    assert foreign.name in left, "only names this module wrote are ever swept"
    assert any(job["id"] in name for name in left), "a live job's file was swept"
    assert (await admin_client.get(f"/api/v1/exports/{job['id']}/download")).status_code == 200


# ---------------------------------------------------------------------------
# The time window
# ---------------------------------------------------------------------------


async def test_an_inventory_export_holds_every_record_whatever_the_window(
    admin_client, db, factory, workspace
):
    """The dialog sends ``days: 30`` with every request, inventory or not."""
    old = await factory.secret(workspace, name="Payments signing key")
    await factory.secret(workspace, name="Created this morning")
    await db.execute(
        update(Secret)
        .where(Secret.id == old.id)
        .values(created_at=utcnow() - dt.timedelta(days=400))
    )

    job = await generate(
        admin_client, source_screen="Secrets Compliance", filters={"days": 30, "vault": []}
    )

    assert job["row_count"] == 2, "the secret most likely to be overdue was left out"
    body = (await admin_client.get(f"/api/v1/exports/{job['id']}/download")).text
    assert "Payments signing key" in body
    assert "days" not in job["filters"], "the record must say what was actually applied"


async def test_a_log_of_events_is_still_windowed(admin_client, db, factory, workspace):
    recent = await factory.alert(workspace, title="Raised today")
    stale = await factory.alert(workspace, title="Raised last quarter")
    await db.execute(
        update(Alert)
        .where(Alert.id == stale.id)
        .values(raised_at=utcnow() - dt.timedelta(days=90))
    )

    job = await generate(admin_client, source_screen="Alerts", filters={"days": 30})

    assert job["row_count"] == 1
    assert job["filters"] == {"days": 30}
    body = (await admin_client.get(f"/api/v1/exports/{job['id']}/download")).text
    assert recent.title in body and stale.title not in body


async def test_the_picker_is_told_which_datasets_take_a_window(admin_client):
    listed = (await admin_client.get("/api/v1/exports/datasets")).json()
    datasets = {item["source_screen"]: item for item in listed}

    for name in ("Agent Registry", "Alert Rules", "Budgets", "Quotas", "Secrets Compliance"):
        assert datasets[name]["windowed"] is False, name
        assert datasets[name]["time_label"] is None
    assert datasets["Alerts"]["windowed"] is True
    assert datasets["Alerts"]["time_label"] == "Raised At"
    assert datasets["Audit Trail"]["time_label"] == "Occurred At"

    summary = (await admin_client.get("/api/v1/exports/summary")).json()
    assert {"windowed", "time_label"} <= set(summary["datasets"][0])


# ---------------------------------------------------------------------------
# Jobs a restart left in flight
# ---------------------------------------------------------------------------


async def in_flight(factory, workspace, *, ref: str, status: str, age: dt.timedelta) -> ExportJob:
    """A job as a killed process leaves it: claimed or queued, then nothing."""
    then = utcnow() - age
    return await factory.add(
        ExportJob(
            workspace_id=workspace.id,
            export_ref=ref,
            name=f"Weekly audit trail {ref}",
            source_screen="Audit Trail",
            export_format="CSV",
            status=status,
            filters={"days": 7},
            requested_at=then,
            expires_at=then + dt.timedelta(days=7),
            created_at=then,
            updated_at=then,
        )
    )


async def test_the_clock_settles_jobs_a_restart_left_in_flight(
    admin_client, db, factory, workspace, spool
):
    wedged = await in_flight(
        factory, workspace, ref="exp-901", status="Generating", age=dt.timedelta(minutes=40)
    )
    stranded = await in_flight(
        factory, workspace, ref="exp-902", status="Queued", age=dt.timedelta(minutes=10)
    )
    rendering = await in_flight(
        factory, workspace, ref="exp-903", status="Generating", age=dt.timedelta(seconds=20)
    )
    just_queued = await in_flight(
        factory, workspace, ref="exp-904", status="Queued", age=dt.timedelta(seconds=5)
    )

    await scheduler.run_once()

    failed = await db.get(ExportJob, wedged.id)
    assert failed.status == ExportStatus.FAILED.value, "it said Generating for ever"
    assert "Interrupted" in failed.error
    assert failed.completed_at is not None

    finished = await db.get(ExportJob, stranded.id)
    assert finished.status == ExportStatus.READY.value, finished.error
    assert any(stranded.id in name for name in spooled(spool)), "picked up, and really generated"

    # Work that is within its lifetime belongs to the process doing it.
    assert (await db.get(ExportJob, rendering.id)).status == ExportStatus.GENERATING.value
    assert (await db.get(ExportJob, just_queued.id)).status == ExportStatus.QUEUED.value

    polled = (await admin_client.get(f"/api/v1/exports/{wedged.id}/status")).json()
    assert polled["status"] == "Failed", "the console polls until it sees a terminal state"



# ---------------------------------------------------------------------------
# Recipients nobody delivers to
# ---------------------------------------------------------------------------

WEEKLY = {"name": "Weekly audit trail", "source_screen": "Audit Trail", "cron": "0 6 * * 1"}


async def test_a_schedule_cannot_promise_a_delivery_nothing_makes(admin_client, db):
    summary = (await admin_client.get("/api/v1/exports/summary")).json()
    assert summary["delivery_available"] is False, "the console keys its input off this"

    refused = await admin_client.post(
        "/api/v1/exports/schedules", json={**WEEKLY, "recipients": ["audit@example.com"]}
    )
    assert refused.status_code == 422, "it was stored, shown and audited, and never sent"
    error = refused.json()["error"]
    assert error["details"]["field"] == "recipients"
    assert "not configured" in error["message"]
    assert await db.count(ExportSchedule) == 0

    created = await admin_client.post("/api/v1/exports/schedules", json=WEEKLY)
    assert created.status_code == 201, created.text
    assert created.json()["recipients"] == []


async def test_a_schedule_that_already_holds_recipients_can_still_be_edited(
    admin_client, factory, workspace
):
    """The editor sends the whole form back, recipients included."""
    legacy = await factory.add(
        ExportSchedule(
            workspace_id=workspace.id,
            name="Written before the check",
            source_screen="Audit Trail",
            export_format="CSV",
            cron="0 6 * * 1",
            filters={},
            recipients=["audit@example.com"],
            enabled=True,
            next_run_at=utcnow() + dt.timedelta(days=3),
        )
    )
    path = f"/api/v1/exports/schedules/{legacy.id}"

    renamed = await admin_client.patch(
        path, json={"name": "Weekly audit trail", "recipients": ["audit@example.com"]}
    )
    assert renamed.status_code == 200, renamed.text

    widened = await admin_client.patch(
        path, json={"recipients": ["audit@example.com", "ciso@example.com"]}
    )
    assert widened.status_code == 422, "nobody new may be promised a delivery"

    cleared = await admin_client.patch(path, json={"recipients": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["recipients"] == []


# ---------------------------------------------------------------------------
# Schedule inputs that validated and then broke something later
# ---------------------------------------------------------------------------


async def due_schedule(factory, workspace, *, name: str, cron: str, minutes_late: int):
    """A schedule the next tick owes a firing, written as an older build left it."""
    return await factory.add(
        ExportSchedule(
            workspace_id=workspace.id,
            name=name,
            source_screen="Audit Trail",
            export_format="CSV",
            cron=cron,
            filters={"days": 7},
            recipients=[],
            enabled=True,
            next_run_at=utcnow() - dt.timedelta(minutes=minutes_late),
        )
    )


@pytest.mark.parametrize(
    ("cron", "reason"),
    [
        ("0 0 31 4 *", "never fires"),  # every field in range; April has 30 days
        ("0 0 30 2 *", "never fires"),
        ("* * * * * *", "five fields"),  # a seconds field: due on every tick
    ],
)
async def test_a_cron_the_clock_could_not_honour_is_refused_at_the_door(
    admin_client, db, cron, reason
):
    refused = await admin_client.post("/api/v1/exports/schedules", json={**WEEKLY, "cron": cron})

    assert refused.status_code == 422, "it passed validation and then answered 500"
    assert reason in refused.text
    assert await db.count(ExportSchedule) == 0

    # An alias is five fields by another name, and a rare date is still a date.
    for index, fine in enumerate(("@daily", "0 0 29 2 *")):
        created = await admin_client.post(
            "/api/v1/exports/schedules", json={**WEEKLY, "name": f"Fine {index}", "cron": fine}
        )
        assert created.status_code == 201, created.text
        assert created.json()["next_run_at"] is not None


async def test_resuming_a_schedule_whose_cron_never_fires_explains_itself(
    admin_client, factory, workspace
):
    """Saved paused by an older build, so no validator ever looked at its cron."""
    parked = await factory.add(
        ExportSchedule(
            workspace_id=workspace.id,
            name="Saved paused",
            source_screen="Audit Trail",
            export_format="CSV",
            cron="0 0 31 4 *",
            enabled=False,
        )
    )

    resumed = await admin_client.patch(
        f"/api/v1/exports/schedules/{parked.id}", json={"enabled": True}
    )

    assert resumed.status_code == 422, "every Resume click was a 500"
    assert resumed.json()["error"]["details"]["field"] == "cron"


async def test_a_firing_is_named_inside_the_column_whatever_the_schedule_is_called(
    admin_client, db, factory, workspace
):
    """Postgres refuses 201 characters in a String(200); SQLite just stores them."""
    longest = "Quarterly attestation pack " + "x" * 173
    assert len(longest) == 200
    created = await admin_client.post(
        "/api/v1/exports/schedules", json={**WEEKLY, "name": longest}
    )
    assert created.status_code == 201, created.text
    await db.execute(
        update(ExportSchedule)
        .where(ExportSchedule.id == created.json()["id"])
        .values(next_run_at=utcnow() - dt.timedelta(minutes=1))
    )

    counts = await scheduler.run_once()

    assert counts["exports_fired"] == 1
    [job] = await db.scalars(select(ExportJob))
    assert len(job.name) <= 200, "the flush raised and took every tenant's firings with it"
    assert job.name.startswith("Quarterly attestation pack")
    assert job.name.endswith(f" — {utcnow():%Y-%m-%d}")
    assert job.status == ExportStatus.READY.value, job.error


async def test_one_schedule_that_cannot_fire_does_not_stop_the_others(
    admin_client, db, factory, workspace, other_workspace
):
    broken = await due_schedule(
        factory, workspace, name="Cannot be advanced", cron="0 0 31 4 *", minutes_late=30
    )
    healthy = await due_schedule(
        factory, other_workspace, name="Another tenant's Monday export", cron="0 6 * * 1",
        minutes_late=5,
    )

    counts = await scheduler.run_once()

    assert counts["exports_fired"] == 1, "the first failure rolled the whole batch back"
    [job] = await db.scalars(select(ExportJob))
    assert job.workspace_id == other_workspace.id
    assert job.status == ExportStatus.READY.value, job.error
    assert (await db.get(ExportSchedule, healthy.id)).next_run_at > utcnow()
    # Parked, not left due: it would otherwise sort first again on every tick.
    assert (await db.get(ExportSchedule, broken.id)).next_run_at is None
    assert (await scheduler.run_once())["exports_fired"] == 0


async def test_two_exports_requested_at_once_both_get_a_reference(admin_client, db):
    """Both requests count the same rows and both choose exp-1."""
    answers = await asyncio.gather(
        *[
            admin_client.post(
                "/api/v1/exports",
                json={"name": f"Concurrent {index}", "source_screen": "Audit Trail"},
            )
            for index in range(4)
        ]
    )

    assert [answer.status_code for answer in answers] == [202] * 4, [a.text for a in answers]
    refs = [job.export_ref for job in await db.scalars(select(ExportJob))]
    assert len(refs) == len(set(refs)) == 4
