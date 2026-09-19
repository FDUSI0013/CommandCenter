"""Regressions from the September audit of Prompt Studio.

Each test pins one thing the screen got wrong in production and the suite could
not see, usually because the engine double was more generous than the engine:
a table built from a field the registry's listing does not carry, a Test Prompt
button whose default always failed and left a dataset behind, a page that
re-asked the store for the same figures on every click, and a version history
that credited every commit to the registry's own service user.
"""

from __future__ import annotations

import pytest

from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.registry import EnvironmentType
from fulcrum_ops_api.services import prompts as prompts_service


async def _author(http, name: str = "Triage system prompt", **fields) -> dict:
    response = await http.post(
        "/api/v1/prompts",
        json={
            "name": name,
            "template": "You are the triage assistant for {{customer}}. Be brief.",
            "environment": EnvironmentType.PRODUCTION.value,
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _head_reads(engine, prompt_id: str) -> int:
    """How many times the registry was asked for this one prompt."""
    return sum(
        1
        for call in engine.calls
        if call.method == "GET" and call.path.endswith(f"/prompts/{prompt_id}")
    )


# ---------------------------------------------------------------------------
# The table is built from the head version, which the listing does not carry
# ---------------------------------------------------------------------------


async def test_the_registry_listing_is_the_public_view(admin_client, engine, engine_client):
    """The contract the rest of this section depends on, pinned on the double."""
    await _author(admin_client)

    listed = await engine_client.list_prompts(name="northwind")
    row = listed["content"][0]

    assert "latest_version" not in row
    assert "metadata" not in row
    assert row["version_count"] == 1


async def test_the_table_row_carries_what_the_head_version_says(
    admin_client, factory, workspace, engine
):
    """Version, environment, agent and tokens were blank on every row in production."""
    await factory.provisioned_agent(workspace, engine, name="Triage Agent")
    created = await _author(admin_client, agent="Triage Agent", version="v1.4.0")

    listed = await admin_client.get("/api/v1/prompts")

    assert listed.status_code == 200, listed.text
    row = listed.json()["items"][0]
    assert row["version"] == "v1.4.0"
    assert row["environment"] == "Production"
    assert row["agent"] == "Triage Agent"
    assert row["commit"] == created["commit"]
    assert row["template"].startswith("You are the triage assistant")
    assert row["estimated_tokens"] > 0
    assert row["variables"] == ["customer"]


async def test_the_dropdowns_the_search_and_the_export_see_the_head(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Triage Agent")
    await _author(admin_client, agent="Triage Agent", version="v1.4.0")
    await _author(admin_client, name="Unowned prompt", environment="Development")

    by_env = await admin_client.get("/api/v1/prompts", params={"env": "Production"})
    by_agent = await admin_client.get("/api/v1/prompts", params={"agent": "Triage Agent"})
    by_search = await admin_client.get("/api/v1/prompts", params={"q": "triage agent"})
    exported = await admin_client.get("/api/v1/prompts/export")

    for response in (by_env, by_agent, by_search):
        assert response.status_code == 200, response.text
        assert [row["name"] for row in response.json()["items"]] == ["Triage system prompt"]
    assert exported.status_code == 200, exported.text
    line = next(row for row in exported.text.splitlines() if "Triage system prompt" in row)
    assert "v1.4.0" in line
    assert "Production" in line


async def test_the_kpi_row_counts_the_runs_of_the_agents_that_own_prompts(
    admin_client, factory, workspace, engine
):
    """No head, no agent; no agent, no project to read run statistics from."""
    agent = await factory.provisioned_agent(workspace, engine, name="Triage Agent")
    engine.add_trace(project_name=agent.engine_project_name, name="a run")
    await _author(admin_client, agent="Triage Agent")

    summary = await admin_client.get("/api/v1/prompts/summary")

    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["projects_total"] == 1
    assert body["runs_30d"] == 1


async def test_a_head_is_read_once_and_again_only_when_a_version_is_committed(
    admin_client, engine
):
    """The steady state costs the registry nothing beyond the listing itself."""
    created = await _author(admin_client)
    prompt_id = created["id"]

    await admin_client.get("/api/v1/prompts")
    engine.reset_calls()
    await admin_client.get("/api/v1/prompts")
    await admin_client.get("/api/v1/prompts/summary")
    assert _head_reads(engine, prompt_id) == 0

    committed = await admin_client.post(
        f"/api/v1/prompts/{prompt_id}/versions",
        json={"template": "You are terse.", "version": "v2.0.0"},
    )
    assert committed.status_code == 201, committed.text
    # The registry is not promised to move a prompt's own timestamp when a
    # version is committed; the version count is what must be enough.
    engine.prompts[prompt_id]["last_updated_at"] = created["modified_at"]

    listed = await admin_client.get("/api/v1/prompts")

    row = listed.json()["items"][0]
    assert row["version"] == "v2.0.0"
    assert row["template"] == "You are terse."


async def test_one_unreadable_prompt_does_not_cost_the_workspace_its_table(
    admin_client, engine
):
    healthy = await _author(admin_client, name="Healthy prompt", version="v1.0.0")
    broken = await _author(admin_client, name="Broken prompt", version="v1.0.0")
    engine.fail_path(f"/prompts/{broken['id']}", 500)

    listed = await admin_client.get("/api/v1/prompts")

    assert listed.status_code == 200, listed.text
    rows = {row["id"]: row for row in listed.json()["items"]}
    assert rows[healthy["id"]]["version"] == "v1.0.0"
    assert rows[broken["id"]]["version"] is None, "not measured, so not invented"


# ---------------------------------------------------------------------------
# Test Prompt, with the box that is ticked by default
# ---------------------------------------------------------------------------


async def test_testing_a_prompt_with_the_defaults_records_the_cases(admin_client, engine):
    """The default always answered 422: the cases went to the store without a provenance."""
    created = await _author(admin_client)

    tested = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/test",
        json={"cases": [{"customer": "Northwind"}, {}]},
    )

    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["passed"] == 1 and body["failed"] == 1
    assert body["recorded"] is True
    assert body["scored"] is False, "no evaluator is attached, so nothing has scored it"
    assert "::" not in body["dataset_name"], "the namespace never reaches the client"

    stored = engine.dataset_items[body["dataset_id"]]
    assert [item["source"] for item in stored] == ["manual", "manual"]
    assert stored[0]["data"]["rendered_prompt"].startswith(
        "You are the triage assistant for Northwind"
    )
    assert engine.datasets[body["dataset_id"]]["name"] == f"northwind::{body['dataset_name']}"
    assert [row["dataset_name"] for row in engine.experiments.values()] == [
        f"northwind::{body['dataset_name']}"
    ]


async def test_a_test_that_cannot_be_stored_leaves_no_dataset_behind(admin_client, engine):
    """Every failed attempt used to leave an empty dataset on the Evaluations screen."""
    created = await _author(admin_client)
    engine.fail_path("/datasets/items", 500)

    tested = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/test", json={"cases": [{"customer": "Northwind"}]}
    )

    assert tested.status_code == 503, tested.text
    assert engine.datasets == {}


async def test_a_supplied_dataset_name_stays_inside_the_workspace(admin_client, engine):
    created = await _author(admin_client)

    tested = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/test",
        json={"cases": [{"customer": "Northwind"}], "dataset_name": "contoso::stolen"},
    )

    assert tested.status_code == 200, tested.text
    assert tested.json()["dataset_name"] == "contoso::stolen"
    assert [row["name"] for row in engine.datasets.values()] == ["northwind::contoso::stolen"]


async def test_two_tests_in_the_same_second_do_not_collide(admin_client, engine):
    created = await _author(admin_client)

    first = await admin_client.post(f"/api/v1/prompts/{created['id']}/test", json={})
    second = await admin_client.post(f"/api/v1/prompts/{created['id']}/test", json={})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["dataset_name"] != second.json()["dataset_name"]
    assert len(engine.datasets) == 2


# ---------------------------------------------------------------------------
# What one visit to the screen asks of the store
# ---------------------------------------------------------------------------


def _reads(engine, suffix: str) -> int:
    return sum(1 for call in engine.calls if call.method == "GET" and call.path.endswith(suffix))


@pytest.fixture
def remembered_stats(monkeypatch):
    """The per-agent run counters as production keeps them: for a minute."""
    monkeypatch.setattr(settings, "prompt_stats_cache_seconds", 60.0)
    prompts_service._stats.invalidate()
    yield
    prompts_service._stats.invalidate()


async def test_the_table_the_kpi_row_and_the_export_share_one_stats_read_per_agent(
    admin_client, factory, workspace, engine, remembered_stats
):
    """Each of them asked for a 30-day aggregate per agent, on every load."""
    agent = await factory.provisioned_agent(workspace, engine, name="Triage Agent")
    engine.add_trace(project_name=agent.engine_project_name, name="a run")
    await _author(admin_client, agent="Triage Agent")
    prompts_service._stats.invalidate()
    engine.reset_calls()

    listed = await admin_client.get("/api/v1/prompts")
    searched = await admin_client.get("/api/v1/prompts", params={"q": "triage"})
    summary = await admin_client.get("/api/v1/prompts/summary")
    exported = await admin_client.get("/api/v1/prompts/export")

    for response in (listed, searched, summary, exported):
        assert response.status_code == 200, response.text
    assert _reads(engine, "/traces/stats") == 1
    assert listed.json()["items"][0]["runs_30d"] == 1
    assert searched.json()["items"][0]["runs_30d"] == 1
    assert summary.json()["runs_30d"] == 1


async def test_a_stats_read_that_failed_is_not_remembered(
    admin_client, factory, workspace, engine, remembered_stats
):
    await factory.provisioned_agent(workspace, engine, name="Triage Agent")
    await _author(admin_client, agent="Triage Agent")
    prompts_service._stats.invalidate()

    engine.fail_path("/traces/stats", 503)
    assert (await admin_client.get("/api/v1/prompts")).status_code == 503
    engine.recover()

    assert (await admin_client.get("/api/v1/prompts")).status_code == 200


async def test_creating_a_prompt_does_not_walk_the_registry_first(admin_client, engine):
    """The registry answers 409 for a taken name; nobody needs to ask it twice."""
    await _author(admin_client)

    assert _reads(engine, "/prompts") == 0

    again = await admin_client.post(
        "/api/v1/prompts", json={"name": "Triage system prompt", "template": "Another"}
    )
    assert again.status_code == 409, again.text
    assert "already exists" in again.json()["error"]["message"]
    assert "Triage system prompt" in again.json()["error"]["message"]
    assert len(engine.prompts) == 1


async def test_testing_the_head_reads_the_prompt_not_its_whole_history(admin_client, engine):
    created = await _author(admin_client, version="v1.0.0")
    engine.reset_calls()

    tested = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/test",
        json={"cases": [{"customer": "Northwind"}], "score": False},
    )

    assert tested.status_code == 200, tested.text
    assert _reads(engine, "/versions") == 0
    body = tested.json()
    assert body["commit"] == created["commit"]
    assert body["version"] == "v1.0.0"
    assert body["cases"][0]["rendered"].startswith("You are the triage assistant for Northwind")


async def test_testing_an_earlier_commit_reports_that_commits_version(admin_client):
    """It reported the head's label beside the older commit's hash."""
    created = await _author(admin_client, version="v1.0.0")
    committed = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"template": "You are terse.", "version": "v2.0.0"},
    )
    assert committed.status_code == 201, committed.text

    tested = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/test",
        json={"commit": created["commit"], "score": False},
    )

    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["commit"] == created["commit"]
    assert body["version"] == "v1.0.0"
    assert body["variables"] == ["customer"]


async def test_a_diff_walks_the_history_once(admin_client, engine):
    created = await _author(admin_client, version="v1.0.0")
    await admin_client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"template": "You are terse.", "version": "v2.0.0"},
    )
    engine.reset_calls()

    compared = await admin_client.get(
        f"/api/v1/prompts/{created['id']}/diff", params={"from": "v1.0.0", "to": "v2.0.0"}
    )

    assert compared.status_code == 200, compared.text
    assert compared.json()["from_version"] == "v1.0.0"
    assert compared.json()["to_version"] == "v2.0.0"
    assert compared.json()["identical"] is False
    assert _reads(engine, "/versions") == 1


async def test_a_lifecycle_click_reads_the_prompt_once(admin_client, admin, engine):
    """The registry holds nothing a transition changes, so it is not re-read after one."""
    created = await _author(admin_client)
    engine.reset_calls()

    submitted = await admin_client.post(
        f"/api/v1/prompts/{created['id']}/submit-review", json={"note": "Ready"}
    )

    assert submitted.status_code == 200, submitted.text
    assert _head_reads(engine, created["id"]) == 1
    body = submitted.json()
    assert body["previous_status"] == "Draft"
    assert body["prompt"]["status"] == "In Review"
    assert body["prompt"]["status_changed_by"] == admin.email
    assert body["prompt"]["owner"] == admin.email
    assert body["prompt"]["status_changed_at"] is not None

    read_back = await admin_client.get(f"/api/v1/prompts/{created['id']}")
    for field in ("status", "owner", "status_changed_by", "version", "commit"):
        assert read_back.json()[field] == body["prompt"][field]


# ---------------------------------------------------------------------------
# Who cut each commit
# ---------------------------------------------------------------------------


async def test_version_history_names_the_person_who_cut_each_commit(
    admin_client, admin, as_role, engine
):
    """Every row read 'by <the registry's own user>', whoever had committed it."""
    created = await _author(admin_client, version="v1.0.0")
    async with as_role(Role.MEMBER) as member_client:
        committed = await member_client.post(
            f"/api/v1/prompts/{created['id']}/versions",
            json={"template": "You are terse.", "version": "v2.0.0"},
        )
    assert committed.status_code == 201, committed.text
    member_email = "member-0@northwind.test"
    assert committed.json()["version"]["author"] == member_email

    # A review and an approval name the same commit. Neither is authorship.
    await admin_client.post(f"/api/v1/prompts/{created['id']}/submit-review", json={})
    approved = await admin_client.post(f"/api/v1/prompts/{created['id']}/approve", json={})
    assert approved.status_code == 200, approved.text

    history = await admin_client.get(f"/api/v1/prompts/{created['id']}/versions")

    assert history.status_code == 200, history.text
    authors = {row["version"]: row["author"] for row in history.json()["items"]}
    assert authors == {"v2.0.0": member_email, "v1.0.0": admin.email}

    detail = await admin_client.get(
        f"/api/v1/prompts/{created['id']}/versions/{committed.json()['version']['commit']}"
    )
    assert detail.json()["author"] == member_email


async def test_a_restored_commit_belongs_to_whoever_restored_it(admin_client, as_role, engine):
    """A restore copies the old commit's metadata, stamped author and all."""
    created = await _author(admin_client, version="v1.0.0")
    await admin_client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"template": "You are terse.", "version": "v2.0.0"},
    )
    async with as_role(Role.OPERATOR) as operator_client:
        restored = await operator_client.post(
            f"/api/v1/prompts/{created['id']}/restore/v1.0.0"
        )
    assert restored.status_code == 200, restored.text

    history = await admin_client.get(f"/api/v1/prompts/{created['id']}/versions")

    head = next(row for row in history.json()["items"] if row["is_head"])
    assert head["author"] == "operator-0@northwind.test"


async def test_the_first_commit_shows_its_change_note(admin_client):
    """The create call has no change description, so the note lives in the metadata."""
    noted = await _author(admin_client, change_note="First cut for the pilot")
    silent = await _author(admin_client, name="Another prompt")

    first = await admin_client.get(f"/api/v1/prompts/{noted['id']}/versions")
    second = await admin_client.get(f"/api/v1/prompts/{silent['id']}/versions")

    assert first.json()["items"][0]["change_note"] == "First cut for the pilot"
    assert second.json()["items"][0]["change_note"] == "Initial draft"
