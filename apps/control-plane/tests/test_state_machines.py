"""The governed lifecycles, and the moves they refuse.

Six domains in this product own a state machine, and in every one of them the
interesting behaviour is the *refusal*: a request that has already been decided
cannot be decided again, a revoked credential cannot be re-enabled, an agent
still accepting runs cannot be deleted, a prompt cannot skip review. Those edges
are what stop the audit trail from becoming fiction, so each one gets a test
that drives the illegal move and reads the status back.

Every transition here goes through HTTP as the operator would make it, and every
one is checked twice: the response says the move happened, and a subsequent read
agrees. A service that answered 200 and forgot to commit would pass the first
assertion and fail the second.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

from conftest import error_code
from fulcrum_ops_api.models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    Secret,
    SecretAccessAction,
    SecretAccessLog,
    SecretStatus,
)
from fulcrum_ops_api.models.operations import (
    DeploymentStageStatus,
    DeploymentStatus,
)
from fulcrum_ops_api.models.registry import (
    AgentStatus,
    Configuration,
    ConfigurationStatus,
    EnvironmentStatus,
    EnvironmentType,
    PolicyStatus,
)
from fulcrum_ops_api.services import deployments as deployments_service


async def audit_actions(db, workspace) -> list[str]:
    return await db.scalars(
        select(AuditEvent.action).where(AuditEvent.workspace_id == workspace.id)
    )


# ===========================================================================
# Approvals
# ===========================================================================


async def test_approving_a_pending_request_decides_it_and_records_who(
    admin_client, db, factory, workspace
):
    row = await factory.approval(workspace, action="Refund over threshold")

    response = await admin_client.post(
        f"/api/v1/approvals/{row.id}/approve", json={"note": "Within the agreed band"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request"]["status"] == ApprovalStatus.APPROVED.value
    assert body["request"]["decision_note"] == "Within the agreed band"
    assert body["request"]["decided_at"] is not None

    stored = await db.get(ApprovalRequest, row.id)
    assert stored.status == ApprovalStatus.APPROVED.value
    assert "Request approved" in await audit_actions(db, workspace)


async def test_the_decision_lands_in_the_discussion_thread(
    admin_client, factory, workspace
):
    """A reviewer reading the request later must see the reasoning in context."""
    row = await factory.approval(workspace)
    await admin_client.post(
        f"/api/v1/approvals/{row.id}/approve", json={"note": "Signed off with finance"}
    )

    comments = await admin_client.get(f"/api/v1/approvals/{row.id}/comments")

    assert comments.status_code == 200, comments.text
    bodies = [item["body"] for item in comments.json()]
    assert "Signed off with finance" in bodies


async def test_a_rejection_without_a_reason_is_refused(admin_client, db, factory, workspace):
    """The reason is quoted in the audit trail, so there has to be one."""
    row = await factory.approval(workspace)

    response = await admin_client.post(f"/api/v1/approvals/{row.id}/reject", json={})

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_failed"
    stored = await db.get(ApprovalRequest, row.id)
    assert stored.status == ApprovalStatus.PENDING.value, "a refused decision changes nothing"


async def test_rejecting_with_a_reason_decides_the_request(
    admin_client, db, factory, workspace
):
    row = await factory.approval(workspace)

    response = await admin_client.post(
        f"/api/v1/approvals/{row.id}/reject", json={"note": "Outside policy"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["request"]["status"] == ApprovalStatus.REJECTED.value
    stored = await db.get(ApprovalRequest, row.id)
    assert stored.status == ApprovalStatus.REJECTED.value


async def test_escalation_keeps_the_request_open_for_a_second_reviewer(
    admin_client, db, factory, workspace, approver
):
    row = await factory.approval(workspace)

    escalated = await admin_client.post(
        f"/api/v1/approvals/{row.id}/escalate",
        json={"note": "Needs the risk committee", "escalate_to_user_id": approver.id},
    )

    assert escalated.status_code == 200, escalated.text
    assert escalated.json()["request"]["status"] == ApprovalStatus.ESCALATED.value

    # Escalated is not terminal: the senior reviewer can still decide it.
    decided = await admin_client.post(
        f"/api/v1/approvals/{row.id}/approve", json={"note": "Committee agreed"}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["request"]["status"] == ApprovalStatus.APPROVED.value
    stored = await db.get(ApprovalRequest, row.id)
    assert stored.status == ApprovalStatus.APPROVED.value


async def test_escalating_to_someone_outside_the_workspace_is_refused(
    admin_client, factory, workspace, other_workspace
):
    row = await factory.approval(workspace)
    outsider = await factory.user(other_workspace, email="stranger@contoso.test")

    response = await admin_client.post(
        f"/api/v1/approvals/{row.id}/escalate",
        json={"note": "Over to you", "escalate_to_user_id": outsider.id},
    )

    assert response.status_code in (404, 422), response.text


@pytest.mark.parametrize(
    ("settled", "verb"),
    [
        (ApprovalStatus.APPROVED, "approve"),
        (ApprovalStatus.APPROVED, "reject"),
        (ApprovalStatus.REJECTED, "approve"),
        (ApprovalStatus.EXPIRED, "approve"),
    ],
    ids=["approved-then-approve", "approved-then-reject", "rejected-then-approve",
         "expired-then-approve"],
)
async def test_a_settled_request_cannot_be_decided_again(
    admin_client, db, factory, workspace, settled, verb
):
    row = await factory.approval(workspace, status=settled.value)

    response = await admin_client.post(
        f"/api/v1/approvals/{row.id}/{verb}", json={"note": "Changed my mind"}
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"
    stored = await db.get(ApprovalRequest, row.id)
    assert stored.status == settled.value, "the original decision stands"


async def test_an_escalated_request_cannot_be_escalated_again(
    admin_client, factory, workspace
):
    """Escalation is a hand-off, not a queue: it happens once."""
    row = await factory.approval(workspace, status=ApprovalStatus.ESCALATED.value)

    response = await admin_client.post(
        f"/api/v1/approvals/{row.id}/escalate", json={"note": "And again"}
    )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["details"]["attempted"] == ApprovalStatus.ESCALATED.value
    assert sorted(error["details"]["allowed"]) == [
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.REJECTED.value,
    ]


# ===========================================================================
# Secrets
# ===========================================================================


async def test_revealing_a_credential_returns_it_and_leaves_a_trail(
    admin_client, db, factory, workspace
):
    secret = await factory.secret(
        workspace, name="Payments key", value="sk-live-abcdef123456"
    )

    response = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal",
        json={"justification": "Investigating a failed settlement"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == "sk-live-abcdef123456"
    assert body["justification"] == "Investigating a failed settlement"
    assert body["access_log_id"]

    entries = await db.scalars(
        select(SecretAccessLog).where(SecretAccessLog.secret_id == secret.id)
    )
    assert [entry.action for entry in entries] == [SecretAccessAction.REVEAL.value]
    assert entries[0].success is True
    assert entries[0].justification == "Investigating a failed settlement"
    assert "secret.revealed" in await audit_actions(db, workspace)


async def test_a_reveal_appears_in_the_credentials_own_access_log(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")
    await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Rotation check"}
    )

    log = await admin_client.get(f"/api/v1/secrets/{secret.id}/access-log")

    assert log.status_code == 200, log.text
    assert log.json()["total"] == 1
    assert log.json()["items"][0]["action"] == SecretAccessAction.REVEAL.value


async def test_a_refused_reveal_is_audited_just_as_loudly(
    as_role, db, factory, workspace
):
    """A refusal is the interesting half of an access log, not the boring half."""
    from fulcrum_ops_api.models.identity import Role

    secret = await factory.secret(workspace, name="Payments key")
    async with as_role(Role.OPERATOR) as http:
        response = await http.post(
            f"/api/v1/secrets/{secret.id}/reveal",
            json={"justification": "Just curious"},
        )

    assert response.status_code == 403
    entries = await db.scalars(
        select(SecretAccessLog).where(SecretAccessLog.secret_id == secret.id)
    )
    assert len(entries) == 1
    assert entries[0].success is False
    assert entries[0].justification == "Just curious"


async def test_a_disabled_credential_cannot_be_revealed_and_the_attempt_is_logged(
    admin_client, db, factory, workspace
):
    secret = await factory.secret(
        workspace, name="Retired key", status=SecretStatus.DISABLED.value
    )

    response = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Need it back"}
    )

    assert response.status_code == 412, response.text
    assert error_code(response) == "precondition_failed"
    entries = await db.scalars(
        select(SecretAccessLog).where(SecretAccessLog.secret_id == secret.id)
    )
    assert [entry.success for entry in entries] == [False]


async def test_a_credential_the_control_plane_only_points_at_cannot_be_revealed(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Vault-only key", value=None)

    response = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Reading it"}
    )

    assert response.status_code == 412, response.text
    assert "vault" in response.json()["error"]["message"].lower()


async def test_rotation_replaces_the_material_and_restarts_the_clock(
    admin_client, db, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key", value="sk-old-000000")
    before = (await db.get(Secret, secret.id)).next_rotation_at

    rotated = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/rotate",
        json={"value": "sk-new-111111", "reason": "Quarterly rotation"},
    )

    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["generated"] is False
    assert body["value"] is None, "a supplied value is never echoed back"
    assert body["next_rotation_at"] is not None

    revealed = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Confirming"}
    )
    assert revealed.json()["value"] == "sk-new-111111"

    stored = await db.get(Secret, secret.id)
    assert stored.next_rotation_at != before
    assert "secret.rotated" in await audit_actions(db, workspace)


async def test_a_generated_rotation_hands_the_value_back_exactly_once(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")

    rotated = await admin_client.post(f"/api/v1/secrets/{secret.id}/rotate", json={})

    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["generated"] is True
    assert body["value"], "a generated value must be shown once or it is lost"

    # Reading the credential back does not repeat it in the list payload.
    listed = await admin_client.get("/api/v1/secrets")
    assert body["value"] not in listed.text


async def test_rotation_clears_a_warning_state(admin_client, db, factory, workspace):
    secret = await factory.secret(
        workspace, name="Overdue key", status=SecretStatus.ROTATION_OVERDUE.value
    )

    await admin_client.post(f"/api/v1/secrets/{secret.id}/rotate", json={})

    stored = await db.get(Secret, secret.id)
    assert stored.status == SecretStatus.ACTIVE.value


async def test_a_disabled_credential_cannot_be_rotated(admin_client, factory, workspace):
    secret = await factory.secret(
        workspace, name="Retired key", status=SecretStatus.DISABLED.value
    )
    response = await admin_client.post(f"/api/v1/secrets/{secret.id}/rotate", json={})
    assert response.status_code == 412, response.text


async def test_disable_then_enable_returns_a_credential_to_service(
    admin_client, db, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")

    disabled = await admin_client.post(
        f"/api/v1/secrets/{secret.id}/disable", json={"reason": "Suspected leak"}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == SecretStatus.DISABLED.value

    enabled = await admin_client.post(f"/api/v1/secrets/{secret.id}/enable", json={})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == SecretStatus.ACTIVE.value

    stored = await db.get(Secret, secret.id)
    assert stored.status == SecretStatus.ACTIVE.value
    actions = await audit_actions(db, workspace)
    assert "secret.disabled" in actions
    assert "secret.enabled" in actions


async def test_disabling_twice_is_a_conflict(admin_client, factory, workspace):
    secret = await factory.secret(
        workspace, name="Retired key", status=SecretStatus.DISABLED.value
    )
    response = await admin_client.post(f"/api/v1/secrets/{secret.id}/disable", json={})
    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"


async def test_enabling_a_credential_that_is_not_disabled_is_a_conflict(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Live key")
    response = await admin_client.post(f"/api/v1/secrets/{secret.id}/enable", json={})
    assert response.status_code == 409, response.text


async def test_revocation_is_final(admin_client, db, factory, workspace):
    """The answer to needing a revoked credential back is a new credential."""
    secret = await factory.secret(
        workspace, name="Burned key", status=SecretStatus.REVOKED.value
    )

    enabled = await admin_client.post(f"/api/v1/secrets/{secret.id}/enable", json={})
    disabled = await admin_client.post(f"/api/v1/secrets/{secret.id}/disable", json={})

    assert enabled.status_code == 412, enabled.text
    assert disabled.status_code == 409, disabled.text
    stored = await db.get(Secret, secret.id)
    assert stored.status == SecretStatus.REVOKED.value


# ===========================================================================
# Deployments
# ===========================================================================


@pytest.fixture
def instant_pipeline(monkeypatch) -> None:
    """Run the pipeline without its simulated stage durations.

    The runner sleeps for a few seconds per stage so a demo looks like a real
    release. Nothing about the state machine depends on that, and waiting for it
    would make these tests take a minute each, so the table of durations is
    emptied — ``STAGE_RUNTIME_SECONDS.get(name, 0.0)`` then answers zero for
    every stage and the pipeline advances as fast as the database allows.
    """
    monkeypatch.setattr(deployments_service, "STAGE_RUNTIME_SECONDS", {})


@pytest.fixture(autouse=True)
def stop_pipelines():
    """Cancel any pipeline a test left running, before its database goes away."""
    yield
    for deployment_id in list(deployments_service.runner._tasks):
        deployments_service.runner.cancel(deployment_id)


async def wait_for(probe, predicate, *, give_up_after: float = 10.0):
    """Poll ``probe`` until ``predicate`` holds, the way the console polls."""
    deadline = asyncio.get_running_loop().time() + give_up_after
    latest = None
    while asyncio.get_running_loop().time() < deadline:
        latest = await probe()
        if predicate(latest):
            return latest
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition never held; last saw {latest!r}")


async def test_a_new_deployment_is_queued_with_its_five_stages_pending(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")

    response = await admin_client.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": "v2.0.0"},
    )

    assert response.status_code == 201, response.text
    deployment_id = response.json()["id"]
    # The runner may already have started; the create response is the contract.
    assert response.json()["status"] in (
        DeploymentStatus.QUEUED.value,
        DeploymentStatus.RUNNING.value,
    )

    stages = await admin_client.get(f"/api/v1/deployments/{deployment_id}/stages")
    assert [stage["name"] for stage in stages.json()] == [
        "Build",
        "Automated Tests",
        "Security Scan",
        "Approval",
        "Deploy",
    ]


async def test_the_pipeline_runs_its_stages_in_order_and_stops_at_the_gate(
    admin_client, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    created = await admin_client.post(
        "/api/v1/deployments",
        json={
            "environment_id": environment.id,
            "version": "v2.0.0",
            "commit_ref": "9f2c1ab",
        },
    )
    deployment_id = created.json()["id"]

    async def stages():
        response = await admin_client.get(f"/api/v1/deployments/{deployment_id}/stages")
        return {stage["name"]: stage["status"] for stage in response.json()}

    reached = await wait_for(
        stages, lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value
    )

    assert reached["Build"] == DeploymentStageStatus.COMPLETED.value
    assert reached["Automated Tests"] == DeploymentStageStatus.COMPLETED.value
    assert reached["Security Scan"] == DeploymentStageStatus.COMPLETED.value
    assert reached["Deploy"] == DeploymentStageStatus.PENDING.value, (
        "Deploy must not start before the gate is decided"
    )

    detail = await admin_client.get(f"/api/v1/deployments/{deployment_id}")
    assert detail.json()["status"] == DeploymentStatus.RUNNING.value


async def test_approving_the_gate_lets_the_release_finish(
    admin_client, owner_client, factory, workspace, instant_pipeline
):
    """One person starts the release and a different one approves it.

    Four eyes: whoever started a deployment cannot also wave it through the
    gate. (Rejecting your own release is still allowed -- see the next test.)
    """
    environment = await factory.environment(workspace, name="Production East")
    created = await admin_client.post(
        "/api/v1/deployments",
        json={
            "environment_id": environment.id,
            "version": "v2.0.0",
            "commit_ref": "9f2c1ab",
        },
    )
    deployment_id = created.json()["id"]

    async def stages():
        response = await admin_client.get(f"/api/v1/deployments/{deployment_id}/stages")
        return {stage["name"]: stage["status"] for stage in response.json()}

    await wait_for(stages, lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value)

    own = await admin_client.post(
        f"/api/v1/deployments/{deployment_id}/approve",
        json={"approved": True, "note": "Approving my own release"},
    )
    assert own.status_code == 403, own.text

    approved = await owner_client.post(
        f"/api/v1/deployments/{deployment_id}/approve",
        json={"approved": True, "note": "Release window agreed"},
    )
    assert approved.status_code == 200, approved.text

    async def status_of():
        response = await admin_client.get(f"/api/v1/deployments/{deployment_id}")
        return response.json()["status"]

    final = await wait_for(status_of, lambda s: s == DeploymentStatus.SUCCEEDED.value)
    assert final == DeploymentStatus.SUCCEEDED.value
    assert (await stages())["Deploy"] == DeploymentStageStatus.COMPLETED.value


async def test_rejecting_the_gate_halts_the_release(
    admin_client, factory, workspace, instant_pipeline
):
    environment = await factory.environment(workspace, name="Production East")
    created = await admin_client.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": "v2.0.0"},
    )
    deployment_id = created.json()["id"]

    async def stages():
        response = await admin_client.get(f"/api/v1/deployments/{deployment_id}/stages")
        return {stage["name"]: stage["status"] for stage in response.json()}

    await wait_for(stages, lambda s: s["Approval"] == DeploymentStageStatus.RUNNING.value)

    rejected = await admin_client.post(
        f"/api/v1/deployments/{deployment_id}/approve",
        json={"approved": False, "note": "Not in this window"},
    )

    assert rejected.status_code == 200, rejected.text
    detail = await admin_client.get(f"/api/v1/deployments/{deployment_id}")
    assert detail.json()["status"] == DeploymentStatus.HALTED.value
    assert (await stages())["Deploy"] != DeploymentStageStatus.COMPLETED.value


async def test_deciding_a_gate_that_has_not_opened_is_refused(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Staging")
    deployment = await factory.deployment(
        workspace,
        environment,
        status=DeploymentStatus.QUEUED.value,
        stage_status=DeploymentStageStatus.PENDING.value,
    )

    response = await admin_client.post(
        f"/api/v1/deployments/{deployment.id}/approve", json={"approved": True}
    )

    assert response.status_code == 412, response.text
    assert error_code(response) == "precondition_failed"


async def test_halting_an_in_flight_deployment_skips_what_is_left(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Staging")
    deployment = await factory.deployment(
        workspace,
        environment,
        status=DeploymentStatus.RUNNING.value,
        stage_status=DeploymentStageStatus.PENDING.value,
        finished_at=None,
    )

    response = await admin_client.post(
        f"/api/v1/deployments/{deployment.id}/halt", json={"reason": "Incident in progress"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == DeploymentStatus.HALTED.value

    stages = await admin_client.get(f"/api/v1/deployments/{deployment.id}/stages")
    assert {stage["status"] for stage in stages.json()} == {
        DeploymentStageStatus.SKIPPED.value
    }
    assert all(stage["log"] == "Incident in progress" for stage in stages.json())


async def test_halting_a_finished_deployment_is_a_conflict(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Staging")
    deployment = await factory.deployment(
        workspace, environment, status=DeploymentStatus.SUCCEEDED.value
    )

    response = await admin_client.post(f"/api/v1/deployments/{deployment.id}/halt", json={})

    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"


async def test_a_rollback_is_a_new_deployment_pointing_at_the_one_it_undoes(
    admin_client, db, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")
    await factory.deployment(
        workspace,
        environment,
        version="v1.9.0",
        status=DeploymentStatus.SUCCEEDED.value,
        started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        finished_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
    )
    broken = await factory.deployment(
        workspace, environment, version="v2.0.0", status=DeploymentStatus.SUCCEEDED.value
    )

    response = await admin_client.post(
        f"/api/v1/deployments/{broken.id}/rollback", json={"reason": "Error rate spiked"}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["version"] == "v1.9.0", "the rollback deploys the previous good version"
    assert data["rollback_of_deployment_id"] == broken.id

    source = await admin_client.get(f"/api/v1/deployments/{broken.id}")
    assert source.json()["status"] == DeploymentStatus.ROLLED_BACK.value
    assert "deployment.rollback" in await audit_actions(db, workspace)


async def test_rolling_the_same_release_back_twice_is_a_conflict(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")
    await factory.deployment(
        workspace, environment, version="v1.9.0", status=DeploymentStatus.SUCCEEDED.value
    )
    broken = await factory.deployment(
        workspace, environment, version="v2.0.0", status=DeploymentStatus.SUCCEEDED.value
    )

    first = await admin_client.post(f"/api/v1/deployments/{broken.id}/rollback", json={})
    second = await admin_client.post(f"/api/v1/deployments/{broken.id}/rollback", json={})

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert error_code(second) == "conflict"


async def test_an_in_flight_deployment_must_be_halted_before_it_is_rolled_back(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")
    await factory.deployment(
        workspace, environment, version="v1.9.0", status=DeploymentStatus.SUCCEEDED.value
    )
    running = await factory.deployment(
        workspace,
        environment,
        version="v2.0.0",
        status=DeploymentStatus.RUNNING.value,
        finished_at=None,
    )

    response = await admin_client.post(
        f"/api/v1/deployments/{running.id}/rollback", json={}
    )

    assert response.status_code == 412, response.text
    assert "halt" in response.json()["error"]["message"].lower()


async def test_a_rollback_with_nothing_to_roll_back_to_is_refused(
    admin_client, factory, workspace
):
    environment = await factory.environment(workspace, name="Production East")
    only = await factory.deployment(
        workspace, environment, version="v1.0.0", status=DeploymentStatus.SUCCEEDED.value
    )

    response = await admin_client.post(f"/api/v1/deployments/{only.id}/rollback", json={})

    assert response.status_code == 412, response.text
    assert "target_version" in response.json()["error"]["message"]


async def test_a_deployment_into_an_offline_environment_is_refused(
    admin_client, factory, workspace
):
    environment = await factory.environment(
        workspace, name="Cold DR", status=EnvironmentStatus.OFFLINE.value
    )

    response = await admin_client.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": "v2.0.0"},
    )

    assert response.status_code == 412, response.text
    assert "offline" in response.json()["error"]["message"].lower()


# ===========================================================================
# Prompts
# ===========================================================================


async def create_prompt(http, name: str = "Support system prompt") -> dict:
    response = await http.post(
        "/api/v1/prompts",
        json={
            "name": name,
            "template": "You are a helpful assistant for {{customer}}.",
            "environment": EnvironmentType.PRODUCTION.value,
            "change_note": "Initial draft",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_new_prompt_starts_as_a_draft(admin_client):
    prompt = await create_prompt(admin_client)
    assert prompt["status"] == "Draft"


async def test_draft_to_review_to_approved(admin_client, db, workspace):
    prompt = await create_prompt(admin_client)

    submitted = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/submit-review", json={"note": "Ready for review"}
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["prompt"]["status"] == "In Review"
    assert submitted.json()["previous_status"] == "Draft"

    approved = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/approve", json={"note": "Reads well"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["prompt"]["status"] == "Approved"

    read_back = await admin_client.get(f"/api/v1/prompts/{prompt['id']}")
    assert read_back.json()["status"] == "Approved"
    actions = await audit_actions(db, workspace)
    assert "prompt.submitted_for_review" in actions
    assert "prompt.approved" in actions


async def test_a_draft_cannot_skip_review(admin_client):
    prompt = await create_prompt(admin_client)

    response = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/approve", json={"note": "Looks fine to me"}
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"
    assert "In Review" in response.json()["error"]["message"]


async def test_submitting_a_prompt_that_is_already_in_review_is_a_conflict(admin_client):
    prompt = await create_prompt(admin_client)
    await admin_client.post(f"/api/v1/prompts/{prompt['id']}/submit-review", json={})

    response = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/submit-review", json={}
    )

    assert response.status_code == 409, response.text
    assert "already" in response.json()["error"]["message"].lower()


async def test_a_new_body_sends_an_approved_prompt_back_to_draft(admin_client):
    """Approval is of a body, not of a name; changing the body voids it."""
    prompt = await create_prompt(admin_client)
    await admin_client.post(f"/api/v1/prompts/{prompt['id']}/submit-review", json={})
    await admin_client.post(f"/api/v1/prompts/{prompt['id']}/approve", json={})

    committed = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/versions",
        json={"template": "You are a terse assistant.", "change_note": "Tighter tone"},
    )

    assert committed.status_code == 201, committed.text
    assert committed.json()["prompt"]["status"] == "Draft"


async def test_blocking_takes_an_approved_prompt_out_of_service(admin_client):
    prompt = await create_prompt(admin_client)
    await admin_client.post(f"/api/v1/prompts/{prompt['id']}/submit-review", json={})
    await admin_client.post(f"/api/v1/prompts/{prompt['id']}/approve", json={})

    blocked = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/block", json={"note": "Leaks internal names"}
    )

    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["prompt"]["status"] == "Blocked"

    # A blocked prompt goes back through review; it cannot be approved directly.
    straight_back = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/approve", json={}
    )
    assert straight_back.status_code == 409, straight_back.text

    resubmitted = await admin_client.post(
        f"/api/v1/prompts/{prompt['id']}/submit-review", json={}
    )
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["prompt"]["status"] == "In Review"


async def test_a_second_prompt_may_not_reuse_a_name(admin_client):
    await create_prompt(admin_client, name="Support system prompt")
    response = await admin_client.post(
        "/api/v1/prompts",
        json={"name": "Support system prompt", "template": "Another body"},
    )
    assert response.status_code == 409, response.text


# ===========================================================================
# Agents
# ===========================================================================


async def test_a_registered_agent_waits_for_review_before_it_runs(
    admin_client, db, workspace, engine
):
    created = await admin_client.post(
        "/api/v1/agents",
        json={
            "name": "Refund Bot",
            "platform": "Custom Agent",
            "agent_type": "Pro-code",
            "environment": "Development",
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == AgentStatus.PENDING_REVIEW.value
    assert body["is_provisioned"] is True, "its telemetry project is created first"

    activated = await admin_client.post(
        f"/api/v1/agents/{body['id']}/activate", json={"reason": "Review passed"}
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["status"] == AgentStatus.ACTIVE.value


async def test_activating_an_active_agent_is_a_conflict(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    response = await admin_client.post(f"/api/v1/agents/{agent.id}/activate", json={})
    assert response.status_code == 409, response.text
    assert "already active" in response.json()["error"]["message"].lower()


async def test_deactivating_and_reactivating_an_agent(
    admin_client, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    off = await admin_client.post(
        f"/api/v1/agents/{agent.id}/deactivate", json={"reason": "Cost review"}
    )
    assert off.status_code == 200, off.text
    assert off.json()["data"]["status"] == AgentStatus.INACTIVE.value

    on = await admin_client.post(f"/api/v1/agents/{agent.id}/activate", json={})
    assert on.status_code == 200, on.text

    actions = await audit_actions(db, workspace)
    assert "agent.deactivated" in actions
    assert "agent.activated" in actions


async def test_an_agent_with_no_telemetry_project_cannot_be_activated(
    admin_client, factory, workspace
):
    agent = await factory.agent(
        workspace, name="Orphan Bot", status=AgentStatus.PENDING_REVIEW.value
    )

    response = await admin_client.post(f"/api/v1/agents/{agent.id}/activate", json={})

    assert response.status_code == 412, response.text
    assert "telemetry project" in response.json()["error"]["message"]


async def test_a_blocked_agent_cannot_be_activated(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(
        workspace,
        engine,
        name="Blocked Bot",
        status=AgentStatus.PENDING_REVIEW.value,
        policy_status=PolicyStatus.BLOCKED.value,
    )

    response = await admin_client.post(f"/api/v1/agents/{agent.id}/activate", json={})

    assert response.status_code == 412, response.text
    assert "policy" in response.json()["error"]["message"].lower()


async def test_an_active_agent_may_not_be_deleted(
    admin_client, factory, workspace, engine
):
    """Deleting a live registration silently is how telemetry goes missing."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    refused = await admin_client.delete(f"/api/v1/agents/{agent.id}")

    assert refused.status_code == 412, refused.text
    assert error_code(refused) == "precondition_failed"

    await admin_client.post(f"/api/v1/agents/{agent.id}/deactivate", json={})
    deleted = await admin_client.delete(f"/api/v1/agents/{agent.id}")
    assert deleted.status_code == 204, deleted.text
    assert (await admin_client.get(f"/api/v1/agents/{agent.id}")).status_code == 404


async def test_deleting_an_agent_leaves_its_audit_trail_behind(
    admin_client, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot", status=AgentStatus.INACTIVE.value
    )
    await admin_client.delete(f"/api/v1/agents/{agent.id}")

    events = await db.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == agent.id)
    )
    assert [event.action for event in events] == ["agent.deleted"]
    assert events[0].entity_label == "Support Bot"


# ===========================================================================
# Configurations
# ===========================================================================

#: A Model configuration body that passes validation in Production: the schema
#: makes provider and model required outright, and temperature and max_tokens
#: required once the configuration runs in Production.
def model_body(**overrides) -> dict:
    body = {
        "provider": "azure-openai",
        "model": "gpt-4o",
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    body.update(overrides)
    return body


async def test_a_new_version_becomes_current_and_demotes_the_old_one(
    admin_client, db, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={
            "payload": model_body(temperature=0.7),
            "change_note": "Warmer replies",
            "activate": True,
        },
    )

    assert response.status_code == 201, response.text
    new_version = response.json()["version"]["version"]
    assert new_version != "v1.0.0"

    versions = await admin_client.get(f"/api/v1/configurations/{configuration.id}/versions")
    current = [row for row in versions.json()["items"] if row["is_current"]]
    assert [row["version"] for row in current] == [new_version], "exactly one is current"

    stored = await db.get(Configuration, configuration.id)
    assert stored.current_version == new_version


async def test_a_version_that_fails_validation_is_refused_rather_than_published(
    admin_client, db, factory, workspace
):
    """The findings come back field by field, which is what the dialog renders."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": {"temperature": 0.7}, "activate": True},
    )

    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert {finding["field"] for finding in details["findings"]} >= {"provider", "model"}
    stored = await db.get(Configuration, configuration.id)
    assert stored.current_version == "v1.0.0", "nothing was published"


async def test_a_version_may_be_drafted_without_going_live(
    admin_client, db, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", payload=model_body()
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.9), "activate": False},
    )

    assert response.status_code == 201, response.text
    assert response.json()["version"]["status"] == ConfigurationStatus.DRAFT.value
    assert response.json()["version"]["is_current"] is False
    stored = await db.get(Configuration, configuration.id)
    assert stored.current_version == "v1.0.0", "drafting does not publish"


async def test_reusing_a_version_label_is_a_conflict(admin_client, factory, workspace):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"version": "v1.0.0", "payload": model_body()},
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"


async def test_rollback_republishes_the_old_body_without_rewriting_history(
    admin_client, db, factory, workspace
):
    configuration = await factory.configuration(
        workspace,
        name="Router settings",
        current_version="v1.0.0",
        payload=model_body(temperature=0.2),
    )
    bumped = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.9), "activate": True},
    )
    assert bumped.status_code == 201, bumped.text

    rolled = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback",
        json={"version": "v1.0.0", "change_note": "Too warm"},
    )

    assert rolled.status_code == 200, rolled.text
    restored = rolled.json()["version"]["version"]
    assert restored != "v1.0.0", "the rollback lands as a new version on top"

    body = await admin_client.get(
        f"/api/v1/configurations/{configuration.id}/versions/{restored}"
    )
    assert body.json()["payload"]["temperature"] == 0.2

    history = await admin_client.get(f"/api/v1/configurations/{configuration.id}/versions")
    labels = [row["version"] for row in history.json()["items"]]
    assert "v1.0.0" in labels, "the original revision keeps its own row"
    assert len(labels) == 3
    assert "configuration.rolled_back" in await audit_actions(db, workspace)


async def test_rolling_back_to_the_version_already_live_is_refused(
    admin_client, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={"version": "v1.0.0"}
    )

    assert response.status_code == 412, response.text
    assert "already current" in response.json()["error"]["message"]


async def test_a_configuration_with_no_history_cannot_be_rolled_back(
    admin_client, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={}
    )
    assert response.status_code == 412, response.text
    assert "earlier version" in response.json()["error"]["message"]


async def test_an_archived_configuration_cannot_be_versioned_until_it_is_restored(
    admin_client, factory, workspace
):
    configuration = await factory.configuration(
        workspace,
        name="Retired router",
        status=ConfigurationStatus.ARCHIVED.value,
        payload=model_body(),
    )

    refused = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.4)},
    )

    assert refused.status_code == 412, refused.text
    assert "archived" in refused.json()["error"]["message"].lower()

    restored = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/restore", json={}
    )
    assert restored.status_code == 200, restored.text

    accepted = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.4)},
    )
    assert accepted.status_code == 201, accepted.text


@pytest.mark.xfail(
    reason=(
        "Archiving is a one-way door for a configuration whose stored body does not "
        "pass its schema. create_configuration does not validate the body, so a Draft "
        "carrying an incomplete Model body is reachable through the API; Draft -> "
        "Archived is a legal move; and from Archived both ways out are refused — "
        "create_version answers 412 ('archived, restore it first') and restore answers "
        "422 because it re-validates the current body. The row can then never come "
        "back. Either create should validate, or restore should accept a replacement "
        "body, or create_version should be allowed on an archived row."
    ),
    strict=True,
)
async def test_an_archived_configuration_can_always_be_brought_back(admin_client):
    created = await admin_client.post(
        "/api/v1/configurations",
        json={
            "name": "Legacy router",
            "config_type": "Model",
            "environment": "Production",
            "version": "v1.0.0",
            "payload": {"temperature": 0.2},
        },
    )
    assert created.status_code == 201, created.text
    configuration_id = created.json()["id"]

    archived = await admin_client.post(
        f"/api/v1/configurations/{configuration_id}/deprecate", json={"archive": True}
    )
    assert archived.status_code == 200, archived.text

    # Way out one: cut a version carrying a body that would pass.
    versioned = await admin_client.post(
        f"/api/v1/configurations/{configuration_id}/versions",
        json={"payload": model_body(), "activate": True},
    )
    assert versioned.status_code == 412, versioned.text

    # Way out two: restore it, which re-validates the body it still carries.
    restored = await admin_client.post(
        f"/api/v1/configurations/{configuration_id}/restore", json={}
    )
    assert restored.status_code == 200, restored.text
