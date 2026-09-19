"""Hand-offs reconciled into the quota, approvals, licensing and metrics services.

Each of these was asked for by the owner of a neighbouring file who could see
the gap and could not reach it:

* **An approval that expires tells somebody.** Expiry is the one outcome of a
  request that no person chooses, and it left only an audit row behind.

Everything here goes through the real paths: the application, the engine double
behind a real adapter, the platform clock's own entry points and the database
the handlers themselves use.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from fulcrum_ops_api.models.governance import ApprovalRequest, ApprovalStatus
from fulcrum_ops_api.models.operations import Alert, AlertSeverity
from fulcrum_ops_api.services import approvals as approvals_service
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
