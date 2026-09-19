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

import contextlib
import datetime as dt

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

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


# ---------------------------------------------------------------------------
# #162 - a plan change changes what is enforced
# ---------------------------------------------------------------------------


async def _resolved(owner_client, license_id: str) -> dict:
    response = await owner_client.get(f"/api/v1/licensing/tenants/{license_id}/entitlements")
    assert response.status_code == 200, response.text
    return {row["key"]: row for row in response.json()["entitlements"]}


async def test_moving_a_licence_to_another_plan_re_resolves_what_is_enforced(
    owner_client, db, factory
):
    starter = await _plan(
        owner_client, "starter", included_tokens=1_000_000, features=["Tracing", "Email support"]
    )
    enterprise = await _plan(
        owner_client,
        "enterprise",
        tier="Enterprise",
        included_tokens=100_000_000,
        included_runs=50_000,
        features=["Tracing", "SSO", "Policy Center"],
    )
    held = await _license(owner_client, starter)
    # Not resolved from any plan: an operator's own row, which a plan change keeps.
    stored = await db.get(TenantLicense, held["id"])
    await factory.entitlement(stored, "max_agents", 40)

    check = "/api/v1/licensing/entitlement-check?key=included_tokens&current_usage=2000000"
    assert (await owner_client.get(check)).status_code == 402

    upgraded = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held['id']}", json={"plan_id": enterprise["id"]}
    )
    assert upgraded.status_code == 200, upgraded.text

    rows = await _resolved(owner_client, held["id"])
    assert rows["included_tokens"]["int_value"] == 100_000_000, "still the old plan's ceiling"
    assert rows["included_runs"]["int_value"] == 50_000
    assert sorted(rows) == [
        "Policy Center",
        "SSO",
        "Tracing",
        "included_runs",
        "included_tokens",
        "max_agents",
    ]
    # The gate the ingest path runs agrees with the panel.
    assert (await owner_client.get(check)).status_code == 200
    granted = await owner_client.get("/api/v1/licensing/entitlement-check?key=SSO")
    assert granted.json()["data"]["entitled"] is True
    event = await db.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == "licensing.license.updated")
        .order_by(AuditEvent.occurred_at.desc())
    )
    assert "re-resolved" in event.detail

    # And back down: the tenant does not keep what it no longer pays for.
    downgraded = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held['id']}", json={"plan_id": starter["id"]}
    )
    assert downgraded.status_code == 200, downgraded.text
    rows = await _resolved(owner_client, held["id"])
    assert sorted(rows) == ["Email support", "Tracing", "included_tokens", "max_agents"]
    assert rows["included_tokens"]["int_value"] == 1_000_000
    assert rows["max_agents"]["int_value"] == 40
    assert (await owner_client.get(check)).status_code == 402
    assert await db.count(Entitlement, Entitlement.license_id == held["id"]) == 4


async def test_editing_a_plan_reaches_every_current_licence_sold_on_it(
    owner_client, db, factory, other_workspace
):
    plan = await _plan(owner_client, "team", included_tokens=1_000_000, features=["Tracing"])
    held = await _license(owner_client, plan)
    # Plans are global: another tenant on the same plan, and one whose licence is
    # history and must keep the rows it ended with.
    elsewhere = await factory.add(
        TenantLicense(
            tenant_workspace_id=other_workspace.id,
            plan_id=plan["id"],
            status="Suspended",
            seats_purchased=3,
            starts_at=_now() - dt.timedelta(days=10),
        )
    )
    await factory.entitlement(elsewhere, "included_tokens", 1_000_000)
    ended = await factory.add(
        TenantLicense(
            tenant_workspace_id=other_workspace.id,
            plan_id=plan["id"],
            status="Revoked",
            seats_purchased=3,
            starts_at=_now() - dt.timedelta(days=400),
        )
    )
    await factory.entitlement(ended, "included_tokens", 1_000_000)

    edited = await owner_client.patch(
        f"/api/v1/licensing/plans/{plan['id']}",
        json={"included_tokens": 5_000_000, "features": ["Tracing", "Replay Studio"]},
    )
    assert edited.status_code == 200, edited.text

    rows = await _resolved(owner_client, held["id"])
    assert rows["included_tokens"]["int_value"] == 5_000_000
    assert "Replay Studio" in rows

    async def ceiling(license_id: str) -> int:
        return await db.scalar(
            select(Entitlement.int_value).where(
                Entitlement.license_id == license_id, Entitlement.key == "included_tokens"
            )
        )

    assert await ceiling(elsewhere.id) == 5_000_000, "a suspended licence is still current"
    assert await ceiling(ended.id) == 1_000_000, "a revoked licence was rewritten"
    event = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "licensing.plan.updated")
    )
    assert event.event_metadata["licenses_reseeded"] == 2

    # An edit that changes nothing a licence is resolved from leaves them alone.
    before = {row.id for row in await db.scalars(select(Entitlement))}
    renamed = await owner_client.patch(
        f"/api/v1/licensing/plans/{plan['id']}", json={"description": "For small teams"}
    )
    assert renamed.status_code == 200, renamed.text
    assert {row.id for row in await db.scalars(select(Entitlement))} == before


async def test_saving_the_same_plan_repairs_a_licence_that_moved_before_the_fix(
    owner_client, db
):
    starter = await _plan(owner_client, "starter", included_tokens=1_000_000, features=["Tracing"])
    enterprise = await _plan(
        owner_client, "enterprise", included_tokens=100_000_000, features=["Tracing", "SSO"]
    )
    held = await _license(owner_client, enterprise)
    # How a downgrade used to land: the plan swapped, the rows left as they were.
    await db.execute(
        update(TenantLicense).where(TenantLicense.id == held["id"]).values(plan_id=starter["id"])
    )
    assert "SSO" in await _resolved(owner_client, held["id"])

    # Change Plan, keep the plan, save -- which is what the console sends.
    saved = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held['id']}",
        json={"plan_id": starter["id"], "seats_purchased": 5},
    )
    assert saved.status_code == 200, saved.text
    rows = await _resolved(owner_client, held["id"])
    assert sorted(rows) == ["Tracing", "included_tokens"]
    assert rows["included_tokens"]["int_value"] == 1_000_000


# ---------------------------------------------------------------------------
# #223 - seat mutations serialise on the licence
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _locked_reads():
    """Collect the tables the handlers read ``FOR UPDATE`` while this is open.

    The test database is SQLite, which has no row locks and never emits the
    clause, so what is observed is the statement the handler asked for -- the
    same object Postgres compiles ``FOR UPDATE`` from.
    """
    locked: list[str] = []

    def watch(state) -> None:
        if state.is_select and getattr(state.statement, "_for_update_arg", None) is not None:
            locked.extend(table.name for table in state.statement.get_final_froms())

    event.listen(Session, "do_orm_execute", watch)
    try:
        yield locked
    finally:
        event.remove(Session, "do_orm_execute", watch)


async def test_every_seat_and_status_mutation_takes_the_licence_row(
    owner_client, factory, workspace, member
):
    held = await factory.license(workspace, seats_purchased=2)
    base = f"/api/v1/licensing/tenants/{held.id}"
    calls = (
        ("assign", "post", f"{base}/seats", {"user_id": member.id}, 201),
        ("purchase", "post", f"{base}/seats/purchase", {"seats": 3}, 200),
        ("amend", "patch", base, {"seats_purchased": 9}, 200),
        ("suspend", "post", f"{base}/suspend", {}, 200),
        ("reactivate", "post", f"{base}/reactivate", None, 200),
        ("revoke", "post", f"{base}/revoke", {}, 200),
    )
    for name, verb, url, body, expected in calls:
        with _locked_reads() as locked:
            response = await getattr(owner_client, verb)(url, json=body)
        assert response.status_code == expected, f"{name}: {response.text}"
        assert "tenant_licenses" in locked, f"{name} decided on a licence row it had not locked"

    # A read has nothing to decide and must not queue behind the writers.
    with _locked_reads() as locked:
        assert (await owner_client.get(f"{base}/seats")).status_code == 200
    assert locked == []


async def test_the_locked_lookup_is_for_update_where_rows_can_be_locked():
    from sqlalchemy.dialects import postgresql

    plain = licensing_service._license_lookup("ws", "lic")
    locked = licensing_service._license_lookup("ws", "lic", lock=True)
    assert "FOR UPDATE" not in str(plain.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in str(locked.compile(dialect=postgresql.dialect()))


async def test_a_user_left_holding_two_active_seats_can_still_be_managed(
    owner_client, db, factory, workspace, member, admin
):
    held = await factory.license(workspace, seats_purchased=5)
    # What two assignments racing past the unlocked check left behind.
    now = _now()
    await factory.add_all(
        [
            SeatAssignment(
                license_id=held.id,
                user_id=member.id,
                assigned_at=now - dt.timedelta(seconds=offset),
                role="member",
            )
            for offset in (2, 1)
        ]
    )

    again = await owner_client.post(
        f"/api/v1/licensing/tenants/{held.id}/seats", json={"user_id": member.id}
    )
    assert again.status_code == 409, again.text

    moved = await owner_client.post(
        f"/api/v1/licensing/tenants/{held.id}/seats",
        json={"user_id": admin.id, "replaces_user_id": member.id},
    )
    assert moved.status_code == 201, moved.text

    active = await db.scalars(
        select(SeatAssignment).where(
            SeatAssignment.license_id == held.id, SeatAssignment.released_at.is_(None)
        )
    )
    assert [seat.user_id for seat in active] == [admin.id], "the duplicate was not cleared"
    stored = await db.get(TenantLicense, held.id)
    assert stored.seats_assigned == 1


async def test_a_plan_whose_bullets_cannot_all_be_keys_still_resolves(owner_client, db):
    # Feature bullets are marketing copy: nothing stops two of them reading the
    # same, one being named like a ceiling, or one running past the 120
    # characters an entitlement key can hold. Each of those broke the insert --
    # at issue, and, now that a plan edit re-resolves its licences, on the edit.
    plan = await _plan(owner_client, "team", included_tokens=1_000_000, features=["Tracing"])
    held = await _license(owner_client, plan)

    long_bullet = "Dedicated success manager " + "x" * 120
    edited = await owner_client.patch(
        f"/api/v1/licensing/plans/{plan['id']}",
        json={"features": ["SSO", " SSO ", "included_tokens", long_bullet, "Tracing"]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["features"][-2] == long_bullet, "the plan card keeps every bullet"

    rows = await _resolved(owner_client, held["id"])
    assert sorted(rows) == ["SSO", "Tracing", "included_tokens"]
    # The ceiling is the enforceable row under that key, not the bullet.
    assert rows["included_tokens"]["value_type"] == "int"
    assert rows["included_tokens"]["int_value"] == 1_000_000

    # And a licence can still be issued on it.
    revoked = await owner_client.post(f"/api/v1/licensing/tenants/{held['id']}/revoke", json={})
    assert revoked.status_code == 200, revoked.text
    again = await _license(owner_client, plan)
    assert sorted(await _resolved(owner_client, again["id"])) == [
        "SSO",
        "Tracing",
        "included_tokens",
    ]


async def test_a_term_sent_half_with_an_offset_and_half_without_is_judged_not_crashed(
    owner_client,
):
    # A date picker sends "2027-01-01T00:00:00"; an SDK sends "...+00:00". Python
    # will not compare the two, and the TypeError left the validator as a 500.
    plan = await _plan(owner_client, "starter")
    backwards = await owner_client.post(
        "/api/v1/licensing/tenants",
        json={
            "plan_id": plan["id"],
            "seats_purchased": 5,
            "starts_at": "2027-06-01T00:00:00+00:00",
            "expires_at": "2027-01-01T00:00:00",
        },
    )
    assert backwards.status_code == 422, backwards.text

    held = await _license(
        owner_client,
        plan,
        starts_at="2027-01-01T00:00:00",
        expires_at="2028-01-01T00:00:00+00:00",
    )
    amended = await owner_client.patch(
        f"/api/v1/licensing/tenants/{held['id']}",
        json={"starts_at": "2027-02-01T00:00:00+00:00", "expires_at": "2027-01-15T00:00:00"},
    )
    assert amended.status_code == 422, amended.text
