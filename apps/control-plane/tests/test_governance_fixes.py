"""Regressions for a sweep of governance fixes made after the first takeover.

Each test pins one behaviour that was silently wrong: a console control that
did nothing, an audit column that was never written, a decision rule the
screen implied but the service did not hold, a mirror only one of two entry
paths applied, and a summary field the schema silently discarded.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select

from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.identity import Role


async def test_an_admin_can_set_a_password_the_member_can_sign_in_with(
    admin_client, client, factory, workspace
):
    """The console's "Set Password" action must actually set one."""
    created = await admin_client.post(
        "/api/v1/workspaces/users",
        json={"email": "quinn@northwind.test", "full_name": "Quinn Naylor"},
    )
    assert created.status_code == 201, created.text
    member = created.json()
    assert member["password_set"] is False

    patched = await admin_client.patch(
        f"/api/v1/workspaces/users/{member['id']}",
        json={"password": "horse-battery-staple-9"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["password_set"] is True

    signed_in = await client.post(
        "/api/v1/auth/login",
        json={"email": "quinn@northwind.test", "password": "horse-battery-staple-9"},
    )
    assert signed_in.status_code == 200, signed_in.text

    weak = await admin_client.patch(
        f"/api/v1/workspaces/users/{member['id']}", json={"password": "short"}
    )
    assert weak.status_code == 422, "the strength floor applies to admin resets too"


async def test_audit_rows_carry_their_previous_checksum(
    admin_client, db, factory, workspace, engine
):
    """External verifiers walk the chain row by row; the link must be stored."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    deactivated = await admin_client.post(
        f"/api/v1/agents/{agent.id}/deactivate", json={"reason": "Cost"}
    )
    assert deactivated.status_code == 200, deactivated.text

    rows = await db.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace.id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    )
    assert rows, "the deactivation must have been recorded"
    assert all(row.previous_checksum for row in rows)
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later.previous_checksum == earlier.checksum

    verified = await admin_client.get("/api/v1/audit/verify")
    assert verified.json()["intact"] is True


async def test_the_requester_cannot_decide_their_own_approval(as_role, workspace):
    """Four eyes means two people: raising and deciding must not be one."""
    async with as_role(Role.APPROVER) as requester:
        raised = await requester.post(
            "/api/v1/approvals",
            json={"action": "Refund Initiate", "resource": "orders/9912", "risk": "High"},
        )
        assert raised.status_code == 201, raised.text
        request_id = raised.json()["id"]

        selfie = await requester.post(
            f"/api/v1/approvals/{request_id}/approve", json={"note": "Looks fine to me"}
        )
        assert selfie.status_code == 403, selfie.text
        assert "different reviewer" in selfie.text

        # Escalating your own request is asking for eyes, not supplying them.
        escalated = await requester.post(f"/api/v1/approvals/{request_id}/escalate", json={})
        assert escalated.status_code == 200, escalated.text

    async with as_role(Role.APPROVER) as reviewer:
        decided = await reviewer.post(
            f"/api/v1/approvals/{request_id}/approve", json={"note": "Verified with finance"}
        )
        assert decided.status_code == 200, decided.text


async def test_sdk_feedback_is_mirrored_onto_the_trace(
    ingest_client, factory, workspace, engine
):
    """Both feedback paths must read identically in the telemetry store."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)

    landed = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "id": trace_id,
                    "name": "answer question",
                    "start_time": now.isoformat(),
                    "end_time": (now + dt.timedelta(seconds=2)).isoformat(),
                }
            ],
        },
    )
    assert landed.json()["accepted"] == 1, landed.text

    submitted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "agent": "Support Bot",
            "events": [
                {
                    "kind": "feedback.submitted",
                    "trace_id": trace_id,
                    "rating": 5,
                    "body": "Nailed it",
                    "ref": "FB-MIRROR-1",
                }
            ],
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["events_recorded"] == 1

    stored = engine.traces[trace_id]
    names = {score.get("name") for score in stored.get("feedback_scores", [])}
    assert "user_feedback" in names, "the rating must land on the trace itself"


async def test_the_exports_summary_actually_carries_its_datasets(admin_client):
    summary = await admin_client.get("/api/v1/exports/summary")
    assert summary.status_code == 200, summary.text
    datasets = summary.json()["datasets"]
    assert datasets, "the schema used to silently discard this field"
    assert {"source_screen", "columns", "filterable"} <= set(datasets[0])
