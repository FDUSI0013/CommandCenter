"""Every governance figure about violations and escalations is one count.

The Policy Center's KPI cards, its per-policy "Violations (30d)" column, its
violations feed, the Agent Registry's "Policy Violations" card and rows, and
Agent Detail each used to count policy violations their own way -- a rollup
column the scheduler rewrote every few minutes, a feed with no window, a card
over the whole workspace -- so the same week read as three different numbers.

The definition every one of them now shares:

* **Policy violations** are the rows of ``policy_violations`` for the
  workspace inside the window -- whatever enforcement fired, resolved or not,
  and whatever agent they name, including an agent since deleted from the
  registry.
* **Human escalations** are those of them whose ``action_taken`` is one of
  ``services.agents.ESCALATING_ACTIONS``: Escalate and Require Approval.

The seed below spreads violations across policies, agents (one of them
deleted, one row naming no agent at all), every enforcement outcome, resolved
and open rows, and times inside the window, in the window before it, and older
still -- plus a neighbouring workspace that must never be counted.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io

import pytest

from fulcrum_ops_api.models.governance import Policy, PolicyEnforcement, PolicyViolation
from fulcrum_ops_api.models.registry import AgentStatus
from fulcrum_ops_api.services import agents as agents_service
from fulcrum_ops_api.services import policies as policies_service

POLICIES = "/api/v1/policies"
AGENTS = "/api/v1/agents"
WINDOW = 30
ESCALATING = {"Escalate", "Require Approval"}


@dataclasses.dataclass(frozen=True)
class Seed:
    policy: str
    agent: str | None
    action: str
    days_ago: float
    resolved: bool = False


#: Inside the window, every enforcement outcome appears at least once.
SEEDS: tuple[Seed, ...] = (
    Seed("pii", "support", "Block", 1),
    Seed("pii", "support", "Escalate", 2, resolved=True),
    Seed("pii", "support", "Require Approval", 3),
    Seed("pii", "billing", "Warn", 5),
    Seed("pii", "retired", "Block", 6),
    Seed("pii", "retired", "Escalate", 7),
    Seed("pii", None, "Log Only", 8),
    Seed("refunds", "billing", "Require Approval", 10),
    Seed("refunds", "billing", "Mask", 12),
    Seed("refunds", "retired", "Require Approval", 20, resolved=True),
    Seed("refunds", "support", "Route", 25),
    Seed("refunds", "support", "Throttle", 29),
    Seed("refunds", "billing", "Allow", 29.5),
    # The previous window: counted by the registry card's delta only.
    Seed("pii", "support", "Escalate", 31),
    Seed("outside", "billing", "Block", 40),
    Seed("refunds", "retired", "Require Approval", 45),
    Seed("pii", "billing", "Block", 59),
    # Older than both windows.
    Seed("outside", "support", "Escalate", 75),
)


def _in_window(seed: Seed) -> bool:
    return seed.days_ago < WINDOW


def _in_previous_window(seed: Seed) -> bool:
    return WINDOW <= seed.days_ago < 2 * WINDOW


def _expected(**match) -> tuple[int, int, int]:
    """(violations, blocked, escalations) in the window among seeds matching ``match``."""
    rows = [
        seed
        for seed in SEEDS
        if _in_window(seed) and all(getattr(seed, key) == value for key, value in match.items())
    ]
    return (
        len(rows),
        sum(1 for seed in rows if seed.action == "Block"),
        sum(1 for seed in rows if seed.action in ESCALATING),
    )


@pytest.fixture
async def seeded(admin_client, factory, workspace, other_workspace):
    """The seed, persisted; the "retired" agent is then deleted through the API."""
    agents = {
        # A cache written on some earlier visit, with governance figures that
        # have gone stale since: the row must not repeat them.
        "support": await factory.agent(
            workspace,
            name="Support Bot",
            metrics_cache={"runs_30d": 12, "violations_30d": 99, "escalations_30d": 42},
        ),
        "billing": await factory.agent(workspace, name="Billing Bot"),
        "retired": await factory.agent(
            workspace, name="Retired Bot", status=AgentStatus.INACTIVE.value
        ),
    }
    policies = {
        name: await factory.policy(workspace, name=name.title())
        for name in ("pii", "refunds", "quiet", "outside")
    }
    now = dt.datetime.now(dt.UTC)
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policies[seed.policy].id,
                agent_id=agents[seed.agent].id if seed.agent else None,
                action_taken=seed.action,
                occurred_at=now - dt.timedelta(days=seed.days_ago),
                resolved_at=now if seed.resolved else None,
            )
            for seed in SEEDS
        ]
    )

    # The neighbour's violations, inside the window, of every kind.
    foreign = await factory.policy(other_workspace, name="Elsewhere")
    stranger = await factory.agent(other_workspace, name="Stranger Bot")
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=other_workspace.id,
                policy_id=foreign.id,
                agent_id=stranger.id,
                action_taken=action,
                occurred_at=now - dt.timedelta(days=2),
            )
            for action in ("Block", "Escalate", "Require Approval", "Warn")
        ]
    )

    deleted = await admin_client.delete(f"{AGENTS}/{agents['retired'].id}")
    assert deleted.status_code == 204, deleted.text
    return agents, policies


async def _summary(admin_client) -> dict:
    response = await admin_client.get(f"{POLICIES}/summary")
    assert response.status_code == 200, response.text
    return response.json()


async def _rows(admin_client, **params) -> list[dict]:
    response = await admin_client.get(POLICIES, params={"page_size": 50, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _feed_total(admin_client, **params) -> int:
    response = await admin_client.get(
        f"{POLICIES}/violations", params={"window_days": WINDOW, "page_size": 1, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()["total"]


# ---------------------------------------------------------------------------
# One definition, one window
# ---------------------------------------------------------------------------


def test_every_screen_counts_over_the_same_window():
    assert agents_service.GOVERNANCE_WINDOW_DAYS == WINDOW
    assert policies_service.DEFAULT_WINDOW_DAYS == agents_service.GOVERNANCE_WINDOW_DAYS
    assert agents_service.STATS_WINDOW_DAYS == agents_service.GOVERNANCE_WINDOW_DAYS
    assert set(agents_service.ESCALATING_ACTIONS) == ESCALATING


async def test_the_policy_center_cards_count_the_seeded_window(admin_client, seeded):
    violations, blocked, escalations = _expected()
    summary = await _summary(admin_client)

    assert summary["window_days"] == WINDOW
    assert summary["violations_30d"] == violations == 13
    assert summary["blocked_actions_30d"] == blocked == 2
    assert summary["human_escalations_30d"] == escalations == 5
    assert summary["policies_violated_30d"] == 2, "pii and refunds; 'outside' is older"
    assert sorted(summary["escalating_actions"]) == sorted(ESCALATING)


async def test_the_cards_the_rows_the_feed_and_the_registry_all_agree(admin_client, seeded):
    summary = await _summary(admin_client)
    rows = {row["name"]: row for row in await _rows(admin_client)}
    registry = (await admin_client.get(f"{AGENTS}/summary")).json()

    # Each policy's row is its own slice of the window.
    for name in ("pii", "refunds", "quiet", "outside"):
        row = rows[name.title()]
        assert (row["violations_30d"], row["blocked_30d"], row["escalations_30d"]) == _expected(
            policy=name
        ), name
        # ...and the feed, filtered to that policy, lists exactly that many.
        assert await _feed_total(admin_client, policy_id=row["id"]) == row["violations_30d"]

    # The rows add up to the cards above them.
    assert sum(row["violations_30d"] for row in rows.values()) == summary["violations_30d"]
    assert sum(row["blocked_30d"] for row in rows.values()) == summary["blocked_actions_30d"]
    assert sum(row["escalations_30d"] for row in rows.values()) == summary["human_escalations_30d"]
    assert sum(1 for row in rows.values() if row["violations_30d"]) == summary[
        "policies_violated_30d"
    ]

    # The feed's total for the window is the card.
    assert await _feed_total(admin_client) == summary["violations_30d"]

    # The Agent Registry's card is the same count, and its delta compares it
    # with the window before, counted the same way.
    assert registry["policy_violations_30d"] == summary["violations_30d"]
    assert registry["policy_violations_previous_30d"] == sum(
        1 for seed in SEEDS if _in_previous_window(seed)
    )


async def test_a_deleted_agents_violations_are_counted_everywhere_alike(admin_client, seeded):
    agents, _ = seeded
    retired = agents["retired"]
    in_window = _expected(agent="retired")[0]
    assert in_window == 3

    # Its registration is gone...
    assert (await admin_client.get(f"{AGENTS}/{retired.id}")).status_code == 404
    # ...its rows are not, and the feed lists them without an agent name.
    feed = await admin_client.get(
        f"{POLICIES}/violations",
        params={"window_days": WINDOW, "agent_id": retired.id, "page_size": 50},
    )
    assert feed.json()["total"] == in_window
    assert {item["agent_name"] for item in feed.json()["items"]} == {None}
    assert {item["agent_id"] for item in feed.json()["items"]} == {retired.id}

    # Registered agents, the deleted one and the row naming no agent make up
    # the whole: nothing is dropped and nothing counted twice.
    detail_total = 0
    detail_escalations = 0
    for key in ("support", "billing"):
        body = (await admin_client.get(f"{AGENTS}/{agents[key].id}")).json()
        metrics = body["agent"]["metrics"]
        expected_violations, _, expected_escalations = _expected(agent=key)
        assert metrics["violations_30d"] == expected_violations, key
        assert metrics["escalations_30d"] == expected_escalations, key
        detail_total += metrics["violations_30d"]
        detail_escalations += metrics["escalations_30d"]
    summary = await _summary(admin_client)
    unattributed = _expected(agent=None)
    assert detail_total + in_window + unattributed[0] == summary["violations_30d"]
    assert (
        detail_escalations + _expected(agent="retired")[2] + unattributed[2]
        == summary["human_escalations_30d"]
    )


async def test_the_registry_row_and_agent_detail_policy_rows_are_counted_now(
    admin_client, seeded
):
    agents, _ = seeded
    listed = {row["name"]: row for row in (await admin_client.get(AGENTS)).json()["items"]}

    support = listed["Support Bot"]["metrics"]
    violations, _, escalations = _expected(agent="support")
    assert (support["violations_30d"], support["escalations_30d"]) == (violations, escalations)
    assert support["runs_30d"] == 12, "the cached run figure is still served as cached"
    assert listed["Billing Bot"]["metrics"] is None, "no cache: not measured, not zeroed"

    policy_rows = {row["name"]: row for row in await _rows(admin_client)}
    detail = (await admin_client.get(f"{AGENTS}/{agents['support'].id}")).json()
    for bound in detail["policies"]:
        assert bound["violations_30d"] == policy_rows[bound["name"]]["violations_30d"], bound[
            "name"
        ]


async def test_the_rows_do_not_wait_for_the_rollup_sweep(admin_client, db, seeded):
    _, policies = seeded
    stored = await db.get(Policy, policies["pii"].id)
    assert stored.violations_30d == 0, "the sweep has not run"

    row = next(r for r in await _rows(admin_client) if r["id"] == policies["pii"].id)
    assert row["violations_30d"] == _expected(policy="pii")[0]
    single = (await admin_client.get(f"{POLICIES}/{policies['pii'].id}")).json()
    assert single["violations_30d"] == row["violations_30d"]

    # Once it has run, the stored copy is the same figure.
    async with db.session() as session:
        await policies_service.refresh_rollups(session)
        await session.commit()
    stored = await db.get(Policy, policies["pii"].id)
    assert (stored.violations_30d, stored.blocked_30d) == _expected(policy="pii")[:2]

    # And a read still does not count as an edit of the policy.
    assert (await admin_client.get(f"{POLICIES}/{policies['pii'].id}")).json()[
        "updated_at"
    ] == single["updated_at"]


async def test_sorting_by_violations_orders_by_the_figures_shown(admin_client, seeded):
    descending = await _rows(admin_client, sort="-violations_30d")
    counts = [row["violations_30d"] for row in descending]
    assert counts == sorted(counts, reverse=True)
    assert [row["name"] for row in descending[:2]] == ["Pii", "Refunds"]

    ascending = await _rows(admin_client, sort="violations_30d")
    assert [row["violations_30d"] for row in ascending] == sorted(counts)

    blocked = await _rows(admin_client, sort="-blocked_30d")
    assert [row["blocked_30d"] for row in blocked] == sorted(
        (row["blocked_30d"] for row in blocked), reverse=True
    )


async def test_the_csv_carries_the_figures_the_table_shows(admin_client, seeded):
    exported = await admin_client.get(f"{POLICIES}/export")
    assert exported.status_code == 200, exported.text
    records = {row["Policy Name"]: row for row in csv.DictReader(io.StringIO(exported.text))}
    for name in ("pii", "refunds", "quiet"):
        violations, blocked, escalations = _expected(policy=name)
        record = records[name.title()]
        assert int(record["Violations (30d)"]) == violations, name
        assert int(record["Blocked (30d)"]) == blocked, name
        assert int(record["Human Escalations (30d)"]) == escalations, name


async def test_a_record_stamped_in_the_future_is_in_no_window(admin_client, db, factory, workspace):
    """A reporting host whose clock runs ahead is not "the last 30 days".

    Live Runs always closes its window at a moment it names; the governance
    screens close theirs at now. Left open, they counted a future-stamped
    record that Live Runs did not, until its timestamp came round.
    """
    agent = await factory.agent(workspace, name="Skewed Clock Bot", metrics_cache={"runs_30d": 1})
    policy = await factory.policy(workspace, name="Skewed")
    now = dt.datetime.now(dt.UTC)
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                action_taken=PolicyEnforcement.ESCALATE.value,
                occurred_at=occurred_at,
            )
            for occurred_at in (
                now - dt.timedelta(hours=1),
                # A local time in UTC+05:30 sent as if it were UTC.
                now + dt.timedelta(hours=5, minutes=30),
            )
        ]
    )

    summary = await _summary(admin_client)
    assert (summary["violations_30d"], summary["human_escalations_30d"]) == (1, 1)
    row = next(r for r in await _rows(admin_client) if r["id"] == policy.id)
    assert (row["violations_30d"], row["escalations_30d"]) == (1, 1)
    assert await _feed_total(admin_client) == 1
    registry = (await admin_client.get(f"{AGENTS}/summary")).json()
    assert registry["policy_violations_30d"] == 1
    listed = {r["name"]: r for r in (await admin_client.get(AGENTS)).json()["items"]}
    assert listed["Skewed Clock Bot"]["metrics"]["violations_30d"] == 1
    detail = (await admin_client.get(f"{AGENTS}/{agent.id}")).json()
    assert detail["agent"]["metrics"]["violations_30d"] == 1

    # Live Runs' window names its end; over the same 30 days it counts the same.
    async with db.session() as session:
        live = await agents_service.violation_counts(
            session,
            workspace.id,
            agents_service.governance_window_start(),
            dt.datetime.now(dt.UTC),
        )
    assert (live.violations, live.escalations) == (1, 1)

    # The sweep's stored copy leaves it out too.
    async with db.session() as session:
        await policies_service.refresh_rollups(session, workspace_id=workspace.id)
        await session.commit()
    assert (await db.get(Policy, policy.id)).violations_30d == 1


# ---------------------------------------------------------------------------
# Human escalations are Escalate and Require Approval, and nothing else
# ---------------------------------------------------------------------------


async def test_human_escalations_count_only_escalate_and_require_approval(
    admin_client, factory, workspace
):
    agent = await factory.agent(workspace, name="Every Outcome Bot")
    now = dt.datetime.now(dt.UTC)
    by_action = {}
    for action in PolicyEnforcement:
        policy = await factory.policy(workspace, name=f"Fires {action.value}")
        by_action[action.value] = policy
        await factory.add(
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                action_taken=action.value,
                occurred_at=now - dt.timedelta(days=3),
            )
        )

    summary = await _summary(admin_client)
    assert summary["violations_30d"] == len(PolicyEnforcement)
    assert summary["human_escalations_30d"] == 2

    rows = {row["id"]: row for row in await _rows(admin_client)}
    for action, policy in by_action.items():
        expected = 1 if action in ESCALATING else 0
        assert rows[policy.id]["escalations_30d"] == expected, action

    metrics = (await admin_client.get(f"{AGENTS}/{agent.id}")).json()["agent"]["metrics"]
    assert metrics["escalations_30d"] == summary["human_escalations_30d"]
    assert metrics["violations_30d"] == summary["violations_30d"]


# ---------------------------------------------------------------------------
# The Metrics screen's governance chart counts the same records
# ---------------------------------------------------------------------------


async def _series_totals(admin_client, **params) -> dict[str, float]:
    response = await admin_client.get(
        "/api/v1/metrics/series",
        params={"window": "30d", "metric": ["violations", "escalations"], **params},
    )
    assert response.status_code == 200, response.text
    return {line["metric"]: line["total"] for line in response.json()["series"]}


async def test_the_metrics_chart_totals_are_the_policy_center_cards(admin_client, seeded):
    """The chart's escalations line used to count approval requests in the
    Escalated state -- the Approvals screen's own figure, under the name every
    other screen uses for Human Escalations -- so the chart and the cards beside
    it disagreed."""
    summary = await _summary(admin_client)
    totals = await _series_totals(admin_client)

    assert totals["violations"] == summary["violations_30d"]
    assert totals["escalations"] == summary["human_escalations_30d"]


async def test_the_metrics_chart_narrows_to_the_agent_filter(admin_client, seeded):
    """Every engine line on the chart honours the agent filter; the two
    governance lines ignored it and drew the whole workspace underneath."""
    agents, _ = seeded
    violations, _, escalations = _expected(agent="support")

    totals = await _series_totals(admin_client, agent_id=agents["support"].id)

    assert (totals["violations"], totals["escalations"]) == (violations, escalations)
