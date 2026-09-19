"""Regressions from the 2026-09-18 audit of the Policy Center.

The thread through all of them: what the Policy Center *shows* and what the
ingest path *enforces* had drifted apart. A policy created from the form's
defaults refused every trace in the workspace; the Enforcement dropdown changed
a label and nothing else; a rule could name a signal that does not exist. Each
test below goes in through the same HTTP surface the console uses and, where
the point is enforcement, out through ``/ingest/traces``.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import event, select

from conftest import error_code
from fulcrum_ops_api.models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    Policy,
    PolicyBinding,
    PolicyEnforcement,
    PolicyStatus,
    PolicyViolation,
)
from fulcrum_ops_api.schemas.ingest import RejectionCode
from fulcrum_ops_api.schemas.policies import KNOWN_SIGNALS

NOW = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)

POLICIES = "/api/v1/policies"


def iso(offset_seconds: float = 0.0) -> str:
    return (NOW + dt.timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def trace(**overrides) -> dict:
    item = {
        "name": "answer customer question",
        "start_time": iso(),
        "end_time": iso(1.5),
        "input": {"question": "Where is my order?"},
        "output": {"answer": "It ships tomorrow."},
    }
    item.update(overrides)
    return item


def outcomes(response) -> list[tuple[int, str, str | None]]:
    return [
        (row["index"], row["outcome"], row["code"]) for row in response.json()["results"]
    ]


def rule_body(signal: str = "content", *, mode: str = "Block", **extra) -> dict:
    body = {
        "conditions": [{"signal": signal, "operator": "contains", "value": "4111 1111"}],
        "action": {"mode": mode},
    }
    body.update(extra)
    return body


async def _post_one_trace(ingest_client, agent_name: str, **overrides):
    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": agent_name, "traces": [trace(**overrides)]},
    )
    assert posted.status_code == 200, posted.text
    return posted


# ===========================================================================
# n=0 — a policy made from the form's defaults must not refuse all telemetry
# ===========================================================================


async def test_a_policy_created_from_the_forms_defaults_enforces_nothing_yet(
    admin_client, ingest_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    # Exactly what the Create Policy modal sends when only a name is typed.
    created = await admin_client.post(
        POLICIES,
        json={
            "name": "Default form",
            "category": "Guardrails",
            "enforcement": "Block",
            "scope": "Global",
            "scope_ref": None,
            "risk_level": "Medium",
            "status": "Active",
            "description": None,
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == PolicyStatus.INACTIVE.value, "a derived rule is never enforcing"
    assert body["rules"]["fail_mode"] == "open"
    assert body["rules"]["conditions"][0]["signal"] == "score:safety_score"

    posted = await _post_one_trace(ingest_client, "Support Bot")
    assert outcomes(posted) == [(0, "accepted", None)]
    assert engine.trace_count(agent.engine_project_name) == 1


async def test_a_derived_risk_rule_does_not_block_every_agent_at_that_risk(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot", risk="High")

    created = await admin_client.post(
        POLICIES,
        json={"name": "Security default", "category": "Security", "risk_level": "Low"},
    )

    assert created.status_code == 201, created.text
    assert created.json()["status"] == PolicyStatus.INACTIVE.value

    posted = await _post_one_trace(ingest_client, "Support Bot")
    assert outcomes(posted) == [(0, "accepted", None)]


async def test_a_derived_policy_can_still_be_activated_on_purpose(admin_client):
    created = await admin_client.post(POLICIES, json={"name": "Reviewed later"})
    assert created.status_code == 201, created.text

    activated = await admin_client.post(f"{POLICIES}/{created.json()['id']}/activate")

    assert activated.status_code == 200, activated.text
    assert activated.json()["policy"]["status"] == PolicyStatus.ACTIVE.value


async def test_a_policy_with_a_written_rule_body_is_still_created_active(admin_client):
    created = await admin_client.post(
        POLICIES, json={"name": "No card numbers", "rules": rule_body()}
    )

    assert created.status_code == 201, created.text
    assert created.json()["status"] == PolicyStatus.ACTIVE.value


async def test_import_never_activates_a_definition_whose_rule_was_derived(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    # The import modal's placeholder, with the "Activate every imported policy" box ticked.
    imported = await admin_client.post(
        f"{POLICIES}/import",
        json={
            "policies": [
                {
                    "name": "Block External File Sharing",
                    "category": "Data Protection",
                    "enforcement": "Block",
                },
                {"name": "No card numbers", "rules": rule_body()},
            ],
            "activate": True,
        },
    )

    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert (result["created"], result["skipped"]) == (2, 0)
    assert [issue["index"] for issue in result["issues"]] == [0]
    assert "Inactive" in result["issues"][0]["reason"]

    listed = (await admin_client.get(POLICIES)).json()["items"]
    statuses = {row["name"]: row["status"] for row in listed}
    assert statuses == {
        "Block External File Sharing": PolicyStatus.INACTIVE.value,
        "No card numbers": PolicyStatus.ACTIVE.value,
    }

    posted = await _post_one_trace(ingest_client, "Support Bot")
    assert outcomes(posted) == [(0, "accepted", None)]


async def test_a_rule_naming_a_signal_ingest_does_not_resolve_is_refused(admin_client):
    # Both names were advertised by the schema's own docstring; neither exists.
    for signal in ("data_classification", "tokens_used"):
        refused = await admin_client.post(
            POLICIES, json={"name": f"Bad {signal}", "rules": rule_body(signal)}
        )
        assert refused.status_code == 422, refused.text
        assert error_code(refused) == "validation_failed"
        details = refused.json()["error"]["details"]
        assert details["unknown_signals"] == [signal]
        assert "total_tokens" in details["allowed"]

    assert (await admin_client.get(POLICIES)).json()["total"] == 0


async def test_a_feedback_score_is_addressed_with_the_score_prefix(admin_client):
    created = await admin_client.post(
        POLICIES,
        json={
            "name": "Grounding floor",
            "rules": {
                "conditions": [
                    {"signal": "score:Grounding", "operator": "lt", "value": 0.7},
                    {"signal": "Total_Tokens", "operator": "gt", "value": 10},
                ],
                "action": {"mode": "Warn"},
            },
        },
    )

    assert created.status_code == 201, created.text


async def test_import_skips_a_definition_with_an_unknown_signal(admin_client):
    imported = await admin_client.post(
        f"{POLICIES}/import",
        json={"policies": [{"name": "Typo", "rules": rule_body("tool_nmae")}]},
    )

    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert (result["created"], result["skipped"]) == (0, 1)
    assert "tool_nmae" in result["issues"][0]["reason"]


async def test_an_older_policy_with_an_unknown_signal_stays_editable_but_not_activatable(
    admin_client, factory, workspace
):
    legacy = await factory.policy(
        workspace,
        name="Legacy safety",
        status=PolicyStatus.INACTIVE.value,
        rules=rule_body("safety_score", fail_mode="closed"),
    )

    # The console sends the stored body back untouched with every edit.
    edited = await admin_client.patch(
        f"{POLICIES}/{legacy.id}",
        json={"description": "Reviewed.", "rules": legacy.rules},
    )
    assert edited.status_code == 200, edited.text

    activated = await admin_client.post(f"{POLICIES}/{legacy.id}/activate")
    assert activated.status_code == 412, activated.text
    assert activated.json()["error"]["details"]["unknown_signals"] == ["safety_score"]

    # The edit form's Status dropdown is an activation too, and answers the same.
    switched_on = await admin_client.patch(
        f"{POLICIES}/{legacy.id}", json={"status": "Active"}
    )
    assert switched_on.status_code == 412, switched_on.text

    rewritten = await admin_client.patch(
        f"{POLICIES}/{legacy.id}", json={"rules": rule_body("tool_nmae")}
    )
    assert rewritten.status_code == 422, rewritten.text


async def test_a_rule_body_that_omits_fail_mode_is_stored_fail_open(admin_client):
    created = await admin_client.post(
        POLICIES, json={"name": "Tool gate", "rules": rule_body("tool_name")}
    )

    assert created.status_code == 201, created.text
    assert created.json()["rules"]["fail_mode"] == "open"


async def test_the_signal_vocabulary_is_the_one_ingest_resolves(factory, workspace):
    """``KNOWN_SIGNALS`` is a copy; this is what keeps the copy honest."""
    from fulcrum_ops_api.services import ingest

    agent = await factory.agent(workspace)
    resolved = ingest._signals(
        agent,
        entity="trace",
        name="answer",
        span_type=None,
        model=None,
        provider=None,
        usage=None,
        cost=None,
        duration_ms=None,
        tags=(),
        has_error=False,
        scores={},
        thread_id=None,
        content="",
    )

    assert set(resolved) == set(KNOWN_SIGNALS)


# ===========================================================================
# n=15 — the Enforcement dropdown and rules.action.mode are one setting
# ===========================================================================

CARD = {"question": "my card is 4111 1111 1111 1111"}


async def test_switching_enforcement_to_log_only_stops_the_policy_blocking(
    admin_client, ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    created = await admin_client.post(
        POLICIES,
        json={"name": "No card numbers", "enforcement": "Block", "rules": rule_body()},
    )
    assert created.status_code == 201, created.text
    policy = created.json()

    blocked = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(blocked) == [(0, "blocked", RejectionCode.POLICY_BLOCKED.value)]

    # The edit modal: the dropdown moved, the rule body came back untouched.
    saved = await admin_client.patch(
        f"{POLICIES}/{policy['id']}",
        json={"enforcement": "Log Only", "rules": policy["rules"]},
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["enforcement"] == "Log Only"
    assert body["rules"]["action"]["mode"] == "Log Only", "the body ingest reads moved too"
    assert body["version"] == "v1.1.0", "what is enforced changed, so the version did"

    allowed = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(allowed) == [(0, "accepted", None)]

    stored = await db.get(Policy, policy["id"])
    assert stored.enforcement == stored.rules["action"]["mode"] == "Log Only"


async def test_the_dropdown_alone_drives_the_rule_body(admin_client, db):
    created = await admin_client.post(
        POLICIES,
        json={"name": "Warn first", "enforcement": "Warn", "rules": rule_body(mode="Warn")},
    )
    assert created.status_code == 201, created.text

    saved = await admin_client.patch(
        f"{POLICIES}/{created.json()['id']}", json={"enforcement": "Block"}
    )

    assert saved.status_code == 200, saved.text
    stored = await db.get(Policy, created.json()["id"])
    assert stored.enforcement == stored.rules["action"]["mode"] == "Block"


async def test_a_new_mode_in_the_rule_body_moves_the_badge(admin_client):
    created = await admin_client.post(
        POLICIES, json={"name": "No card numbers", "enforcement": "Block", "rules": rule_body()}
    )
    policy = created.json()

    # The dropdown was left alone; the JSON was edited.
    saved = await admin_client.patch(
        f"{POLICIES}/{policy['id']}",
        json={"enforcement": "Block", "rules": rule_body(mode="Mask")},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["enforcement"] == "Mask"
    assert saved.json()["rules"]["action"]["mode"] == "Mask"


async def test_an_edit_that_moves_the_two_apart_is_refused(admin_client):
    created = await admin_client.post(
        POLICIES, json={"name": "No card numbers", "enforcement": "Block", "rules": rule_body()}
    )

    refused = await admin_client.patch(
        f"{POLICIES}/{created.json()['id']}",
        json={"enforcement": "Warn", "rules": rule_body(mode="Mask")},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["field"] == "enforcement"


async def test_create_refuses_an_enforcement_that_contradicts_its_rule_body(admin_client):
    refused = await admin_client.post(
        POLICIES,
        json={"name": "Mixed up", "enforcement": "Log Only", "rules": rule_body(mode="Block")},
    )
    assert refused.status_code == 422, refused.text

    # Left unsaid, the field takes the mode the body names rather than "Block".
    adopted = await admin_client.post(
        POLICIES, json={"name": "Mask only", "rules": rule_body(mode="Mask")}
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["enforcement"] == "Mask"


async def test_import_reports_a_definition_whose_enforcement_contradicts_its_body(
    admin_client, db, workspace
):
    imported = await admin_client.post(
        f"{POLICIES}/import",
        json={
            "policies": [
                {"name": "Mixed up", "enforcement": "Log Only", "rules": rule_body(mode="Block")},
                {"name": "Mask only", "rules": rule_body(mode="Mask")},
            ]
        },
    )

    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert (result["created"], result["skipped"]) == (1, 1)
    assert result["issues"][0]["index"] == 0

    rows = await db.scalars(select(Policy).where(Policy.workspace_id == workspace.id))
    assert [(row.name, row.enforcement) for row in rows] == [("Mask only", "Mask")]


async def test_an_older_row_shows_what_it_enforces_and_can_be_corrected(
    admin_client, ingest_client, db, factory, workspace, engine
):
    """The state the bug left behind: a "Log Only" label on a policy that blocks."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    drifted = await factory.policy(
        workspace,
        name="Drifted",
        enforcement=PolicyEnforcement.LOG_ONLY.value,
        rules=rule_body(mode="Block"),
    )

    shown = await admin_client.get(f"{POLICIES}/{drifted.id}")
    assert shown.json()["enforcement"] == "Block", "the badge tells the truth"

    # Seeing Block, the reviewer picks Log Only — which is what the column
    # already said, and must still count as a change.
    saved = await admin_client.patch(
        f"{POLICIES}/{drifted.id}",
        json={"enforcement": "Log Only", "rules": shown.json()["rules"]},
    )
    assert saved.status_code == 200, saved.text

    stored = await db.get(Policy, drifted.id)
    assert stored.enforcement == stored.rules["action"]["mode"] == "Log Only"
    allowed = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(allowed) == [(0, "accepted", None)]


# ===========================================================================
# n=203 — every header of the console's policy table can be sorted on
# ===========================================================================

#: The ``key`` of each column the Policy Center table declares.
CONSOLE_COLUMNS = (
    "name",
    "category",
    "scope",
    "risk_level",
    "status",
    "enforcement",
    "version",
    "violations_30d",
    "updated_at",
)


async def test_every_column_header_of_the_policy_table_sorts(admin_client, factory, workspace):
    await factory.policy(workspace, name="Older", version="v1.0.0", rules=rule_body())
    await factory.policy(workspace, name="Newer", version="v2.3.0", rules=rule_body())

    for key in CONSOLE_COLUMNS:
        for sort in (key, f"-{key}"):
            listed = await admin_client.get(POLICIES, params={"sort": sort})
            assert listed.status_code == 200, f"sort={sort}: {listed.text}"

    by_version = await admin_client.get(POLICIES, params={"sort": "-version"})
    assert [row["name"] for row in by_version.json()["items"]] == ["Newer", "Older"]

    exported = await admin_client.get(f"{POLICIES}/export", params={"sort": "version"})
    assert exported.status_code == 200, exported.text


# ===========================================================================
# n=204 — a console edit does not reset a chosen scope label
# ===========================================================================


async def test_an_unrelated_edit_keeps_a_custom_scope_label(admin_client, factory, workspace):
    policy = await factory.policy(
        workspace, name="Finance only", scope_label="Finance Agents", rules=rule_body()
    )

    # readPolicyForm always sends scope and scope_ref, and never scope_label.
    saved = await admin_client.patch(
        f"{POLICIES}/{policy.id}",
        json={"description": "Typo fixed.", "scope": "Global", "scope_ref": None},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["scope_label"] == "Finance Agents"

    found = await admin_client.get(POLICIES, params={"scope": "Finance Agents"})
    assert [row["id"] for row in found.json()["items"]] == [policy.id]


async def test_a_real_rescope_still_rederives_the_label(admin_client, factory, workspace):
    policy = await factory.policy(
        workspace, name="Finance only", scope_label="Finance Agents", rules=rule_body()
    )

    moved = await admin_client.patch(
        f"{POLICIES}/{policy.id}", json={"scope": "Environment", "scope_ref": "Production"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["scope_label"] == "Production Only"

    # An explicit null is a request for the derived label, not for "no change".
    relabelled = await admin_client.patch(
        f"{POLICIES}/{policy.id}",
        json={"scope": "Environment", "scope_ref": "Production", "scope_label": "Prod fleet"},
    )
    assert relabelled.json()["scope_label"] == "Prod fleet"
    reset = await admin_client.patch(
        f"{POLICIES}/{policy.id}",
        json={"scope": "Environment", "scope_ref": "Production", "scope_label": None},
    )
    assert reset.json()["scope_label"] == "Production Only"


# ===========================================================================
# Delete and the name race — neither may turn into a 500 or an unbounded load
# ===========================================================================


async def test_deleting_a_policy_does_not_load_its_violation_history(
    admin_client, db, db_engine, factory, workspace
):
    agent = await factory.agent(workspace)
    policy = await factory.policy(
        workspace, name="Noisy", status=PolicyStatus.INACTIVE.value, rules=rule_body()
    )
    await factory.add_all(
        [
            PolicyViolation(
                workspace_id=workspace.id,
                policy_id=policy.id,
                agent_id=agent.id,
                detail={"matched": ["content"]},
            )
            for _ in range(5)
        ]
    )
    await factory.add(
        PolicyBinding(workspace_id=workspace.id, policy_id=policy.id, agent_id=agent.id)
    )
    approval = await factory.approval(workspace, policy_id=policy.id)

    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        deleted = await admin_client.delete(f"{POLICIES}/{policy.id}")
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    assert deleted.status_code == 204, deleted.text
    assert await db.get(Policy, policy.id) is None
    assert await db.count(PolicyViolation) == 0
    assert await db.count(PolicyBinding) == 0
    assert (await db.get(ApprovalRequest, approval.id)).policy_id is None

    # The history is counted for the audit row, and that is all: no statement
    # reads the violation rows back, and none deletes them one id at a time.
    assert not [s for s in statements if "policy_violations.detail" in s]
    assert not [s for s in statements if s.startswith("DELETE FROM policy_violations")]


async def test_two_admins_saving_the_same_name_at_once_get_a_conflict_not_a_500(
    admin_client, db
):
    body = {"name": "Same name", "rules": rule_body()}

    first, second = await asyncio.gather(
        admin_client.post(POLICIES, json=body), admin_client.post(POLICIES, json=body)
    )

    assert sorted([first.status_code, second.status_code]) == [201, 409], (
        first.text,
        second.text,
    )
    loser = first if first.status_code == 409 else second
    assert error_code(loser) == "conflict"
    assert await db.count(Policy) == 1


# ===========================================================================
# n=102 — the 30-day rollups and the reach counters are actually computed
# ===========================================================================


def _violation(workspace, policy, *, action: str, days_ago: float = 1.0) -> PolicyViolation:
    return PolicyViolation(
        workspace_id=workspace.id,
        policy_id=policy.id,
        action_taken=action,
        occurred_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
    )


async def test_the_rollup_refresh_fills_the_columns_the_table_sorts_and_shows(
    admin_client, db, factory, workspace, other_workspace
):
    from fulcrum_ops_api.services import policies as service

    noisy = await factory.policy(workspace, name="Noisy", rules=rule_body())
    await factory.policy(workspace, name="Quiet", rules=rule_body())
    foreign = await factory.policy(other_workspace, name="Elsewhere", rules=rule_body())
    await factory.add_all(
        [
            _violation(workspace, noisy, action="Block"),
            _violation(workspace, noisy, action="Block"),
            _violation(workspace, noisy, action="Warn"),
            _violation(workspace, noisy, action="Block", days_ago=45),  # outside the window
            _violation(other_workspace, foreign, action="Block"),
        ]
    )
    for status in (ApprovalStatus.APPROVED, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        await factory.approval(workspace, policy_id=noisy.id, status=status.value)
    # Registered after both policies were saved, the way ingest registers one.
    await factory.agent(workspace)
    await factory.agent(workspace, environment="Staging")

    before = await admin_client.get(f"{POLICIES}/{noisy.id}")
    assert (before.json()["violations_30d"], before.json()["applies_agents"]) == (0, 0)

    async with db.session() as session:
        refreshed = await service.refresh_rollups(session, workspace_id=workspace.id)
        await session.commit()
    assert refreshed == 2, "one workspace's policies, not the neighbour's"

    listed = await admin_client.get(POLICIES, params={"sort": "-violations_30d"})
    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json()["items"]}
    assert [row["name"] for row in listed.json()["items"]] == ["Noisy", "Quiet"]
    assert (
        rows["Noisy"]["violations_30d"],
        rows["Noisy"]["blocked_30d"],
        rows["Noisy"]["requests_30d"],
        rows["Noisy"]["approved_pct"],
    ) == (3, 2, 3, 67)
    assert (rows["Quiet"]["violations_30d"], rows["Quiet"]["approved_pct"]) == (0, 0)
    assert (rows["Noisy"]["applies_agents"], rows["Noisy"]["applies_envs"]) == (2, 2)

    untouched = await db.get(Policy, foreign.id)
    assert untouched.violations_30d == 0

    # A counter moving is not an edit: the token an open edit form holds is good.
    assert rows["Noisy"]["updated_at"] == before.json()["updated_at"]
    saved = await admin_client.patch(
        f"{POLICIES}/{noisy.id}",
        json={"description": "Still mine.", "expected_updated_at": before.json()["updated_at"]},
    )
    assert saved.status_code == 200, saved.text


async def test_the_rollup_refresh_covers_every_workspace_when_asked_to(
    db, factory, workspace, other_workspace
):
    from fulcrum_ops_api.services import policies as service

    mine = await factory.policy(workspace, name="Mine", rules=rule_body())
    theirs = await factory.policy(other_workspace, name="Theirs", rules=rule_body())
    await factory.add_all(
        [
            _violation(workspace, mine, action="Block"),
            _violation(other_workspace, theirs, action="Warn"),
        ]
    )

    async with db.session() as session:
        assert await service.refresh_rollups(session) == 2
        await session.commit()

    assert (await db.get(Policy, mine.id)).blocked_30d == 1
    stored = await db.get(Policy, theirs.id)
    assert (stored.violations_30d, stored.blocked_30d) == (1, 0)


# ===========================================================================
# n=103 — a policy can be bound to an agent, which ingest has always honoured
# ===========================================================================


async def test_binding_a_policy_to_an_agent_is_what_makes_ingest_apply_it(
    admin_client, ingest_client, db, factory, workspace, engine
):
    bound_agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.provisioned_agent(workspace, engine, name="Billing Bot")
    elsewhere = await factory.agent(workspace, name="Scoped Target")
    # Scoped to a different agent, so by scope alone it reaches neither bot.
    created = await admin_client.post(
        POLICIES,
        json={
            "name": "No card numbers",
            "scope": "Agent",
            "scope_ref": elsewhere.id,
            "rules": rule_body(),
        },
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    assert created.json()["applies_agents"] == 1

    unbound = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(unbound) == [(0, "accepted", None)]

    bound = await admin_client.post(f"{POLICIES}/{policy_id}/bindings/{bound_agent.id}")
    assert bound.status_code == 200, bound.text
    body = bound.json()
    assert (body["bound_agents"], body["agents_affected"]) == (1, 2)
    assert body["policy"]["applies_agents"] == 2, "reach counts the binding as well as the scope"
    assert body["policy"]["updated_at"] == created.json()["updated_at"], "not an edit"

    again = await admin_client.post(f"{POLICIES}/{policy_id}/bindings/{bound_agent.id}")
    assert again.status_code == 200, again.text
    assert again.json()["bound_agents"] == 1
    assert await db.count(PolicyBinding) == 1

    listed = await admin_client.get(f"{POLICIES}/{policy_id}/bindings")
    assert listed.status_code == 200, listed.text
    assert [(row["agent_id"], row["agent_name"]) for row in listed.json()] == [
        (bound_agent.id, "Support Bot")
    ]
    assert listed.json()[0]["bound_by"] == "admin@northwind.test"

    blocked = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(blocked) == [(0, "blocked", RejectionCode.POLICY_BLOCKED.value)]
    other = await _post_one_trace(ingest_client, "Billing Bot", input=CARD)
    assert outcomes(other) == [(0, "accepted", None)]

    removed = await admin_client.delete(f"{POLICIES}/{policy_id}/bindings/{bound_agent.id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["bound_agents"] == 0
    assert removed.json()["policy"]["applies_agents"] == 1
    assert (
        await admin_client.delete(f"{POLICIES}/{policy_id}/bindings/{bound_agent.id}")
    ).status_code == 200

    released = await _post_one_trace(ingest_client, "Support Bot", input=CARD)
    assert outcomes(released) == [(0, "accepted", None)]


async def test_bindings_are_admin_only_and_stay_inside_the_workspace(
    admin_client, as_role, db, factory, workspace, other_workspace
):
    from conftest import Role

    policy = await factory.policy(workspace, name="Mine", rules=rule_body())
    agent = await factory.agent(workspace)
    foreign_agent = await factory.agent(other_workspace)
    foreign_policy = await factory.policy(other_workspace, name="Theirs", rules=rule_body())

    async with as_role(Role.OPERATOR) as operator_http:
        refused = await operator_http.post(f"{POLICIES}/{policy.id}/bindings/{agent.id}")
    assert refused.status_code == 403, refused.text

    for policy_id, agent_id in ((policy.id, foreign_agent.id), (foreign_policy.id, agent.id)):
        missing = await admin_client.post(f"{POLICIES}/{policy_id}/bindings/{agent_id}")
        assert missing.status_code == 404, missing.text
    assert (
        await admin_client.get(f"{POLICIES}/{foreign_policy.id}/bindings")
    ).status_code == 404

    assert await db.count(PolicyBinding) == 0


async def test_the_rollup_refresh_counts_bound_agents_in_a_policys_reach(
    admin_client, db, factory, workspace
):
    from fulcrum_ops_api.services import policies as service

    target = await factory.agent(workspace, name="Scoped Target")
    extra = await factory.agent(workspace, name="Bound Extra", environment="Staging")
    policy = await factory.policy(
        workspace, name="Two ways in", scope="Agent", scope_ref=target.id, rules=rule_body()
    )
    bound = await admin_client.post(f"{POLICIES}/{policy.id}/bindings/{extra.id}")
    assert bound.status_code == 200, bound.text

    async with db.session() as session:
        await service.refresh_rollups(session)
        await session.commit()

    stored = await db.get(Policy, policy.id)
    assert (stored.applies_agents, stored.applies_envs) == (2, 2)
