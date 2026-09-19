"""Regressions for the 2026-09-18 audit of Approvals & Audit.

Two halves, as on the screen. The audit half: a trail that two workers forked
must still verify (and an edit inside a forked trail must still be caught), the
before/after columns must actually be written, and an overlong header must not
turn every governed change into a 500. The approvals half: a decision taken in
the queue has to move the deployment it gates, four eyes has to mean two people
whatever the request body claims, a rule has to do something, and the write
paths have to refuse what they used to answer with a 500.

Forks are built the only way a test can build one on a single-writer database:
by writing the second child directly, exactly as the losing worker of a race
would have -- same parent, its own content, a checksum that follows from both.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select, update

from fulcrum_ops_api.models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    default_workflow,
)
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.operations import DeploymentStageStatus, DeploymentStatus
from fulcrum_ops_api.services import audit as audit_service
from fulcrum_ops_api.services import deployments as deployments_service
from fulcrum_ops_api.services.audit import GENESIS


async def trail(db, workspace) -> list[AuditEvent]:
    return await db.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace.id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    )


async def some_governed_work(client, factory, workspace, count: int = 4) -> None:
    """``count`` audited changes through the API, one row each."""
    for index in range(count):
        secret = await factory.secret(workspace, name=f"Key {index}")
        response = await client.post(
            f"/api/v1/secrets/{secret.id}/disable", json={"reason": "Rotated"}
        )
        assert response.status_code == 200, response.text


def sibling_of(row: AuditEvent, *, parent: str | None, stored_parent: str | None) -> AuditEvent:
    """The row a second worker leaves when it read the same tail as ``row``'s writer."""
    occurred_at = row.occurred_at + dt.timedelta(milliseconds=1)
    canonical = audit_service._canonical(
        workspace_id=row.workspace_id,
        occurred_at=occurred_at,
        actor="api-key:fleet",
        action="ingest.guardrails.triggered",
        entity_type="Guardrail",
        entity_id="g-1",
        detail="Raised by a second worker in the same instant",
    )
    return AuditEvent(
        workspace_id=row.workspace_id,
        occurred_at=occurred_at,
        actor="api-key:fleet",
        action="ingest.guardrails.triggered",
        entity_type="Guardrail",
        entity_id="g-1",
        detail="Raised by a second worker in the same instant",
        event_metadata={},
        previous_checksum=stored_parent,
        checksum=audit_service._checksum(canonical, parent or GENESIS),
    )


# ---------------------------------------------------------------------------
# 18 - a forked trail is history, not tampering
# ---------------------------------------------------------------------------


async def test_two_rows_chained_to_the_same_tail_still_verify(
    admin_client, db, factory, workspace
):
    await some_governed_work(admin_client, factory, workspace)
    rows = await trail(db, workspace)
    # rows[2] chained to rows[1]; so did another worker, a millisecond later.
    parent = rows[1].checksum
    await factory.add(sibling_of(rows[2], parent=parent, stored_parent=parent))

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is True, body
    assert body["checked"] == len(rows) + 1, "every row is replayed, the fork included"
    assert body["forks"] == 1, "and the fork is reported, not hidden"
    assert body["broken_at_event_id"] is None


async def test_an_edit_inside_a_forked_trail_is_still_caught(
    admin_client, db, factory, workspace
):
    await some_governed_work(admin_client, factory, workspace)
    rows = await trail(db, workspace)
    parent = rows[1].checksum
    fork = await factory.add(sibling_of(rows[2], parent=parent, stored_parent=parent))

    await db.execute(
        update(AuditEvent).where(AuditEvent.id == fork.id).values(detail="quietly rewritten")
    )

    body = (await admin_client.get("/api/v1/audit/verify")).json()
    assert body["intact"] is False
    assert body["broken_at_event_id"] == fork.id


async def test_a_fork_whose_parent_was_deleted_is_a_break(admin_client, db, factory, workspace):
    """Tolerating forks must not tolerate a child that names a row which is gone."""
    await some_governed_work(admin_client, factory, workspace)
    rows = await trail(db, workspace)
    vanished = "f" * 64
    orphan = await factory.add(sibling_of(rows[2], parent=vanished, stored_parent=vanished))

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is False
    assert body["broken_at_event_id"] == orphan.id


async def test_a_fork_written_before_the_link_was_stored_still_verifies(
    admin_client, db, factory, workspace
):
    """Rows older than ``previous_checksum`` carry no parent; it has to be found."""
    await some_governed_work(admin_client, factory, workspace)
    rows = await trail(db, workspace)
    await factory.add(sibling_of(rows[2], parent=rows[1].checksum, stored_parent=None))
    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.workspace_id == workspace.id)
        .values(previous_checksum=None)
    )

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is True, body
    assert body["forks"] == 1


async def test_a_writer_that_stamped_its_time_before_reading_the_tail(
    admin_client, db, factory, workspace
):
    """It names a parent dated *after* itself. That is a fork, not a missing row."""
    await some_governed_work(admin_client, factory, workspace)
    rows = await trail(db, workspace)
    early = sibling_of(rows[0], parent=rows[2].checksum, stored_parent=rows[2].checksum)
    await factory.add(early)

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is True, body
    assert body["forks"] >= 1


def test_the_writers_lock_key_fits_the_two_int_advisory_lock():
    """Both halves are int4 on the wire; a key outside it fails every audited write."""
    for workspace_id in ("a", "0b1e7c52-7f4e-4d0a-9a55-0d8f6c1f2a33", "z" * 36):
        key = audit_service._lock_key(workspace_id)
        assert -(2**31) <= key < 2**31
        assert key == audit_service._lock_key(workspace_id), "stable across workers"
    assert 0 < audit_service.AUDIT_LOCK_NAMESPACE < 2**31


# ---------------------------------------------------------------------------
# 106 - four eyes means two people, whatever the request body says
# ---------------------------------------------------------------------------


async def key_caller(app, factory, workspace, *, scopes):
    import httpx

    from conftest import APP_BASE_URL

    token, row = await factory.api_key(workspace, name="Automation key", scopes=scopes)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL)
    http.headers["Authorization"] = f"Bearer {token}"
    return http, row


async def test_a_person_cannot_raise_a_request_in_someone_elses_name(
    as_role, factory, workspace, member
):
    """The old guard compared the decider with a requester the body supplied."""
    async with as_role(Role.APPROVER) as http:
        for claimed in (member.id, "z" * 36):
            forged = await http.post(
                "/api/v1/approvals",
                json={"action": "Refund Initiate", "requested_by_user_id": claimed},
            )
            assert forged.status_code == 422, forged.text
            assert forged.json()["error"]["details"]["field"] == "requested_by_user_id"

        honest = await http.post("/api/v1/approvals", json={"action": "Refund Initiate"})
        assert honest.status_code == 201, honest.text
        selfie = await http.post(
            f"/api/v1/approvals/{honest.json()['id']}/approve", json={"note": "Fine"}
        )
        assert selfie.status_code == 403, selfie.text


async def test_a_key_cannot_decide_an_approval(app, db, factory, workspace):
    """An admin-scope key ranks as an approver and has no user id to compare."""
    http, key = await key_caller(app, factory, workspace, scopes=["admin"])
    try:
        raised = await http.post("/api/v1/approvals", json={"action": "Wire transfer"})
        assert raised.status_code == 201, raised.text
        request_id = raised.json()["id"]

        for verb, body in (("approve", {}), ("reject", {"note": "No"})):
            decided = await http.post(f"/api/v1/approvals/{request_id}/{verb}", json=body)
            assert decided.status_code == 403, decided.text
            assert "signed-in person" in decided.text

        # Asking for eyes is still open to it.
        escalated = await http.post(f"/api/v1/approvals/{request_id}/escalate", json={})
        assert escalated.status_code == 200, escalated.text
    finally:
        await http.aclose()

    row = await db.get(ApprovalRequest, request_id)
    assert row.status == ApprovalStatus.ESCALATED.value
    assert row.workflow[0]["raised_by_api_key_id"] == key.id, "who filed it is on record"


async def test_a_key_may_raise_for_a_member_but_only_a_member(
    app, as_role, factory, workspace, member
):
    http, _key = await key_caller(app, factory, workspace, scopes=["admin"])
    try:
        stranger = await http.post(
            "/api/v1/approvals",
            json={"action": "Wire transfer", "requested_by_user_id": "z" * 36},
        )
        assert stranger.status_code == 422, stranger.text
        assert stranger.json()["error"]["details"]["field"] == "requested_by_user_id"

        raised = await http.post(
            "/api/v1/approvals",
            json={"action": "Wire transfer", "requested_by_user_id": member.id},
        )
        assert raised.status_code == 201, raised.text
        assert raised.json()["requested_by_name"] == member.full_name
    finally:
        await http.aclose()


async def test_whoever_filed_a_request_cannot_decide_it_either(
    app, db, factory, workspace, approver, member
):
    """A row filed by one person in another's name: neither of them decides it."""
    import httpx

    from conftest import APP_BASE_URL, authorise

    workflow = default_workflow()
    workflow[0].update(done=True, by=approver.full_name, raised_by_user_id=approver.id)
    request_row = await factory.approval(workspace, requested_by_user_id=member.id)
    await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_row.id)
        .values(workflow=workflow)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL
    ) as http:
        authorise(http, approver, workspace, Role.APPROVER)
        decided = await http.post(
            f"/api/v1/approvals/{request_row.id}/approve", json={"note": "Mine, really"}
        )

    assert decided.status_code == 403, decided.text
    assert "different reviewer" in decided.text


# ---------------------------------------------------------------------------
# 17 / 34 - a decision in the queue reaches the release it gates
# ---------------------------------------------------------------------------


@pytest.fixture
def instant_pipeline(monkeypatch) -> None:
    """The runner's simulated stage durations, emptied -- as test_state_machines does.

    Nothing about the gate depends on how long a build pretends to take.
    """
    monkeypatch.setattr(deployments_service, "STAGE_RUNTIME_SECONDS", {})


@pytest.fixture(autouse=True)
def stop_pipelines():
    """Cancel any pipeline a test left running, before its database goes away."""
    yield
    for deployment_id in list(deployments_service.runner._tasks):
        deployments_service.runner.cancel(deployment_id)


async def wait_for(probe, predicate, *, stalled_after: float = 20.0, give_up_after: float = 180.0):
    """Wait for ``predicate`` while what ``probe`` sees is still moving.

    The pipeline runs as a task on this loop and commits once per stage, so how
    long it takes to reach its gate is a property of the machine, not of the
    code: on a box running a dozen suites at once a fixed ten seconds failed a
    different pair of these tests on every run, with the release visibly still
    advancing. So the clock that matters is time since the last *change* -- a
    release that is making progress is given it, and one that has genuinely
    stopped (the defect these tests exist for) still fails, saying where.
    """
    clock = asyncio.get_running_loop().time
    started = moved = clock()
    latest = None
    while clock() - moved < stalled_after and clock() - started < give_up_after:
        seen = await probe()
        if predicate(seen):
            return seen
        if seen != latest:
            latest, moved = seen, clock()
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"condition never held after {clock() - started:.0f}s "
        f"({clock() - moved:.0f}s without change); last saw {latest!r}"
    )


async def a_release_parked_at_its_gate(
    client, factory, workspace, *, environment_name: str = "Production East"
) -> tuple[str, str]:
    """Deploy through the API and wait for the gate. Returns (deployment, request) ids."""
    environment = await factory.environment(workspace, name=environment_name)
    created = await client.post(
        "/api/v1/deployments", json={"environment_id": environment.id, "version": "v2.0.0"}
    )
    assert created.status_code == 201, created.text
    deployment_id = created.json()["id"]

    parked, _stages = await wait_for(
        lambda: progress_of(client, deployment_id), lambda seen: seen[0].get("approval_request_id")
    )
    return deployment_id, parked["approval_request_id"]


async def progress_of(client, deployment_id: str) -> tuple[dict, dict[str, str]]:
    """The release and its stages together: the stages are where progress shows.

    The deployment row changes twice on the way to its gate; every step in
    between is a stage row, so a probe of the release alone looks stalled while
    the pipeline is working.
    """
    detail = (await client.get(f"/api/v1/deployments/{deployment_id}")).json()
    return detail, await stage_statuses(client, deployment_id)


async def stage_statuses(client, deployment_id: str) -> dict[str, str]:
    response = await client.get(f"/api/v1/deployments/{deployment_id}/stages")
    return {stage["name"]: stage["status"] for stage in response.json()}


async def test_approving_the_gate_request_in_the_queue_lets_the_release_finish(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    deployment_id, request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )

    async with as_role(Role.APPROVER) as reviewer:
        decided = await reviewer.post(
            f"/api/v1/approvals/{request_id}/approve", json={"note": "Release window agreed"}
        )
    assert decided.status_code == 200, decided.text
    assert decided.json()["request"]["status"] == "Approved"

    await wait_for(
        lambda: progress_of(admin_client, deployment_id),
        lambda seen: seen[0]["status"] == DeploymentStatus.SUCCEEDED.value,
    )
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.APPROVED.value
    assert stages["Deploy"] == DeploymentStageStatus.COMPLETED.value


async def test_rejecting_the_gate_request_in_the_queue_halts_the_release(
    admin_client, as_role, db, factory, workspace, instant_pipeline
):
    deployment_id, request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )

    async with as_role(Role.APPROVER) as reviewer:
        decided = await reviewer.post(
            f"/api/v1/approvals/{request_id}/reject", json={"note": "Not in this window"}
        )
    assert decided.status_code == 200, decided.text

    detail = (await admin_client.get(f"/api/v1/deployments/{deployment_id}")).json()
    assert detail["status"] == DeploymentStatus.HALTED.value
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.FAILED.value
    assert stages["Deploy"] == DeploymentStageStatus.SKIPPED.value

    # The environment is free again: a parked release used to lock it for ever.
    again = await admin_client.post(
        "/api/v1/deployments",
        json={"environment_id": detail["environment_id"], "version": "v2.0.1"},
    )
    assert again.status_code == 201, again.text

    # Let it reach its own gate, so teardown has no pipeline to cancel mid-write.
    await wait_for(
        lambda: progress_of(admin_client, again.json()["id"]),
        lambda seen: seen[0].get("approval_request_id"),
    )

    halted = [
        event
        for event in await trail(db, workspace)
        if event.action == "deployment.reject" and event.entity_id == deployment_id
    ]
    assert len(halted) == 1, "the halt is on the record, against the deployment"
    assert "Not in this window" in (halted[0].detail or "")


async def test_escalating_the_gate_request_leaves_the_release_waiting(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    deployment_id, request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )

    async with as_role(Role.APPROVER) as reviewer:
        escalated = await reviewer.post(f"/api/v1/approvals/{request_id}/escalate", json={})
    assert escalated.status_code == 200, escalated.text

    detail = (await admin_client.get(f"/api/v1/deployments/{deployment_id}")).json()
    assert detail["status"] == DeploymentStatus.RUNNING.value
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.RUNNING.value


async def test_a_gate_nobody_opened_in_time_halts_the_release(
    admin_client, db, factory, workspace, sessionmaker, instant_pipeline
):
    """The SLA sweeper used to expire the request and leave the release parked."""
    from fulcrum_ops_api.services import approvals as approvals_service
    from fulcrum_ops_api.services import scheduler

    deployment_id, request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )
    await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .values(sla_due_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
    )

    async with sessionmaker() as session:
        expired = await approvals_service.expire_overdue(
            session, scheduler._system_principal(workspace.id)
        )
        await session.commit()
    assert [row.id for row in expired] == [request_id]

    detail = (await admin_client.get(f"/api/v1/deployments/{deployment_id}")).json()
    assert detail["status"] == DeploymentStatus.HALTED.value
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Deploy"] == DeploymentStageStatus.SKIPPED.value


async def test_a_request_that_only_names_a_deployment_cannot_release_it(
    admin_client, as_role, factory, workspace, instant_pipeline
):
    """The gate is found from the deployment's pointer, not from a payload anyone can write."""
    deployment_id, _request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )
    async with as_role(Role.MEMBER) as requester:
        lookalike = await requester.post(
            "/api/v1/approvals",
            json={"action": "Deploy", "payload": {"deployment_id": deployment_id}},
        )
        assert lookalike.status_code == 201, lookalike.text

    async with as_role(Role.APPROVER) as reviewer:
        decided = await reviewer.post(
            f"/api/v1/approvals/{lookalike.json()['id']}/approve", json={}
        )
    assert decided.status_code == 200, decided.text

    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.RUNNING.value, "still on its own gate"


async def test_a_release_left_parked_by_an_old_decision_can_be_settled(
    admin_client, db, factory, workspace, sessionmaker, admin, instant_pipeline
):
    """Requests approved in the queue before decisions reached the deployment."""
    from conftest import principal_for
    from fulcrum_ops_api.services import approvals as approvals_service

    deployment_id, request_id = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )
    # What the old queue left behind: the request closed, the release untouched.
    await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .values(status=ApprovalStatus.APPROVED.value, decided_at=dt.datetime.now(dt.UTC))
    )

    async with sessionmaker() as session:
        resume = await approvals_service.settle_decided_gates(
            session, principal_for(workspace, admin, Role.ADMIN)
        )
        await session.commit()

    assert resume == [deployment_id]
    stages = await stage_statuses(admin_client, deployment_id)
    assert stages["Approval"] == DeploymentStageStatus.APPROVED.value


async def test_the_gate_sweep_finishes_what_old_decisions_left_parked(
    admin_client, db, factory, workspace, instant_pipeline
):
    """The scheduler's entry point: every workspace, its own session, and the
    pipeline started only once the opened gate is committed."""
    from fulcrum_ops_api.services import approvals as approvals_service

    released, approved_request = await a_release_parked_at_its_gate(
        admin_client, factory, workspace
    )
    refused, rejected_request = await a_release_parked_at_its_gate(
        admin_client, factory, workspace, environment_name="Production West"
    )
    assert await approvals_service.settle_parked_gates() == 0, "an open gate is a person's"

    for request_id, status in (
        (approved_request, ApprovalStatus.APPROVED),
        (rejected_request, ApprovalStatus.REJECTED),
    ):
        await db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(
                status=status.value,
                decision_note="Decided before decisions reached the release",
                decided_at=dt.datetime.now(dt.UTC),
            )
        )

    assert await approvals_service.settle_parked_gates() == 2

    detail = (await admin_client.get(f"/api/v1/deployments/{refused}")).json()
    assert detail["status"] == DeploymentStatus.HALTED.value
    await wait_for(
        lambda: progress_of(admin_client, released),
        lambda seen: seen[0]["status"] == DeploymentStatus.SUCCEEDED.value,
    )
    assert await approvals_service.settle_parked_gates() == 0, "settled once, not every tick"


# ---------------------------------------------------------------------------
# 109 - write paths that answered 500, or could be made to
# ---------------------------------------------------------------------------


async def test_a_hand_written_reference_cannot_squat_in_the_allocated_namespace(
    as_role, workspace
):
    """"APR-2026-hotfix" sorted above every number and reset the sequence to 1."""
    year = dt.datetime.now(dt.UTC).year
    async with as_role(Role.MEMBER) as http:
        first = await http.post("/api/v1/approvals", json={"action": "Refund Initiate"})
        assert first.status_code == 201, first.text
        assert first.json()["request_ref"] == f"APR-{year}-00001"

        for squatter in (f"APR-{year}-hotfix", "REQ-7", f"apr-{year}-zzzzz"):
            refused = await http.post(
                "/api/v1/approvals", json={"action": "Refund Initiate", "request_ref": squatter}
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["details"]["field"] == "request_ref"

        own = await http.post(
            "/api/v1/approvals", json={"action": "Refund Initiate", "request_ref": "FIN-2291"}
        )
        assert own.status_code == 201, own.text

        second = await http.post("/api/v1/approvals", json={"action": "Refund Initiate"})
        assert second.status_code == 201, second.text
        assert second.json()["request_ref"] == f"APR-{year}-00002"


async def test_a_poisoned_reference_already_in_the_table_no_longer_resets_the_sequence(
    as_role, factory, workspace
):
    """Rows written before the namespace was reserved are still there."""
    year = dt.datetime.now(dt.UTC).year
    await factory.approval(workspace, request_ref=f"APR-{year}-00041")
    await factory.approval(workspace, request_ref=f"APR-{year}-hotfix")

    async with as_role(Role.MEMBER) as http:
        raised = await http.post("/api/v1/approvals", json={"action": "Refund Initiate"})

    assert raised.status_code == 201, raised.text
    assert raised.json()["request_ref"] == f"APR-{year}-00042"


async def test_losing_the_race_for_a_reference_costs_a_re_read_not_a_500(
    as_role, db, db_engine, factory, workspace
):
    """Two workers read the same max() and the unique constraint refuses the second.

    The other worker is played by a second connection that takes the reference
    in the instant between this request's read and its insert.
    """
    import sqlite3

    from sqlalchemy import event

    year = dt.datetime.now(dt.UTC).year
    contested = f"APR-{year}-00001"
    other_workers = await factory.approval(workspace, request_ref="FIN-1")
    taken: list[str] = []

    def take_it_first(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if taken or not statement.lstrip().upper().startswith("INSERT INTO APPROVAL_REQUESTS"):
            return
        taken.append(contested)
        with sqlite3.connect(db_engine.url.database, timeout=30) as other:
            other.execute(
                "UPDATE approval_requests SET request_ref = ? WHERE id = ?",
                (contested, other_workers.id),
            )

    event.listen(db_engine.sync_engine, "before_cursor_execute", take_it_first)
    try:
        async with as_role(Role.MEMBER) as http:
            raised = await http.post("/api/v1/approvals", json={"action": "Refund Initiate"})
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", take_it_first)

    assert taken, "the race was staged"
    assert raised.status_code == 201, raised.text
    assert raised.json()["request_ref"] == f"APR-{year}-00002"
    refs = await db.scalars(
        select(ApprovalRequest.request_ref).where(ApprovalRequest.workspace_id == workspace.id)
    )
    assert sorted(refs) == [contested, f"APR-{year}-00002"]


@pytest.mark.parametrize("field", ["action", "payload"])
async def test_clearing_a_required_field_is_refused_not_a_500(
    as_role, db, factory, workspace, field
):
    request_row = await factory.approval(workspace, payload={"action": "refund"})

    async with as_role(Role.MEMBER) as http:
        response = await http.patch(f"/api/v1/approvals/{request_row.id}", json={field: None})
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["field"] == field

        still_there = await http.get(f"/api/v1/approvals/{request_row.id}")
    assert still_there.status_code == 200, still_there.text
    assert still_there.json()["action"] == request_row.action
    assert still_there.json()["payload"] == {"action": "refund"}


async def test_deciding_editing_and_expiring_lock_the_row_they_are_about_to_change(
    as_role, factory, workspace, sessionmaker
):
    """Two workers deciding at once must queue, not both read Pending.

    SQLite has one writer and drops the clause, so the lock cannot be raced
    here; what can be pinned is that the real request path asks for it.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from fulcrum_ops_api.services import approvals as approvals_service
    from fulcrum_ops_api.services import scheduler

    asked: list[object] = []

    def watch(state) -> None:
        lock = getattr(state.statement, "_for_update_arg", None)
        if state.is_select and lock is not None and "approval_requests" in str(state.statement):
            asked.append(lock)

    to_edit = await factory.approval(workspace)
    to_decide = await factory.approval(workspace)
    await factory.approval(
        workspace, sla_due_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    )

    event.listen(Session, "do_orm_execute", watch)
    try:
        async with as_role(Role.APPROVER) as http:
            edited = await http.patch(
                f"/api/v1/approvals/{to_edit.id}", json={"reason": "Customer called twice"}
            )
            assert edited.status_code == 200, edited.text
            assert len(asked) == 1, "an edit re-checks is_open, so it locks too"

            decided = await http.post(f"/api/v1/approvals/{to_decide.id}/approve", json={})
            assert decided.status_code == 200, decided.text
            assert len(asked) == 2

        async with sessionmaker() as session:
            expired = await approvals_service.expire_overdue(
                session, scheduler._system_principal(workspace.id)
            )
            await session.commit()
        assert len(expired) == 1
        assert len(asked) == 3
        assert asked[-1].skip_locked, "the sweeper steps round a row someone is deciding"
    finally:
        event.remove(Session, "do_orm_execute", watch)


# ---------------------------------------------------------------------------
# 108 - an approval rule has to do something
# ---------------------------------------------------------------------------


async def a_rule(client, **body) -> dict:
    """Create a rule the way the New Approval Rule modal does."""
    created = await client.post(
        "/api/v1/approvals/rules",
        json={
            "name": "Payments above $5,000",
            "trigger": "Financial action above threshold",
            "approvers": ["Finance Leads"],
            "sla_minutes": 60,
            "threshold_amount": 5000,
            **body,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_a_request_above_a_rules_threshold_is_filed_under_the_rule(
    admin_client, as_role, workspace
):
    """The rule was stored and read by nothing: no SLA, no approvers, no count."""
    rule = await a_rule(admin_client)

    async with as_role(Role.MEMBER) as http:
        large = await http.post(
            "/api/v1/approvals",
            json={"action": "Refund Initiate", "payload": {"action": "refund", "amount": 7200}},
        )
        small = await http.post(
            "/api/v1/approvals",
            json={"action": "Refund Initiate", "payload": {"action": "refund", "amount": 1200}},
        )
    assert large.status_code == 201, large.text
    assert small.status_code == 201, small.text

    filed = large.json()
    assert filed["policy_id"] == rule["id"]
    assert filed["policy_name"] == "Payments above $5,000"
    assert filed["approvers"] == ["Finance Leads"], "the inspector can say who should decide"
    assert filed["sla_label"] == "1h", "the rule's clock, not the Medium-risk day"
    assert filed["payload"] == {"action": "refund", "amount": 7200}, "the payload is replayed as is"

    under = small.json()
    assert under["policy_id"] is None and under["approvers"] == []
    assert under["sla_label"] == "1d"

    after = (await admin_client.get(f"/api/v1/approvals/rules/{rule['id']}")).json()
    assert after["requests_30d"] == 1
    assert after["last_triggered_at"] is not None
    assert after["updated_at"] == rule["updated_at"], "a rule firing is not an edit to it"


@pytest.mark.parametrize(
    ("body", "falls_under"),
    [
        ({"impact": {"financial": "$7,200.00"}}, True),
        ({"payload": {"parameters": {"amount": 5000.01}}}, True),
        ({"impact": {"financial": "$5,000"}}, False),
        ({"impact": {"financial": "7.2k"}}, False),
        ({"impact": {"financial": "—"}}, False),
        ({}, False),
        ({"payload": {"trigger": "Financial action above threshold"}}, True),
    ],
)
async def test_a_financial_rule_is_judged_on_the_amount_the_request_states(
    admin_client, as_role, workspace, body, falls_under
):
    """Above means above; a figure that cannot be read is not guessed at."""
    rule = await a_rule(admin_client)

    async with as_role(Role.MEMBER) as http:
        raised = await http.post("/api/v1/approvals", json={"action": "Refund Initiate", **body})
    assert raised.status_code == 201, raised.text

    assert raised.json()["policy_id"] == (rule["id"] if falls_under else None)


async def test_a_request_that_names_its_trigger_runs_on_the_rules_clock_unless_it_set_its_own(
    admin_client, as_role, workspace
):
    rule = await a_rule(
        admin_client,
        name="Exports need Data Governance",
        trigger="Bulk data export",
        approvers=["Data Governance", "Security"],
        sla_minutes=30,
        threshold_amount=None,
    )

    async with as_role(Role.MEMBER) as http:
        named = await http.post("/api/v1/approvals", json={"action": "bulk data export"})
        hurried = await http.post(
            "/api/v1/approvals",
            json={
                "action": "Export customers",
                "payload": {"trigger": "Bulk data export"},
                "sla_minutes": 10,
            },
        )
        unrelated = await http.post("/api/v1/approvals", json={"action": "Export customers"})

    assert named.json()["policy_id"] == rule["id"]
    assert named.json()["approvers"] == ["Data Governance", "Security"]
    assert named.json()["sla_label"] == "30m"
    assert hurried.json()["policy_id"] == rule["id"]
    assert hurried.json()["sla_label"] == "10m", "an explicit window is the caller's to set"
    assert unrelated.json()["policy_id"] is None, "words in a free-text action are not a trigger"


async def test_a_rule_that_is_switched_off_files_nothing(admin_client, as_role, workspace):
    rule = await a_rule(admin_client)
    paused = await admin_client.patch(
        f"/api/v1/approvals/rules/{rule['id']}", json={"status": "Inactive"}
    )
    assert paused.status_code == 200, paused.text

    async with as_role(Role.MEMBER) as http:
        raised = await http.post(
            "/api/v1/approvals", json={"action": "Refund Initiate", "payload": {"amount": 9000}}
        )
        named = await http.post(
            "/api/v1/approvals", json={"action": "Refund Initiate", "policy_id": rule["id"]}
        )

    assert raised.json()["policy_id"] is None and raised.json()["approvers"] == []
    # Naming it keeps the link the caller asked for, and takes nothing from it.
    assert named.json()["policy_id"] == rule["id"]
    assert named.json()["approvers"] == [] and named.json()["sla_label"] == "1d"


async def test_an_agents_own_rule_comes_before_the_workspaces(
    admin_client, as_role, factory, workspace
):
    governed = await factory.agent(workspace, name="Refunds Agent")
    other = await factory.agent(workspace, name="Billing Agent")
    everyone = await a_rule(admin_client)
    own = await a_rule(
        admin_client,
        name="Refunds Agent payments",
        approvers=["Refunds Desk"],
        sla_minutes=120,
        threshold_amount=100,
        scope="Agent",
        scope_ref=governed.id,
    )

    async with as_role(Role.MEMBER) as http:
        by_governed = await http.post(
            "/api/v1/approvals",
            json={"action": "Refund", "agent_id": governed.id, "payload": {"amount": 9000}},
        )
        by_other = await http.post(
            "/api/v1/approvals",
            json={"action": "Refund", "agent_id": other.id, "payload": {"amount": 9000}},
        )
        small_by_other = await http.post(
            "/api/v1/approvals",
            json={"action": "Refund", "agent_id": other.id, "payload": {"amount": 500}},
        )

    assert by_governed.json()["policy_id"] == own["id"]
    assert by_governed.json()["approvers"] == ["Refunds Desk"]
    assert by_other.json()["policy_id"] == everyone["id"]
    assert small_by_other.json()["policy_id"] is None, "another agent's rule does not reach it"


async def test_deciding_a_rules_requests_moves_its_approved_share(
    admin_client, as_role, workspace
):
    """"Approved %" was never written for a rule, so it read 0 whatever was decided."""
    rule = await a_rule(admin_client)
    async with as_role(Role.MEMBER) as http:
        raised = [
            (
                await http.post(
                    "/api/v1/approvals",
                    json={"action": "Refund Initiate", "payload": {"amount": amount}},
                )
            ).json()["id"]
            for amount in (6000, 7000, 8000)
        ]

    async with as_role(Role.APPROVER) as reviewer:
        for request_id, verb, body in (
            (raised[0], "approve", {}),
            (raised[1], "approve", {"note": "Within the customer's plan"}),
            (raised[2], "reject", {"note": "Duplicate of the first"}),
        ):
            decided = await reviewer.post(f"/api/v1/approvals/{request_id}/{verb}", json=body)
            assert decided.status_code == 200, decided.text

    after = (await admin_client.get(f"/api/v1/approvals/rules/{rule['id']}")).json()
    assert (after["requests_30d"], after["approved_pct"]) == (3, 67)
    assert after["updated_at"] == rule["updated_at"]


# ---------------------------------------------------------------------------
# 107 - verifying must not cost a full ORM load of the trail on every tab open
# ---------------------------------------------------------------------------


def watching_audit_reads():
    """Count the statements that read ``audit_events``, and note whole-row loads."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    seen: list[str] = []

    def watch(state) -> None:
        text = str(state.statement)
        if state.is_select and "FROM audit_events" in text:
            seen.append(text)

    event.listen(Session, "do_orm_execute", watch)
    return seen, lambda: event.remove(Session, "do_orm_execute", watch)


async def test_a_replay_reads_only_the_hashed_columns(admin_client, factory, workspace):
    """It used to hydrate every row whole -- metadata, before/after text, user agent."""
    await some_governed_work(admin_client, factory, workspace, count=2)

    seen, stop = watching_audit_reads()
    try:
        body = (await admin_client.get("/api/v1/audit/verify")).json()
    finally:
        stop()

    assert body["intact"] is True and body["checked"] == 2
    assert seen, "the replay must have read the trail"
    for statement in seen:
        columns = statement.split("FROM audit_events")[0]
        for unhashed in ("event_metadata", "user_agent", "prev_value", "new_value"):
            assert unhashed not in columns, f"the replay loaded {unhashed}"


async def test_one_replay_is_shared_by_everyone_who_asks(
    admin_client, as_role, db, factory, workspace, monkeypatch
):
    """The console asked twice per tab open, and again on every inspector close."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "audit_verify_cache_seconds", 30.0)
    await some_governed_work(admin_client, factory, workspace, count=3)

    seen, stop = watching_audit_reads()
    try:
        async with as_role(Role.VIEWER) as second_tab:
            answers = await asyncio.gather(
                admin_client.get("/api/v1/audit/verify"),
                second_tab.get("/api/v1/audit/verify"),
                admin_client.get("/api/v1/audit/verify"),
            )
            again = await second_tab.get("/api/v1/audit/verify")
    finally:
        stop()

    bodies = [answer.json() for answer in (*answers, again)]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["checked"] == 3
    assert len(seen) == 1, f"four questions, one replay; saw {len(seen)}"


async def test_a_shared_replay_expires_so_an_edit_to_an_old_row_is_still_caught(
    admin_client, db, factory, workspace, monkeypatch
):
    """Hard expiry, not a key on the newest row: an old row's edit moves no such key."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "audit_verify_cache_seconds", 0.2)
    await some_governed_work(admin_client, factory, workspace, count=3)
    assert (await admin_client.get("/api/v1/audit/verify")).json()["intact"] is True

    victim = (await trail(db, workspace))[0]
    await db.execute(
        update(AuditEvent).where(AuditEvent.id == victim.id).values(detail="quietly rewritten")
    )
    await asyncio.sleep(0.3)

    body = (await admin_client.get("/api/v1/audit/verify")).json()
    assert body["intact"] is False
    assert body["broken_at_event_id"] == victim.id


# ---------------------------------------------------------------------------
# 205 - the before/after the drawer promises, and headers wider than a column
# ---------------------------------------------------------------------------


async def test_a_transition_records_its_before_and_after(admin_client, db, factory, workspace):
    """The drawer's State Change section and the CSV's two columns were always empty."""
    request_row = await factory.approval(workspace)

    decided = await admin_client.post(
        f"/api/v1/approvals/{request_row.id}/approve", json={"note": "Within band"}
    )
    assert decided.status_code == 200, decided.text

    stored = (await trail(db, workspace))[-1]
    assert (stored.prev_value, stored.new_value) == ("Pending", "Approved")

    listed = (await admin_client.get("/api/v1/audit")).json()["items"][0]
    assert (listed["prev_value"], listed["new_value"]) == ("Pending", "Approved")

    exported = (await admin_client.get("/api/v1/audit/export")).text.splitlines()
    header = exported[0].split(",")
    row = exported[1].split(",")
    assert row[header.index("Previous")] == "Pending"
    assert row[header.index("New")] == "Approved"


async def test_a_row_written_before_the_columns_were_still_shows_its_transition(
    admin_client, db, factory, workspace
):
    """Append-only means no backfill; the transition is read from the row's metadata."""
    request_row = await factory.approval(workspace)
    await admin_client.post(f"/api/v1/approvals/{request_row.id}/approve", json={})
    stored = (await trail(db, workspace))[-1]
    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.id == stored.id)
        .values(prev_value=None, new_value=None)
    )

    listed = (await admin_client.get("/api/v1/audit")).json()["items"][0]

    assert (listed["prev_value"], listed["new_value"]) == ("Pending", "Approved")
    assert (await admin_client.get("/api/v1/audit/verify")).json()["intact"] is True


async def test_a_change_with_no_before_and_after_invents_none(
    admin_client, db, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")
    await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Incident 41"}
    )

    listed = (await admin_client.get("/api/v1/audit")).json()["items"][0]
    assert listed["prev_value"] is None and listed["new_value"] is None


async def test_an_overlong_user_agent_does_not_fail_the_change(
    admin_client, db, factory, workspace
):
    """VARCHAR(255): PostgreSQL refuses the insert, and with it the change itself."""
    secret = await factory.secret(workspace, name="Payments key")

    response = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/disable",
        json={"reason": "Rotated"},
        headers={
            "User-Agent": "ManagedBrowser/9 " + "x" * 600,
            "X-Forwarded-For": "2001:db8:" + "f" * 90 + ", 10.0.0.1",
        },
    )

    assert response.status_code == 200, response.text
    stored = (await trail(db, workspace))[-1]
    assert len(stored.user_agent) == 255 and stored.user_agent.startswith("ManagedBrowser/9")
    assert len(stored.ip_address) == 64


async def test_a_hashed_column_is_cut_before_it_is_hashed(db, workspace, admin, sessionmaker):
    """Cut after hashing and the stored row no longer matches its own checksum."""
    from conftest import principal_for

    async with sessionmaker() as session:
        await audit_service.record(
            session,
            principal=principal_for(workspace, admin, Role.ADMIN),
            action="connector." + "x" * 200,
            entity_type="Connector" + "y" * 80,
            entity_id="c-" + "9" * 100,
            entity_label="L" * 400,
            source_screen="S" * 120,
            detail="An integration with very long identifiers",
        )
        await session.commit()

    stored = (await trail(db, workspace))[-1]
    assert (len(stored.action), len(stored.entity_type), len(stored.entity_id)) == (120, 48, 64)
    assert (len(stored.entity_label), len(stored.source_screen)) == (255, 80)

    async with sessionmaker() as session:
        outcome = await audit_service.verify_chain(session, workspace.id)
    assert outcome["intact"] is True, outcome
