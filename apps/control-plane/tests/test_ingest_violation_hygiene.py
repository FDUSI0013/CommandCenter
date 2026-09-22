"""A violation reported at ingest is stored so that every screen counts it.

Two ways a correctly reported violation used to be counted wrongly -- on every
screen alike, so nothing disagreed and nothing was right:

* its action spelt other than the enforcement vocabulary ("escalate",
  "require_approval") made it a violation that was never a Human Escalation or
  a Block, because those are exact matches;
* a timestamp ahead of the server's clock put it outside every window, all of
  which run up to now, until the clock caught up.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from fulcrum_ops_api.models.governance import PolicyViolation

EVENTS = "/api/v1/ingest/events"


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _report(ingest_client, *events: dict) -> None:
    posted = await ingest_client.post(EVENTS, json={"agent": "Support Bot", "events": list(events)})
    assert posted.status_code < 300, posted.text


def _violation(action: str, when: dt.datetime | None = None) -> dict:
    return {
        "kind": "policy.violation",
        "policy": "Refunds",
        "action_taken": action,
        "occurred_at": _iso(when or dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)),
    }


async def test_an_escalation_counts_however_it_was_spelt(
    ingest_client, admin_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Refunds")
    spellings = [
        "require_approval", "REQUIRE APPROVAL", "Require-Approval", "escalate", " Escalate ",
    ]

    await _report(ingest_client, *(_violation(action) for action in spellings))

    stored = sorted(await db.scalars(select(PolicyViolation.action_taken)))
    assert stored == ["Escalate"] * 2 + ["Require Approval"] * 3
    summary = (await admin_client.get("/api/v1/policies/summary")).json()
    assert summary["human_escalations_30d"] == 5


async def test_an_action_outside_the_vocabulary_is_kept_as_sent(
    ingest_client, admin_client, factory, workspace, engine, db
):
    """Folding spelling is not guessing: a word that is not an enforcement is
    stored as the reporter wrote it, counted as a violation and nothing more."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Refunds")

    await _report(ingest_client, _violation("quarantine"))

    assert list(await db.scalars(select(PolicyViolation.action_taken))) == ["quarantine"]
    summary = (await admin_client.get("/api/v1/policies/summary")).json()
    assert (summary["violations_30d"], summary["human_escalations_30d"]) == (1, 0)


async def test_a_violation_stamped_in_the_future_is_counted_now(
    ingest_client, admin_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Refunds")
    tomorrow = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)

    await _report(ingest_client, _violation("Block", when=tomorrow))

    [stamp] = list(await db.scalars(select(PolicyViolation.occurred_at)))
    assert stamp <= dt.datetime.now(dt.UTC), "stored at its arrival, not in the future"
    summary = (await admin_client.get("/api/v1/policies/summary")).json()
    assert (summary["violations_30d"], summary["blocked_actions_30d"]) == (1, 1)
