"""Reconciliation of the governance services after the 2026-09-18 fix pass.

An approval rule is a policy, so the Policy Center lists it and offers the same
Deactivate / Activate verbs as for every other row. The rule body carries a
trigger and approvers and, on purpose, no ``conditions`` — and the activation
precondition read "no conditions" as "nothing to enforce". A rule switched off
in the Policy Center could therefore never be switched back on there.
"""

from __future__ import annotations

from conftest import error_code
from fulcrum_ops_api.models.governance import PolicyCategory, PolicyStatus

POLICIES = "/api/v1/policies"
RULES = "/api/v1/approvals/rules"


async def a_rule(client, **body) -> dict:
    """Create a rule the way the New Approval Rule modal does."""
    created = await client.post(
        RULES,
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


async def test_an_approval_rule_deactivated_in_the_policy_center_can_be_activated_again(
    admin_client,
):
    rule = await a_rule(admin_client)

    stopped = await admin_client.post(f"{POLICIES}/{rule['id']}/deactivate", json={})
    assert stopped.status_code == 200, stopped.text

    started = await admin_client.post(f"{POLICIES}/{rule['id']}/activate")

    assert started.status_code == 200, started.text
    after = (await admin_client.get(f"{RULES}/{rule['id']}")).json()
    assert after["status"] == PolicyStatus.ACTIVE.value
    # Still the rule it was: activating it must not have grown it a clause list.
    assert after["trigger"] == "Financial action above threshold"
    assert after["approvers"] == ["Finance Leads"]


async def test_an_approval_rule_can_be_switched_on_from_the_policy_edit_form(admin_client):
    rule = await a_rule(admin_client, status="Inactive")

    saved = await admin_client.patch(
        f"{POLICIES}/{rule['id']}", json={"status": PolicyStatus.ACTIVE.value}
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == PolicyStatus.ACTIVE.value


async def test_any_other_policy_with_no_conditions_is_still_refused(
    admin_client, factory, workspace
):
    hollow = await factory.policy(
        workspace, name="Hollow", status=PolicyStatus.INACTIVE.value, rules={}
    )

    started = await admin_client.post(f"{POLICIES}/{hollow.id}/activate")

    assert started.status_code == 412, started.text
    assert error_code(started) == "precondition_failed"


async def test_an_approval_policy_that_does_carry_conditions_has_them_checked(
    admin_client, factory, workspace
):
    drifted = await factory.policy(
        workspace,
        name="Escalate on a made-up signal",
        category=PolicyCategory.APPROVAL_ESCALATION.value,
        status=PolicyStatus.INACTIVE.value,
        rules={
            "conditions": [{"signal": "no_such_signal", "operator": "eq", "value": 1}],
            "action": {"mode": "Require Approval"},
        },
    )

    started = await admin_client.post(f"{POLICIES}/{drifted.id}/activate")

    assert started.status_code == 412, started.text
    assert "no_such_signal" in started.text
