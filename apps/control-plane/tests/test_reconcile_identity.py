"""Regressions for the identity hand-offs applied after the audit fix pass.

Each test pins a behaviour another screen's fix depends on but could not make
itself, because the code lives in the identity files.
"""

from __future__ import annotations

from fulcrum_ops_api.models.identity import ApiKey, Role

# ---------------------------------------------------------------------------
# #105  The escalation dialog reads the names-only directory, which opened one
#       role above the people entitled to escalate
# ---------------------------------------------------------------------------


async def test_an_approver_can_name_the_reviewer_they_escalate_to(as_role, operator, workspace):
    """Escalating is an approver's action, so the picker it fills has to open to them."""
    async with as_role(Role.APPROVER) as approver:
        raised = await approver.post(
            "/api/v1/approvals",
            json={"action": "Refund Initiate", "resource": "orders/4410", "risk": "High"},
        )
        assert raised.status_code == 201, raised.text

        listed = await approver.get("/api/v1/workspaces/users/directory")
        assert listed.status_code == 200, listed.text
        names = {row["id"]: row for row in listed.json()}
        assert operator.id in names, "the reviewer has to be on offer to be chosen"
        assert set(names[operator.id]) == {"id", "full_name", "initials"}, (
            "opening the picker one role lower must not widen what it carries"
        )

        escalated = await approver.post(
            f"/api/v1/approvals/{raised.json()['id']}/escalate",
            json={"escalate_to_user_id": operator.id, "note": "Needs a second pair of eyes"},
        )
        assert escalated.status_code == 200, escalated.text
        assert escalated.json()["request"]["escalated_to_user_id"] == operator.id

        roster = await approver.get("/api/v1/workspaces/users")
        assert roster.status_code == 403, "the who-has-access roster stays admin-only"


async def test_the_directory_still_stops_below_approver_and_at_keys(as_role, ingest_client):
    """Lowering the gate one role must not lower it two, or open it to credentials."""
    async with as_role(Role.MEMBER) as member:
        assert (await member.get("/api/v1/workspaces/users/directory")).status_code == 403
    async with as_role(Role.VIEWER) as viewer:
        assert (await viewer.get("/api/v1/workspaces/users/directory")).status_code == 403

    keyed = await ingest_client.get("/api/v1/workspaces/users/directory")
    assert keyed.status_code == 403, "a key never reads people, whatever its role maps to"


# ---------------------------------------------------------------------------
# #174  The once-a-minute note on a key stayed uncommitted -- and its row locked
#       -- until the request that wrote it had finished
# ---------------------------------------------------------------------------


async def test_a_key_refused_at_the_door_still_reads_as_used(client, db, factory, workspace):
    """The note is its own transaction: it outlives the request that carried it.

    While it shared the request's transaction, the row lock it took was held for
    as long as the handler ran -- an engine wait included -- so a fleet sharing
    one key queued behind it once a minute. The visible half of the same fault:
    a request that ended in a refusal rolled the note back, so a leaked
    ingest-only key being tried against the read routes showed "never used".
    """
    token, row = await factory.api_key(workspace, name="Ingest-only key", scopes=["ingest"])
    client.headers["Authorization"] = f"Bearer {token}"

    refused = await client.get("/api/v1/agents")
    assert refused.status_code == 403, refused.text

    stored = await db.get(ApiKey, row.id)
    assert stored.last_used_at is not None, "the key was presented, whatever came of it"
    assert stored.updated_at == row.updated_at, "being used is still not an edit of the key"
