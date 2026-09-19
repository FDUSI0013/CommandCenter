"""Hand-offs other domains left for the ingest path after the 2026-09-18 audit.

Each of these was a fix that shipped in somebody else's module and could not
work until ingest did its half: a meter nothing fed, a scrub the SDK route went
round, a default two modules read differently. They go in through the HTTP
surface an SDK uses and are checked where the console reads: in the rows, or in
the engine double.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import event, select

from fulcrum_ops_api.db.base import new_id
from fulcrum_ops_api.models.governance import PolicyCategory
from fulcrum_ops_api.models.licensing import UsageRecord
from fulcrum_ops_api.models.quality import FeedbackItem
from fulcrum_ops_api.models.registry import Agent

NOW = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)

TRACE_ID = "01932c1e-7a40-7c55-9f6b-3d2a1b0c9e8f"


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


def span(**overrides) -> dict:
    item = {"name": "step", "start_time": iso(), "end_time": iso(0.5)}
    item.update(overrides)
    return item


def outcomes(response) -> list[tuple[int, str, str | None]]:
    return [
        (row["index"], row["outcome"], row["code"]) for row in response.json()["results"]
    ]


# ===========================================================================
# licensing #50 -- the licence's meter is fed by what ingest accepted
# ===========================================================================


async def usage_of(db, license_id: str) -> list[tuple[str, int]]:
    rows = await db.scalars(select(UsageRecord).where(UsageRecord.license_id == license_id))
    return sorted((row.metric, int(row.quantity)) for row in rows)


async def test_an_accepted_trace_batch_is_metered_against_the_licence(
    ingest_client, owner_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    held = await factory.license(workspace)
    # The second trace is refused by governance, so it is neither a run nor tokens.
    await factory.policy(
        workspace,
        name="No refunds by bot",
        rules={"conditions": [{"signal": "name", "operator": "eq", "value": "issue refund"}]},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    spans=[
                        span(
                            name="chat",
                            type="llm",
                            model="gpt-4o",
                            usage={"prompt_tokens": 100, "completion_tokens": 20},
                        )
                    ]
                ),
                trace(
                    name="issue refund",
                    spans=[span(name="chat", type="llm", usage={"total_tokens": 9_000})],
                ),
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert outcomes(posted) == [(0, "accepted", None), (1, "blocked", "policy_blocked")]
    assert await usage_of(db, held.id) == [("runs", 1), ("tokens", 120)]

    response = await owner_client.get(f"/api/v1/licensing/tenants/{held.id}/entitlements")
    assert response.status_code == 200, response.text
    usage = {row["metric"]: row["used"] for row in response.json()["usage"]}
    assert (usage["tokens"], usage["runs"]) == (120, 1)


async def test_spans_posted_on_their_own_meter_tokens_and_no_run(
    ingest_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    held = await factory.license(workspace)

    posted = await ingest_client.post(
        "/api/v1/ingest/spans",
        json={
            "agent": "Support Bot",
            "spans": [
                span(trace_id=TRACE_ID, name="chat", type="llm", usage={"total_tokens": 75}),
                span(trace_id=TRACE_ID, name="search", type="tool"),
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    # The run is counted where its trace is reported, not once per span batch.
    assert await usage_of(db, held.id) == [("tokens", 75)]


async def test_a_workspace_without_a_licence_still_ingests(
    ingest_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    assert outcomes(posted) == [(0, "accepted", None)]
    assert engine.trace_count(agent.engine_project_name) == 1
    assert await db.count(UsageRecord) == 0


# ===========================================================================
# feedback #152 -- the PII switch governs a comment whichever door it came in by
# ===========================================================================

PII_COMMENT = (
    "Wrong total. My SSN is 123-45-6789, card 4111 1111 1111 1111, "
    "reach me at jane.doe@example.com or (415) 555-0134"
)
PII_VALUES = ("123-45-6789", "4111 1111 1111 1111", "jane.doe@example.com", "555-0134")
SCORES_PATH = "/traces/feedback-scores"


def feedback(trace_id: str, **overrides) -> dict:
    item = {
        "kind": "feedback.submitted",
        "ref": "fb-1",
        "trace_id": trace_id,
        "rating": 1,
        "body": PII_COMMENT,
    }
    item.update(overrides)
    return item


async def reported_run(ingest_client) -> str:
    trace_id = new_id()
    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace(id=trace_id)]}
    )
    assert posted.status_code == 200, posted.text
    return trace_id


async def test_a_comment_an_sdk_reports_is_scrubbed_in_the_row_and_in_the_mirror(
    ingest_client, factory, workspace, engine, db
):
    """'PII scrubbing: Enabled' is the default, and this route went round it."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = await reported_run(ingest_client)

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "agent": "Support Bot",
            # A reporter cannot state what was scrubbed; only the scrub can.
            "events": [feedback(trace_id, detail={"pii_scrubbed": ["nothing"], "page": "cart"})],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 1
    (item,) = await db.scalars(select(FeedbackItem))
    reason = engine.calls_to(SCORES_PATH)[-1].body["scores"][0]["reason"]
    assert item.body.startswith("Wrong total."), "only the identifiers go"
    for value in PII_VALUES:
        assert value not in item.body
        assert value not in reason, "the mirror copies the comment into the telemetry store"
        assert value not in str(item.event_metadata), "the kinds are kept, never the values"
    assert item.event_metadata == {
        "page": "cart",
        "pii_scrubbed": ["email", "ssn", "card", "phone"],
    }


async def test_a_reported_comment_is_kept_as_written_when_the_switch_is_off(
    ingest_client, admin_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    saved = await admin_client.put("/api/v1/feedback/settings", json={"pii_scrubbing": False})
    assert saved.status_code == 200, saved.text

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "agent": "Support Bot",
            "events": [feedback(new_id(), detail={"pii_scrubbed": ["email"]})],
        },
    )

    assert posted.status_code == 200, posted.text
    (item,) = await db.scalars(select(FeedbackItem))
    assert item.body == PII_COMMENT
    assert "pii_scrubbed" not in item.event_metadata, "nothing was scrubbed, whoever says so"


# ===========================================================================
# approvals #18 -- the audit chain is not held across the hand-off to the store
# ===========================================================================


async def test_an_auto_registration_is_audited_after_the_store_has_the_batch(
    registrar_client, admin_client, engine, db_engine
):
    """An audit row keeps the workspace's turn in the chain until the commit.

    Written at registration, it kept that turn for as long as the telemetry
    store took -- and everybody else auditing in the workspace waited on it.
    """
    store_calls_when_audited: list[int] = []

    def watch(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_EVENTS") and (
            "agent.auto_registered" in str(parameters)
        ):
            store_calls_when_audited.append(len(engine.calls_to("/traces/batch")))

    event.listen(db_engine.sync_engine, "before_cursor_execute", watch)
    try:
        posted = await registrar_client.post(
            "/api/v1/ingest/traces",
            json={"agent": "Brand New Service", "traces": [trace()]},
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", watch)

    assert posted.status_code == 200, posted.text
    assert [row["name"] for row in posted.json()["auto_registered"]] == ["Brand New Service"]
    assert store_calls_when_audited == [1], "audited once, and only once the store was done"

    trail = (await admin_client.get("/api/v1/audit")).json()["items"]
    (row,) = [item for item in trail if item["action"] == "agent.auto_registered"]
    assert row["entity_label"] == "Brand New Service"


async def test_an_agent_registered_by_a_score_batch_is_still_audited(
    registrar_client, admin_client
):
    posted = await registrar_client.post(
        "/api/v1/ingest/scores",
        json={
            "agent": "Scoring Service",
            "scores": [{"target": "thread", "id": "thread-1", "name": "helpfulness", "value": 1}],
        },
    )

    assert posted.status_code == 200, posted.text
    assert [row["name"] for row in posted.json()["auto_registered"]] == ["Scoring Service"]
    trail = (await admin_client.get("/api/v1/audit")).json()["items"]
    assert "agent.auto_registered" in [item["action"] for item in trail]


# ===========================================================================
# sdk-typescript #191 -- the provenance that is checked is the one that is sent
# ===========================================================================


async def test_a_score_source_reaches_the_store_in_the_spelling_it_accepts(
    ingest_client, factory, workspace, engine
):
    """'UI' passed a case-blind check and was then forwarded exactly as written."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = new_id()

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    id=trace_id,
                    feedback_scores=[
                        {"name": "reviewed", "value": 1, "source": "UI"},
                        {"name": "scored", "value": 1, "source": " Online_Scoring "},
                        {"name": "judged", "value": 1, "source": "Judge"},
                    ],
                )
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["scores_accepted"] == 3
    sent = engine.calls_to(SCORES_PATH)[-1].body["scores"]
    assert {score["name"]: score["source"] for score in sent} == {
        "reviewed": "ui",
        "scored": "online_scoring",
        "judged": "sdk",
    }


# ===========================================================================
# policies #0 -- a rule body that does not state a fail mode fails open
# ===========================================================================


def undecidable(**extra) -> dict:
    """A clause about a signal every trace carries, that cannot be decided."""
    body = {"conditions": [{"signal": "name", "operator": "matches", "value": "("}]}
    body.update(extra)
    return body


async def test_a_body_without_a_fail_mode_fails_open_as_the_rule_schema_reads_it(
    ingest_client, factory, workspace, engine
):
    """``PolicyRules`` reads a missing fail mode as open; ingest read it as closed."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Unstated", rules=undecidable())

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    assert outcomes(posted) == [(0, "accepted", None)]
    assert posted.json()["violations_recorded"] == 0


async def test_a_body_that_states_fail_closed_still_fails_closed(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Stated", rules=undecidable(fail_mode="closed"))

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    assert outcomes(posted) == [(0, "blocked", "policy_blocked")]


# ===========================================================================
# sdk-python #225 -- the envelope says which environment the reporter runs in
# ===========================================================================


async def test_an_agent_first_seen_in_a_score_batch_is_filed_where_its_envelope_says(
    registrar_client, db
):
    """A score has no metadata to state an environment in; the envelope does."""
    posted = await registrar_client.post(
        "/api/v1/ingest/scores",
        json={
            "agent": "Scoring Service",
            "environment": " production ",
            "scores": [{"target": "thread", "id": "thread-1", "name": "helpfulness", "value": 1}],
        },
    )

    assert posted.status_code == 200, posted.text
    assert [row["environment"] for row in posted.json()["auto_registered"]] == ["Production"]
    stored = await db.scalar(select(Agent).where(Agent.name == "Scoring Service"))
    assert stored.environment == "Production"


async def test_what_a_trace_says_about_itself_outranks_its_envelope(registrar_client):
    posted = await registrar_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Brand New Service",
            "environment": "Production",
            "traces": [trace(metadata={"environment": "Staging"})],
        },
    )

    assert posted.status_code == 200, posted.text
    assert [row["environment"] for row in posted.json()["auto_registered"]] == ["Staging"]


async def test_an_envelope_environment_that_cannot_be_one_does_not_cost_the_batch(
    ingest_client, factory, workspace, engine
):
    """The key was ignored until it was declared; declaring it must refuse nothing."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    for unusable in ({"name": "prod"}, "x" * 200, 7):
        posted = await ingest_client.post(
            "/api/v1/ingest/traces",
            json={"agent": "Support Bot", "environment": unusable, "traces": [trace()]},
        )
        assert posted.status_code == 200, posted.text
        assert outcomes(posted) == [(0, "accepted", None)]

    assert engine.trace_count(agent.engine_project_name) == 3


# ===========================================================================
# approvals #108 -- an approval rule is never a control telemetry is judged by
# ===========================================================================


REFUND = {"conditions": [{"signal": "name", "operator": "eq", "value": "issue refund"}]}


async def test_an_approval_rule_cannot_block_a_trace_however_its_body_is_edited(
    ingest_client, factory, workspace, engine
):
    """A rule body carries no conditions -- until someone edits one in as JSON."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(
        workspace,
        name="Refund over threshold",
        category=PolicyCategory.APPROVAL_ESCALATION.value,
        rules={**REFUND, "approvers": ["ops"], "sla_minutes": 60},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(name="issue refund")]},
    )

    assert outcomes(posted) == [(0, "accepted", None)]
    assert posted.json()["violations_recorded"] == 0
    assert engine.trace_count(agent.engine_project_name) == 1


async def test_the_same_body_in_a_guardrail_policy_still_blocks(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="No refunds by bot", rules=REFUND)

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(name="issue refund")]},
    )

    assert outcomes(posted) == [(0, "blocked", "policy_blocked")]
