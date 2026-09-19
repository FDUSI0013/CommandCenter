"""The platform clock actually runs the jobs the screens depend on.

Four domains each grew a sweep -- budgets re-measured, policy counters
recounted, capacity recorded, orphaned syncs closed out -- and each was written
by someone who could not touch the scheduler. A sweep nothing calls is a feature
that looks finished in its own file and does nothing in production, which is
precisely how these four came to be dead in the first place. These pin the
wiring, not the sweeps: each has its own tests next to its own code.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from fulcrum_ops_api.models.governance import Policy, PolicyViolation, ViolationSeverity
from fulcrum_ops_api.services import quota, scheduler


@pytest.fixture(autouse=True)
def _due_now(monkeypatch):
    """Every interval gate reads as "due", whatever an earlier test left behind."""
    monkeypatch.setattr(scheduler, "_last_policy_rollup", 0.0)
    monkeypatch.setattr(scheduler, "POLICY_ROLLUP_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(quota, "_last_budget_sweep", None)


async def test_a_tick_recounts_the_policy_centers_violation_column(
    db, factory, workspace, engine_client
):
    policy = await factory.policy(workspace, name="PII must be masked")
    async with db.session() as session:
        session.add(
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                severity=ViolationSeverity.MEDIUM.value,
                action_taken="Warn",
                detail={},
                occurred_at=dt.datetime.now(dt.UTC),
            )
        )
        await session.commit()
    assert policy.violations_30d == 0, "nothing has counted it yet"
    before = (await db.scalar(select(Policy).where(Policy.id == policy.id))).updated_at

    counts = await scheduler.run_once()

    after = await db.scalar(select(Policy).where(Policy.id == policy.id))
    assert counts.get("policy_rollups_refreshed", 0) >= 1
    assert after.violations_30d == 1
    assert after.updated_at == before, "a counter moving is not an edit to the policy"


async def test_a_tick_runs_every_domains_sweep(db, factory, workspace, engine_client, monkeypatch):
    """Each sweep is reached, and the slow one only after the lock is given back."""
    order: list[str] = []
    real_try_lock = scheduler._try_lock

    async def spy_lock(session):
        held = await real_try_lock(session)
        order.append("lock")
        return held

    async def budgets(**_):
        order.append("budgets")
        return {"budgets_measured": 0}

    async def capacity(**_):
        order.append("capacity")
        return {"capacity_recorded": 0}

    async def stranded():
        order.append("stranded")
        return 0

    from fulcrum_ops_api.services import knowledge

    monkeypatch.setattr(scheduler, "_try_lock", spy_lock)
    monkeypatch.setattr(quota, "run_budget_sweep", budgets)
    monkeypatch.setattr(quota, "run_capacity_sweep", capacity)
    monkeypatch.setattr(knowledge, "fail_stranded_syncs", stranded)

    counts = await scheduler.run_once()

    assert {"budgets", "capacity", "stranded"} <= set(order)
    assert "policy_rollups_refreshed" in counts
    # Measuring budgets waits on the telemetry store; it must not do that while
    # holding the scheduler's lock and the pooled connection underneath it.
    assert order.index("budgets") > order.index("capacity")
    assert order[-1] == "budgets", f"budget measurement must run after the locked sweeps: {order}"
