"""Regressions for the run screens, from the reconcile pass after the audit fixes.

Run Agent records a request as an open trace and dispatches nothing. The writer
was fixed to mark such a trace as console-issued; this is the reader's half. A
request no runtime ever reported on has no end time, and "no end time" was the
whole definition of Running -- so it read Running on Live Runs, on Agent Detail
and in the KPI row for as long as the store kept it.
"""

from __future__ import annotations

import datetime as dt

from fulcrum_ops_api.core.config import settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


async def _request_run(http, agent, **body) -> str:
    """Press Run Agent, the way the console does, and return the run id."""
    response = await http.post(f"/api/v1/agents/{agent.id}/run", json=body)
    assert response.status_code == 202, response.text
    return response.json()["entity_id"]


def _age(engine, run_id: str, by: dt.timedelta) -> None:
    """Make the store's copy of a run older, instead of sleeping for an hour."""
    engine.traces[run_id]["start_time"] = _iso(_utcnow() - by)


async def _row(http, run_id: str, **params) -> dict | None:
    listed = await http.get("/api/v1/runs", params=params)
    assert listed.status_code == 200, listed.text
    return next((row for row in listed.json()["items"] if row["id"] == run_id), None)


async def test_a_console_request_nobody_answered_stops_reading_as_running(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    run_id = await _request_run(admin_client, agent, input="hello")

    # Just pressed: a runtime may yet pick it up, and Running is the truth.
    fresh = await _row(admin_client, run_id)
    assert fresh is not None and fresh["status"] == "Running"

    _age(engine, run_id, dt.timedelta(hours=2))

    stale = await _row(admin_client, run_id)
    assert stale is not None and stale["status"] == "Failed", stale
    # The Status filter and the table agree, in both directions.
    assert await _row(admin_client, run_id, status="Failed") is not None
    assert await _row(admin_client, run_id, status="Running") is None

    summary = await admin_client.get("/api/v1/runs/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["total_runs"] == 1
    assert summary.json()["success_rate"] == 0.0

    # A Failed row with no reason beside it reads as a fault in the screen.
    inspector = await admin_client.get(f"/api/v1/runs/{run_id}")
    assert inspector.status_code == 200, inspector.text
    assert inspector.json()["status"] == "Failed"
    assert "never picked up" in inspector.json()["errors"]["message"]


async def test_only_a_console_request_is_judged_by_its_age(
    admin_client, factory, workspace, engine
):
    """An agent's own run is open because the agent has not finished, and a
    batch job may take all night. Beside it, an unanswered request of the same
    age is not running at all."""
    agent = await factory.provisioned_agent(workspace, engine, name="Batch Bot")
    reported = engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=_utcnow() - dt.timedelta(hours=5),
        metadata={"tenant": "acme"},
    )
    reported["end_time"] = None
    requested = await _request_run(admin_client, agent, input="hello")
    _age(engine, requested, dt.timedelta(hours=5))

    listed = await admin_client.get("/api/v1/runs", params={"agent_id": agent.id})

    assert listed.status_code == 200, listed.text
    statuses = {row["id"]: row["status"] for row in listed.json()["items"]}
    assert statuses == {reported["id"]: "Running", requested: "Failed"}
    inspector = await admin_client.get(f"/api/v1/runs/{reported['id']}")
    assert inspector.json()["errors"]["message"] is None


async def test_a_console_request_a_runtime_picked_up_is_still_running(
    admin_client, factory, workspace, engine
):
    """Handed its run id, a runtime reports under it. From the first span on it
    is a run in progress, not an unanswered request, whatever its age."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    picked_up = await _request_run(admin_client, agent, input="hello")
    ignored = await _request_run(admin_client, agent, input="hello again")
    for run_id in (picked_up, ignored):
        _age(engine, run_id, dt.timedelta(hours=2))
    engine.traces[picked_up]["span_count"] = 3

    listed = await admin_client.get("/api/v1/runs", params={"agent_id": agent.id})

    assert listed.status_code == 200, listed.text
    statuses = {row["id"]: row["status"] for row in listed.json()["items"]}
    assert statuses == {picked_up: "Running", ignored: "Failed"}


async def test_the_age_limit_is_a_setting_and_zero_switches_it_off(
    admin_client, factory, workspace, engine, monkeypatch
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    run_id = await _request_run(admin_client, agent)
    _age(engine, run_id, dt.timedelta(minutes=10))

    assert (await _row(admin_client, run_id))["status"] == "Running"

    monkeypatch.setattr(settings, "run_request_abandoned_after_seconds", 300.0)
    assert (await _row(admin_client, run_id))["status"] == "Failed"

    monkeypatch.setattr(settings, "run_request_abandoned_after_seconds", 0.0)
    assert (await _row(admin_client, run_id))["status"] == "Running"
