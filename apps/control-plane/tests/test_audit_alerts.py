"""Regressions from the September 2026 audit of Alerts.

Two things the screen promised and did not do:

* **A rule is read.** An admin could write "Quota exceeded / High", switch it
  off, switch it on, delete it -- and nothing anywhere ever looked at it. The
  raise now consults the workspace's rules: an enabled rule is named on the
  alert it governs, and a rule that is switched off silences what its screen
  raises at its severity, without losing the condition.
* **A burst of raises is a burst of alerts.** The reference ``al-N`` is counted
  and then probed, without a lock, so raises that landed together chose the
  same one and all but the first answered 500. That is exactly the flood the
  dedupe key exists for.

Everything here goes through the real request path. The machine raise is the
quota evaluator's, reached the way an admin reaches it: by lowering a ceiling
below the usage already counted.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.operations import Alert, AlertSeverity, AlertStatus

QUOTA_SCREEN = "Quota, Cost & Capacity"


def _long_ago() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=30)


async def _breach(http, quota, *, limit_value: float) -> None:
    """Lower a quota's ceiling so the evaluator raises its own alert."""
    response = await http.patch(f"/api/v1/quota/{quota.id}", json={"limit_value": limit_value})
    assert response.status_code == 200, response.text


async def _quota_alerts(db, workspace) -> list[Alert]:
    return await db.scalars(
        select(Alert)
        .where(Alert.workspace_id == workspace.id, Alert.source == QUOTA_SCREEN)
        .order_by(Alert.raised_at.asc())
    )


async def _summary(http) -> dict:
    response = await http.get("/api/v1/alerts/summary")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# #49 - alert rules are read
# ---------------------------------------------------------------------------


async def test_an_enabled_rule_is_named_on_the_alert_it_governs(
    admin_client, factory, workspace, db
):
    """The rule editor takes the source as free text, so the match ignores case."""
    created = await admin_client.post(
        "/api/v1/alerts/rules",
        json={
            "name": "Quota exceeded",
            "source": "quota, cost & capacity",
            "severity": "High",
            "notify_channels": ["PagerDuty"],
        },
    )
    assert created.status_code == 201, created.text
    quota = await factory.quota(workspace, name="Monthly tokens", used_value=500_000.0)

    await _breach(admin_client, quota, limit_value=400_000.0)

    [alert] = await _quota_alerts(db, workspace)
    assert alert.severity == AlertSeverity.HIGH.value
    assert alert.status == AlertStatus.OPEN.value
    assert alert.event_metadata["alert_rule"] == "Quota exceeded"
    assert alert.event_metadata["alert_rule_id"] == created.json()["id"]
    # Nothing is delivered from here, so nothing on the alert may say it was.
    assert "notify_channels" not in alert.event_metadata
    # The raiser's own payload is still there beside it.
    assert alert.event_metadata["limit_value"] == 400_000.0


async def test_a_disabled_rule_silences_its_screen_until_it_is_enabled_again(
    admin_client, factory, workspace, db
):
    rule = await factory.alert_rule(
        workspace,
        name="Quota exceeded",
        source=QUOTA_SCREEN,
        severity=AlertSeverity.HIGH.value,
        enabled=False,
    )
    quota = await factory.quota(workspace, name="Monthly tokens", used_value=500_000.0)

    await _breach(admin_client, quota, limit_value=400_000.0)

    # Written down, but out of the open count: the badge stays dark.
    [alert] = await _quota_alerts(db, workspace)
    assert alert.status == AlertStatus.MUTED.value
    assert alert.event_metadata["silenced_by_rule_id"] == rule.id
    assert "Quota exceeded" in alert.event_metadata["mute_reason"]
    assert "muted_until" not in alert.event_metadata
    summary = await _summary(admin_client)
    assert (summary["open"], summary["muted"]) == (0, 1)

    # The quota alerts on the change to Exceeded, not on staying there, so the
    # raise will not come again. Switching the rule on has to bring it back.
    enabled = await admin_client.patch(f"/api/v1/alerts/rules/{rule.id}", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text

    [alert] = await _quota_alerts(db, workspace)
    assert alert.status == AlertStatus.OPEN.value
    assert "silenced_by_rule_id" not in alert.event_metadata
    assert "mute_reason" not in alert.event_metadata
    assert "rule_mute_lifted_at" in alert.event_metadata
    summary = await _summary(admin_client)
    assert (summary["open"], summary["muted"]) == (1, 0)


async def test_a_disabled_rule_silences_only_its_own_severity_and_never_a_person(
    admin_client, as_role, factory, workspace, db
):
    await factory.alert_rule(
        workspace,
        name="Quota exceeded",
        source=QUOTA_SCREEN,
        severity=AlertSeverity.HIGH.value,
        enabled=False,
    )
    quota = await factory.quota(workspace, name="Monthly tokens", used_value=500_000.0)

    # 83% of the ceiling is the Medium "approaching limit" warning: not this rule's.
    await _breach(admin_client, quota, limit_value=600_000.0)
    [warning] = await _quota_alerts(db, workspace)
    assert warning.severity == AlertSeverity.MEDIUM.value
    assert warning.status == AlertStatus.OPEN.value
    assert "alert_rule" not in warning.event_metadata

    # Past the ceiling is the High alert the rule does speak for.
    await _breach(admin_client, quota, limit_value=400_000.0)
    _warning, breach = await _quota_alerts(db, workspace)
    assert breach.severity == AlertSeverity.HIGH.value
    assert breach.status == AlertStatus.MUTED.value

    # A rule speaks for the screen it watches, not for whoever posts by hand.
    async with as_role(Role.OPERATOR) as http:
        posted = await http.post(
            "/api/v1/alerts",
            json={"title": "Spend looks wrong", "source": QUOTA_SCREEN, "severity": "High"},
        )
    assert posted.status_code == 201, posted.text
    assert posted.json()["status"] == AlertStatus.OPEN.value


async def test_deleting_a_disabled_rule_reopens_what_it_silenced_but_not_an_operators_mute(
    admin_client, factory, workspace, db
):
    rule = await factory.alert_rule(
        workspace,
        name="Quota exceeded",
        source=QUOTA_SCREEN,
        severity=AlertSeverity.HIGH.value,
        enabled=False,
    )
    first = await factory.quota(workspace, name="Monthly tokens", used_value=500_000.0)
    second = await factory.quota(workspace, name="Daily requests", used_value=500_000.0)
    await _breach(admin_client, first, limit_value=400_000.0)
    await _breach(admin_client, second, limit_value=400_000.0)
    silenced, held = await _quota_alerts(db, workspace)
    assert {silenced.status, held.status} == {AlertStatus.MUTED.value}

    # An operator then mutes one of them for a maintenance window of their own.
    muted = await admin_client.post(
        f"/api/v1/alerts/{held.id}/mute", json={"duration_minutes": 60}
    )
    assert muted.status_code == 200, muted.text

    deleted = await admin_client.delete(f"/api/v1/alerts/rules/{rule.id}")
    assert deleted.status_code == 204, deleted.text

    assert (await db.get(Alert, silenced.id)).status == AlertStatus.OPEN.value
    assert (await db.get(Alert, held.id)).status == AlertStatus.MUTED.value


# ---------------------------------------------------------------------------
# #154 - raises that land together
# ---------------------------------------------------------------------------

BURST = 6


async def test_a_burst_of_raises_is_a_burst_of_alerts(as_role, workspace, db):
    """Every raise in the burst reads the same table and chooses ``al-1``."""
    async with as_role(Role.OPERATOR) as http:
        responses = await asyncio.gather(
            *[
                http.post(
                    "/api/v1/alerts",
                    json={"title": f"Connection {n} is down", "source": "Connection Center"},
                )
                for n in range(BURST)
            ]
        )

    assert [response.status_code for response in responses] == [201] * BURST, [
        response.text for response in responses if response.status_code != 201
    ]
    rows = await db.scalars(select(Alert).where(Alert.workspace_id == workspace.id))
    assert len(rows) == BURST
    assert len({row.alert_ref for row in rows}) == BURST
    assert {row.title for row in rows} == {f"Connection {n} is down" for n in range(BURST)}


async def test_a_burst_of_one_condition_is_one_alert_that_counts_every_occurrence(
    as_role, workspace, db
):
    """The raise that loses the race for the reference lost it to its own condition."""
    async with as_role(Role.OPERATOR) as http:
        responses = await asyncio.gather(
            *[
                http.post(
                    "/api/v1/alerts",
                    json={
                        "title": "Vector store unreachable",
                        "source": "Connection Center",
                        "severity": "Critical",
                        "dedupe_key": "connection:vector-store:down",
                    },
                )
                for _ in range(BURST)
            ]
        )

    codes = sorted(response.status_code for response in responses)
    assert codes == [200] * (BURST - 1) + [201], [response.text for response in responses]
    [row] = await db.scalars(select(Alert).where(Alert.workspace_id == workspace.id))
    assert row.occurrence_count == BURST
    assert {response.json()["id"] for response in responses} == {row.id}


async def test_two_raises_that_both_settle_for_an_opaque_reference_do_not_collide(
    as_role, workspace, db
):
    """Fifty taken references in a row and the allocator stops probing.

    The opaque reference it settles for was the *head* of a time-ordered id,
    which is its clock: every raise in the same minute was handed the same one.
    """
    async with db.session() as session:
        session.add_all(
            [
                Alert(
                    workspace_id=workspace.id,
                    alert_ref=f"al-{n}",
                    title=f"Imported alert {n}",
                    source="Policy Center",
                    severity=AlertSeverity.LOW.value,
                    status=AlertStatus.RESOLVED.value,
                    raised_at=_long_ago(),
                )
                # Sixty rows numbered from 61: the count says al-61 is next, and
                # al-61 to al-120 are taken, however the next raise counts.
                for n in range(61, 121)
            ]
        )
        await session.commit()

    async with as_role(Role.OPERATOR) as http:
        first = await http.post(
            "/api/v1/alerts", json={"title": "First", "source": "Policy Center"}
        )
        second = await http.post(
            "/api/v1/alerts", json={"title": "Second", "source": "Policy Center"}
        )

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert first.json()["alert_ref"] != second.json()["alert_ref"]
