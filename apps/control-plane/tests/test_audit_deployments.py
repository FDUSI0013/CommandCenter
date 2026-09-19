"""Regression tests for the deployment findings of the 2026-09-18 audit.

* the pipeline runner was started before the request that created (or approved)
  the deployment had committed, so on Postgres it could look, find nothing, and
  leave the release Queued with its environment locked;
* ``POST /deployments/{id}/approve`` let the person who started a release open
  its gate, and rewrote a request a reviewer had already rejected;
* a decision taken in Approvals & Audit had no way to reach the release it gates;
* pipelines orphaned by a restart were never picked up again;
* a rollback marked its source ``RolledBack`` when it was queued rather than when
  it landed, and ``active_deployment_count`` only ever grew;
* the progress stream pinned the request's database connection for its lifetime
  and polled once a second while nothing could change.

Everything goes through the real request path, as the rest of the suite does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect

import httpx
import pytest
from sqlalchemy import event, select, update

from conftest import APP_BASE_URL, error_code, principal_for
from fulcrum_ops_api.models.governance import ApprovalRequest, ApprovalStatus, AuditEvent
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.operations import (
    Deployment,
    DeploymentStage,
    DeploymentStageStatus,
    DeploymentStatus,
)
from fulcrum_ops_api.schemas.deployments import DeploymentCreate
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
    """Poll ``probe`` until ``predicate`` holds, the way the console polls.

    Generous on purpose: the limit is only ever reached by a failing test, and
    a loaded machine must not turn a slow pipeline into a red one.
    """
    deadline = asyncio.get_running_loop().time() + give_up_after
    latest = None
    while asyncio.get_running_loop().time() < deadline:
        latest = await probe()
        if predicate(latest):
            return latest
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition never held; last saw {latest!r}")


async def stage_statuses(client, deployment_id: str) -> dict[str, str]:
    response = await client.get(f"/api/v1/deployments/{deployment_id}/stages")
    return {stage["name"]: stage["status"] for stage in response.json()}


async def status_of(client, deployment_id: str) -> str:
    return (await client.get(f"/api/v1/deployments/{deployment_id}")).json()["status"]


async def parked_at_the_gate(client, environment, *, version: str = "v2.0.0") -> str:
    """Deploy through the API and wait for the pipeline to reach its gate."""
    created = await client.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": version, "commit_ref": "9f2c1ab"},
    )
    assert created.status_code == 201, created.text
    deployment_id = created.json()["id"]
    await wait_for(
        lambda: stage_statuses(client, deployment_id),
        lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
    )
    return deployment_id


class RunnerAtCommit:
    """What the runner had been handed at each commit that carried deployments.

    On Postgres the runner's first SELECT raced the request's COMMIT. SQLite
    cannot lose that race, so the order itself is what gets pinned.
    """

    def __init__(self, db_engine) -> None:
        self._engine = db_engine.sync_engine
        self.seen: list[tuple[frozenset[str], frozenset[str]]] = []
        self.approved: list[tuple[frozenset[str], frozenset[str]]] = []

    def _on_commit(self, connection) -> None:
        held = frozenset(deployments_service.runner._tasks)
        rows = connection.exec_driver_sql("SELECT id FROM deployments").scalars().all()
        self.seen.append((frozenset(rows), held))
        gates = connection.exec_driver_sql(
            "SELECT deployment_id FROM deployment_stages "
            "WHERE name = 'Approval' AND status = 'Approved'"
        ).scalars().all()
        self.approved.append((frozenset(gates), held))

    def __enter__(self) -> RunnerAtCommit:
        event.listen(self._engine, "commit", self._on_commit)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine, "commit", self._on_commit)

    def handed_over_before_commit(self, deployment_id: str) -> bool:
        """True if the runner already held ``deployment_id`` at the commit that
        first made its row visible to other connections."""
        known = next(tasks for rows, tasks in self.seen if deployment_id in rows)
        return deployment_id in known

    def resumed_before_the_gate_was_committed(self, deployment_id: str) -> bool:
        """True if the runner had been restarted by the commit that first showed
        other connections an ``Approved`` gate."""
        known = next(tasks for gates, tasks in self.approved if deployment_id in gates)
        return deployment_id in known


# ---------------------------------------------------------------------------
# 33 - the row is durable before the runner is allowed to look for it
# ---------------------------------------------------------------------------


async def test_a_deployment_is_committed_before_its_pipeline_is_started(
    admin_client, factory, workspace, db_engine, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")

    with RunnerAtCommit(db_engine) as order:
        created = await admin_client.post(
            "/api/v1/deployments",
            json={"environment_id": environment.id, "version": "v2.0.0"},
        )
        assert created.status_code == 201, created.text
        deployment_id = created.json()["id"]
        await wait_for(
            lambda: stage_statuses(admin_client, deployment_id),
            lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
        )

    assert not order.handed_over_before_commit(deployment_id), (
        "the runner was started before the deployment was committed; on Postgres it "
        "can look, find nothing, and leave the release Queued with nobody driving it"
    )


async def test_a_promotion_and_a_rollback_are_committed_before_their_pipelines_start(
    admin_client, factory, workspace, db_engine, instant_pipeline
):
    production = await factory.environment(workspace, name="Production East")
    staging = await factory.environment(workspace, name="Staging")
    await factory.deployment(
        workspace, production, version="v1.9.0", status=DeploymentStatus.SUCCEEDED.value
    )
    live = await factory.deployment(
        workspace, production, version="v2.0.0", status=DeploymentStatus.SUCCEEDED.value
    )

    async def at_the_gate(started) -> str:
        assert started.status_code == 200, started.text
        deployment_id = started.json()["data"]["deployment_id"]
        # Parked before the next request writes: SQLite has one writer.
        await wait_for(
            lambda: stage_statuses(admin_client, deployment_id),
            lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
        )
        return deployment_id

    with RunnerAtCommit(db_engine) as order:
        promoted = await at_the_gate(
            await admin_client.post(
                f"/api/v1/deployments/{live.id}/promote",
                json={"target_environment_id": staging.id},
            )
        )
        rolled_back = await at_the_gate(
            await admin_client.post(f"/api/v1/deployments/{live.id}/rollback", json={})
        )

    assert not order.handed_over_before_commit(promoted)
    assert not order.handed_over_before_commit(rolled_back)


async def test_an_approval_is_committed_before_the_pipeline_is_resumed(
    admin_client, as_role, factory, workspace, db_engine, instant_pipeline
):
    """The same race on the way out of the gate: a runner that cannot see the
    Approved stage yet finds the gate shut and exits, and the release stays
    parked behind a gate that reads Approved."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)
    await wait_for(
        lambda: asyncio.sleep(0, deployment_id in deployments_service.runner._tasks),
        lambda still_held: not still_held,
    )

    with RunnerAtCommit(db_engine) as order:
        async with as_role(Role.APPROVER) as reviewer:
            approved = await reviewer.post(
                f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
            )
        assert approved.status_code == 200, approved.text
        await wait_for(
            lambda: status_of(admin_client, deployment_id),
            lambda s: s == DeploymentStatus.SUCCEEDED.value,
        )

    assert not order.resumed_before_the_gate_was_committed(deployment_id)


async def test_the_runner_waits_for_a_deployment_that_has_not_landed_yet(
    admin_client, factory, workspace, admin, sessionmaker, instant_pipeline
):
    """The second guard: a runner handed a row its caller has not committed yet
    looks again instead of giving up for good."""
    environment = await factory.environment(workspace, name="Production East")
    principal = principal_for(workspace, admin, Role.ADMIN)

    async with sessionmaker() as session:
        deployment = await deployments_service.create_deployment(
            session,
            principal,
            DeploymentCreate(environment_id=environment.id, version="v2.0.0"),
        )
        deployment_id = deployment.id
        await deployments_service.run_pipeline(deployment_id, workspace.id)
        await asyncio.sleep(0.3)  # the runner has looked, and found nothing
        await session.commit()

    reached = await wait_for(
        lambda: stage_statuses(admin_client, deployment_id),
        lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
    )
    assert reached["Build"] == DeploymentStageStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# 36 - the gate takes a second person, and a closed request stays closed
# ---------------------------------------------------------------------------


async def gate_request(db, deployment_id: str) -> ApprovalRequest:
    deployment = await db.get(Deployment, deployment_id)
    return await db.get(ApprovalRequest, deployment.approval_request_id)


async def test_whoever_started_a_release_cannot_open_its_own_gate(
    admin_client, as_role, db, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)

    selfie = await admin_client.post(
        f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
    )

    assert selfie.status_code == 403, selfie.text
    assert error_code(selfie) == "permission_denied"
    assert "different reviewer" in selfie.text
    assert (await stage_statuses(admin_client, deployment_id))["Approval"] == (
        DeploymentStageStatus.RUNNING.value
    )
    assert (await gate_request(db, deployment_id)).status == ApprovalStatus.PENDING.value

    # A second person opens it, and is the one on record as having decided.
    async with as_role(Role.APPROVER) as reviewer:
        approved = await reviewer.post(
            f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
        )
    assert approved.status_code == 200, approved.text
    await wait_for(
        lambda: status_of(admin_client, deployment_id),
        lambda s: s == DeploymentStatus.SUCCEEDED.value,
    )
    decided = await gate_request(db, deployment_id)
    assert decided.status == ApprovalStatus.APPROVED.value
    assert decided.decided_by_user_id != decided.requested_by_user_id


async def test_whoever_started_a_release_may_still_withdraw_it_at_the_gate(
    admin_client, db, factory, workspace, instant_pipeline
):
    """Refusing your own release is a halt, which its owner may always do."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)

    withdrawn = await admin_client.post(
        f"/api/v1/deployments/{deployment_id}/approve",
        json={"approved": False, "note": "Wrong build"},
    )

    assert withdrawn.status_code == 200, withdrawn.text
    assert await status_of(admin_client, deployment_id) == DeploymentStatus.HALTED.value
    assert (await gate_request(db, deployment_id)).status == ApprovalStatus.REJECTED.value


async def test_a_key_cannot_open_the_gate(app, admin_client, factory, workspace, instant_pipeline):
    """An admin-scope key ranks above an approver and has no user id to compare."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)

    token, _row = await factory.api_key(workspace, name="Release bot", scopes=["admin"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL
    ) as bot:
        bot.headers["Authorization"] = f"Bearer {token}"
        waved_through = await bot.post(
            f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
        )

    assert waved_through.status_code == 403, waved_through.text
    assert "signed-in person" in waved_through.text
    assert (await stage_statuses(admin_client, deployment_id))["Deploy"] == (
        DeploymentStageStatus.PENDING.value
    )


async def test_a_request_a_reviewer_rejected_cannot_be_approved_from_the_deployments_tab(
    admin_client, as_role, db, factory, workspace, instant_pipeline
):
    """What the queue used to leave behind: the request closed as Rejected, the
    release still parked -- and one click on the other screen rewrote it."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)
    request_row = await gate_request(db, deployment_id)
    reviewer_b = await factory.user(
        workspace, email="b@northwind.test", full_name="Bea Reviewer", role=Role.APPROVER
    )
    await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_row.id)
        .values(
            status=ApprovalStatus.REJECTED.value,
            decided_by_user_id=reviewer_b.id,
            decided_at=dt.datetime.now(dt.UTC),
            decision_note="Change freeze",
        )
    )

    async with as_role(Role.APPROVER) as reviewer:
        overturned = await reviewer.post(
            f"/api/v1/deployments/{deployment_id}/approve", json={"approved": True}
        )
        assert overturned.status_code == 409, overturned.text
        assert error_code(overturned) == "conflict"
        assert request_row.request_ref in overturned.text

        still = await gate_request(db, deployment_id)
        assert still.status == ApprovalStatus.REJECTED.value
        assert still.decided_by_user_id == reviewer_b.id
        assert still.decision_note == "Change freeze"
        assert (await stage_statuses(admin_client, deployment_id))["Approval"] == (
            DeploymentStageStatus.RUNNING.value
        )

        # Agreeing with the reviewer carries the rejection through, and leaves
        # their decision -- not this caller's -- on the request.
        carried = await reviewer.post(
            f"/api/v1/deployments/{deployment_id}/approve", json={"approved": False}
        )
    assert carried.status_code == 200, carried.text
    assert await status_of(admin_client, deployment_id) == DeploymentStatus.HALTED.value
    final = await gate_request(db, deployment_id)
    assert final.decided_by_user_id == reviewer_b.id
    assert final.decision_note == "Change freeze"


# ---------------------------------------------------------------------------
# 36 / notes - a decision taken in Approvals & Audit reaches the release
# ---------------------------------------------------------------------------


async def decide_in_the_queue(sessionmaker, principal, request_id: str, *, approved: bool, note):
    """What the approvals service does: close the request, then call the hook
    in the same transaction. Returns the hook's answer."""
    async with sessionmaker() as session:
        row = await session.get(ApprovalRequest, request_id)
        row.status = (
            ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
        )
        row.decided_by_user_id = principal.user_id
        row.decided_at = dt.datetime.now(dt.UTC)
        row.decision_note = note
        resume = await deployments_service.apply_gate_decision(
            session=session,
            principal=principal,
            approval_request=row,
            approved=approved,
            note=note,
            request=None,
        )
        await session.commit()
    return resume


def test_the_gate_hook_takes_exactly_what_the_approvals_queue_offers():
    """services/approvals.py binds these keywords before it calls the hook, and
    ignores a hook that does not take them."""
    offered = {
        "session": None,
        "principal": None,
        "approval_request": None,
        "approved": True,
        "note": None,
        "request": None,
    }
    inspect.signature(deployments_service.apply_gate_decision).bind(**offered)


async def test_an_approval_in_the_queue_opens_the_gate_and_the_release_finishes(
    admin_client, db, factory, workspace, approver, sessionmaker, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)
    request_row = await gate_request(db, deployment_id)
    reviewer = principal_for(workspace, approver, Role.APPROVER)

    resume = await decide_in_the_queue(
        sessionmaker, reviewer, request_row.id, approved=True, note="Release window agreed"
    )

    assert resume is True
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.APPROVED.value
    assert stages["Deploy"] == DeploymentStageStatus.PENDING.value, "resumed by the caller"
    # The queue's decision is the record; the hook leaves the request alone.
    assert (await gate_request(db, deployment_id)).decided_by_user_id == approver.id

    await deployments_service.run_pipeline(deployment_id, workspace.id)
    await wait_for(
        lambda: status_of(admin_client, deployment_id),
        lambda s: s == DeploymentStatus.SUCCEEDED.value,
    )


async def test_a_rejection_in_the_queue_halts_the_release_and_frees_the_environment(
    admin_client, db, factory, workspace, approver, sessionmaker, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)
    request_row = await gate_request(db, deployment_id)
    reviewer = principal_for(workspace, approver, Role.APPROVER)

    resume = await decide_in_the_queue(
        sessionmaker, reviewer, request_row.id, approved=False, note="Not in this window"
    )

    assert resume is False
    assert await status_of(admin_client, deployment_id) == DeploymentStatus.HALTED.value
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.FAILED.value
    assert stages["Deploy"] == DeploymentStageStatus.SKIPPED.value

    rejected = [
        row
        for row in await db.scalars(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace.id)
        )
        if row.action == "deployment.reject" and row.entity_id == deployment_id
    ]
    assert len(rejected) == 1
    assert "Not in this window" in (rejected[0].detail or "")

    # A parked release used to lock its environment for ever.
    await parked_at_the_gate(admin_client, environment, version="v2.0.1")


async def test_a_request_that_only_names_a_release_does_not_move_it(
    admin_client, db, factory, workspace, approver, sessionmaker, instant_pipeline
):
    """The hook follows the deployment's pointer, not a payload anyone can write."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await parked_at_the_gate(admin_client, environment)
    lookalike = await factory.approval(
        workspace, action="Deploy", payload={"deployment_id": deployment_id}
    )
    reviewer = principal_for(workspace, approver, Role.APPROVER)

    resume = await decide_in_the_queue(
        sessionmaker, reviewer, lookalike.id, approved=True, note=None
    )

    assert resume is False
    assert (await stage_statuses(admin_client, deployment_id))["Approval"] == (
        DeploymentStageStatus.RUNNING.value
    )


# ---------------------------------------------------------------------------
# 132 - a pipeline whose runner died with its worker is picked up again
# ---------------------------------------------------------------------------


async def left_by_a_restart(db, factory, workspace, environment, **stage_states) -> str:
    """A release as a killed worker leaves it: in flight, nobody driving it.

    ``stage_states`` maps a stage's name (spaces as underscores) to
    ``(status, minutes since it started)``; every other stage is Pending.
    """
    now = dt.datetime.now(dt.UTC)
    deployment = await factory.deployment(
        workspace,
        environment,
        version="v2.0.0",
        status=DeploymentStatus.RUNNING.value,
        stage_status=DeploymentStageStatus.PENDING.value,
        started_at=now - dt.timedelta(minutes=30),
        finished_at=None,
        commit_ref="9f2c1ab",
    )
    for name, (status, minutes_ago) in stage_states.items():
        started = now - dt.timedelta(minutes=minutes_ago)
        done = status != DeploymentStageStatus.RUNNING.value
        await db.execute(
            update(DeploymentStage)
            .where(
                DeploymentStage.deployment_id == deployment.id,
                DeploymentStage.name == name.replace("_", " "),
            )
            .values(status=status, started_at=started, finished_at=started if done else None)
        )
    return deployment.id


async def test_a_stage_left_running_by_a_restart_is_started_again(
    admin_client, db, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await left_by_a_restart(
        db,
        factory,
        workspace,
        environment,
        Build=(DeploymentStageStatus.COMPLETED.value, 29),
        Automated_Tests=(DeploymentStageStatus.RUNNING.value, 28),
    )

    assert await deployments_service.recover_orphaned_pipelines() == 1

    reached = await wait_for(
        lambda: stage_statuses(admin_client, deployment_id),
        lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
    )
    assert reached["Automated Tests"] == DeploymentStageStatus.COMPLETED.value
    recovered = await db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "deployment.recovered", AuditEvent.entity_id == deployment_id
        )
    )
    assert len(recovered) == 1, "picking a release up again is on the record"
    assert "Automated Tests" in (recovered[0].detail or "")


async def test_a_release_that_never_got_a_runner_is_given_one(
    admin_client, db, factory, workspace, instant_pipeline
):
    """Queued, every stage Pending: what the commit race used to leave behind."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await left_by_a_restart(db, factory, workspace, environment)
    await db.execute(
        update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(status=DeploymentStatus.QUEUED.value)
    )

    assert await deployments_service.recover_orphaned_pipelines() == 1

    await wait_for(
        lambda: stage_statuses(admin_client, deployment_id),
        lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value,
    )


async def test_an_approved_release_nobody_resumed_is_finished(
    admin_client, db, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await left_by_a_restart(
        db,
        factory,
        workspace,
        environment,
        Build=(DeploymentStageStatus.COMPLETED.value, 29),
        Automated_Tests=(DeploymentStageStatus.COMPLETED.value, 28),
        Security_Scan=(DeploymentStageStatus.COMPLETED.value, 27),
        Approval=(DeploymentStageStatus.APPROVED.value, 20),
    )

    assert await deployments_service.recover_orphaned_pipelines() == 1

    await wait_for(
        lambda: status_of(admin_client, deployment_id),
        lambda s: s == DeploymentStatus.SUCCEEDED.value,
    )


async def test_a_release_waiting_for_a_person_is_not_an_orphan(
    admin_client, db, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await left_by_a_restart(
        db,
        factory,
        workspace,
        environment,
        Build=(DeploymentStageStatus.COMPLETED.value, 29),
        Automated_Tests=(DeploymentStageStatus.COMPLETED.value, 28),
        Security_Scan=(DeploymentStageStatus.COMPLETED.value, 27),
        Approval=(DeploymentStageStatus.RUNNING.value, 26),
    )

    assert await deployments_service.recover_orphaned_pipelines() == 0

    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.RUNNING.value
    assert stages["Deploy"] == DeploymentStageStatus.PENDING.value


async def test_a_pipeline_that_is_merely_mid_stage_is_left_to_its_runner(
    admin_client, db, factory, workspace
):
    """Inside its allowance plus the grace: another worker's runner has it."""
    environment = await factory.environment(workspace, name="Production East")
    deployment_id = await left_by_a_restart(
        db,
        factory,
        workspace,
        environment,
        Build=(DeploymentStageStatus.COMPLETED.value, 1),
        Automated_Tests=(DeploymentStageStatus.RUNNING.value, 0),
    )

    assert await deployments_service.recover_orphaned_pipelines() == 0

    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Automated Tests"] == DeploymentStageStatus.RUNNING.value
