"""Regressions from the September 2026 audit of Licensing & Entitlements.

Four things the screen promised and did not do:

* **Usage has a writer.** Usage & Limits, the Overage Alerts KPI and the usage
  export all read ``usage_records``, and nothing in the product ever wrote one.
* **A licence cannot be driven into a corner.** A term that had already ended
  was accepted at issue and then refused every later amendment; a plain PATCH
  could lift a suspension past the expiry check; and ``Revoked`` -- which the
  409 on a second licence tells the owner to reach for -- had no endpoint.
* **A plan change changes what is enforced.** Moving a licence to another plan,
  or editing the plan it is on, left the enforceable entitlement rows exactly as
  they were first seeded.
* **Seat mutations serialise on the licence.** Nothing locked the row, and one
  duplicate active seat turned every later Assign or Reassign into a 500.

Everything here goes through the real request path: the application and the
database the handlers themselves use.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.licensing import (
    Entitlement,
    SeatAssignment,
    TenantLicense,
    UsageRecord,
)
from fulcrum_ops_api.services import licensing as licensing_service


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _plan(owner_client, code: str, **fields) -> dict:
    body = {"name": code.title(), "code": code, "status": "Active", **fields}
    response = await owner_client.post("/api/v1/licensing/plans", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _license(owner_client, plan: dict, **fields) -> dict:
    body = {"plan_id": plan["id"], "seats_purchased": 5, **fields}
    response = await owner_client.post("/api/v1/licensing/tenants", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# #50 - usage has a writer
# ---------------------------------------------------------------------------


async def test_metered_usage_reaches_usage_and_limits_and_the_overage_kpi(
    owner_client, db, workspace
):
    plan = await _plan(owner_client, "starter", included_tokens=1_000_000, included_runs=500)
    held = await _license(owner_client, plan)

    # Two batches on the same day, as the ingest path reports them.
    for tokens, runs in ((900_000, 40), (600_000, 2)):
        async with db.session() as session:
            await licensing_service.record_usage(
                session, workspace.id, tokens=tokens, runs=runs
            )
            await session.commit()

    rows = await db.scalars(select(UsageRecord).where(UsageRecord.license_id == held["id"]))
    assert sorted((row.metric, int(row.quantity)) for row in rows) == [
        ("runs", 42),
        ("tokens", 1_500_000),
    ], "one bucket per meter per day, grown in place"

    response = await owner_client.get(f"/api/v1/licensing/tenants/{held['id']}/entitlements")
    assert response.status_code == 200, response.text
    usage = {row["metric"]: row for row in response.json()["usage"]}
    tokens = usage["tokens"]
    # JSON numbers, not the strings a Decimal serialises to: the console formats
    # these with toLocaleString, which leaves a string exactly as it was.
    assert tokens["used"] == 1_500_000 and isinstance(tokens["used"], int)
    assert tokens["remaining"] == -500_000 and isinstance(tokens["remaining"], int)
    assert tokens["over_limit"] is True
    assert tokens["utilization_pct"] == 150.0
    assert usage["runs"]["used"] == 42
    assert usage["runs"]["over_limit"] is False
    assert response.json()["over_limit_metrics"] == ["tokens"]

    summary = (await owner_client.get("/api/v1/licensing/summary")).json()
    assert summary["overage_alerts"] == 1

    export = await owner_client.get("/api/v1/licensing/export?dataset=usage")
    assert export.status_code == 200, export.text
    assert len(export.text.strip().splitlines()) == 3, "a header and the two buckets"


async def test_a_plan_with_no_allowance_is_not_over_it(owner_client, db, workspace):
    # Zero is the column default and means "not capped": no ceiling is seeded for
    # it, so metering must not flag the first token as overage.
    plan = await _plan(owner_client, "uncapped")
    held = await _license(owner_client, plan)

    async with db.session() as session:
        await licensing_service.record_usage(session, workspace.id, tokens=1234, runs=1)
        await session.commit()

    body = (
        await owner_client.get(f"/api/v1/licensing/tenants/{held['id']}/entitlements")
    ).json()
    tokens = next(row for row in body["usage"] if row["metric"] == "tokens")
    assert tokens["used"] == 1234
    assert tokens["included"] is None and tokens["utilization_pct"] is None
    assert tokens["over_limit"] is False
    assert body["over_limit_metrics"] == []
    assert (await owner_client.get("/api/v1/licensing/summary")).json()["overage_alerts"] == 0


async def test_metering_without_a_licence_is_a_quiet_no_op(db, workspace, other_workspace, factory):
    await factory.license(other_workspace)

    async with db.session() as session:
        await licensing_service.record_usage(session, workspace.id, tokens=500, runs=1)
        await session.commit()

    assert await db.count(UsageRecord) == 0, "somebody else's licence was billed"


# ---------------------------------------------------------------------------
# #51 - a licence cannot be driven into a corner
# ---------------------------------------------------------------------------


async def test_a_term_that_has_already_ended_is_refused_at_issue(owner_client):
    plan = await _plan(owner_client, "starter")
    # What the console sends when today is picked in the Expires field: midnight
    # UTC, which is already behind a term that starts now.
    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)

    for expires in (midnight.isoformat(), "2020-01-01T00:00:00"):
        response = await owner_client.post(
            "/api/v1/licensing/tenants",
            json={"plan_id": plan["id"], "seats_purchased": 5, "expires_at": expires},
        )
        assert response.status_code == 422, response.text
        assert "expires_at" in response.text

    issued = await _license(
        owner_client, plan, expires_at=(_now() + dt.timedelta(days=365)).isoformat()
    )
    assert issued["status"] == "Active"


async def test_a_licence_already_holding_a_bad_term_can_still_be_amended_and_mended(
    owner_client, db, factory, workspace
):
    held = await factory.license(workspace)
    # A row from before the check existed: its term ends before it starts.
    await db.execute(
        update(TenantLicense)
        .where(TenantLicense.id == held.id)
        .values(expires_at=held.starts_at - dt.timedelta(days=1))
    )

    seats = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held.id}", json={"seats_purchased": 40}
    )
    assert seats.status_code == 200, "an amendment that moves no date was judged on the dates"
    assert seats.json()["seats_purchased"] == 40

    still_bad = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held.id}",
        json={"expires_at": (held.starts_at - dt.timedelta(days=2)).isoformat()},
    )
    assert still_bad.status_code == 422, still_bad.text

    mended = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held.id}",
        json={"expires_at": (_now() + dt.timedelta(days=90)).isoformat()},
    )
    assert mended.status_code == 200, mended.text
    assert mended.json()["days_until_expiry"] >= 89


async def test_a_suspension_is_lifted_by_reactivate_and_by_nothing_else(
    owner_client, db, factory, workspace
):
    held = await factory.license(workspace)
    suspended = await owner_client.post(
        f"/api/v1/licensing/tenants/{held.id}/suspend", json={"reason": "Non-payment"}
    )
    assert suspended.status_code == 200, suspended.text
    # The owner is told what a suspension does to telemetry, in the response.
    assert "telemetry" in suspended.json()["message"]
    assert suspended.json()["data"]["ingest_refused"] is True

    # The term lapses while the licence is suspended.
    await db.execute(
        update(TenantLicense)
        .where(TenantLicense.id == held.id)
        .values(expires_at=_now() - dt.timedelta(days=1))
    )

    forged = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held.id}", json={"status": "Active"}
    )
    assert forged.status_code == 412, "a plain PATCH lifted a suspension past the expiry check"
    stored = await db.get(TenantLicense, held.id)
    assert stored.status == "Suspended" and stored.suspended_reason == "Non-payment"

    lapsed = await owner_client.post(f"/api/v1/licensing/tenants/{held.id}/reactivate")
    assert lapsed.status_code == 412, lapsed.text

    # The way out the 412 names: extend the term, then reactivate.
    extended = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held.id}",
        json={"expires_at": (_now() + dt.timedelta(days=200)).isoformat()},
    )
    assert extended.status_code == 200, extended.text
    back = await owner_client.post(f"/api/v1/licensing/tenants/{held.id}/reactivate")
    assert back.status_code == 200, back.text
    stored = await db.get(TenantLicense, held.id)
    assert stored.status == "Active"
    assert stored.suspended_at is None and stored.suspended_reason is None


async def test_revoking_a_licence_releases_its_seats_and_lets_another_be_issued(
    owner_client, admin_client, db, member, workspace
):
    plan = await _plan(owner_client, "starter")
    first = await _license(owner_client, plan)
    seat = await owner_client.post(
        f"/api/v1/licensing/tenants/{first['id']}/seats", json={"user_id": member.id}
    )
    assert seat.status_code == 201, seat.text

    blocked = await owner_client.post(
        "/api/v1/licensing/tenants", json={"plan_id": plan["id"], "seats_purchased": 5}
    )
    assert blocked.status_code == 409, blocked.text

    # Commercial terms are the owner's; an admin may not end a licence.
    refused = await admin_client.post(
        f"/api/v1/licensing/tenants/{first['id']}/revoke", json={"reason": "nope"}
    )
    assert refused.status_code == 403, refused.text

    revoked = await owner_client.post(
        f"/api/v1/licensing/tenants/{first['id']}/revoke",
        json={"reason": "Issued against the wrong plan"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"] == {
        "status": "Revoked",
        "previous_status": "Active",
        "seats_released": 1,
        "seats_assigned": 0,
    }

    stored = await db.get(TenantLicense, first["id"])
    assert stored.status == "Revoked" and stored.seats_assigned == 0
    assert await db.count(SeatAssignment, SeatAssignment.released_at.is_(None)) == 0
    event = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "licensing.license.revoked")
    )
    assert event is not None and "Issued against the wrong plan" in event.detail

    # Terminal: not twice, not amended, not reactivated.
    again = await owner_client.post(
        f"/api/v1/licensing/tenants/{first['id']}/revoke", json={}
    )
    assert again.status_code == 409, again.text
    amended = await owner_client.patch(
        f"/api/v1/licensing/tenants/{first['id']}", json={"seats_purchased": 9}
    )
    assert amended.status_code == 412, amended.text

    summary = (await owner_client.get("/api/v1/licensing/summary")).json()
    assert summary["suspended_or_revoked"] == 1

    replacement = await _license(owner_client, plan)
    assert replacement["id"] != first["id"]
