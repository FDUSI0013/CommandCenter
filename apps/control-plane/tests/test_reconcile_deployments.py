"""Regression tests for the hand-offs reconciled into the deployments module.

Each of these was found by an owner of another file during the 2026-09-18 fix
pass and needed a change here:

* the History tab exports with ``terminal=true`` and the server ignored it, so
  the file carried releases that were not in the table it was exported from;
* two pipelines reaching their approval gates in the same instant picked the same
  ``REQ-n`` and the loser's pipeline was abandoned at the gate;
* a release that failed (or had to be rolled back) raised nothing in Alerts &
  Incidents.

Everything goes through the real request path, as the rest of the suite does.
"""

from __future__ import annotations

import asyncio
import csv
import io
import sqlite3

import pytest
from sqlalchemy import event, select

from fulcrum_ops_api.models.governance import ApprovalRequest
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.operations import (
    Alert,
    AlertSeverity,
    AlertStatus,
    DeploymentStageStatus,
    DeploymentStatus,
)
from fulcrum_ops_api.services import deployments as deployments_service


@pytest.fixture
def instant_pipeline(monkeypatch) -> None:
    """The runner's simulated stage durations, emptied -- as test_state_machines does."""
    monkeypatch.setattr(deployments_service, "STAGE_RUNTIME_SECONDS", {})


@pytest.fixture(autouse=True)
def stop_pipelines():
    """Cancel any pipeline a test left running, before its database goes away."""
    yield
    for deployment_id in list(deployments_service.runner._tasks):
        deployments_service.runner.cancel(deployment_id)


async def wait_for(probe, predicate, *, give_up_after: float = 30.0):
    """Poll ``probe`` until ``predicate`` holds, the way the console polls."""
    deadline = asyncio.get_running_loop().time() + give_up_after
    latest = None
    while asyncio.get_running_loop().time() < deadline:
        latest = await probe()
        if predicate(latest):
            return latest
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition never held; last saw {latest!r}")


async def pipeline_state(client, deployment_id: str) -> dict:
    """The deployment's status and its stages' statuses, in one look."""
    deployment = (await client.get(f"/api/v1/deployments/{deployment_id}")).json()
    stages = (await client.get(f"/api/v1/deployments/{deployment_id}/stages")).json()
    return {
        "status": deployment["status"],
        "stages": {stage["name"]: stage["status"] for stage in stages},
    }


def parked_or_over(state: dict) -> bool:
    """At the gate, or finished -- so a pipeline that dies says so at once."""
    return state["stages"]["Approval"] == DeploymentStageStatus.RUNNING.value or state[
        "status"
    ] in (DeploymentStatus.FAILED.value, DeploymentStatus.HALTED.value)


# ---------------------------------------------------------------------------
# 134 - the History export is the History table
# ---------------------------------------------------------------------------


def _exported_refs(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["Deployment"] for row in csv.DictReader(io.StringIO(response.text))}


async def test_the_history_export_leaves_out_releases_still_in_flight(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")
    staging = await factory.environment(workspace, name="Staging")
    await factory.deployment(
        workspace, environment, deployment_ref="dep-1", status=DeploymentStatus.SUCCEEDED.value
    )
    await factory.deployment(
        workspace, environment, deployment_ref="dep-2", status=DeploymentStatus.FAILED.value
    )
    await factory.deployment(
        workspace, staging, deployment_ref="dep-3", status=DeploymentStatus.RUNNING.value
    )
    await factory.deployment(
        workspace, staging, deployment_ref="dep-4", status=DeploymentStatus.HALTED.value
    )

    # Exactly the call the History tab makes.
    history = await admin_client.get("/api/v1/deployments/export", params={"terminal": "true"})
    listed = await admin_client.get("/api/v1/deployments", params={"terminal": "true"})

    assert _exported_refs(history) == {"dep-1", "dep-2", "dep-4"}
    assert _exported_refs(history) == {row["deployment_ref"] for row in listed.json()["items"]}

    in_flight = await admin_client.get("/api/v1/deployments/export", params={"terminal": "false"})
    assert _exported_refs(in_flight) == {"dep-3"}

    everything = await admin_client.get("/api/v1/deployments/export")
    assert _exported_refs(everything) == {"dep-1", "dep-2", "dep-3", "dep-4"}


# ---------------------------------------------------------------------------
# 109 - losing the race for a REQ-n does not cost the release
# ---------------------------------------------------------------------------


async def test_a_gate_that_loses_the_race_for_its_reference_still_opens(
    admin_client, db, db_engine, factory, workspace, instant_pipeline
):
    """Two gates read the same count() and the unique constraint refuses the second.

    The other gate is played by a second connection that takes the reference in
    the instant between this runner's read and its insert -- the way the
    approvals suite stages the same race for its own allocator.
    """
    environment = await factory.environment(workspace, name="Production East")
    other_gates = await factory.approval(workspace, request_ref="FIN-1")
    contested = "REQ-2"  # one request on file, so the allocator's first pick
    taken: list[str] = []

    def take_it_first(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if taken or not statement.lstrip().upper().startswith("INSERT INTO APPROVAL_REQUESTS"):
            return
        taken.append(contested)
        with sqlite3.connect(db_engine.url.database, timeout=30) as other:
            other.execute(
                "UPDATE approval_requests SET request_ref = ? WHERE id = ?",
                (contested, other_gates.id),
            )

    event.listen(db_engine.sync_engine, "before_cursor_execute", take_it_first)
    try:
        created = await admin_client.post(
            "/api/v1/deployments",
            json={"environment_id": environment.id, "version": "v2.0.0"},
        )
        assert created.status_code == 201, created.text
        deployment_id = created.json()["id"]
        state = await wait_for(
            lambda: pipeline_state(admin_client, deployment_id), parked_or_over
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", take_it_first)

    assert taken, "the race was staged"
    assert state["status"] == DeploymentStatus.RUNNING.value, (
        "the release was written off because its gate could not get a reference"
    )
    assert state["stages"]["Approval"] == DeploymentStageStatus.RUNNING.value
    assert state["stages"]["Deploy"] == DeploymentStageStatus.PENDING.value

    refs = await db.scalars(
        select(ApprovalRequest.request_ref).where(ApprovalRequest.workspace_id == workspace.id)
    )
    assert sorted(refs) == [contested, "REQ-3"]
    deployment = (await admin_client.get(f"/api/v1/deployments/{deployment_id}")).json()
    gate = await db.get(ApprovalRequest, deployment["approval_request_id"])
    assert gate.request_ref == "REQ-3"
    assert gate.payload["deployment_id"] == deployment_id


# ---------------------------------------------------------------------------
# 49 - a release that goes wrong is raised in Alerts
# ---------------------------------------------------------------------------


async def decide_the_gate(client, as_role, deployment_id: str) -> dict:
    """A second person opens the gate; wait for the release to come to rest."""
    await wait_for(lambda: pipeline_state(client, deployment_id), parked_or_over)
    async with as_role(Role.APPROVER) as reviewer:
        decided = await reviewer.post(
            f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
        )
    assert decided.status_code == 200, decided.text
    return await wait_for(
        lambda: pipeline_state(client, deployment_id),
        lambda state: state["status"]
        in (DeploymentStatus.SUCCEEDED.value, DeploymentStatus.FAILED.value),
    )


async def a_release_that_fails(client, as_role, environment) -> tuple[str, dict]:
    """Deploy, and have the environment go offline while the release is parked
    at its gate -- the Deploy stage then has nowhere to land."""
    created = await client.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": "v2.0.0", "commit_ref": "9f2c1ab"},
    )
    assert created.status_code == 201, created.text
    deployment_id = created.json()["id"]
    await wait_for(lambda: pipeline_state(client, deployment_id), parked_or_over)
    retired = await client.patch(
        f"/api/v1/environments/{environment.id}", json={"status": "Offline"}
    )
    assert retired.status_code == 200, retired.text
    return deployment_id, await decide_the_gate(client, as_role, deployment_id)


async def test_a_release_that_fails_raises_one_high_alert(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id, state = await a_release_that_fails(admin_client, as_role, environment)
    assert state["status"] == DeploymentStatus.FAILED.value

    raised = (await admin_client.get("/api/v1/alerts")).json()["items"]
    assert len(raised) == 1, raised
    alert = raised[0]
    assert alert["source"] == deployments_service.SOURCE_SCREEN
    assert alert["severity"] == AlertSeverity.HIGH.value
    assert alert["status"] == AlertStatus.OPEN.value
    assert alert["dedupe_key"] == f"deployment:{deployment_id}:failed"
    assert (alert["source_entity_type"], alert["source_entity_id"]) == (
        "Deployment",
        deployment_id,
    )
    assert "v2.0.0" in alert["title"]
    assert "Production East" in alert["description"]
    assert "offline" in alert["description"]


async def test_a_release_that_succeeds_raises_nothing(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    created = await admin_client.post(
        "/api/v1/deployments", json={"environment_id": environment.id, "version": "v2.0.0"}
    )
    state = await decide_the_gate(admin_client, as_role, created.json()["id"])

    assert state["status"] == DeploymentStatus.SUCCEEDED.value
    assert (await admin_client.get("/api/v1/alerts")).json()["items"] == []


async def test_a_rollback_that_lands_raises_an_alert_against_the_release_it_undid(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    await factory.deployment(
        workspace, environment, version="v1.9.0", status=DeploymentStatus.SUCCEEDED.value
    )
    live = await factory.deployment(
        workspace, environment, version="v2.0.0", status=DeploymentStatus.SUCCEEDED.value
    )

    asked = await admin_client.post(f"/api/v1/deployments/{live.id}/rollback", json={})
    assert asked.status_code == 200, asked.text
    rollback_id = asked.json()["data"]["deployment_id"]
    await wait_for(lambda: pipeline_state(admin_client, rollback_id), parked_or_over)
    assert (await admin_client.get("/api/v1/alerts")).json()["items"] == [], (
        "asking for a rollback undoes nothing yet, and so raises nothing yet"
    )

    state = await decide_the_gate(admin_client, as_role, rollback_id)
    assert state["status"] == DeploymentStatus.SUCCEEDED.value

    raised = (await admin_client.get("/api/v1/alerts")).json()["items"]
    assert len(raised) == 1, raised
    alert = raised[0]
    assert alert["severity"] == AlertSeverity.HIGH.value
    assert alert["source"] == deployments_service.SOURCE_SCREEN
    assert alert["dedupe_key"] == f"deployment:{live.id}:rolled_back"
    assert alert["source_entity_id"] == live.id
    assert "v2.0.0" in alert["title"]
    assert "v1.9.0" in alert["description"]


async def test_a_switched_off_rule_silences_the_failure_without_losing_it(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    """The raise goes through the workspace's rules like any other screen's."""
    await factory.alert_rule(
        workspace,
        name="Failed releases",
        source=deployments_service.SOURCE_SCREEN,
        severity=AlertSeverity.HIGH.value,
        enabled=False,
    )
    environment = await factory.environment(workspace, name="Production East")
    deployment_id, state = await a_release_that_fails(admin_client, as_role, environment)
    assert state["status"] == DeploymentStatus.FAILED.value

    raised = (await admin_client.get("/api/v1/alerts")).json()["items"]
    assert [row["status"] for row in raised] == [AlertStatus.MUTED.value]
    assert raised[0]["dedupe_key"] == f"deployment:{deployment_id}:failed"


async def test_an_alert_that_cannot_be_raised_does_not_strand_the_release(
    admin_client, as_role, db, db_engine, factory, workspace, instant_pipeline
):
    """The alert is a courtesy; closing the release out is the duty. The two
    share a transaction, and the runner's last resort runs the same code, so a
    fault in the first must not be able to cost the second."""
    environment = await factory.environment(workspace, name="Production East")

    def refuse_alerts(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO ALERTS"):
            raise RuntimeError("staged fault: the alerts table cannot be written")

    event.listen(db_engine.sync_engine, "before_cursor_execute", refuse_alerts)
    try:
        deployment_id, state = await a_release_that_fails(admin_client, as_role, environment)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", refuse_alerts)

    assert state["status"] == DeploymentStatus.FAILED.value
    assert state["stages"]["Deploy"] == DeploymentStageStatus.FAILED.value
    stages = (await admin_client.get(f"/api/v1/deployments/{deployment_id}/stages")).json()
    deploy = next(stage for stage in stages if stage["name"] == "Deploy")
    assert "offline" in deploy["log"], "closed out by the stage itself, not by the last resort"
    assert await db.count(Alert, Alert.workspace_id == workspace.id) == 0
