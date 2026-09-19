"""Hand-offs reconciled into the quota, approvals, licensing and metrics services.

Each of these was asked for by the owner of a neighbouring file who could see
the gap and could not reach it:

* **An approval that expires tells somebody.** Expiry is the one outcome of a
  request that no person chooses, and it left only an audit row behind.
* **The clock's budget roll-up prices no tokens.** It reads cost and nothing
  else, and it was paying for one token aggregation per busy namespace on every
  pass for a number it never looked at.
* **Traffic volume has a public reader.** A screen that wants runs per
  namespace was reaching into the metrics service's private spellings.
* **A licence stops entitling the moment its term ends.** Expired is written by
  the renewal sweep on the clock, and access must not wait for that tick.

Everything here goes through the real paths: the application, the engine double
behind a real adapter, the platform clock's own entry points and the database
the handlers themselves use.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from fulcrum_ops_api.models.governance import ApprovalRequest, ApprovalStatus
from fulcrum_ops_api.models.licensing import LicenseStatus, TenantLicense
from fulcrum_ops_api.models.operations import Alert, AlertSeverity, Budget
from fulcrum_ops_api.services import approvals as approvals_service
from fulcrum_ops_api.services import licensing as licensing_service
from fulcrum_ops_api.services import metrics as telemetry
from fulcrum_ops_api.services import quota as quota_service
from fulcrum_ops_api.services import scheduler


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------------------
# #49 - an approval nobody decided raises an alert
# ---------------------------------------------------------------------------


async def test_an_approval_that_expires_undecided_raises_one_alert(
    admin_client, db, factory, workspace
):
    overdue = await factory.approval(
        workspace, action="Refund over threshold", sla_due_at=_now() - dt.timedelta(minutes=5)
    )
    in_time = await factory.approval(workspace, sla_due_at=_now() + dt.timedelta(hours=1))

    counts = await scheduler.run_once()
    await scheduler.run_once()  # the next tick finds nothing left to expire

    assert counts["approvals_expired"] == 1
    assert (await db.get(ApprovalRequest, overdue.id)).status == ApprovalStatus.EXPIRED.value
    assert (await db.get(ApprovalRequest, in_time.id)).status == ApprovalStatus.PENDING.value

    raised = await db.scalars(select(Alert).where(Alert.workspace_id == workspace.id))
    assert [alert.dedupe_key for alert in raised] == [f"approval:{overdue.id}:expired"]
    alert = raised[0]
    assert alert.source == approvals_service.SOURCE_SCREEN
    assert alert.severity == AlertSeverity.MEDIUM.value
    assert (alert.source_entity_type, alert.source_entity_id) == ("approval_request", overdue.id)
    assert overdue.request_ref in alert.title
    assert alert.occurrence_count == 1

    # And it is on the screen that shows alerts, not only in the table.
    listed = await admin_client.get("/api/v1/alerts", params={"q": overdue.request_ref})
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"]] == [alert.id]


# ---------------------------------------------------------------------------
# #28 - the roll-up on the clock reads cost, and pays for nothing else
# ---------------------------------------------------------------------------


async def test_the_budget_sweep_does_not_price_tokens_it_never_reads(
    db, factory, workspace, engine, engine_client
):
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot", model="gpt-4o"
    )
    engine.add_trace(
        project_name=agent.engine_project_name,
        name="answer",
        total_estimated_cost=4.0,
        usage={"total_tokens": 1000, "prompt_tokens": 600, "completion_tokens": 400},
    )
    budget = await factory.budget(workspace, name="Workspace", amount_usd=20.0, spent_usd=0.0)
    engine.reset_calls()

    counts = await quota_service.run_budget_sweep(force=True)

    assert counts == {"budgets_measured": 1, "budget_sweeps_failed": 0}
    assert (await db.get(Budget, budget.id)).spent_usd == 4.0

    # Cost comes out of the statistics walk. Token totals are the other, larger
    # half -- one aggregation per busy namespace -- and a budget compares
    # dollars against its ceiling, so the sweep stopped asking for them.
    assert engine.calls_to("/projects/stats"), "the sweep measured nothing at all"
    assert engine.calls_to("/traces/stats") == []


# ---------------------------------------------------------------------------
# #13 - runs per namespace, without the private spellings
# ---------------------------------------------------------------------------


async def test_run_counts_are_the_statistics_walk_and_nothing_else(
    factory, workspace, engine, engine_client
):
    busy = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quiet = await factory.provisioned_agent(workspace, engine, name="Night Bot")
    for _ in range(3):
        engine.add_trace(
            project_name=busy.engine_project_name, name="answer", total_estimated_cost=1.0
        )
    engine.reset_calls()

    end = _now() + dt.timedelta(minutes=1)
    counts = await telemetry.project_run_counts(
        engine_client,
        {busy.engine_project_id, quiet.engine_project_id},
        end - dt.timedelta(days=1),
        end,
    )

    # A namespace the engine has no statistics for counts zero runs: an agent
    # nobody used in the window, not a gap.
    assert counts == {busy.engine_project_id: 3, quiet.engine_project_id: 0}
    assert engine.calls_to("/traces/stats") == []

    # And the envelope reader those callers were paging rows with has a public
    # name, so it is no longer private code holding up another screen.
    assert telemetry.records({"content": [{"id": "a"}], "total": 1}) == [{"id": "a"}]
    assert telemetry.records({"data": [{"id": "b"}]}) == [{"id": "b"}]


# ---------------------------------------------------------------------------
# #163 - entitlements do not outlive the term
# ---------------------------------------------------------------------------


async def test_a_lapsed_licence_that_will_not_renew_entitles_nothing(
    db, factory, workspace, sessionmaker
):
    key = "guardrails.ml_detection"
    held = await factory.license(workspace, entitlements={key: True})
    async with sessionmaker() as session:
        assert await licensing_service.check_entitlement(session, workspace.id, key) is True

    # The term ends with auto-renew off. Expired is written by the renewal
    # sweep on the platform clock, so until its next tick the stored status
    # still says Active -- and the feature must already be off.
    await db.execute(
        update(TenantLicense)
        .where(TenantLicense.id == held.id)
        .values(expires_at=_now() - dt.timedelta(minutes=1), auto_renew=False)
    )

    async with sessionmaker() as session:
        assert await licensing_service.check_entitlement(session, workspace.id, key) is None
    assert (await db.get(TenantLicense, held.id)).status == LicenseStatus.ACTIVE.value

    # A term that renews itself is still the current licence: the sweep rolls
    # it forward rather than expiring it, and nothing should go dark meanwhile.
    await db.execute(
        update(TenantLicense).where(TenantLicense.id == held.id).values(auto_renew=True)
    )
    async with sessionmaker() as session:
        assert await licensing_service.check_entitlement(session, workspace.id, key) is True
