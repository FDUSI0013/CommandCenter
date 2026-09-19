"""Regressions from the September 2026 audit of Quota, Cost & Capacity.

Four things the screen promised and did not do:

* **One page, one measurement.** The Overview tab measured the same period four
  times over and the Costs tab three more. ``/quota/overview`` answers every
  cost panel from a single measurement, and the capacity table no longer loads
  a month of readings to show the latest one.
* **Budgets keep themselves true.** Spend was rolled into a budget only when an
  operator pressed a button, so a new budget read $0 for ever, no threshold
  alert ever fired, and on the first of the month the budget vanished.
* **Capacity has a writer.** Nothing in the product recorded a capacity reading,
  so a third of the screen was permanently empty.
* **An approved increase is applied.** Approving a quota increase changed the
  request and left the quota exactly where it was.

Everything here goes through the real request path: the application, the engine
double behind a real adapter, and the database the handlers themselves use.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

from conftest import principal_for
from engine_double import EngineDouble
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.governance import ApprovalRequest, AuditEvent
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.operations import (
    Alert,
    Budget,
    CapacityRecord,
    CapacityStatus,
    LimitPeriod,
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
)
from fulcrum_ops_api.services import quota as quota_service


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HeldEngine(EngineDouble):
    """The double, able to hold one kind of read open.

    ``hold`` is ``(path suffix, seconds)``: a request whose path ends with the
    suffix waits that long before it is answered, and every other read is as
    quick as ever. Unset, this is the double exactly as every other test has it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hold: tuple[str, float] | None = None

    async def __call__(self, scope, receive, send) -> None:
        if self.hold and scope["type"] == "http" and scope["path"].endswith(self.hold[0]):
            await asyncio.sleep(self.hold[1])
        await super().__call__(scope, receive, send)


@pytest.fixture
def engine() -> HeldEngine:
    return HeldEngine()


async def _spending_agent(factory, workspace, engine, *, name, model, team, cost):
    """A provisioned agent with one run that cost ``cost`` dollars just now."""
    agent = await factory.provisioned_agent(
        workspace, engine, name=name, model=model, team=team
    )
    engine.add_trace(
        project_name=agent.engine_project_name,
        name="answer",
        total_estimated_cost=cost,
        usage={"total_tokens": 1000, "prompt_tokens": 600, "completion_tokens": 400},
    )
    return agent


# ---------------------------------------------------------------------------
# #27 - one page view, one measurement
# ---------------------------------------------------------------------------


async def test_the_overview_answers_every_cost_panel_from_one_measurement(
    admin_client, factory, workspace, engine
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=3.0
    )
    await _spending_agent(
        factory, workspace, engine, name="Search Bot", model="text-embedding-3", team="Search",
        cost=1.0,
    )
    await factory.budget(workspace, name="Production monthly", amount_usd=100.0, spent_usd=90.0)
    engine.reset_calls()

    response = await admin_client.get("/api/v1/quota/overview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"summary", "models", "services", "drivers", "teams", "insights"}

    # Two windows were measured -- this period and the one before it -- once
    # each. Asking the six routes this replaces walked the statistics six times
    # per window.
    assert len(engine.calls_to("/projects/stats")) == 2
    assert len(engine.calls_to("/costs/summaries")) == 2

    assert body["summary"]["budget"]["value"] == 100.0
    assert {row["model"] for row in body["models"]} == {"gpt-4o", "text-embedding-3"}
    assert [row["model"] for row in body["models"]][0] == "gpt-4o"  # heaviest first
    assert {row["key"] for row in body["services"]} == {"inference", "embedding"}
    assert [row["label"] for row in body["drivers"]] == ["gpt-4o", "text-embedding-3"]
    assert [row["team"] for row in body["teams"]] == ["Support", "Search"]
    assert body["teams"][0]["spend_usd"] == 3.0
    assert body["teams"][0]["trend"], "the first page of teams carries its sparkline"
    assert any(item["key"].startswith("budget-pace:") for item in body["insights"])


async def test_the_overview_says_what_the_separate_routes_say(
    admin_client, factory, workspace, engine
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=2.5
    )

    overview = (await admin_client.get("/api/v1/quota/overview")).json()
    summary = (await admin_client.get("/api/v1/quota/summary")).json()
    models = (await admin_client.get("/api/v1/quota/cost-breakdown")).json()["items"]
    services = (await admin_client.get("/api/v1/quota/cost-by-service")).json()["items"]
    teams = (await admin_client.get("/api/v1/quota/team-allocation")).json()["items"]

    for card in ("spend", "budget", "tokens", "api_calls", "cost_per_1k_tokens", "capacity"):
        assert overview["summary"][card]["value"] == summary[card]["value"], card
    assert overview["summary"]["forecast_spend_usd"] == summary["forecast_spend_usd"]
    assert overview["models"] == models
    assert overview["services"] == services
    assert [row["team"] for row in overview["teams"]] == [row["team"] for row in teams]


async def test_the_overview_fails_closed_when_the_store_is_down(
    admin_client, factory, workspace, engine
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=2.5
    )
    engine.fail(503)

    response = await admin_client.get("/api/v1/quota/overview")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "telemetry_unavailable"


async def test_the_team_sparklines_answer_inside_the_request_deadline(
    admin_client, factory, workspace, engine, monkeypatch
):
    """One cost series per team is a fan-out too, and nothing bounded it."""
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=2.5
    )
    monkeypatch.setattr(settings, "engine_fanout_deadline_seconds", 0.5)
    # Only the per-team series is slow; the period's measurement answers at once.
    engine.hold = ("/workspaces/costs", 2.0)

    response = await admin_client.get("/api/v1/quota/team-allocation")

    # It used to wait the store out -- the table arrived, seconds later, however
    # long that was -- holding the worker and its database connection meanwhile.
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "telemetry_unavailable"


async def test_the_capacity_table_shows_the_latest_reading_and_a_bounded_trend(
    admin_client, factory, workspace
):
    now = _now()
    # Forty readings, one an hour, climbing: the newest is the highest.
    for hours_ago in range(40, 0, -1):
        await factory.add(
            CapacityRecord(
                workspace_id=workspace.id,
                name="GPU Capacity (A100)",
                resource_type="GPU",
                region="eastus",
                provisioned=100.0,
                used=float(60 - hours_ago),
                unit="GPUs",
                utilization_percent=float(60 - hours_ago),
                status=CapacityStatus.HEALTHY.value,
                measured_at=now - dt.timedelta(hours=hours_ago),
            )
        )
    # A reading older than the sparkline's week is counted but not drawn.
    await factory.add(
        CapacityRecord(
            workspace_id=workspace.id,
            name="Host Disk",
            resource_type="Disk",
            provisioned=500.0,
            used=450.0,
            unit="GB",
            utilization_percent=90.0,
            status=CapacityStatus.CRITICAL.value,
            measured_at=now - dt.timedelta(days=10),
        )
    )

    response = await admin_client.get("/api/v1/quota/capacity")

    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["items"]}
    gpu = rows["GPU Capacity (A100)"]
    assert gpu["utilization_percent"] == 59.0  # the newest reading, not the oldest
    assert gpu["used"] == 59.0
    assert gpu["reading_count"] == 40
    assert len(gpu["trend"]) == 24
    assert gpu["trend"][-1] == 59.0 and gpu["trend"][0] == 36.0  # oldest first
    assert gpu["icon"] == "cpu"
    assert rows["Host Disk"]["trend"] == []
    assert rows["Host Disk"]["health"] == "Critical"

    summary = (await admin_client.get("/api/v1/quota/summary")).json()
    assert summary["capacity_health"]["resource_count"] == 2
    assert summary["capacity_health"]["label"] == "Critical"


async def test_capacity_readings_stay_inside_their_workspace(
    as_role, factory, workspace, other_workspace
):
    await factory.add(
        CapacityRecord(
            workspace_id=other_workspace.id,
            name="Contoso GPUs",
            resource_type="GPU",
            provisioned=10.0,
            used=5.0,
            unit="GPUs",
            utilization_percent=50.0,
            status=CapacityStatus.HEALTHY.value,
            measured_at=_now(),
        )
    )
    async with as_role(Role.VIEWER) as http:
        response = await http.get("/api/v1/quota/capacity")
    assert response.status_code == 200
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# #28 - budgets are measured, alerted on and rolled over without a button
# ---------------------------------------------------------------------------


async def test_a_new_budget_is_measured_as_it_is_opened(
    admin_client, db, factory, workspace, engine
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=6.0
    )

    response = await admin_client.post(
        "/api/v1/quota/budgets", json={"name": "Production monthly", "amount_usd": 5}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    # It used to open at $0 / Active and stay there until somebody pressed
    # Re-measure Spend, over spend that was already past the ceiling.
    assert body["spent_usd"] == 6.0
    assert body["status"] == "Exceeded"
    raised = await db.scalars(select(Alert).where(Alert.source_entity_id == body["id"]))
    assert [alert.title for alert in raised] == [
        "Budget threshold exceeded: Production monthly"
    ]


async def test_a_budget_still_opens_when_the_store_cannot_answer(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)

    response = await admin_client.post(
        "/api/v1/quota/budgets", json={"name": "Production monthly", "amount_usd": 5}
    )

    assert response.status_code == 201, response.text
    assert response.json()["spent_usd"] == 0.0
    assert response.json()["status"] == "Active"


async def test_the_scheduled_sweep_measures_budgets_and_raises_their_alerts(
    db, factory, workspace, engine, engine_client
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=9.0
    )
    await _spending_agent(
        factory, workspace, engine, name="Search Bot", model="gpt-4o", team="Search", cost=2.0
    )
    whole = await factory.budget(workspace, name="Workspace", amount_usd=20.0, spent_usd=0.0)
    team = await factory.budget(
        workspace,
        name="Support team",
        amount_usd=10.0,
        spent_usd=0.0,
        scope=LimitScope.TEAM.value,
        scope_ref="Support",
    )
    switched_off = await factory.budget(
        workspace,
        name="Parked",
        amount_usd=1.0,
        spent_usd=0.0,
        status=LimitStatus.DISABLED.value,
    )

    counts = await quota_service.run_budget_sweep(force=True)

    assert counts == {"budgets_measured": 2, "budget_sweeps_failed": 0}
    measured = await db.get(Budget, whole.id)
    assert (measured.spent_usd, measured.status) == (11.0, "Active")
    scoped = await db.get(Budget, team.id)
    assert (scoped.spent_usd, scoped.status) == (9.0, "Warning")  # 90% of $10, warn at 80%
    parked = await db.get(Budget, switched_off.id)
    assert (parked.spent_usd, parked.status) == (0.0, "Disabled")

    raised = await db.scalars(select(Alert).where(Alert.source_entity_type == "budget"))
    assert [alert.source_entity_id for alert in raised] == [team.id]

    # The roll-up the clock makes is an observation, not an edit: it does not
    # move the concurrency token under whoever has the budget open, and it does
    # not write an audit row every ten minutes.
    assert scoped.updated_at == team.updated_at
    assert await db.count(AuditEvent, AuditEvent.action == "budget.refreshed") == 0

    # Asked again inside its interval, it is not due.
    assert await quota_service.run_budget_sweep() == {}


async def test_the_sweep_closes_a_lapsed_period_and_opens_the_next(
    db, factory, workspace, engine, engine_client
):
    now = _now()
    this_start, this_end = quota_service.period_bounds(LimitPeriod.MONTHLY, now)
    last_start, last_end = quota_service.period_bounds(
        LimitPeriod.MONTHLY, this_start - dt.timedelta(days=1)
    )
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=last_start + dt.timedelta(days=3),
        total_estimated_cost=40.0,
    )
    engine.add_trace(project_name=agent.engine_project_name, total_estimated_cost=5.0)
    lapsed = await factory.add(
        Budget(
            workspace_id=workspace.id,
            name="Production monthly",
            amount_usd=100.0,
            spent_usd=0.0,
            warn_threshold_percent=70.0,
            hard_threshold_percent=90.0,
            period_start=last_start,
            period_end=last_end,
            owner_user_id="owner-1",
        )
    )
    # Hand-picked dates are not a calendar period: there is no "next" to infer.
    one_off = await factory.add(
        Budget(
            workspace_id=workspace.id,
            name="Launch week",
            amount_usd=50.0,
            spent_usd=0.0,
            period_start=last_start + dt.timedelta(days=2),
            period_end=last_start + dt.timedelta(days=9),
        )
    )

    await quota_service.run_budget_sweep(force=True)
    await quota_service.run_budget_sweep(force=True)  # and it is idempotent

    rows = await db.scalars(
        select(Budget)
        .where(Budget.name == "Production monthly")
        .order_by(Budget.period_start.asc())
    )
    assert len(rows) == 2
    closed, opened = rows
    assert closed.id == lapsed.id
    assert (closed.status, closed.spent_usd) == ("Expired", 40.0)  # the final spend
    assert (opened.period_start, opened.period_end) == (this_start, this_end)
    assert (opened.amount_usd, opened.spent_usd, opened.status) == (100.0, 5.0, "Active")
    assert (opened.warn_threshold_percent, opened.hard_threshold_percent) == (70.0, 90.0)
    assert opened.owner_user_id == "owner-1"

    launch = await db.scalars(select(Budget).where(Budget.name == "Launch week"))
    assert [row.id for row in launch] == [one_off.id]
    assert launch[0].status == "Expired"

    assert await db.count(AuditEvent, AuditEvent.action == "budget.rolled_over") == 1
    # Closing a period is not a breach: nothing is raised for it.
    assert await db.count(Alert, Alert.source_entity_type == "budget") == 0


async def test_a_budget_nobody_renewed_is_closed_but_not_reopened(
    db, factory, workspace, engine, engine_client
):
    now = _now()
    this_start, _ = quota_service.period_bounds(LimitPeriod.MONTHLY, now)
    last_start, _ = quota_service.period_bounds(
        LimitPeriod.MONTHLY, this_start - dt.timedelta(days=1)
    )
    old_start, old_end = quota_service.period_bounds(
        LimitPeriod.MONTHLY, last_start - dt.timedelta(days=1)
    )
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    abandoned = await factory.add(
        Budget(
            workspace_id=workspace.id,
            name="Spring pilot",
            amount_usd=500.0,
            spent_usd=120.0,
            period_start=old_start,
            period_end=old_end,
        )
    )

    await quota_service.run_budget_sweep(force=True)

    # The sweep is new, and every workspace has rows like this one. Its books
    # are closed; it is not brought back to life and added to Total Budget.
    rows = await db.scalars(select(Budget).where(Budget.name == "Spring pilot"))
    assert [(row.id, row.status, row.spent_usd) for row in rows] == [
        (abandoned.id, "Expired", 120.0)
    ]
    assert await db.count(AuditEvent, AuditEvent.action == "budget.rolled_over") == 0


async def test_a_period_still_opens_when_the_store_cannot_answer(
    db, factory, workspace, engine, engine_client
):
    now = _now()
    this_start, this_end = quota_service.period_bounds(LimitPeriod.MONTHLY, now)
    last_start, last_end = quota_service.period_bounds(
        LimitPeriod.MONTHLY, this_start - dt.timedelta(days=1)
    )
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.add(
        Budget(
            workspace_id=workspace.id,
            name="Production monthly",
            amount_usd=100.0,
            spent_usd=80.0,
            period_start=last_start,
            period_end=last_end,
        )
    )
    engine.fail(503)

    counts = await quota_service.run_budget_sweep(force=True)

    # Opening a period is our own SQL. It is not lost with the measurement.
    assert counts == {"budgets_measured": 0, "budget_sweeps_failed": 1}
    rows = await db.scalars(
        select(Budget)
        .where(Budget.name == "Production monthly")
        .order_by(Budget.period_start.asc())
    )
    assert [(row.period_start, row.period_end) for row in rows] == [
        (last_start, last_end),
        (this_start, this_end),
    ]
    assert (rows[1].amount_usd, rows[1].spent_usd, rows[1].status) == (100.0, 0.0, "Active")
    assert rows[0].spent_usd == 80.0  # unmeasured, so left exactly as it was


async def test_a_workspace_the_store_cannot_answer_for_keeps_the_spend_it_had(
    db, factory, workspace, engine, engine_client
):
    await factory.budget(workspace, name="Workspace", amount_usd=20.0, spent_usd=3.0)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)

    counts = await quota_service.run_budget_sweep(force=True)

    assert counts == {"budgets_measured": 0, "budget_sweeps_failed": 1}
    kept = await db.scalar(select(Budget).where(Budget.name == "Workspace"))
    assert kept.spent_usd == 3.0  # an unanswered read is not evidence of zero spend


async def test_a_manual_re_measure_is_still_an_audited_edit(
    as_role, db, factory, workspace, engine
):
    await _spending_agent(
        factory, workspace, engine, name="Support Bot", model="gpt-4o", team="Support", cost=4.0
    )
    budget = await factory.budget(workspace, name="Workspace", amount_usd=20.0, spent_usd=0.0)

    async with as_role(Role.OPERATOR) as http:
        response = await http.post("/api/v1/quota/budgets/refresh")

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "1 budget(s) refreshed; 0 past a threshold."
    assert (await db.get(Budget, budget.id)).spent_usd == 4.0
    assert await db.count(AuditEvent, AuditEvent.action == "budget.refreshed") == 1


# ---------------------------------------------------------------------------
# #124 - capacity has a writer
# ---------------------------------------------------------------------------


async def test_a_reporter_puts_a_pool_on_the_capacity_tab(as_role, db, workspace):
    reading = {
        "name": "GPU Capacity (A100)",
        "resource_type": "GPU",
        "region": "eastus",
        "provisioned": 64,
        "used": 56,
        "unit": "GPUs",
    }
    async with as_role(Role.OPERATOR) as http:
        reported = await http.post(
            "/api/v1/quota/capacity",
            json={
                "readings": [
                    reading,
                    {"name": "Vector Store", "resource_type": "Storage", "provisioned": 500,
                     "used": 100, "unit": "GB"},
                ]
            },
        )
        assert reported.status_code == 201, reported.text
        assert reported.json()["data"] == {"recorded": 2}

        page = (await http.get("/api/v1/quota/capacity")).json()
        summary = (await http.get("/api/v1/quota/summary")).json()

    rows = {row["name"]: row for row in page["items"]}
    gpu = rows["GPU Capacity (A100)"]
    # Utilisation and status are derived here, with the screen's thresholds.
    assert (gpu["utilization_percent"], gpu["status"], gpu["health"]) == (87.5, "Critical",
                                                                         "Critical")
    assert (gpu["headroom"], gpu["icon"], gpu["region"]) == (8.0, "cpu", "eastus")
    assert rows["Vector Store"]["status"] == "Healthy"
    assert summary["capacity_health"]["label"] == "Critical"
    assert summary["capacity"]["value"] is not None  # the sixth card is no longer a dash

    stored = await db.scalars(select(CapacityRecord))
    assert {row.workspace_id for row in stored} == {workspace.id}


async def test_a_reading_must_be_reportable_and_recent(as_role):
    base = {"name": "GPUs", "resource_type": "GPU", "provisioned": 8, "used": 2}
    async with as_role(Role.MEMBER) as http:
        refused = await http.post("/api/v1/quota/capacity", json={"readings": [base]})
    assert refused.status_code == 403

    async with as_role(Role.OPERATOR) as http:
        for bad in (
            {**base, "measured_at": (_now() + dt.timedelta(hours=1)).isoformat()},
            {**base, "measured_at": (_now() - dt.timedelta(days=45)).isoformat()},
            {**base, "provisioned": 0},
            {**base, "utilization_percent": 12.0},  # derived, never accepted
        ):
            response = await http.post("/api/v1/quota/capacity", json={"readings": [bad]})
            assert response.status_code == 422, (bad, response.text)


async def test_a_reporter_cannot_write_under_the_platforms_own_pools(as_role, db, workspace):
    """Whether the platform's pass is due is read from its pools' newest reading."""
    async with as_role(Role.OPERATOR) as http:
        response = await http.post(
            "/api/v1/quota/capacity",
            json={
                "readings": [
                    {"name": quota_service.PLATFORM_DISK, "resource_type": "Disk",
                     "provisioned": 500, "used": 100, "unit": "GiB"}
                ]
            },
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["field"] == "readings[0].name"
    # Accepted, that reading would have told the sweep a pass had just run --
    # for this workspace and for every other one on the machine.
    assert (await quota_service.run_capacity_sweep())["capacity_recorded"] >= 1
    assert await db.count(CapacityRecord, CapacityRecord.workspace_id == workspace.id) >= 1


async def test_the_platform_records_what_it_can_see_of_itself(
    admin_client, db, factory, workspace, other_workspace
):
    stale = await factory.add(
        CapacityRecord(
            workspace_id=workspace.id,
            name="Decommissioned pool",
            resource_type="GPU",
            provisioned=8.0,
            used=1.0,
            unit="GPUs",
            utilization_percent=12.5,
            status=CapacityStatus.HEALTHY.value,
            measured_at=_now() - dt.timedelta(days=45),
        )
    )

    first = await quota_service.run_capacity_sweep()

    # Whatever this machine exposes, its disk is one of them; every reading is
    # written once per workspace, and the reading nobody can see any more goes.
    assert first["capacity_recorded"] >= 2
    assert first["capacity_recorded"] % 2 == 0
    assert first["capacity_expired"] == 1
    assert await db.get(CapacityRecord, stale.id) is None
    disks = await db.scalars(
        select(CapacityRecord).where(CapacityRecord.name == quota_service.PLATFORM_DISK)
    )
    assert {row.workspace_id for row in disks} == {workspace.id, other_workspace.id}
    assert all(row.provisioned > 0 and 0 <= row.utilization_percent <= 100 for row in disks)

    # Whether a pass is due is read from the table, so a second worker's tick a
    # moment later records nothing.
    assert await quota_service.run_capacity_sweep() == {}
    assert await db.count(
        CapacityRecord, CapacityRecord.name == quota_service.PLATFORM_DISK
    ) == 2

    page = (await admin_client.get("/api/v1/quota/capacity")).json()
    assert quota_service.PLATFORM_DISK in {row["name"] for row in page["items"]}


# ---------------------------------------------------------------------------
# #127 - an approved increase is applied
#
# ``services.approvals`` calls ``apply_approved_increase`` inside the decision's
# transaction. These tests file the request the way a member does, over HTTP,
# and then make that call the way the approvals service makes it.
# ---------------------------------------------------------------------------


async def _decide_increase(db, workspace, approver, request_id) -> str | None:
    """What approving ``request_id`` does to the quota it names."""
    async with db.session() as session:
        row = await session.get(ApprovalRequest, request_id)
        outcome = await quota_service.apply_approved_increase(
            session=session,
            principal=principal_for(workspace, approver, Role.APPROVER),
            approval_request=row,
        )
        await session.commit()
    return outcome


async def test_an_approved_increase_raises_the_ceiling_and_clears_the_breach(
    as_role, db, factory, workspace, approver
):
    quota = await factory.quota(
        workspace,
        name="Tokens per month",
        limit_value=1_000_000.0,
        used_value=1_000_000.0,
        enforcement=QuotaEnforcement.BLOCK.value,
        status=LimitStatus.EXCEEDED.value,
    )
    async with as_role(Role.MEMBER) as http:
        asked = await http.post(
            f"/api/v1/quota/{quota.id}/request-increase",
            json={"requested_limit": 2_000_000, "reason": "Launch week"},
        )
    assert asked.status_code == 200, asked.text
    filed = asked.json()["data"]

    outcome = await _decide_increase(db, workspace, approver, filed["request_id"])

    assert outcome == "'Tokens per month' now allows 2M tokens; the new ceiling is in force."
    raised = await db.get(Quota, quota.id)
    # It used to stay at 1M / Exceeded, refusing ingest, until an admin found
    # the quota and typed the approved number in by hand.
    assert raised.limit_value == 2_000_000.0
    assert raised.status == "Active"
    assert raised.updated_by == approver.email
    applied = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "quota.increase_applied")
    )
    assert applied.entity_id == quota.id
    assert applied.event_metadata["request_ref"] == filed["request_ref"]
    assert (applied.event_metadata["from"], applied.event_metadata["to"]) == (
        1_000_000.0,
        2_000_000.0,
    )

    # Deciding it twice, or after an admin already went further, changes nothing.
    again = await _decide_increase(db, workspace, approver, filed["request_id"])
    assert again == "'Tokens per month' already allows 2M tokens; nothing was changed."


async def test_only_the_increase_that_was_filed_is_applied(
    as_role, db, factory, workspace, approver
):
    quota = await factory.quota(workspace, name="Tokens per month", limit_value=1_000_000.0)
    async with as_role(Role.MEMBER) as http:
        asked = await http.post(
            f"/api/v1/quota/{quota.id}/request-increase", json={"requested_limit": 1_200_000}
        )
        filed = asked.json()["data"]
        assert filed["risk"] == "Low"  # a 20% ask, reviewed as one

        # Any member may edit an open request's payload. The approver was shown,
        # and the risk rung was set by, the number that was filed.
        edited = await http.patch(
            f"/api/v1/approvals/{filed['request_id']}",
            json={"payload": {"quota_id": quota.id, "requested_limit": 9_000_000_000}},
        )
        assert edited.status_code == 200, edited.text

        # And anybody may raise a request by hand under the same action.
        forged = await http.post(
            "/api/v1/approvals",
            json={
                "action": "Quota Increase",
                "resource": quota.name,
                "risk": "Low",
                "payload": {"quota_id": quota.id, "requested_limit": 9_000_000_000},
            },
        )
        assert forged.status_code == 201, forged.text

    drifted = await _decide_increase(db, workspace, approver, filed["request_id"])
    by_hand = await _decide_increase(db, workspace, approver, forged.json()["id"])

    assert "no longer matches the increase that was filed" in drifted
    assert "was not filed from Quota, Cost & Capacity" in by_hand
    assert (await db.get(Quota, quota.id)).limit_value == 1_000_000.0
    assert await db.count(AuditEvent, AuditEvent.action == "quota.increase_applied") == 0


async def test_an_approval_outlives_the_quota_it_was_for(
    as_role, admin_client, db, factory, workspace, approver
):
    quota = await factory.quota(workspace, name="Tokens per month", limit_value=1_000_000.0)
    other = await factory.approval(workspace, action="Refund Initiate")
    async with as_role(Role.MEMBER) as http:
        asked = await http.post(
            f"/api/v1/quota/{quota.id}/request-increase", json={"requested_limit": 2_000_000}
        )
    assert (await admin_client.delete(f"/api/v1/quota/{quota.id}")).status_code == 204

    gone = await _decide_increase(db, workspace, approver, asked.json()["data"]["request_id"])
    unrelated = await _decide_increase(db, workspace, approver, other.id)

    assert gone == "The quota no longer exists, so there was nothing to raise."
    assert unrelated is None  # not a quota increase: nothing to say, nothing done
