"""Secrets & Credentials: the figures, the rotation clock and the PATCH door.

Three defects from the September audit, each pinned by driving the API the way
the console does and reading the answer back.

* A disabled or revoked credential cannot be rotated, so it must not be scored
  as overdue, expiring or non-compliant — by a KPI card, a panel filter or its
  own row flags. Before the fix it sat in "Rotation Overdue" for ever.
* A credential the control plane only points at has no local material. Saving
  its access form must not restart its rotation clock, and "Rotate Now" with no
  value must not mint a secret that exists nowhere upstream.
* PATCH is a metadata edit. It must not walk a revoked credential back into
  service, answer 500 to an explicit null, or accept an owner from another
  tenant and echo that person's name back.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update

from conftest import error_code, utcnow
from fulcrum_ops_api.models.governance import (
    AuditEvent,
    Secret,
    SecretAccessAction,
    SecretAccessLog,
    SecretStatus,
    SecretType,
)


async def audit_actions(db, workspace) -> list[str]:
    return await db.scalars(
        select(AuditEvent.action).where(AuditEvent.workspace_id == workspace.id)
    )


async def access_actions(db, secret) -> list[str]:
    return await db.scalars(
        select(SecretAccessLog.action)
        .where(SecretAccessLog.secret_id == secret.id)
        .order_by(SecretAccessLog.occurred_at, SecretAccessLog.id)
    )


async def backdate_rotation(db, secret, *, days: int) -> dt.datetime:
    """Move a credential's rotation deadline into the past, as time would."""
    deadline = utcnow() - dt.timedelta(days=days)
    await db.execute(
        update(Secret).where(Secret.id == secret.id).values(next_rotation_at=deadline)
    )
    return deadline


def names(page: dict) -> set[str]:
    return {item["name"] for item in page["items"]}


# ===========================================================================
# Out-of-service credentials are out of the score
# ===========================================================================


async def test_a_disabled_credential_past_its_deadline_is_not_scored_as_overdue(
    admin_client, db, factory, workspace
):
    live = await factory.secret(workspace, name="Live overdue key")
    retired = await factory.secret(
        workspace, name="Retired overdue key", status=SecretStatus.DISABLED.value
    )
    await factory.secret(workspace, name="Healthy key")
    await backdate_rotation(db, live, days=10)
    await backdate_rotation(db, retired, days=10)

    summary = (await admin_client.get("/api/v1/secrets/summary")).json()

    assert summary["total"] == 3
    assert summary["in_service"] == 2
    assert summary["rotation_overdue"] == 1
    # One of the two credentials in service is inside policy. Counting the
    # retired key as non-compliant would have made this 33.
    assert summary["compliance_score"] == 50

    overdue = await admin_client.get(
        "/api/v1/secrets", params={"rotation_state": "overdue"}
    )
    assert overdue.status_code == 200, overdue.text
    assert names(overdue.json()) == {"Live overdue key"}


async def test_a_retired_credentials_own_row_agrees_with_the_cards(
    admin_client, db, factory, workspace
):
    """The Rotation column already read "N/A"; the flags beside it said "Overdue"."""
    retired = await factory.secret(
        workspace,
        name="Retired key",
        status=SecretStatus.REVOKED.value,
        expires_at=utcnow() + dt.timedelta(days=2),
    )
    await backdate_rotation(db, retired, days=40)

    row = (await admin_client.get(f"/api/v1/secrets/{retired.id}")).json()

    assert row["rotation_label"] == "N/A"
    assert row["is_rotation_overdue"] is False
    assert row["is_expiring_soon"] is False
    assert row["compliance"] == "N/A"


async def test_a_live_credential_still_reports_overdue_and_non_compliant(
    admin_client, db, factory, workspace
):
    live = await factory.secret(workspace, name="Live key")
    await backdate_rotation(db, live, days=3)

    row = (await admin_client.get(f"/api/v1/secrets/{live.id}")).json()

    assert row["is_rotation_overdue"] is True
    assert row["rotation_label"] == "Overdue"
    assert row["compliance"] == "Non-Compliant"


async def test_the_expiring_panel_leaves_out_credentials_already_out_of_service(
    admin_client, factory, workspace
):
    soon = utcnow() + dt.timedelta(days=3)
    await factory.secret(workspace, name="Live certificate", expires_at=soon)
    await factory.secret(
        workspace,
        name="Revoked certificate",
        status=SecretStatus.REVOKED.value,
        expires_at=soon,
    )
    await factory.secret(
        workspace,
        name="Lapsed certificate",
        expires_at=utcnow() - dt.timedelta(days=1),
    )

    panel = await admin_client.get(
        "/api/v1/secrets", params={"expiring_within_days": 7, "sort": "expires_at"}
    )
    summary = (await admin_client.get("/api/v1/secrets/summary")).json()

    assert panel.status_code == 200, panel.text
    # Already lapsed is the most urgent row, so it stays — and sorts first.
    assert [item["name"] for item in panel.json()["items"]] == [
        "Lapsed certificate",
        "Live certificate",
    ]
    assert summary["expiring_soon"] == 2


async def test_compliance_by_type_is_scored_over_the_credentials_in_service(
    admin_client, db, factory, workspace
):
    certificate = SecretType.CERTIFICATE.value
    oauth = SecretType.OAUTH_CLIENT.value
    await factory.secret(workspace, name="Good cert", secret_type=certificate)
    retired = await factory.secret(
        workspace,
        name="Retired cert",
        secret_type=certificate,
        status=SecretStatus.DISABLED.value,
    )
    await backdate_rotation(db, retired, days=5)
    await factory.secret(
        workspace,
        name="Revoked client",
        secret_type=oauth,
        status=SecretStatus.REVOKED.value,
    )

    summary = (await admin_client.get("/api/v1/secrets/summary")).json()
    by_type = {row["secret_type"]: row for row in summary["by_type"]}

    assert by_type[certificate]["count"] == 2
    assert by_type[certificate]["in_service"] == 1
    assert by_type[certificate]["compliant"] == 1
    assert by_type[certificate]["compliance_pct"] == 100
    # Nothing of this type is in service, so there is nothing to score: the
    # bar reads "—", not a flattering 100 or an alarming 0.
    assert by_type[oauth]["count"] == 1
    assert by_type[oauth]["in_service"] == 0
    assert by_type[oauth]["compliance_pct"] is None


async def test_privileged_access_counts_reads_and_rotations_not_paperwork(
    admin_client, as_role, factory, workspace
):
    """The card says "Reveals and rotations"; it used to count every log row."""
    from fulcrum_ops_api.models.identity import Role

    secret = await factory.secret(workspace, name="Root key", privileged=True)
    ordinary = await factory.secret(workspace, name="Ordinary key")

    edited = await admin_client.patch(f"/api/v1/secrets/{secret.id}", json={"risk": "High"})
    assert edited.status_code == 200, edited.text
    async with as_role(Role.OPERATOR) as http:
        refused = await http.post(
            f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Curious"}
        )
    assert refused.status_code == 403
    await admin_client.post(
        f"/api/v1/secrets/{ordinary.id}/reveal", json={"justification": "Not privileged"}
    )

    before = (await admin_client.get("/api/v1/secrets/summary")).json()
    assert before["privileged_access_30d"] == 0

    await admin_client.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Break glass"}
    )
    await admin_client.post(f"/api/v1/secrets/{secret.id}/rotate", json={})

    after = (await admin_client.get("/api/v1/secrets/summary")).json()
    assert after["privileged_access_30d"] == 2


# ===========================================================================
# Credentials the control plane only points at
# ===========================================================================

#: What the console's Edit Access form sends on every save, changed or not.
def access_form(secret, **edits) -> dict:
    body = {
        "owner_user_id": secret.owner_user_id,
        "environment": secret.environment,
        "risk": secret.risk,
        "rotation_period_days": secret.rotation_period_days,
        "vault_reference": secret.vault_reference,
        "privileged": secret.privileged,
    }
    body.update(edits)
    return body


async def test_saving_the_access_form_does_not_restart_a_pointers_rotation_clock(
    admin_client, db, factory, workspace
):
    pointer = await factory.secret(
        workspace,
        name="Key Vault pointer",
        value=None,
        vault_reference="https://kv.example.test/secrets/openai",
    )
    deadline = await backdate_rotation(db, pointer, days=40)

    saved = await admin_client.patch(
        f"/api/v1/secrets/{pointer.id}", json=access_form(pointer, risk="High")
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["risk"] == "High"
    # Nothing was rotated, so the credential is exactly as overdue as it was.
    assert saved.json()["is_rotation_overdue"] is True
    stored = await db.get(Secret, pointer.id)
    assert abs((stored.next_rotation_at - deadline).total_seconds()) < 1
    summary = (await admin_client.get("/api/v1/secrets/summary")).json()
    assert summary["rotation_overdue"] == 1


async def test_changing_the_cadence_of_a_pointer_dates_it_from_its_creation(
    admin_client, db, factory, workspace
):
    pointer = await factory.secret(
        workspace,
        name="Key Vault pointer",
        value=None,
        vault_reference="https://kv.example.test/secrets/openai",
    )

    saved = await admin_client.patch(
        f"/api/v1/secrets/{pointer.id}", json={"rotation_period_days": 30}
    )

    assert saved.status_code == 200, saved.text
    stored = await db.get(Secret, pointer.id)
    expected = stored.created_at + dt.timedelta(days=30)
    assert abs((stored.next_rotation_at - expected).total_seconds()) < 1


async def test_rotating_a_pointer_with_no_value_records_it_and_mints_nothing(
    admin_client, db, factory, workspace
):
    pointer = await factory.secret(
        workspace,
        name="Key Vault pointer",
        value=None,
        vault_reference="https://kv.example.test/secrets/openai",
    )
    await backdate_rotation(db, pointer, days=40)

    rotated = await admin_client.post(
        f"/api/v1/secrets/{pointer.id}/rotate", json={"reason": "Rotated in Key Vault"}
    )

    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["value"] is None
    assert body["generated"] is False
    assert body["recorded_upstream"] is True
    assert body["secret"]["has_material"] is False
    assert body["secret"]["display_hint"] is None
    assert body["secret"]["is_rotation_overdue"] is False

    stored = await db.get(Secret, pointer.id)
    assert stored.ciphertext is None
    assert stored.last_rotated_at is not None
    assert stored.next_rotation_at > utcnow()
    assert await access_actions(db, pointer) == [SecretAccessAction.ROTATE.value]
    details = await db.scalars(
        select(AuditEvent.detail).where(AuditEvent.action == "secret.rotated")
    )
    assert details and "held upstream" in details[0]


async def test_a_pointer_takes_material_only_when_the_operator_supplies_it(
    admin_client, db, factory, workspace
):
    pointer = await factory.secret(
        workspace,
        name="Key Vault pointer",
        value=None,
        vault_reference="https://kv.example.test/secrets/openai",
    )

    rotated = await admin_client.post(
        f"/api/v1/secrets/{pointer.id}/rotate", json={"value": "sk-reissued-upstream-01"}
    )

    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["recorded_upstream"] is False
    assert rotated.json()["value"] is None
    assert rotated.json()["secret"]["has_material"] is True


# ===========================================================================
# PATCH is a metadata edit, not a way round the verbs
# ===========================================================================


async def test_patch_cannot_walk_a_revoked_credential_back_into_service(
    admin_client, db, factory, workspace
):
    burned = await factory.secret(
        workspace, name="Burned key", status=SecretStatus.REVOKED.value
    )

    response = await admin_client.patch(
        f"/api/v1/secrets/{burned.id}", json={"status": SecretStatus.ACTIVE.value}
    )

    # The same answer POST /enable gives: revocation is final.
    assert response.status_code == 412, response.text
    assert error_code(response) == "precondition_failed"
    stored = await db.get(Secret, burned.id)
    assert stored.status == SecretStatus.REVOKED.value
    assert await access_actions(db, burned) == []


async def test_a_revoked_credentials_metadata_can_still_be_corrected(
    admin_client, factory, workspace
):
    burned = await factory.secret(
        workspace, name="Burned key", status=SecretStatus.REVOKED.value
    )

    response = await admin_client.patch(
        f"/api/v1/secrets/{burned.id}",
        json={"status": SecretStatus.REVOKED.value, "risk": "High"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == SecretStatus.REVOKED.value
    assert response.json()["risk"] == "High"


async def test_disabling_and_enabling_belong_to_their_verbs(
    admin_client, db, factory, workspace
):
    live = await factory.secret(workspace, name="Live key")
    parked = await factory.secret(
        workspace, name="Parked key", status=SecretStatus.DISABLED.value
    )

    disable = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"status": SecretStatus.DISABLED.value}
    )
    enable = await admin_client.patch(
        f"/api/v1/secrets/{parked.id}", json={"status": SecretStatus.ACTIVE.value}
    )

    assert disable.status_code == 422, disable.text
    assert "/disable" in disable.json()["error"]["message"]
    assert enable.status_code == 422, enable.text
    assert "/enable" in enable.json()["error"]["message"]
    assert (await db.get(Secret, live.id)).status == SecretStatus.ACTIVE.value
    assert (await db.get(Secret, parked.id)).status == SecretStatus.DISABLED.value


async def test_revoking_through_patch_leaves_its_own_evidence(
    admin_client, db, factory, workspace
):
    live = await factory.secret(workspace, name="Leaked key")

    response = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"status": SecretStatus.REVOKED.value}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == SecretStatus.REVOKED.value
    assert (await db.get(Secret, live.id)).status == SecretStatus.REVOKED.value
    # A revocation, not an anonymous "update".
    assert await access_actions(db, live) == ["revoke"]
    actions = await audit_actions(db, workspace)
    assert "secret.revoked" in actions
    assert "secret.updated" not in actions

    log = await admin_client.get(f"/api/v1/secrets/{live.id}/access-log")
    assert log.status_code == 200, log.text
    assert log.json()["items"][0]["action"] == "revoke"


async def test_a_health_chip_may_still_be_written(admin_client, db, factory, workspace):
    live = await factory.secret(workspace, name="Live key")

    response = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"status": SecretStatus.WARNING.value}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == SecretStatus.WARNING.value
    assert await access_actions(db, live) == [SecretAccessAction.UPDATE.value]


async def test_an_explicit_null_for_a_required_field_is_a_422_not_a_500(
    admin_client, db, factory, workspace
):
    live = await factory.secret(workspace, name="Live key")

    for field in ("risk", "name", "vault", "secret_type", "privileged", "status"):
        response = await admin_client.patch(
            f"/api/v1/secrets/{live.id}", json={field: None}
        )
        assert response.status_code == 422, f"{field}: {response.text}"

    # The fields that really are optional can still be cleared.
    cleared = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"environment": None, "vault_reference": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert (await db.get(Secret, live.id)).risk == "Medium"


async def test_an_owner_from_another_tenant_is_refused_and_never_named(
    admin_client, db, factory, workspace, other_workspace
):
    outsider = await factory.user(
        other_workspace, email="outsider@elsewhere.test", full_name="Olive Outsider"
    )
    live = await factory.secret(workspace, name="Live key")

    patched = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"owner_user_id": outsider.id}
    )
    created = await admin_client.post(
        "/api/v1/secrets",
        json={"name": "New key", "vault": "Azure Key Vault", "value": "sk-new-000001",
              "owner_user_id": outsider.id},
    )

    for response in (patched, created):
        assert response.status_code == 422, response.text
        assert error_code(response) == "validation_failed"
        assert "Olive" not in response.text
        assert "outsider@elsewhere.test" not in response.text
    assert (await db.get(Secret, live.id)).owner_user_id is None


async def test_a_foreign_owner_already_on_a_row_is_not_resolved_or_searchable(
    admin_client, factory, workspace, other_workspace
):
    """Rows written before the check existed must not keep leaking."""
    outsider = await factory.user(
        other_workspace, email="outsider@elsewhere.test", full_name="Olive Outsider"
    )
    tainted = await factory.secret(
        workspace, name="Tainted key", owner_user_id=outsider.id
    )

    detail = await admin_client.get(f"/api/v1/secrets/{tainted.id}")
    searched = await admin_client.get("/api/v1/secrets", params={"q": "Olive"})
    listed = await admin_client.get("/api/v1/secrets", params={"sort": "owner"})

    assert detail.status_code == 200, detail.text
    assert detail.json()["owner_name"] is None
    assert detail.json()["owner_email"] is None
    assert searched.status_code == 200, searched.text
    assert searched.json()["total"] == 0
    assert "Olive" not in listed.text


async def test_a_member_can_be_made_owner_and_is_named(
    admin_client, factory, workspace, admin
):
    colleague = await factory.user(workspace, full_name="Carla Colleague")
    live = await factory.secret(workspace, name="Live key", owner_user_id=admin.id)

    response = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json={"owner_user_id": colleague.id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["owner_name"] == "Carla Colleague"


async def test_an_owner_who_has_left_does_not_block_other_edits(
    admin_client, factory, workspace, other_workspace
):
    """The access form echoes the stored owner on every save."""
    departed = await factory.user(other_workspace, full_name="Dee Departed")
    live = await factory.secret(workspace, name="Live key", owner_user_id=departed.id)

    response = await admin_client.patch(
        f"/api/v1/secrets/{live.id}", json=access_form(live, risk="High")
    )

    assert response.status_code == 200, response.text
    assert response.json()["risk"] == "High"
