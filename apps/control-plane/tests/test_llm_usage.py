"""The LLM Usage screen: runs by model, and the measured leaders among them.

Every test drives ``GET /api/v1/llm-usage`` through the real request path against
the engine double, and checks a figure that could be computed wrongly: the
attribution of a run to a model, a model nobody recorded, the cost of a
successful run, the minimum sample a leader needs, the filters, the scan cap,
the governance records joined by trace id, and the tenant boundary.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import httpx
import pytest

from conftest import APP_BASE_URL, error_code
from fulcrum_ops_api.models.governance import PolicyViolation
from fulcrum_ops_api.services import runs as runs_service

URL = "/api/v1/llm-usage"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def run(
    engine,
    agent,
    *,
    model: str | None = None,
    outcome: str = "ok",
    minutes_ago: float = 10,
    seconds: float = 1.0,
    usage: bool = True,
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.01,
    provider: str | None = None,
    policy: str | None = None,
    handoff: bool = False,
) -> dict[str, Any]:
    """Store one run for ``agent`` the way ingest records one.

    The store reports a cost on every run -- $0 when it has no price for the
    model -- so ``cost=0.0`` with ``usage`` is how an unpriced run looks.
    """
    started = _utcnow() - dt.timedelta(minutes=minutes_ago)
    metadata: dict[str, Any] = {}
    if model is not None:
        metadata["model"] = model
    if provider:
        metadata["provider"] = provider
    if policy:
        metadata["policy"] = policy
    if handoff:
        metadata["errors"] = {"escalated": True}
    if outcome == "running":
        metadata["status"] = "Running"
    fields: dict[str, Any] = {"metadata": metadata, "total_estimated_cost": cost}
    if usage:
        fields["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if outcome == "failed":
        fields["error_info"] = {
            "exception_type": "Timeout",
            "message": "upstream timed out",
            "traceback": "Traceback (most recent call last): ...",
        }
    return engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=started,
        end_time=started + dt.timedelta(seconds=seconds),
        **fields,
    )


def by_model(body: dict[str, Any]) -> dict[str | None, dict[str, Any]]:
    return {row["model"]: row for row in body["models"]}


def leader(body: dict[str, Any], metric: str) -> dict[str, Any]:
    return next(row for row in body["leaders"] if row["metric"] == metric)


async def usage(http: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await http.get(URL, params={"window": "24h", **params})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Aggregation per model
# ---------------------------------------------------------------------------


async def test_runs_are_attributed_to_the_model_they_recorded_else_their_agents(
    admin_client, factory, workspace, engine
):
    claims = await factory.provisioned_agent(workspace, engine, name="Claims Bot", model="gpt-4o")
    payroll = await factory.provisioned_agent(
        workspace, engine, name="Payroll Bot", model="claude-sonnet"
    )
    # Claims Bot: three runs that recorded gpt-4o, one that recorded another
    # model, one that recorded nothing and falls back to its registration.
    run(engine, claims, model="gpt-4o", seconds=1, provider="openai")
    run(engine, claims, model="gpt-4o", seconds=2, provider="openai")
    run(engine, claims, model="gpt-4o", seconds=3, outcome="failed")
    run(engine, claims, model="claude-sonnet", seconds=5)
    run(engine, claims, seconds=4)
    # Payroll Bot: two runs that recorded nothing.
    run(engine, payroll, seconds=5)
    run(engine, payroll, seconds=5, usage=False)

    body = await usage(admin_client)
    models = by_model(body)

    assert list(models) == ["gpt-4o", "claude-sonnet"], "most runs first"
    gpt = models["gpt-4o"]
    assert gpt["runs"] == 4
    assert gpt["runs_model_recorded"] == 3
    assert gpt["runs_model_from_agent"] == 1
    assert gpt["share_percent"] == pytest.approx(57.1)
    assert gpt["providers"] == ["openai"]
    assert (gpt["finished_runs"], gpt["successful_runs"], gpt["failed_runs"]) == (4, 3, 1)
    assert gpt["success_rate"] == 75.0
    assert (gpt["input_tokens"], gpt["output_tokens"], gpt["total_tokens"]) == (400, 200, 600)
    assert gpt["cost_usd"] == pytest.approx(0.04)
    assert gpt["cost_per_run"] == pytest.approx(0.01)
    # Every finished run's cost, the failure's included, over the successes.
    assert gpt["cost_per_successful_run"] == pytest.approx(0.04 / 3, abs=1e-6)
    assert gpt["latency_p50_seconds"] == pytest.approx(2.5)
    assert gpt["latency_p90_seconds"] == pytest.approx(3.7)
    assert gpt["latency_samples"] == 4
    assert [(a["name"], a["runs"]) for a in gpt["agents"]] == [("Claims Bot", 4)]

    claude = models["claude-sonnet"]
    assert claude["runs"] == 3
    assert claude["runs_model_recorded"] == 1 and claude["runs_model_from_agent"] == 2
    assert claude["providers"] == [], "nothing recorded a provider for it"
    # One of its runs recorded no usage: the tokens are the two that did.
    assert claude["token_runs"] == 2 and claude["total_tokens"] == 300
    assert [(a["name"], a["runs"]) for a in claude["agents"]] == [
        ("Payroll Bot", 2),
        ("Claims Bot", 1),
    ]

    totals = body["totals"]
    assert totals["runs"] == 7
    assert totals["models_in_use"] == 2
    assert totals["runs_in_window"] == 7, "the store's own uncapped count"
    assert totals["coverage_percent"] == 100.0
    assert totals["runs_model_from_agent"] == 3
    assert totals["agents_in_scope"] == 2 and totals["agents_with_runs"] == 2
    assert body["scan_capped"] is False and body["scan"]["truncated"] is False

    # Which agents run which model -- and whether that is the one registered.
    rows = {(row["agent_name"], row["model"]): row for row in body["agents"]}
    assert rows[("Claims Bot", "gpt-4o")]["matches_configuration"] is True
    assert rows[("Claims Bot", "claude-sonnet")]["matches_configuration"] is False
    assert rows[("Claims Bot", "gpt-4o")]["share_of_agent_percent"] == 80.0
    assert rows[("Payroll Bot", "claude-sonnet")]["runs"] == 2

    # The series adds up to the table.
    series = {line["model"]: line for line in body["series"]["models"]}
    assert body["series"]["interval"] == "hourly"
    assert sum(series["gpt-4o"]["runs"]) == 4
    assert sum(v for v in series["claude-sonnet"]["tokens"] if v) == 300
    assert not any(bucket["partial"] for bucket in body["series"]["buckets"])


async def test_a_run_with_no_model_anywhere_is_shown_and_never_ranked(
    admin_client, factory, workspace, engine
):
    anonymous = await factory.provisioned_agent(workspace, engine, name="Legacy Bot", model=None)
    named = await factory.provisioned_agent(workspace, engine, name="Named Bot", model="gpt-4o")
    for index in range(25):
        run(engine, anonymous, seconds=0.1, minutes_ago=5 + index)
    for index in range(20):
        run(
            engine, named, seconds=2, minutes_ago=5 + index, outcome="failed" if index % 2 else "ok"
        )

    body = await usage(admin_client)
    models = by_model(body)

    assert list(models) == ["gpt-4o", None], "the unrecorded model sorts last"
    unrecorded = models[None]
    assert unrecorded["label"] == "Model not recorded"
    assert unrecorded["color"] == "gray"
    assert unrecorded["runs"] == 25
    assert unrecorded["runs_model_recorded"] == 0 and unrecorded["runs_model_from_agent"] == 0
    assert body["totals"]["runs_model_not_recorded"] == 25
    assert body["totals"]["models_in_use"] == 1, "no model is not a model in use"

    # It is faster and never fails, and it still leads nothing.
    assert leader(body, "success_rate")["model"] == "gpt-4o"
    assert leader(body, "latency_p50")["model"] == "gpt-4o"
    assert leader(body, "success_rate")["eligible_models"] == 1


async def test_cost_per_successful_run_needs_a_success(admin_client, factory, workspace, engine):
    agent = await factory.provisioned_agent(workspace, engine, name="Flaky Bot", model="gpt-4o")
    run(engine, agent, outcome="failed", cost=0.02)
    run(engine, agent, outcome="failed", cost=0.03)
    run(engine, agent, outcome="running", cost=0.5)

    body = await usage(admin_client)
    row = by_model(body)["gpt-4o"]

    assert row["successful_runs"] == 0
    assert row["cost_per_successful_run"] is None, "nothing succeeded: not measured, not zero"
    assert row["finished_runs"] == 2 and row["running_runs"] == 1
    assert row["success_rate"] == 0.0
    assert row["cost_usd"] == pytest.approx(0.55)


async def test_a_blank_recorded_model_is_attributed_through_the_agent(
    admin_client, factory, workspace, engine
):
    """Whitespace is no model name, here exactly as in the Live Runs Model column."""
    agent = await factory.provisioned_agent(workspace, engine, name="Padded Bot", model="gpt-4o ")
    run(engine, agent, model="   ")
    run(engine, agent, model="gpt-4o")

    body = await usage(admin_client)
    row = by_model(body)["gpt-4o"]

    assert row["runs"] == 2
    assert (row["runs_model_recorded"], row["runs_model_from_agent"]) == (1, 1)
    assert body["totals"]["runs_model_from_agent"] == 1
    # The registration is read the same way: "gpt-4o " is the model it ran.
    [pair] = body["agents"]
    assert pair["configured_model"] == "gpt-4o"
    assert pair["matches_configuration"] is True


# ---------------------------------------------------------------------------
# Best results: measured leaders, minimum sample
# ---------------------------------------------------------------------------


async def test_a_leader_needs_the_minimum_sample(admin_client, factory, workspace, engine):
    busy = await factory.provisioned_agent(workspace, engine, name="Busy Bot", model="big-model")
    quiet = await factory.provisioned_agent(
        workspace, engine, name="Quiet Bot", model="small-model"
    )
    busy_runs = [
        run(
            engine,
            busy,
            seconds=3,
            minutes_ago=5 + index,
            cost=0.05,
            outcome="failed" if index < 2 else "ok",
        )
        for index in range(22)
    ]
    # Perfect, fast and cheap -- on five runs.
    quiet_runs = [
        run(engine, quiet, seconds=0.5, minutes_ago=5 + index, cost=0.001) for index in range(5)
    ]
    for trace in busy_runs[:10]:
        await factory.feedback_item(workspace, rating=4, trace_id=trace["id"], agent_id=busy.id)
    for trace in quiet_runs[:3]:
        await factory.feedback_item(workspace, rating=5, trace_id=trace["id"], agent_id=quiet.id)

    body = await usage(admin_client)

    assert body["leader_min_runs"] == 20 and body["leader_min_ratings"] == 10
    success = leader(body, "success_rate")
    assert success["model"] == "big-model"
    assert success["value"] == 90.9
    assert success["sample"] == 22 and success["sample_unit"] == "finished runs"
    assert success["min_sample"] == 20
    assert success["eligible_models"] == 1, "five runs do not qualify"

    assert leader(body, "latency_p50")["model"] == "big-model"
    cost = leader(body, "cost_per_successful_run")
    assert cost["model"] == "big-model"
    # Twenty successes: exactly the floor. Both failures' spend is in the price.
    assert cost["value"] == pytest.approx(22 * 0.05 / 20, abs=1e-6)
    assert cost["sample"] == 20 and cost["sample_unit"] == "successful runs"

    feedback = leader(body, "feedback_rating")
    assert feedback["model"] == "big-model"
    assert feedback["value"] == 4.0 and feedback["sample"] == 10

    # The small model's own figures are still reported, with their samples.
    small = by_model(body)["small-model"]
    assert small["success_rate"] == 100.0 and small["finished_runs"] == 5
    assert small["feedback_avg_rating"] == 5.0 and small["feedback_ratings"] == 3


async def test_no_leader_when_no_model_has_the_sample(admin_client, factory, workspace, engine):
    agent = await factory.provisioned_agent(workspace, engine, name="New Bot", model="gpt-4o")
    for index in range(3):
        run(engine, agent, minutes_ago=5 + index)

    body = await usage(admin_client)

    for row in body["leaders"]:
        assert row["model"] is None and row["value"] is None
        assert row["eligible_models"] == 0
        assert str(row["min_sample"]) in row["note"]


async def test_equal_leaders_are_reported_as_a_tie(admin_client, factory, workspace, engine):
    one = await factory.provisioned_agent(workspace, engine, name="One", model="model-a")
    two = await factory.provisioned_agent(workspace, engine, name="Two", model="model-b")
    for index in range(20):
        run(engine, one, minutes_ago=5 + index)
    for index in range(21):
        run(engine, two, minutes_ago=5 + index)

    success = leader(await usage(admin_client), "success_rate")

    # Both are 100%; the larger sample is named first and the other is a tie.
    assert success["model"] == "model-b"
    assert success["tied_with"] == ["model-a"]


async def test_a_leader_is_not_claimed_where_the_measure_was_not_taken(
    admin_client, factory, workspace, engine
):
    partly = await factory.provisioned_agent(workspace, engine, name="Partly", model="model-a")
    unpriced = await factory.provisioned_agent(workspace, engine, name="Unpriced", model="model-b")
    # Twenty-two successes, one of which used tokens the store had no price
    # for -- it reports $0 -- so the price of a success is not known.
    for index in range(22):
        run(engine, partly, minutes_ago=5 + index, cost=0.0 if index == 0 else 0.01)
    # Twenty successes at $0 with no usage recorded: indistinguishable from
    # free, so reported as $0 -- and still not ranked as the cheapest.
    for index in range(20):
        run(engine, unpriced, minutes_ago=5 + index, cost=0.0, usage=False)

    body = await usage(admin_client)
    cost = leader(body, "cost_per_successful_run")
    models = by_model(body)

    assert models["model-a"]["unpriced_runs"] == 1
    assert models["model-a"]["cost_per_successful_run"] is None, "a floor is not a price"
    assert models["model-a"]["cost_usd"] == pytest.approx(0.21), "the recorded sum, as Live Runs"
    assert models["model-b"]["unpriced_runs"] == 0
    assert models["model-b"]["cost_per_successful_run"] == 0.0
    assert body["totals"]["unpriced_runs"] == 1
    assert body["totals"]["cost_per_successful_run"] is None

    assert cost["model"] is None and cost["value"] is None and cost["eligible_models"] == 0
    # Both have the sample; saying "no model has 20 successful runs" would be false.
    assert "No model has" not in cost["note"]
    assert "1 model(s) with at least 20 successful runs were not ranked" in cost["note"]
    assert "no price" in cost["note"]
    assert "1 model(s) with a recorded cost of $0" in cost["note"]

    # A priced model with the sample leads, and the note still says who was left out.
    priced = await factory.provisioned_agent(workspace, engine, name="Priced", model="model-c")
    for index in range(20):
        run(engine, priced, minutes_ago=5 + index, cost=0.02)

    cost = leader(await usage(admin_client), "cost_per_successful_run")

    assert cost["model"] == "model-c" and cost["value"] == pytest.approx(0.02)
    assert cost["eligible_models"] == 1
    assert "no price" in cost["note"] and "$0" in cost["note"]


# ---------------------------------------------------------------------------
# Governance records joined by trace id
# ---------------------------------------------------------------------------


async def test_violations_guardrails_and_ratings_are_attributed_by_trace_id(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Claims Bot", model="gpt-4o")
    first = run(engine, agent, model="gpt-4o")
    second = run(engine, agent, model="gpt-4o", policy="Warned")
    other = run(engine, agent, model="claude-sonnet")
    policy = await factory.policy(workspace, name="No PII")
    now = _utcnow()
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                trace_id=first["id"],
                action_taken="Block",
                occurred_at=now,
            ),
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                trace_id=second["id"],
                action_taken="Escalate",
                occurred_at=now,
            ),
            # An offline check: no run to attribute it to.
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                trace_id=None,
                action_taken="Warn",
                occurred_at=now,
            ),
        ]
    )
    guardrail = await factory.guardrail(workspace, name="PII scanner")
    await factory.guardrail_event(
        workspace, guardrail, trace_id=other["id"], agent_id=agent.id, action_taken="Block"
    )
    await factory.guardrail_event(
        workspace, guardrail, trace_id=other["id"], agent_id=agent.id, action_taken="Log"
    )
    await factory.feedback_item(workspace, rating=5, trace_id=first["id"], agent_id=agent.id)
    await factory.feedback_item(workspace, rating=3, trace_id=second["id"], agent_id=agent.id)
    await factory.feedback_item(workspace, rating=None, trace_id=second["id"], agent_id=agent.id)

    body = await usage(admin_client)
    gpt = by_model(body)["gpt-4o"]
    claude = by_model(body)["claude-sonnet"]

    assert gpt["policy_violations"] == 2
    assert gpt["human_escalations"] == 1
    assert gpt["policy_flagged_runs"] == 1, "the run's own verdict, counted apart"
    assert gpt["guardrail_triggers"] == 0
    assert gpt["feedback_avg_rating"] == 4.0 and gpt["feedback_ratings"] == 2
    assert claude["guardrail_triggers"] == 2 and claude["guardrail_blocks"] == 1
    assert claude["feedback_avg_rating"] is None, "nobody rated it: not measured"

    totals = body["totals"]
    assert totals["policy_violations_attributed"] == 2
    assert totals["policy_violations_in_window"] == 3, "the Policy Center's count"
    assert totals["human_escalations_attributed"] == 1
    assert totals["human_escalations_in_window"] == 1
    assert totals["guardrail_triggers_attributed"] == 2
    assert totals["guardrail_triggers_in_window"] == 2
    assert totals["feedback_ratings"] == 2 and totals["feedback_avg_rating"] == 4.0


async def test_the_window_totals_are_the_policy_centers_counts(
    admin_client, factory, workspace, other_workspace, engine
):
    """Unfiltered over 30 days, the violation and escalation totals are the cards'.

    Every record counts -- one naming an agent with no telemetry, one naming no
    agent at all -- because the Policy Center counts them all.
    """
    prod = await factory.provisioned_agent(workspace, engine, name="Prod Bot", model="gpt-4o")
    staging = await factory.provisioned_agent(
        workspace, engine, name="Staging Bot", model="o3", environment="Staging"
    )
    offline = await factory.agent(workspace, name="Offline Bot")
    run(engine, prod)
    run(engine, staging)
    policy = await factory.policy(workspace, name="Refunds")
    now = _utcnow()
    seeds = [
        (prod, "Block", 1),
        (staging, "Require Approval", 2),
        (prod, "Escalate", 3),
        (staging, "Warn", 10),
        (offline, "Escalate", 12),
        (None, "Log Only", 20),
        (prod, "Require Approval", 29),
        # Older than every window the screen offers.
        (prod, "Escalate", 45),
    ]
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id if agent else None,
                trace_id=None,
                action_taken=action,
                occurred_at=now - dt.timedelta(days=days),
            )
            for agent, action, days in seeds
        ]
    )
    their_policy = await factory.policy(other_workspace)
    await factory.add(
        PolicyViolation(
            workspace_id=other_workspace.id,
            policy_id=their_policy.id,
            agent_id=None,
            action_taken="Escalate",
            occurred_at=now,
        )
    )

    center = (await admin_client.get("/api/v1/policies/summary", params={"window_days": 30})).json()
    month = (await admin_client.get(URL, params={"window": "30d"})).json()["totals"]
    counted = (month["policy_violations_in_window"], month["human_escalations_in_window"])

    assert counted == (center["violations_30d"], center["human_escalations_30d"])
    assert counted == (7, 4)

    week = (await admin_client.get(URL, params={"window": "7d"})).json()["totals"]
    assert (week["policy_violations_in_window"], week["human_escalations_in_window"]) == (3, 2)

    # Narrowed, the records naming the selected agents -- and only those.
    staged = (
        await admin_client.get(URL, params={"window": "30d", "environment": "Staging"})
    ).json()["totals"]
    assert (staged["policy_violations_in_window"], staged["human_escalations_in_window"]) == (
        2,
        1,
    )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


async def test_the_agent_and_environment_filters_narrow_everything(
    admin_client, factory, workspace, engine
):
    prod = await factory.provisioned_agent(workspace, engine, name="Prod Bot", model="gpt-4o")
    staging = await factory.provisioned_agent(
        workspace, engine, name="Staging Bot", model="claude-sonnet", environment="Staging"
    )
    third = await factory.provisioned_agent(workspace, engine, name="Third Bot", model="mistral")
    run(engine, prod)
    run(engine, staging)
    run(engine, third)
    policy = await factory.policy(workspace)
    await factory.add(
        PolicyViolation(
            workspace_id=workspace.id,
            policy_id=policy.id,
            agent_id=prod.id,
            trace_id=None,
            action_taken="Block",
            occurred_at=_utcnow(),
        )
    )

    one = await usage(admin_client, agent_id=staging.id)
    assert list(by_model(one)) == ["claude-sonnet"]
    assert one["agent_ids"] == [staging.id]
    assert one["totals"]["agents_in_scope"] == 1
    assert one["totals"]["policy_violations_in_window"] == 0, "the filter scopes the count"

    two = await usage(admin_client, agent_id=[prod.id, third.id])
    assert set(by_model(two)) == {"gpt-4o", "mistral"}
    assert two["totals"]["policy_violations_in_window"] == 1

    staged = await usage(admin_client, environment="Staging")
    assert list(by_model(staged)) == ["claude-sonnet"]
    assert staged["environment"] == "Staging"

    everything = await usage(admin_client)
    assert everything["totals"]["runs"] == 3


async def test_the_window_bounds_the_runs(admin_client, factory, workspace, engine):
    agent = await factory.provisioned_agent(workspace, engine, name="Old Bot", model="gpt-4o")
    run(engine, agent, minutes_ago=30)
    run(engine, agent, minutes_ago=3 * 24 * 60)

    assert (await usage(admin_client))["totals"]["runs"] == 1
    week = (await admin_client.get(URL, params={"window": "7d"})).json()
    assert week["totals"]["runs"] == 2
    assert week["series"]["interval"] == "daily"

    refused = await admin_client.get(URL, params={"window": "90d"})
    assert refused.status_code == 422


# ---------------------------------------------------------------------------
# The scan cap, and what the store is asked
# ---------------------------------------------------------------------------


async def test_a_capped_scan_says_so(admin_client, factory, workspace, engine, monkeypatch):
    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot", model="gpt-4o")
    for index in range(60):
        run(engine, agent, minutes_ago=2 + index)
    # The smallest budget the scan honours: fifty rows for the one project.
    monkeypatch.setattr(runs_service, "MAX_SCAN_TRACES", 10)
    engine.reset_calls()

    body = await usage(admin_client)

    assert body["scan_capped"] is True and body["scan"]["truncated"] is True
    assert body["totals"]["runs"] == runs_service.MIN_ROWS_PER_AGENT
    assert body["totals"]["runs_in_window"] == 60
    assert body["totals"]["coverage_percent"] == pytest.approx(83.3)
    assert body["scan"]["covered_from"] is not None
    partial = [bucket["partial"] for bucket in body["series"]["buckets"]]
    assert partial[0] is True, "a bucket before the scan's edge is not a measurement"
    assert partial == sorted(partial, reverse=True), "partial buckets are a prefix"

    # One cursor read for the project, and never a read per run.
    assert len(engine.calls_to("/traces/search")) == 1
    per_run = [call for call in engine.calls if re.search(r"/traces/[0-9a-f-]{36}$", call.path)]
    assert per_run == []
    assert engine.calls_to("/spans") == []


async def test_a_capped_scan_compares_models_over_the_same_stretch(
    admin_client, factory, workspace, engine, monkeypatch
):
    """A busy agent's newest hour is never set against a quiet agent's week."""
    busy = await factory.provisioned_agent(workspace, engine, name="Busy Bot", model="busy-model")
    quiet = await factory.provisioned_agent(
        workspace, engine, name="Quiet Bot", model="quiet-model"
    )
    for index in range(60):
        run(engine, busy, minutes_ago=1 + index)
    # Ten runs over four days; one of them inside the busy agent's last hour.
    for index in range(10):
        run(engine, quiet, minutes_ago=30 + index * 600)
    # Fifty rows a project: the busy one is capped, the quiet one read whole.
    monkeypatch.setattr(runs_service, "MAX_SCAN_TRACES", 10)

    body = await usage(admin_client, window="7d")

    assert body["scan_capped"] is True
    assert body["scan"]["runs_scanned"] == 60, "everything read..."
    measured_from = dt.datetime.fromisoformat(body["measured_from"].replace("Z", "+00:00"))
    assert measured_from == dt.datetime.fromisoformat(
        body["scan"]["covered_from"].replace("Z", "+00:00")
    )
    models = by_model(body)
    # ...but only the stretch read whole for both is compared: the busy
    # agent's newest fifty, and the one quiet run inside the same stretch --
    # not the quiet agent's whole week, which made it look like 17% of traffic.
    assert models["busy-model"]["runs"] == 50
    assert models["quiet-model"]["runs"] == 1
    assert body["totals"]["runs"] == 51
    assert body["totals"]["runs_in_window"] == 70
    assert body["totals"]["coverage_percent"] == pytest.approx(72.9)
    assert sum(sum(line["runs"]) for line in body["series"]["models"]) == 51

    # Read whole, the window has no edge and every run counts.
    monkeypatch.setattr(runs_service, "MAX_SCAN_TRACES", 2_000)
    whole = await usage(admin_client, window="7d")
    assert whole["measured_from"] is None and whole["scan_capped"] is False
    assert whole["totals"]["runs"] == 70


async def test_requests_share_one_read_and_join_governance_afresh(
    admin_client, factory, workspace, engine, monkeypatch
):
    """The telemetry read is shared; the records joined to it are this request's."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "runs_summary_cache_seconds", 60.0)
    agent = await factory.provisioned_agent(workspace, engine, name="Claims Bot", model="gpt-4o")
    first = run(engine, agent)
    engine.reset_calls()

    before = await usage(admin_client)
    searches = len(engine.calls_to("/traces/search"))
    policy = await factory.policy(workspace)
    await factory.add(
        PolicyViolation(
            workspace_id=workspace.id,
            policy_id=policy.id,
            agent_id=agent.id,
            trace_id=first["id"],
            action_taken="Block",
            occurred_at=_utcnow(),
        )
    )
    after = await usage(admin_client)

    assert searches == 1
    assert len(engine.calls_to("/traces/search")) == 1, "the second request read nothing"
    assert before["totals"]["policy_violations_attributed"] == 0
    assert after["totals"]["policy_violations_attributed"] == 1
    assert by_model(after)["gpt-4o"]["policy_violations"] == 1


async def test_the_store_being_down_is_a_503_not_a_zero(admin_client, factory, workspace, engine):
    agent = await factory.provisioned_agent(workspace, engine, name="Any Bot")
    run(engine, agent)
    engine.fail(503)

    response = await admin_client.get(URL)

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"
    assert set(response.json()) == {"error"}


async def test_the_store_count_failing_costs_only_the_coverage(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Any Bot", model="gpt-4o")
    run(engine, agent)
    engine.fail_path("/projects/stats")

    body = await usage(admin_client)

    assert body["totals"]["runs"] == 1
    assert body["totals"]["runs_in_window"] is None, "not measured, not zero"
    assert body["totals"]["coverage_percent"] is None


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_another_workspaces_runs_never_appear(
    admin_client, as_role, factory, workspace, other_workspace, engine
):
    from fulcrum_ops_api.models.identity import Role

    mine = await factory.provisioned_agent(workspace, engine, name="Mine", model="gpt-4o")
    theirs = await factory.provisioned_agent(
        other_workspace, engine, name="Theirs", model="secret-model"
    )
    my_run = run(engine, mine)
    run(engine, theirs)
    their_policy = await factory.policy(other_workspace)
    # Their record names my run: it is still theirs, and never counted here.
    await factory.add(
        PolicyViolation(
            workspace_id=other_workspace.id,
            policy_id=their_policy.id,
            agent_id=theirs.id,
            trace_id=my_run["id"],
            action_taken="Block",
            occurred_at=_utcnow(),
        )
    )

    body = await usage(admin_client)
    assert list(by_model(body)) == ["gpt-4o"]
    assert "secret-model" not in str(body) and "Theirs" not in str(body)
    assert body["totals"]["policy_violations_attributed"] == 0
    assert body["totals"]["policy_violations_in_window"] == 0

    # Naming their agent reads nothing at all.
    asked = await usage(admin_client, agent_id=theirs.id)
    assert asked["models"] == [] and asked["totals"]["runs"] == 0
    assert asked["totals"]["agents_in_scope"] == 0

    async with as_role(Role.ADMIN, other_workspace) as http:
        seen = await usage(http)
    assert list(by_model(seen)) == ["secret-model"]
    assert seen["totals"]["policy_violations_in_window"] == 1


async def test_a_key_bound_to_one_agent_reads_only_that_agent(app, factory, workspace, engine):
    mine = await factory.provisioned_agent(workspace, engine, name="Claims Bot", model="gpt-4o")
    theirs = await factory.provisioned_agent(workspace, engine, name="Payroll Bot", model="o3")
    run(engine, mine)
    run(engine, theirs)
    token, _row = await factory.api_key(
        workspace, name="Claims runtime", scopes=["ingest", "read"], agent_id=mine.id
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        own = await usage(http)
        neighbour = await usage(http, agent_id=theirs.id)

    assert list(by_model(own)) == ["gpt-4o"]
    assert neighbour["models"] == [] and neighbour["totals"]["agents_in_scope"] == 0


# ---------------------------------------------------------------------------
# One success rate, whichever screen reads it
# ---------------------------------------------------------------------------


async def test_live_runs_and_llm_usage_read_one_success_rate(
    admin_client, factory, workspace, engine
):
    """Live Runs used to divide by every run, so a run still in flight counted
    as a success; LLM Usage divides by finished runs. Over the same runs the two
    screens read 83.3% and 75% for one window. Now both are successful runs over
    finished runs."""
    agent = await factory.provisioned_agent(workspace, engine, name="Mixed Bot", model="gpt-4o")
    for _ in range(3):
        run(engine, agent)
    run(engine, agent, outcome="failed")
    for _ in range(2):
        run(engine, agent, outcome="running")

    llm = await usage(admin_client)
    live = await admin_client.get("/api/v1/runs/summary", params={"time_range": "Last 24 hours"})
    assert live.status_code == 200, live.text

    assert llm["totals"]["success_rate"] == 75.0
    assert live.json()["success_rate"] == 75.0, "3 of 4 finished; the 2 in flight are neither"
    assert live.json()["total_runs"] == 6, "runs in flight still count as runs"


async def test_a_window_of_runs_all_in_flight_has_no_success_rate(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot", model="gpt-4o")
    run(engine, agent, outcome="running")

    live = await admin_client.get("/api/v1/runs/summary", params={"time_range": "Last 24 hours"})

    assert live.json()["success_rate"] is None, "nothing has finished: not measured, not 100%"
