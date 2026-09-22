"""Live Runs counts policy violations and human escalations as the Policy Center does.

The KPI row's Policy Violations card counted *runs* whose verdict was not
Allowed, reading the verdict off the run's metadata or its guardrail results.
That mixed failed guardrail checks in with enforcement decisions, could not see
a run the enforcement path stopped before it reported, and disagreed with the
Policy Center, which counts the violation records. Human Escalations counted
the runs an agent flagged as escalated -- a third definition beside Agent
Detail's.

Both are now counts of the violation records, for the selected window and the
agents in view. The agent-reported flag survives as its own figure, Agent
Hand-offs. Every test here seeds records the count must include and records it
must not -- another window, another workspace, another agent -- and asserts the
exact figure.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import httpx
import pytest

from conftest import APP_BASE_URL
from fulcrum_ops_api.models.governance import PolicyViolation


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ago(minutes: float) -> dt.datetime:
    return _utcnow() - dt.timedelta(minutes=minutes)


async def _violation(factory, workspace, policy, agent_id, action: str, at: dt.datetime):
    return await factory.add(
        PolicyViolation(
            workspace_id=workspace.id,
            policy_id=policy.id,
            agent_id=agent_id,
            action_taken=action,
            occurred_at=at,
        )
    )


async def _summary(http, **params) -> dict:
    response = await http.get("/api/v1/runs/summary", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def seeded(factory, workspace, other_workspace, engine):
    """Two agents in view, one workspace next door, and records on every side of
    every line the count draws. Times are minutes before now; the window is
    "Last hour", so the previous window is 60-120 minutes ago."""
    claims = await factory.provisioned_agent(workspace, engine, name="Claims Bot")
    payroll = await factory.provisioned_agent(workspace, engine, name="Payroll Bot")
    neighbour = await factory.provisioned_agent(other_workspace, engine, name="Contoso Bot")
    policy = await factory.policy(workspace, name="No PII in answers")
    theirs = await factory.policy(other_workspace, name="Contoso policy")

    rows = [
        # This hour: six records, three of which put a person in the loop.
        (workspace, policy, claims.id, "Block", 10),
        (workspace, policy, claims.id, "Escalate", 20),
        (workspace, policy, claims.id, "Require Approval", 30),
        (workspace, policy, payroll.id, "Warn", 15),
        (workspace, policy, payroll.id, "Escalate", 40),
        # A record with no agent -- an offline check. The Policy Center counts it.
        (workspace, policy, None, "Log Only", 5),
        # The hour before: two records, one escalation.
        (workspace, policy, claims.id, "Block", 70),
        (workspace, policy, payroll.id, "Require Approval", 90),
        # Outside both windows.
        (workspace, policy, claims.id, "Escalate", 180),
        (workspace, policy, payroll.id, "Block", 60 * 24 * 3),
        # Another workspace -- including a record that names our agent's id.
        (other_workspace, theirs, neighbour.id, "Escalate", 10),
        (other_workspace, theirs, claims.id, "Require Approval", 12),
        (other_workspace, theirs, neighbour.id, "Block", 75),
    ]
    for owner, owning_policy, agent_id, action, minutes in rows:
        await _violation(factory, owner, owning_policy, agent_id, action, _ago(minutes))
    return claims, payroll


# ---------------------------------------------------------------------------
# The counts
# ---------------------------------------------------------------------------


async def test_the_workspace_view_counts_every_record_in_the_window(
    admin_client, seeded, engine
):
    summary = await _summary(admin_client, time_range="Last hour")

    assert summary["policy_violations"] == 6
    assert summary["policy_violations_previous"] == 2
    assert summary["policy_violations_delta_percent"] == 200.0

    escalations = summary["human_escalations"]
    assert escalations["label"] == "Human Escalations"
    assert escalations["value"] == 3.0
    assert escalations["unit"] == "violations"
    assert summary["human_escalations_previous"] == 1
    assert summary["human_escalations_delta_percent"] == 200.0
    # The sparkline is the same count, bucketed: nothing is lost or added.
    assert len(escalations["series"]) == 24
    assert sum(point["value"] for point in escalations["series"]) == 3.0

    scope = summary["violations_scope"]
    assert scope["agent_id"] is None
    assert set(scope["escalating_actions"]) == {"Escalate", "Require Approval"}
    assert scope["filters_not_applied"] == []


async def test_a_view_of_one_agent_counts_that_agents_records_only(
    admin_client, seeded
):
    claims, payroll = seeded

    mine = await _summary(admin_client, time_range="Last hour", agent_id=claims.id)
    theirs = await _summary(admin_client, time_range="Last hour", agent_id=payroll.id)

    # Claims Bot: Block, Escalate, Require Approval this hour; Block before.
    # The other workspace's record naming the same agent id is not ours.
    assert (mine["policy_violations"], mine["policy_violations_previous"]) == (3, 1)
    assert mine["human_escalations"]["value"] == 2.0
    assert mine["human_escalations_previous"] == 0
    assert mine["human_escalations_delta_percent"] is None, "nothing before to compare with"
    assert mine["violations_scope"]["agent_id"] == claims.id

    assert (theirs["policy_violations"], theirs["policy_violations_previous"]) == (2, 1)
    assert theirs["human_escalations"]["value"] == 1.0
    assert theirs["human_escalations_previous"] == 1


async def test_a_wider_window_takes_in_the_records_of_the_hours_before(
    admin_client, seeded
):
    summary = await _summary(admin_client, time_range="Last 24 hours")

    # Everything of ours inside 24 hours: the six, the two, and the one from
    # three hours ago. The record from three days ago is outside.
    assert summary["policy_violations"] == 9
    assert summary["human_escalations"]["value"] == 5.0
    assert summary["policy_violations_previous"] == 0
    assert summary["policy_violations_delta_percent"] is None


async def test_live_runs_and_the_policy_center_give_the_same_numbers(
    admin_client, seeded
):
    """The complaint that started this: the two screens did not match."""
    live = await _summary(admin_client, time_range="Last 24 hours")

    feed = await admin_client.get(
        "/api/v1/policies/violations", params={"window_days": 1, "page_size": 100}
    )
    escalated = await admin_client.get(
        "/api/v1/policies/violations",
        params=[
            ("window_days", 1),
            ("page_size", 100),
            ("action", "Escalate"),
            ("action", "Require Approval"),
        ],
    )

    assert feed.status_code == 200, feed.text
    assert escalated.status_code == 200, escalated.text
    assert live["policy_violations"] == feed.json()["total"] == 9
    assert live["human_escalations"]["value"] == escalated.json()["total"] == 5


async def test_a_record_on_a_window_edge_is_counted_in_exactly_one_window(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Windows are half-open: [start, end). A record at the instant one window
    ends and the next begins belongs to the later one only, so the current and
    previous figures never share a record and the delta is not inflated."""
    from fulcrum_ops_api.services import runs as runs_service

    now = _utcnow().replace(microsecond=0) - dt.timedelta(minutes=1)
    monkeypatch.setattr(runs_service, "_now", lambda: now)
    agent = await factory.provisioned_agent(workspace, engine, name="Edge Bot")
    policy = await factory.policy(workspace)
    tick = dt.timedelta(microseconds=1)
    hour = dt.timedelta(hours=1)
    for at in (
        now,  # the end of the current window: not in it
        now - tick,  # current
        now - hour,  # current: its first instant
        now - hour - tick,  # previous: its last instant
        now - 2 * hour,  # previous: its first instant
        now - 2 * hour - tick,  # before both
    ):
        await _violation(factory, workspace, policy, agent.id, "Escalate", at)

    summary = await _summary(admin_client, time_range="Last hour")

    assert (summary["policy_violations"], summary["policy_violations_previous"]) == (2, 2)
    assert summary["human_escalations"]["value"] == 2.0
    assert summary["human_escalations_previous"] == 2
    series = [point["value"] for point in summary["human_escalations"]["series"]]
    assert (series[0], series[-1], sum(series)) == (1.0, 1.0, 2.0)
    scope = summary["violations_scope"]
    assert dt.datetime.fromisoformat(scope["window_end"]) == now
    assert dt.datetime.fromisoformat(scope["window_start"]) == now - hour
    assert dt.datetime.fromisoformat(scope["previous_window_start"]) == now - 2 * hour


async def test_run_filters_that_a_record_cannot_answer_are_named_not_applied(
    admin_client, seeded
):
    """A record names its agent, not the run's tenant or source. Rather than
    silently ignore the filters, or silently apply them to one card and not the
    one beside it, the summary says which did not narrow the counts."""
    plain = await _summary(admin_client, time_range="Last hour")
    filtered = await _summary(
        admin_client, time_range="Last hour", tenant="acme", source="Custom Agent"
    )

    assert filtered["policy_violations"] == plain["policy_violations"] == 6
    assert filtered["violations_scope"]["filters_not_applied"] == ["tenant", "source"]


async def test_the_runs_verdicts_no_longer_count_as_violations(
    admin_client, factory, workspace, engine
):
    """Runs whose verdict is Warned or Blocked, and a run with a failed
    guardrail check, and not one violation record: the card reads zero, as the
    Policy Center does. The verdicts still show in the table's Policy column."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(project_name=agent.engine_project_name, metadata={"policy": "Blocked"})
    engine.add_trace(project_name=agent.engine_project_name, metadata={"policy": "Warned"})
    engine.add_trace(
        project_name=agent.engine_project_name,
        metadata={"guardrails": [{"name": "PII", "result": "Failed"}]},
    )

    summary = await _summary(admin_client, time_range="Last hour")
    table = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})

    assert summary["total_runs"] == 3
    assert summary["policy_violations"] == 0
    assert summary["human_escalations"]["value"] == 0.0
    assert sorted(row["policy"] for row in table.json()["items"]) == [
        "Blocked",
        "Warned",
        "Warned",
    ]


async def test_agent_hand_offs_are_still_measured_under_their_own_name(
    admin_client, factory, workspace, engine
):
    """The flag an agent records when it hands a run to a person used to be the
    Human Escalations figure. It is a real measurement and stays one."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policy = await factory.policy(workspace)
    engine.add_trace(project_name=agent.engine_project_name, metadata={"escalated": True})
    engine.add_trace(
        project_name=agent.engine_project_name, metadata={"errors": {"escalated": "Yes"}}
    )
    engine.add_trace(project_name=agent.engine_project_name)
    await _violation(factory, workspace, policy, agent.id, "Escalate", _ago(5))

    summary = await _summary(admin_client, time_range="Last hour")

    handoffs = summary["agent_handoffs"]
    assert handoffs["label"] == "Agent Hand-offs"
    assert (handoffs["value"], handoffs["unit"]) == (2.0, "runs")
    assert sum(point["value"] for point in handoffs["series"]) == 2.0
    assert summary["human_escalations"]["value"] == 1.0


async def test_a_capped_scan_does_not_withhold_the_counted_figures(
    admin_client, factory, workspace, engine
):
    """A capped scan withholds every trend folded from it. The violation counts
    are not folded from it: both windows are counted whole, so their change is
    a measurement and is reported."""
    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    policy = await factory.policy(workspace)
    now = _utcnow()
    every = dt.timedelta(hours=1) / 2200
    for index in range(2050):
        at = now - dt.timedelta(minutes=1) - every * index
        engine.add_trace(project_name=agent.engine_project_name, start_time=at, end_time=at)
    for minutes in (5, 15, 25, 35):
        await _violation(factory, workspace, policy, agent.id, "Block", _ago(minutes))
    for minutes in (65, 95):
        await _violation(factory, workspace, policy, agent.id, "Escalate", _ago(minutes))

    summary = await _summary(admin_client, time_range="Last hour")

    assert summary["scan"]["truncated"] is True
    assert summary["comparable"] is False
    assert summary["total_runs_delta_percent"] is None
    assert (summary["policy_violations"], summary["policy_violations_previous"]) == (4, 2)
    assert summary["policy_violations_delta_percent"] == 100.0
    assert summary["human_escalations"]["value"] == 0.0
    assert summary["human_escalations_previous"] == 2
    assert summary["human_escalations_delta_percent"] == -100.0


# ---------------------------------------------------------------------------
# A key bound to one agent counts that agent's records and no others
# ---------------------------------------------------------------------------


@pytest.fixture
async def bound_client(app, factory, workspace, seeded) -> AsyncIterator[httpx.AsyncClient]:
    claims, payroll = seeded
    token, _row = await factory.api_key(
        workspace, name="Claims runtime", scopes=["ingest", "read"], agent_id=claims.id
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.claims = claims  # type: ignore[attr-defined]
        http.payroll = payroll  # type: ignore[attr-defined]
        yield http


async def test_a_bound_key_counts_only_its_own_agents_records(bound_client):
    own = await _summary(bound_client, time_range="Last hour")
    neighbour = await _summary(
        bound_client, time_range="Last hour", agent_id=bound_client.payroll.id
    )

    assert own["policy_violations"] == 3
    assert own["human_escalations"]["value"] == 2.0
    assert own["violations_scope"]["agent_id"] == bound_client.claims.id
    # Asking about a neighbour answers as the run figures do: nothing in view.
    assert neighbour["total_runs"] == 0
    assert neighbour["policy_violations"] == 0
    assert neighbour["policy_violations_previous"] == 0
    assert neighbour["human_escalations"]["value"] == 0.0
