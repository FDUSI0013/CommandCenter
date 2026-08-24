"""The audit trail, and the hash chain that makes it evidence.

Every governed change writes one row, and each row's checksum covers its own
canonical form plus the previous row's checksum. That is what turns a table
anyone with database access could edit into something an auditor can rely on:
the log can still be altered, but not *silently* — the alteration shows up as a
break at a named row when the chain is replayed.

So there are two things to prove. First that the trail is actually written: a
run of ordinary operator work leaves one row per change, carrying who did it,
to what, and from which screen. Second that the chain does its job: after
reaching past the API and editing a row directly in the database — which is the
only way to do it, because the router exposes no write at all — ``verify`` says
the chain is broken and names where.

The tampering in this file is done through :class:`DatabaseProbe`, deliberately
outside the request path. A test that could break the chain through the API
would be reporting a much larger problem than a broken checksum.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select, update

from conftest import error_code
from fulcrum_ops_api.models.governance import AuditEvent, SecretStatus
from fulcrum_ops_api.models.registry import AgentStatus
from fulcrum_ops_api.services.audit import GENESIS


async def a_run_of_mutations(client, factory, workspace, engine) -> list[str]:
    """Do a morning's governance work and return the actions it should record."""
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot", status=AgentStatus.PENDING_REVIEW.value
    )
    secret = await factory.secret(workspace, name="Payments key")
    approval = await factory.approval(workspace, action="Refund over threshold")

    assert (
        await client.post(f"/api/v1/agents/{agent.id}/activate", json={})
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/agents/{agent.id}/deactivate", json={"reason": "Cost"})
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Incident 41"}
        )
    ).status_code == 200
    assert (await client.post(f"/api/v1/secrets/{secret.id}/rotate", json={})).status_code == 200
    assert (
        await client.post(f"/api/v1/secrets/{secret.id}/disable", json={"reason": "Rotated"})
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/approvals/{approval.id}/approve", json={"note": "Within band"}
        )
    ).status_code == 200

    return [
        "agent.activated",
        "agent.deactivated",
        "secret.revealed",
        "secret.rotated",
        "secret.disabled",
        "Request approved",
    ]


async def events(db, workspace) -> list[AuditEvent]:
    return await db.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace.id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    )


# ---------------------------------------------------------------------------
# The trail is written
# ---------------------------------------------------------------------------


async def test_a_run_of_mutations_records_one_row_each(
    admin_client, db, factory, workspace, engine
):
    expected = await a_run_of_mutations(admin_client, factory, workspace, engine)

    recorded = [event.action for event in await events(db, workspace)]

    assert recorded == expected, "one row per change, in the order they happened"


async def test_every_row_names_who_did_it_and_to_what(
    admin_client, db, factory, workspace, engine, admin
):
    await a_run_of_mutations(admin_client, factory, workspace, engine)

    for event in await events(db, workspace):
        assert event.actor == admin.email
        assert event.actor_user_id == admin.id
        assert event.entity_type, f"{event.action} recorded no entity type"
        assert event.entity_id, f"{event.action} recorded no entity id"
        assert event.source_screen, f"{event.action} recorded no source screen"
        assert event.checksum, f"{event.action} was written without a checksum"


async def test_a_row_records_where_the_request_came_from(
    admin_client, db, factory, workspace
):
    """The proxy's client address and the caller's agent string, for forensics."""
    secret = await factory.secret(workspace, name="Payments key")

    await admin_client.post(
        f"/api/v1/secrets/{secret.id}/disable",
        json={"reason": "Rotated"},
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "User-Agent": "console/1.4"},
    )

    event = (await events(db, workspace))[-1]
    assert event.ip_address == "203.0.113.9", "the left-most entry is the client"
    assert event.user_agent == "console/1.4"


async def test_a_read_writes_nothing(admin_client, db, factory, workspace, engine):
    """Only changes are evidence; logging every list request would bury them."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.secret(workspace, name="Payments key")

    for path in ("/api/v1/agents", "/api/v1/secrets", "/api/v1/approvals", "/api/v1/audit"):
        assert (await admin_client.get(path)).status_code == 200

    assert await events(db, workspace) == []


async def test_the_trail_offers_no_way_to_write_to_it(app):
    """The absence of a create, update or delete route is the feature."""
    paths = app.openapi()["paths"]
    audit_routes = {
        path: set(methods) for path, methods in paths.items() if path.startswith("/api/v1/audit")
    }

    assert audit_routes, "the audit routes must exist to be checked"
    for path, methods in audit_routes.items():
        assert methods <= {"get"}, f"{path} exposes {sorted(methods - {'get'})}"


async def test_a_failed_change_leaves_no_row(admin_client, db, factory, workspace):
    """A refusal is not a change, and must not pad the trail."""
    secret = await factory.secret(
        workspace, name="Retired key", status=SecretStatus.REVOKED.value
    )

    refused = await admin_client.post(f"/api/v1/secrets/{secret.id}/enable", json={})

    assert refused.status_code == 412
    assert [event.action for event in await events(db, workspace)] == []


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


async def test_the_chain_is_intact_after_a_run_of_mutations(
    admin_client, db, factory, workspace, engine
):
    expected = await a_run_of_mutations(admin_client, factory, workspace, engine)

    response = await admin_client.get("/api/v1/audit/verify")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "intact": True,
        "checked": len(expected),
        "broken_at_event_id": None,
        "broken_at": None,
    }


async def test_an_empty_trail_verifies(admin_client):
    response = await admin_client.get("/api/v1/audit/verify")
    assert response.status_code == 200, response.text
    assert response.json()["intact"] is True
    assert response.json()["checked"] == 0


async def test_each_row_links_to_the_one_before_it(
    admin_client, db, factory, workspace, engine
):
    """Recompute the chain here, independently of the service that wrote it."""
    import hashlib
    import json

    await a_run_of_mutations(admin_client, factory, workspace, engine)

    previous = GENESIS
    for event in await events(db, workspace):
        canonical = json.dumps(
            {
                "workspace_id": event.workspace_id,
                "occurred_at": event.occurred_at.isoformat(),
                "actor": event.actor,
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "detail": event.detail,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(f"{previous}{canonical}".encode()).hexdigest()
        assert event.checksum == expected, f"{event.action} does not link to its predecessor"
        previous = event.checksum


@pytest.mark.parametrize(
    "column",
    ["detail", "actor", "action", "entity_id"],
    ids=["detail", "actor", "action", "entity-id"],
)
async def test_editing_a_row_in_the_database_breaks_the_chain(
    admin_client, db, factory, workspace, engine, column
):
    """The one thing the trail exists to catch."""
    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)
    victim = rows[2]

    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.id == victim.id)
        .values(**{column: "quietly rewritten"})
    )

    response = await admin_client.get("/api/v1/audit/verify")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intact"] is False
    assert body["broken_at_event_id"] == victim.id, "verify must name the row"
    assert body["checked"] == 2, "the rows before the edit still reconcile"
    assert body["broken_at"] is not None


async def test_rewriting_a_checksum_to_match_the_edit_does_not_help(
    admin_client, db, factory, workspace, engine
):
    """Forging one row's checksum only moves the break to the next row."""
    import hashlib
    import json

    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)
    victim, successor = rows[2], rows[3]

    forged_detail = "quietly rewritten"
    canonical = json.dumps(
        {
            "workspace_id": victim.workspace_id,
            "occurred_at": victim.occurred_at.isoformat(),
            "actor": victim.actor,
            "action": victim.action,
            "entity_type": victim.entity_type,
            "entity_id": victim.entity_id,
            "detail": forged_detail,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    forged = hashlib.sha256(f"{rows[1].checksum}{canonical}".encode()).hexdigest()
    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.id == victim.id)
        .values(detail=forged_detail, checksum=forged)
    )

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is False
    assert body["broken_at_event_id"] == successor.id
    assert body["checked"] == 3, "the forged row now reconciles; the next one does not"


async def test_deleting_a_row_from_the_middle_breaks_the_chain(
    admin_client, db, factory, workspace, engine
):
    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)

    await db.execute(delete(AuditEvent).where(AuditEvent.id == rows[2].id))

    body = (await admin_client.get("/api/v1/audit/verify")).json()

    assert body["intact"] is False
    assert body["broken_at_event_id"] == rows[3].id, "the gap shows at the row after it"
    assert body["checked"] == 2


async def test_reordering_two_rows_breaks_the_chain(
    admin_client, db, factory, workspace, engine
):
    """Backdating a row to hide it among earlier ones is itself detectable."""
    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)

    await db.execute(
        update(AuditEvent)
        .where(AuditEvent.id == rows[4].id)
        .values(occurred_at=rows[0].occurred_at - dt.timedelta(seconds=1))
    )

    assert (await admin_client.get("/api/v1/audit/verify")).json()["intact"] is False


async def test_truncating_the_tail_is_not_detectable_by_replay_alone(
    admin_client, db, factory, workspace, engine
):
    """A documented limit, asserted so nobody mistakes it for a guarantee.

    A hash chain detects edits and gaps *inside* itself. Removing the most
    recent rows leaves a shorter chain that is internally consistent, and
    ``verify`` says so. Catching that needs an anchor outside this database —
    a periodically published head checksum, or shipping rows to a write-once
    store. Neither exists yet, which is worth knowing before anyone treats
    ``intact: true`` as proof that nothing was removed.
    """
    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)

    await db.execute(delete(AuditEvent).where(AuditEvent.id == rows[-1].id))

    body = (await admin_client.get("/api/v1/audit/verify")).json()
    assert body["intact"] is True
    assert body["checked"] == len(rows) - 1


async def test_a_row_stores_the_checksum_it_chains_from(
    admin_client, db, factory, workspace, engine
):
    await a_run_of_mutations(admin_client, factory, workspace, engine)
    rows = await events(db, workspace)

    assert rows[0].previous_checksum == GENESIS
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later.previous_checksum == earlier.checksum


# ---------------------------------------------------------------------------
# The chain is per tenant
# ---------------------------------------------------------------------------


async def test_one_tenants_writes_do_not_enter_anothers_chain(
    as_role, admin_client, db, factory, workspace, other_workspace, engine
):
    from fulcrum_ops_api.models.identity import Role

    await a_run_of_mutations(admin_client, factory, workspace, engine)
    ours = (await admin_client.get("/api/v1/audit/verify")).json()

    async with as_role(Role.ADMIN, other_workspace) as http:
        their_secret = await factory.secret(other_workspace, name="Contoso key")
        await http.post(f"/api/v1/secrets/{their_secret.id}/disable", json={})
        theirs = await http.get("/api/v1/audit/verify")
        their_trail = await http.get("/api/v1/audit")

    assert theirs.json() == {
        "intact": True,
        "checked": 1,
        "broken_at_event_id": None,
        "broken_at": None,
    }
    assert their_trail.json()["total"] == 1

    # Ours is unchanged by their activity.
    assert (await admin_client.get("/api/v1/audit/verify")).json() == ours


async def test_breaking_one_tenants_chain_leaves_the_others_intact(
    as_role, admin_client, db, factory, workspace, other_workspace, engine
):
    from fulcrum_ops_api.models.identity import Role

    await a_run_of_mutations(admin_client, factory, workspace, engine)
    async with as_role(Role.ADMIN, other_workspace) as http:
        their_secret = await factory.secret(other_workspace, name="Contoso key")
        await http.post(f"/api/v1/secrets/{their_secret.id}/disable", json={})

        victim = (await events(db, workspace))[1]
        await db.execute(
            update(AuditEvent).where(AuditEvent.id == victim.id).values(detail="rewritten")
        )

        theirs = await http.get("/api/v1/audit/verify")

    ours = await admin_client.get("/api/v1/audit/verify")

    assert ours.json()["intact"] is False
    assert theirs.json()["intact"] is True


async def test_a_tenant_cannot_read_anothers_trail(
    as_role, admin_client, factory, workspace, other_workspace, engine
):
    from fulcrum_ops_api.models.identity import Role

    await a_run_of_mutations(admin_client, factory, workspace, engine)

    async with as_role(Role.ADMIN, other_workspace) as http:
        response = await http.get("/api/v1/audit")

    assert response.json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 25,
        "pages": 1,
    }


async def test_an_anonymous_caller_may_not_verify_the_chain(client):
    response = await client.get("/api/v1/audit/verify")
    assert response.status_code == 401
    assert error_code(response) == "unauthenticated"
