"""Hand-offs reconciled into the Agent Detail service after the September fix pass.

Two things other screens' fixes needed from this one. The Risk & Policy summary
listed explicit bindings only, so an agent governed by a Global policy read "0
policies bound" while every run it made was being checked. And no route served a
whole prompt body, so the version editor could not be opened on a prompt longer
than the preview the history carries.
"""

from __future__ import annotations

from fulcrum_ops_api.models.governance import (
    PolicyBinding,
    PolicyEnforcement,
    PolicyScope,
    PolicyStatus,
)
from fulcrum_ops_api.models.registry import AgentConnector, EnvironmentType
from fulcrum_ops_api.schemas.agents import TEMPLATE_PREVIEW_CHARS


def _listed(body: dict) -> dict[str, dict]:
    return {policy["name"]: policy for policy in body["policies"]}


# ---------------------------------------------------------------------------
# The policies that govern an agent are the ones the enforcement path applies
# ---------------------------------------------------------------------------


async def test_an_agent_governed_only_by_scope_lists_the_policies_that_reach_it(
    admin_client, factory, workspace, other_workspace
):
    """Not one of these has a binding row, and ingest applies every one of them."""
    agent = await factory.agent(
        workspace, name="Refund Bot", environment=EnvironmentType.PRODUCTION.value
    )
    neighbour = await factory.agent(workspace, name="Other Bot")
    granted = await factory.connector(workspace, name="Payments API")
    ungranted = await factory.connector(workspace, name="HR System")
    await factory.add(
        AgentConnector(workspace_id=workspace.id, agent_id=agent.id, connector_id=granted.id)
    )

    await factory.policy(workspace, name="Everyone")
    await factory.policy(
        workspace,
        name="Production only",
        scope=PolicyScope.ENVIRONMENT.value,
        scope_ref=EnvironmentType.PRODUCTION.value,
    )
    await factory.policy(
        workspace, name="This agent", scope=PolicyScope.AGENT.value, scope_ref=agent.id
    )
    await factory.policy(
        workspace,
        name="Payments callers",
        scope=PolicyScope.CONNECTOR.value,
        scope_ref=granted.id,
    )
    await factory.policy(
        workspace, name="Firing above baseline", status=PolicyStatus.WARNING.value
    )
    # None of these reach the agent.
    await factory.policy(
        workspace,
        name="Staging only",
        scope=PolicyScope.ENVIRONMENT.value,
        scope_ref=EnvironmentType.STAGING.value,
    )
    await factory.policy(
        workspace, name="The neighbour", scope=PolicyScope.AGENT.value, scope_ref=neighbour.id
    )
    await factory.policy(
        workspace,
        name="HR callers",
        scope=PolicyScope.CONNECTOR.value,
        scope_ref=ungranted.id,
    )
    await factory.policy(workspace, name="Still a draft", status=PolicyStatus.INACTIVE.value)
    await factory.policy(other_workspace, name="Another tenant's")

    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.status_code == 200, response.text
    listed = _listed(response.json())
    assert sorted(listed) == [
        "Everyone",
        "Firing above baseline",
        "Payments callers",
        "Production only",
        "This agent",
    ]
    for policy in listed.values():
        assert policy["via"] == "scope"
        assert policy["binding_id"] is None
        assert policy["bound_at"] is None
    assert response.json()["agent"]["policies_applied"] == 5


async def test_a_policy_that_exempts_the_agent_is_not_listed_as_governing_it(
    admin_client, factory, workspace
):
    """By id or by tag, the way the enforcement path reads the exception list."""
    agent = await factory.agent(workspace, name="Refund Bot", tags=["finance", "pilot"])
    await factory.policy(workspace, name="Applies")
    await factory.policy(workspace, name="Exempt by id", rules={"exceptions": [agent.id]})
    await factory.policy(workspace, name="Exempt by tag", rules={"exceptions": ["pilot"]})
    await factory.policy(workspace, name="Exempts someone else", rules={"exceptions": ["hr"]})

    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.status_code == 200, response.text
    assert sorted(_listed(response.json())) == ["Applies", "Exempts someone else"]
    assert response.json()["agent"]["policies_applied"] == 2


async def test_an_explicit_binding_is_listed_once_whatever_state_the_policy_is_in(
    admin_client, factory, workspace
):
    agent = await factory.agent(workspace, name="Refund Bot", policies_applied=41)
    enforced = await factory.policy(workspace, name="Bound and global")
    draft = await factory.policy(
        workspace,
        name="Bound draft",
        scope=PolicyScope.AGENT.value,
        scope_ref="some-other-agent",
        status=PolicyStatus.INACTIVE.value,
    )
    bindings = await factory.add_all(
        [
            PolicyBinding(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                bound_by="ada@example.com",
            )
            for policy in (enforced, draft)
        ]
    )

    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [policy["name"] for policy in body["policies"]] == ["Bound and global", "Bound draft"]
    listed = _listed(body)
    assert listed["Bound and global"]["via"] == "binding", "bound wins over its Global scope"
    assert listed["Bound and global"]["binding_id"] == bindings[0].id
    assert listed["Bound draft"]["via"] == "binding"
    assert listed["Bound draft"]["bound_by"] == "ada@example.com"
    assert listed["Bound draft"]["status"] == PolicyStatus.INACTIVE.value
    # One of the two is in force; the 41 typed at registration is not a count.
    assert body["agent"]["policies_applied"] == 1


async def test_the_enforcement_shown_is_the_one_the_rule_body_applies(
    admin_client, factory, workspace
):
    agent = await factory.agent(workspace, name="Refund Bot")
    await factory.policy(
        workspace,
        name="Says log, blocks",
        enforcement=PolicyEnforcement.LOG_ONLY.value,
        rules={"action": {"mode": PolicyEnforcement.BLOCK.value}},
    )

    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.status_code == 200, response.text
    assert _listed(response.json())["Says log, blocks"]["enforcement"] == "Block"


# ---------------------------------------------------------------------------
# A whole prompt body can be read, so the editor has something to open on
# ---------------------------------------------------------------------------


def _commit_prompt(engine, agent, *templates: str) -> list[dict]:
    """The agent's system prompt with some history, filed under its project."""
    prompt = engine._new_prompt(
        {
            "name": f"{agent.engine_project_name}-system-prompt",
            "project_id": agent.engine_project_id,
        }
    )
    return [engine._append_version(prompt, {"template": template}) for template in templates]


LONG_PROMPT = "You reconcile refunds. " + "Check the ledger before you answer. " * 90


async def test_one_version_is_served_with_its_whole_prompt_body(
    admin_client, factory, workspace, engine
):
    """The history carries a preview; committing an edit of *that* cut a
    3,000-character prompt down to its first 400, as the current version."""
    assert len(LONG_PROMPT) > TEMPLATE_PREVIEW_CHARS * 2
    # The registry names the live version, so "current" does not hang on two
    # commits made in the same instant having distinguishable timestamps.
    agent = await factory.provisioned_agent(
        workspace, engine, name="Refund Bot", prompt_version="2"
    )
    first, second = _commit_prompt(engine, agent, "You reconcile refunds.", LONG_PROMPT)

    history = await admin_client.get(f"/api/v1/agents/{agent.id}/versions")
    assert history.status_code == 200, history.text
    newest = next(row for row in history.json() if row["commit"] == second["commit"])
    assert len(newest["template_preview"]) == TEMPLATE_PREVIEW_CHARS
    assert newest["template_length"] == len(LONG_PROMPT), "says the preview was cut short"
    assert "template" not in newest, "the list stays light"

    by_commit = await admin_client.get(
        f"/api/v1/agents/{agent.id}/versions/{second['commit']}"
    )
    assert by_commit.status_code == 200, by_commit.text
    assert by_commit.json()["template"] == LONG_PROMPT
    assert by_commit.json()["commit"] == second["commit"]
    assert by_commit.json()["is_current"] is True

    by_label = await admin_client.get(f"/api/v1/agents/{agent.id}/versions/1")
    assert by_label.status_code == 200, by_label.text
    assert by_label.json()["commit"] == first["commit"]
    assert by_label.json()["template"] == "You reconcile refunds."
    assert by_label.json()["is_current"] is False


async def test_a_version_that_does_not_exist_is_a_404_and_so_is_another_tenants(
    admin_client, factory, workspace, other_workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Refund Bot")
    _commit_prompt(engine, agent, "You reconcile refunds.")
    unprovisioned = await factory.agent(workspace, name="Never Ran")
    theirs = await factory.provisioned_agent(other_workspace, engine, name="Their Bot")
    (their_version,) = _commit_prompt(engine, theirs, "Their secret instructions.")

    unknown = await admin_client.get(f"/api/v1/agents/{agent.id}/versions/nosuchcommit")
    assert unknown.status_code == 404, unknown.text

    no_history = await admin_client.get(f"/api/v1/agents/{unprovisioned.id}/versions/1")
    assert no_history.status_code == 404, no_history.text

    across = await admin_client.get(
        f"/api/v1/agents/{theirs.id}/versions/{their_version['commit']}"
    )
    assert across.status_code == 404, across.text
    assert "secret" not in across.text


async def test_compare_versions_is_still_reached_beside_the_new_route(
    admin_client, factory, workspace, engine
):
    """`/versions/diff` and `/versions/{version}` share a shape; diff must win."""
    agent = await factory.provisioned_agent(workspace, engine, name="Refund Bot")
    first, second = _commit_prompt(engine, agent, "Be brief.", "Be brief.\nCite the ledger.")

    response = await admin_client.get(
        f"/api/v1/agents/{agent.id}/versions/diff",
        params={"from": first["commit"], "to": second["commit"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["added_lines"] == 1
