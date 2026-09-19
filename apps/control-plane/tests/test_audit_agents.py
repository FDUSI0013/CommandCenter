"""Regressions from the September audit of the Agent Registry and Agent Detail.

Each test pins one thing the detail page got wrong in production: counters read
from an endpoint that does not carry them, a page that could not be opened at
all while the telemetry store was slow, a page view that counted as an edit, and
governance figures nothing ever computed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest
from sqlalchemy import delete, update

from engine_double import EngineDouble
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.governance import PolicyViolation
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.registry import Agent, AgentConnector
from fulcrum_ops_api.services import agents as agents_service


class UnhurriedEngine(EngineDouble):
    """The double, able to take its time over the paths a test names.

    The real store's slowness is the whole subject of half this file, and the
    double answers in microseconds. Nothing about its answers changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.slow_paths: dict[str, float] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            for fragment, seconds in self.slow_paths.items():
                if fragment in scope["path"]:
                    await asyncio.sleep(seconds)
        await super().__call__(scope, receive, send)


@pytest.fixture
def engine() -> UnhurriedEngine:
    return UnhurriedEngine()


def _at(minutes_ago: int) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)


def _run(engine, agent, *, tokens: int, seconds: float, minutes_ago: int = 10, **fields):
    """One finished run carrying token usage, as an instrumented agent reports it."""
    started = _at(minutes_ago)
    return engine.add_trace(
        project_name=agent.engine_project_name,
        name="answer customer question",
        start_time=started,
        end_time=started + dt.timedelta(seconds=seconds),
        usage={
            "prompt_tokens": tokens // 2,
            "completion_tokens": tokens - tokens // 2,
            "total_tokens": tokens,
        },
        total_estimated_cost=0.01,
        **fields,
    )


# ---------------------------------------------------------------------------
# Tokens and latency are read from the endpoint that reports them
# ---------------------------------------------------------------------------


async def test_the_detail_page_reports_the_tokens_and_latency_the_agent_used(
    admin_client, factory, workspace, engine
):
    """Tokens read 0 and latency "—" for every agent: the wrong endpoint was asked.

    Two runs, because the project-stats row carries usage as a per-trace
    *average*: read as a total it is exactly right for one run and silently
    wrong for two.
    """
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    _run(engine, agent, tokens=800, seconds=4.0, error_info={"message": "tool timed out"})

    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.status_code == 200, response.text
    stats = response.json()["stats"]
    assert stats["run_count"] == 2
    assert stats["error_count"] == 1
    assert stats["success_rate"] == 50.0
    assert stats["total_tokens"] == 2000
    assert stats["total_cost"] == 0.02
    assert stats["p50_duration_seconds"] is not None

    metrics = response.json()["agent"]["metrics"]
    assert metrics["runs_30d"] == 2
    assert metrics["tokens_30d"] == 2000
    assert metrics["p50_latency_seconds"] == stats["p50_duration_seconds"]
    assert metrics["avg_latency_seconds"] is not None, "the latency card was always a dash"

    # The registry row renders from the cache that read just refreshed.
    row = (await admin_client.get("/api/v1/agents")).json()["items"][0]
    assert row["metrics"]["tokens_30d"] == 2000


async def test_an_agent_is_never_shown_a_neighbours_numbers(
    admin_client, factory, workspace, engine
):
    """The old read selected projects by name prefix and took whichever came back."""
    quiet = await factory.provisioned_agent(
        workspace, engine, name="Support", slug="support"
    )
    busy = await factory.provisioned_agent(
        workspace, engine, name="Support Escalations", slug="support-escalations"
    )
    _run(engine, busy, tokens=5000, seconds=1.0)

    response = await admin_client.get(f"/api/v1/agents/{quiet.id}")

    assert response.status_code == 200, response.text
    assert response.json()["stats"]["run_count"] == 0
    assert response.json()["stats"]["total_tokens"] == 0
    assert engine.calls_to("/projects/stats") == [], "the name-matched listing is not asked"


# ---------------------------------------------------------------------------
# The page opens whatever the telemetry store is doing
# ---------------------------------------------------------------------------


def _commit_prompt(engine, agent, *templates: str) -> dict:
    """The agent's system prompt with some history, filed under its project."""
    prompt = engine._new_prompt(
        {
            "name": f"{agent.engine_project_name}-system-prompt",
            "project_id": agent.engine_project_id,
        }
    )
    for template in templates:
        engine._append_version(prompt, {"template": template})
    return prompt


async def test_the_detail_page_opens_while_the_telemetry_store_is_down(
    admin_client, factory, workspace, engine
):
    """An outage is when an operator most needs the Deactivate button.

    Everything but the counters and the prompt history is our own data. The
    page says what it could not read and reports no figure in its place; the
    header keeps the last numbers that *were* measured, with their date.
    """
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    healthy = await admin_client.get(f"/api/v1/agents/{agent.id}")
    assert healthy.json()["telemetry_error"] is None

    engine.fail(503)
    down = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert down.status_code == 200, down.text
    body = down.json()
    assert body["agent"]["name"] == "Support Bot"
    assert body["telemetry_available"] is True, "it has a project; the store is what failed"
    assert body["telemetry_error"]
    assert "http" not in body["telemetry_error"].lower()
    assert body["stats"] is None, "not measured is null, never a row of zeros"
    assert body["versions"] == []
    assert body["agent"]["metrics"]["runs_30d"] == 1
    assert body["agent"]["metrics"]["computed_at"] is not None

    stopped = await admin_client.post(
        f"/api/v1/agents/{agent.id}/deactivate", json={"reason": "Store outage"}
    )
    assert stopped.status_code == 200, stopped.text

    engine.recover()
    back = await admin_client.get(f"/api/v1/agents/{agent.id}")
    assert back.json()["telemetry_error"] is None
    assert back.json()["stats"]["run_count"] == 1


async def test_an_agent_never_opened_before_an_outage_shows_no_run_figure_at_all(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)

    down = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert down.status_code == 200, down.text
    assert down.json()["stats"] is None
    metrics = down.json()["agent"]["metrics"]
    for unmeasured in ("runs_30d", "success_rate_30d", "avg_latency_seconds", "tokens_30d",
                       "cost_30d", "eval_score", "computed_at"):
        assert metrics[unmeasured] is None, f"{unmeasured} was never measured"
    assert metrics["violations_30d"] == 0, "counted from our own rows, which are not down"


async def test_one_failing_read_costs_the_page_one_panel(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    _commit_prompt(engine, agent, "You are a support agent.")

    engine.fail_path("/prompts", 503)
    without_history = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert without_history["stats"]["run_count"] == 1
    assert without_history["versions"] == []
    assert without_history["telemetry_error"]

    engine.recover()
    engine.fail_path("/traces/stats", 503)
    without_counters = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert without_counters["stats"] is None
    assert len(without_counters["versions"]) == 1
    assert without_counters["telemetry_error"]


async def test_a_slow_store_does_not_hold_the_page(
    admin_client, factory, workspace, engine, monkeypatch
):
    """p95 was 30 s -- the browser's own timeout -- because nothing bounded the wait."""
    monkeypatch.setattr(settings, "agent_detail_read_timeout_seconds", 0.2)
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _commit_prompt(engine, agent, "You are a support agent.")
    engine.slow_paths["/traces/stats"] = 1.5

    started = time.monotonic()
    response = await admin_client.get(f"/api/v1/agents/{agent.id}")
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert elapsed < 1.2, f"the page waited {elapsed:.1f}s on a read it then did without"
    body = response.json()
    assert body["stats"] is None
    assert "in time" in body["telemetry_error"]
    assert len(body["versions"]) == 1, "the read that did answer is still served"

    # Let the abandoned exchange finish before the adapter is closed under it.
    await asyncio.sleep(1.5)


async def test_a_detail_read_asks_the_store_two_questions(
    admin_client, factory, workspace, engine
):
    """It asked four, in part one after another, and drew one of the answers nowhere."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    prompt = _commit_prompt(engine, agent, "You are a support agent.", "Be brief.")

    first = await admin_client.get(f"/api/v1/agents/{agent.id}")
    assert len(first.json()["versions"]) == 2
    assert "latency_series" not in first.json(), "nothing ever rendered it"
    assert engine.calls_to("/metrics") == []

    # The prompt's id does not change, so finding it by name happens once.
    engine.reset_calls()
    second = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert len(second.json()["versions"]) == 2
    asked = sorted(call.path for call in engine.calls)
    assert len(asked) == 2, asked
    assert asked[0].endswith(f"/prompts/{prompt['id']}/versions")
    assert asked[1].endswith("/traces/stats")


async def test_a_remembered_prompt_that_was_deleted_is_found_again(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    old = _commit_prompt(engine, agent, "You are a support agent.")
    await admin_client.get(f"/api/v1/agents/{agent.id}")

    del engine.prompts[old["id"]]
    _commit_prompt(engine, agent, "You are a support agent.", "Be brief.", "Be kind.")
    response = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert response.json()["telemetry_error"] is None
    assert len(response.json()["versions"]) == 3


async def test_the_registry_inspector_can_leave_the_history_out(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    _commit_prompt(engine, agent, "You are a support agent.")

    response = await admin_client.get(
        f"/api/v1/agents/{agent.id}", params={"include_versions": "false"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["versions"] == []
    assert response.json()["agent"]["metrics"]["runs_30d"] == 1, "the cache is still kept warm"
    assert engine.calls_to("/prompts") == []


async def test_the_counters_are_shared_between_opens_for_a_few_seconds(
    admin_client, factory, workspace, engine, monkeypatch
):
    """The registry opens the first row on every visit; reloads follow every action."""
    monkeypatch.setattr(settings, "agent_stats_cache_seconds", 30.0)
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    _run(engine, agent, tokens=1200, seconds=2.0)
    try:
        for _ in range(3):
            response = await admin_client.get(f"/api/v1/agents/{agent.id}")
            assert response.json()["stats"]["run_count"] == 1
        assert len(engine.calls_to("/traces/stats")) == 1

        # A failure is never what gets remembered...
        agents_service.forget_stats(agent.id)
        engine.fail_path("/traces/stats", 503)
        assert (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["stats"] is None
        engine.recover()
        assert (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["stats"] is not None

        # ...and a run started from this page is not hidden behind the memory.
        ran = await admin_client.post(f"/api/v1/agents/{agent.id}/run", json={"input": "hello"})
        assert ran.status_code == 202, ran.text
        after = await admin_client.get(f"/api/v1/agents/{agent.id}")
        assert after.json()["stats"]["run_count"] == 2
    finally:
        agents_service.forget_stats()


# ---------------------------------------------------------------------------
# Observing an agent is not editing it
# ---------------------------------------------------------------------------


async def _backdate(db, agent) -> None:
    """Age the row, so a moved ``updated_at`` is unmistakable without sleeping.

    The guard allows a second of slack for the JSON round trip; a row created a
    moment ago could be "edited" by the system inside that second and pass.
    """
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    await db.execute(update(Agent).where(Agent.id == agent.id).values(updated_at=long_ago))


async def _edit_with_the_token_from(admin_client, agent_id: str, token: str):
    return await admin_client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"team": "Customer Care", "expected_updated_at": token},
    )


async def test_opening_the_detail_page_does_not_make_the_next_edit_a_conflict(
    admin_client, db, factory, workspace, engine
):
    """The page refreshes the row's metrics cache; that write moved the edit token."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await _backdate(db, agent)
    opened = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()
    token = opened["agent"]["updated_at"]

    # The agent keeps working while the modal is open, so the next read of the
    # page -- the inspector, another tab, a colleague -- has new numbers to cache.
    _run(engine, agent, tokens=1200, seconds=2.0)
    reopened = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert reopened["agent"]["metrics"]["runs_30d"] == 1, "the cache was refreshed"
    assert reopened["agent"]["updated_at"] == token, "and nobody edited the agent"

    saved = await _edit_with_the_token_from(admin_client, agent.id, token)

    assert saved.status_code == 200, saved.text
    assert saved.json()["team"] == "Customer Care"
    stored = await db.get(Agent, agent.id)
    assert stored.metrics_cache["runs_30d"] == 1, "the refresh reached the database"


async def test_an_agent_that_is_busy_reporting_can_still_be_edited(
    admin_client, ingest_client, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await _backdate(db, agent)
    token = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["agent"]["updated_at"]

    reported = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [{"name": "answer customer question", "start_time": _at(0).isoformat()}],
        },
    )
    assert reported.status_code in (200, 202), reported.text

    saved = await _edit_with_the_token_from(admin_client, agent.id, token)

    assert saved.status_code == 200, saved.text
    stored = await db.get(Agent, agent.id)
    assert stored.last_used_at > _at(1), "the batch was still noted on the row"


async def test_running_an_agent_is_not_an_edit_of_it(
    admin_client, as_role, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot", updated_by="ada@northwind.test"
    )
    await _backdate(db, agent)
    token = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["agent"]["updated_at"]

    async with as_role(Role.OPERATOR) as operator:
        ran = await operator.post(f"/api/v1/agents/{agent.id}/run", json={"input": "hello"})
    assert ran.status_code == 202, ran.text

    stored = await db.get(Agent, agent.id)
    assert stored.updated_by == "ada@northwind.test", "Last Modified names the last editor"
    assert stored.last_used_at > _at(1)
    saved = await _edit_with_the_token_from(admin_client, agent.id, token)
    assert saved.status_code == 200, saved.text


async def test_a_real_edit_by_someone_else_is_still_a_conflict(
    admin_client, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await _backdate(db, agent)
    token = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["agent"]["updated_at"]

    theirs = await admin_client.patch(f"/api/v1/agents/{agent.id}", json={"model": "gpt-4o-mini"})
    assert theirs.status_code == 200, theirs.text
    mine = await _edit_with_the_token_from(admin_client, agent.id, token)

    assert mine.status_code == 409, mine.text


# ---------------------------------------------------------------------------
# Violations, escalations and health are computed, not left at their defaults
# ---------------------------------------------------------------------------


async def _violation(factory, workspace, policy, agent, action: str, *, days_ago: int = 1):
    return await factory.add(
        PolicyViolation(
            workspace_id=workspace.id,
            policy_id=policy.id,
            agent_id=agent.id,
            action_taken=action,
            occurred_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
    )


async def test_the_agents_page_counts_the_violations_recorded_against_it(
    admin_client, db, factory, workspace, engine
):
    """The Policy Center showed them; the agent's own page said 0, for every agent."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    bystander = await factory.provisioned_agent(workspace, engine, name="Billing Bot")
    policy = await factory.policy(workspace, name="No card numbers in output")
    await _violation(factory, workspace, policy, agent, "Block")
    await _violation(factory, workspace, policy, agent, "Escalate")
    await _violation(factory, workspace, policy, agent, "Require Approval")
    await _violation(factory, workspace, policy, agent, "Block", days_ago=45)
    await _violation(factory, workspace, policy, bystander, "Block")

    metrics = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["agent"]["metrics"]

    assert metrics["violations_30d"] == 3, "this agent's, inside the window"
    assert metrics["escalations_30d"] == 2
    row = next(
        item
        for item in (await admin_client.get("/api/v1/agents")).json()["items"]
        if item["id"] == agent.id
    )
    assert row["metrics"]["violations_30d"] == 3, "the registry row's cache carries them too"

    # And a count can come back down: the old carry-forward read 0 as "missing".
    await db.execute(delete(PolicyViolation).where(PolicyViolation.agent_id == agent.id))
    cleared = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()["agent"]["metrics"]
    assert cleared["violations_30d"] == 0
    assert cleared["escalations_30d"] == 0


async def test_violations_are_reported_without_any_telemetry(
    admin_client, factory, workspace, engine
):
    """They are our rows. An unprovisioned agent, or a dead store, still has them."""
    agent = await factory.agent(workspace, name="Unprovisioned Bot")
    policy = await factory.policy(workspace, name="No card numbers in output")
    await _violation(factory, workspace, policy, agent, "Warn")

    body = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()

    assert body["telemetry_available"] is False
    assert body["agent"]["metrics"]["violations_30d"] == 1
    assert body["agent"]["metrics"]["runs_30d"] is None, "no run figure is made up beside it"


async def test_policy_status_stops_reading_allowed_for_every_agent(
    admin_client, factory, workspace, engine
):
    """Nothing ever moved the column, so the column and its filter were decoration.

    What is derived is Warned and only Warned. Blocked and Approval Required are
    gates -- they refuse activation and Run Agent -- and a gate is never shown
    that is not in force.
    """
    await factory.provisioned_agent(workspace, engine, name="Clean Bot")
    logged =await factory.provisioned_agent(workspace, engine, name="Logged Bot")
    tripped = await factory.provisioned_agent(workspace, engine, name="Tripped Bot")
    gated = await factory.provisioned_agent(
        workspace, engine, name="Gated Bot", policy_status="Blocked"
    )
    policy = await factory.policy(workspace, name="No card numbers in output")
    await _violation(factory, workspace, policy, logged, "Log Only")
    await _violation(factory, workspace, policy, tripped, "Warn")
    await _violation(factory, workspace, policy, tripped, "Block")
    await _violation(factory, workspace, policy, tripped, "Mask", days_ago=45)
    await _violation(factory, workspace, policy, gated, "Warn")

    rows = {
        item["name"]: item for item in (await admin_client.get("/api/v1/agents")).json()["items"]
    }
    assert rows["Clean Bot"]["policy_status"] == "Allowed"
    assert rows["Logged Bot"]["policy_status"] == "Allowed", "a logged match is not a warning"
    assert rows["Tripped Bot"]["policy_status"] == "Warned"
    assert rows["Tripped Bot"]["strongest_enforcement_30d"] == "Block"
    assert rows["Clean Bot"]["strongest_enforcement_30d"] is None
    assert rows["Gated Bot"]["policy_status"] == "Blocked", "a stored gate is never softened"

    async def named(policy_status: str) -> list[str]:
        page = await admin_client.get("/api/v1/agents", params={"policy_status": policy_status})
        return sorted(item["name"] for item in page.json()["items"])

    assert await named("Warned") == ["Tripped Bot"]
    assert await named("Allowed") == ["Clean Bot", "Logged Bot"]
    assert await named("Blocked") == ["Gated Bot"]

    # Derived for display: the gate itself is untouched, so the agent still runs
    # and its exported manifest still states the verdict that is in force.
    detail = (await admin_client.get(f"/api/v1/agents/{tripped.id}")).json()
    assert detail["agent"]["policy_status"] == "Warned"
    assert detail["configuration"]["policy_status"] == "Allowed"
    ran = await admin_client.post(f"/api/v1/agents/{tripped.id}/run", json={})
    assert ran.status_code == 202, ran.text


async def test_an_agent_that_has_run_gets_a_health_score(
    admin_client, db, factory, workspace, engine
):
    """Nothing ever wrote ``health``, so the card said "No health score yet" for ever."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await _backdate(db, agent)
    idle = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()
    assert idle["agent"]["health"] is None, "no runs, no success rate, no score -- not zero"

    for _ in range(3):
        _run(engine, agent, tokens=100, seconds=1.0)
    _run(engine, agent, tokens=100, seconds=1.0, error_info={"message": "tool timed out"})
    policy = await factory.policy(workspace, name="No card numbers in output")
    await _violation(factory, workspace, policy, agent, "Warn")
    await _violation(factory, workspace, policy, agent, "Block")
    blocked = await factory.connector(workspace, name="Payments API", status="Blocked")
    await factory.add(
        AgentConnector(workspace_id=workspace.id, agent_id=agent.id, connector_id=blocked.id)
    )

    body = (await admin_client.get(f"/api/v1/agents/{agent.id}")).json()

    # 75% success, less 5 x 2 violations, less 10 x 1 blocked grant.
    assert body["agent"]["health"] == 55
    assert (await db.get(Agent, agent.id)).health == 55, "the registry's column sorts on it"
    assert body["agent"]["updated_at"] == idle["agent"]["updated_at"], "scoring is not an edit"
