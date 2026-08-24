"""The machine-facing surface: what an agent reports, and what happens to it.

This is the only endpoint a customer's process talks to, and it has a different
shape from every other route in the tree. A batch is not all-or-nothing: one
unusable row must not cost its neighbours their acceptance, so a partial failure
still answers 200 and the fate of each row lives in ``results[i]``. A non-2xx
means the *request* could not be processed at all — unauthorised, too large,
past a hard quota, outside the plan, or the telemetry store is unreachable.

The tests below follow a batch the whole way: in through ``/ingest/traces``,
into the engine double, and back out of ``/runs`` as a row an operator can read.
Then the refusals, one per reason, because a rejection code an SDK cannot branch
on is no better than a 500.

``/v1/traces`` — the OpenTelemetry receiver — is covered in both encodings the
specification defines, including a protobuf body encoded here by hand, because
an OTel-instrumented agent has to be governed identically to one using our SDK.
"""

from __future__ import annotations

import datetime as dt
import struct
import uuid

import pytest
from sqlalchemy import select

from conftest import error_code
from fulcrum_ops_api.models.governance import PolicyEnforcement, PolicyViolation
from fulcrum_ops_api.models.operations import (
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
    QuotaResource,
)
from fulcrum_ops_api.models.quality import FeedbackItem, FeedbackSource, GuardrailEvent, Sentiment
from fulcrum_ops_api.models.registry import Agent, AgentStatus
from fulcrum_ops_api.schemas.ingest import RejectionCode

#: Runs are read through a Time Range dropdown whose widest setting is the last
#: 24 hours, so telemetry a test wants to see again has to be recent.
NOW = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)


def iso(offset_seconds: float = 0.0) -> str:
    return (NOW + dt.timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def trace(**overrides) -> dict:
    """One well-formed trace item, overridable field by field."""
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


# ===========================================================================
# A batch, all the way through
# ===========================================================================


async def test_a_batch_lands_in_the_store_and_comes_back_out_of_runs(
    ingest_client, admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "sdk": "python",
            "sdk_version": "1.2.0",
            "traces": [trace(), trace(name="check order status")],
        },
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert (body["received"], body["accepted"], body["rejected"], body["blocked"]) == (
        2,
        2,
        0,
        0,
    )
    assert body["agents"] == [agent.id]
    assert engine.trace_count(agent.engine_project_name) == 2

    runs = await admin_client.get("/api/v1/runs")
    assert runs.status_code == 200, runs.text
    assert runs.json()["total"] == 2
    names = {row["agent"] for row in runs.json()["items"]}
    assert names == {"Support Bot"}
    previews = {row["input_preview"] for row in runs.json()["items"]}
    assert any("Where is my order?" in preview for preview in previews)


async def test_the_id_the_store_kept_is_the_id_the_caller_is_told(
    ingest_client, admin_client, factory, workspace, engine
):
    """The SDK correlates on this id, and so does the run inspector."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(id=trace_id)]},
    )

    assert posted.json()["results"][0]["id"] == trace_id
    detail = await admin_client.get(f"/api/v1/runs/{trace_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == trace_id


async def test_reporting_the_same_trace_id_twice_does_not_double_count(
    ingest_client, factory, workspace, engine
):
    """A network timeout must not turn one run into two."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())
    batch = {"agent": "Support Bot", "traces": [trace(id=trace_id)]}

    first = await ingest_client.post("/api/v1/ingest/traces", json=batch)
    second = await ingest_client.post("/api/v1/ingest/traces", json=batch)

    assert first.json()["accepted"] == 1
    assert second.json()["accepted"] == 1
    assert engine.trace_count(agent.engine_project_name) == 1


async def test_a_traces_nested_spans_are_stored_with_it(
    ingest_client, admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())
    span_id = str(uuid.uuid4())

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    id=trace_id,
                    spans=[
                        {
                            "id": span_id,
                            "name": "chat completion",
                            "type": "llm",
                            "start_time": iso(),
                            "end_time": iso(1.0),
                            "model": "gpt-4o",
                            "provider": "azure-openai",
                            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                            "total_estimated_cost": 0.0042,
                        }
                    ],
                )
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["spans_accepted"] == 1
    assert body["results"][0]["spans"] == 1
    assert len(engine.spans) == 1

    tree = await admin_client.get(f"/api/v1/runs/{trace_id}/trace")
    assert tree.status_code == 200, tree.text
    assert tree.text.count("chat completion") >= 1


async def test_spans_may_be_reported_separately_from_their_trace(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())
    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(id=trace_id)]},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/spans",
        json={
            "agent": "Support Bot",
            "spans": [
                {
                    "trace_id": trace_id,
                    "name": "retrieve documents",
                    "type": "tool",
                    "start_time": iso(),
                    "end_time": iso(0.4),
                }
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["accepted"] == 1
    assert len(engine.spans) == 1


async def test_a_run_records_the_model_its_own_spans_named(
    ingest_client, factory, workspace, engine
):
    """The model is measured from the spans, not taken from the registration.

    An agent registered without a model — or registered with one and calling
    another — must still report what actually answered, so the console shows a
    measurement rather than a configuration value or a dash.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot", model=None)
    trace_id = str(uuid.uuid4())

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    **trace(id=trace_id),
                    "spans": [
                        {
                            "name": "plan",
                            "type": "llm",
                            "model": "gpt-4o-mini",
                            "start_time": iso(),
                            "end_time": iso(0.2),
                        },
                        {
                            "name": "lookup",
                            "type": "tool",
                            "start_time": iso(0.2),
                            "end_time": iso(0.3),
                        },
                        {
                            "name": "answer",
                            "type": "llm",
                            "model": "gpt-5",
                            "start_time": iso(0.3),
                            "end_time": iso(0.9),
                        },
                    ],
                }
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    stored = engine.traces[trace_id]
    # The last model to speak is the one that produced the answer.
    assert stored["metadata"]["model"] == "gpt-5"


async def test_a_reported_model_is_never_overwritten_by_a_derived_one(
    ingest_client, factory, workspace, engine
):
    """An explicit statement from the reporter outranks our inference."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot", model=None)
    trace_id = str(uuid.uuid4())

    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    **trace(id=trace_id),
                    "metadata": {"model": "declared-by-the-agent"},
                    "spans": [
                        {
                            "name": "answer",
                            "type": "llm",
                            "model": "gpt-5",
                            "start_time": iso(),
                            "end_time": iso(0.5),
                        }
                    ],
                }
            ],
        },
    )

    assert engine.traces[trace_id]["metadata"]["model"] == "declared-by-the-agent"


async def test_scores_may_arrive_after_the_run_they_describe(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = str(uuid.uuid4())
    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(id=trace_id)]},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/scores",
        json={
            "agent": "Support Bot",
            "scores": [
                {"id": trace_id, "target": "trace", "name": "helpfulness", "value": 0.9}
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["accepted"] == 1
    assert engine.traces[trace_id]["feedback_scores"]


async def test_the_sdk_configuration_document_is_cacheable(ingest_client):
    """Every agent process fetches this on boot; a fleet restart must be cheap."""
    first = await ingest_client.get("/api/v1/ingest/config")

    assert first.status_code == 200, first.text
    etag = first.headers["etag"]
    assert first.headers["cache-control"].startswith("private, max-age=")

    again = await ingest_client.get(
        "/api/v1/ingest/config", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304


# ===========================================================================
# Per-item rejection reasons
# ===========================================================================


async def test_one_bad_row_does_not_cost_the_others_their_acceptance(
    ingest_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(name="good one"),
                {"name": "no start time at all"},
                trace(name="another good one"),
            ],
        },
    )

    assert posted.status_code == 200, "a partial failure is still a processed request"
    body = posted.json()
    assert (body["received"], body["accepted"], body["rejected"]) == (3, 2, 1)
    assert outcomes(posted) == [
        (0, "accepted", None),
        (1, "rejected", RejectionCode.MALFORMED.value),
        (2, "accepted", None),
    ]
    assert "start_time" in body["results"][1]["reason"]
    assert engine.trace_count(agent.engine_project_name) == 2


async def test_a_trace_naming_an_unknown_agent_is_rejected_on_its_own_row(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "traces": [
                trace(agent="Support Bot"),
                trace(agent="Ghost Bot"),
            ]
        },
    )

    assert posted.status_code == 200, posted.text
    assert outcomes(posted) == [
        (0, "accepted", None),
        (1, "rejected", RejectionCode.UNKNOWN_AGENT.value),
    ]
    assert "Ghost Bot" in posted.json()["results"][1]["reason"]


async def test_a_trace_naming_no_agent_at_all_says_so(ingest_client, factory, workspace, engine):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post("/api/v1/ingest/traces", json={"traces": [trace()]})

    assert outcomes(posted) == [(0, "rejected", RejectionCode.UNKNOWN_AGENT.value)]
    assert "names no agent" in posted.json()["results"][0]["reason"]


async def test_an_id_the_store_cannot_address_is_rejected_before_the_hand_off(
    ingest_client, factory, workspace, engine
):
    """Sending it on would take the whole batch down at the far end."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(id="not-a-uuid"), trace(name="fine")],
        },
    )

    assert outcomes(posted) == [
        (0, "rejected", RejectionCode.INVALID_ID.value),
        (1, "accepted", None),
    ]
    assert "UUID" in posted.json()["results"][0]["reason"]
    assert engine.trace_count(agent.engine_project_name) == 1


async def test_an_agent_with_no_telemetry_project_is_rejected_with_its_own_reason(
    ingest_client, factory, workspace
):
    await factory.agent(workspace, name="Unprovisioned Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Unprovisioned Bot", "traces": [trace()]},
    )

    assert outcomes(posted) == [(0, "rejected", RejectionCode.AGENT_UNPROVISIONED.value)]


async def test_a_bound_key_may_not_report_for_another_agent(
    app, factory, workspace, engine
):
    import httpx

    from conftest import APP_BASE_URL

    mine = await factory.provisioned_agent(workspace, engine, name="Mine")
    await factory.provisioned_agent(workspace, engine, name="Theirs")
    token, _row = await factory.api_key(
        workspace, name="Bound key", scopes=["ingest"], agent_id=mine.id
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        posted = await http.post(
            "/api/v1/ingest/traces",
            json={"traces": [trace(agent="Mine"), trace(agent="Theirs")]},
        )

    assert posted.status_code == 200, posted.text
    assert outcomes(posted) == [
        (0, "accepted", None),
        (1, "rejected", RejectionCode.AGENT_NOT_PERMITTED.value),
    ]
    assert "Mine" in posted.json()["results"][1]["reason"]


async def test_a_span_posted_on_its_own_must_name_its_trace(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/spans",
        json={
            "agent": "Support Bot",
            "spans": [{"name": "orphan", "start_time": iso()}],
        },
    )

    assert outcomes(posted) == [(0, "rejected", RejectionCode.MISSING_TRACE_ID.value)]


async def test_a_policy_set_to_block_refuses_the_item_and_names_the_control(
    ingest_client, db, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policy = await factory.policy(
        workspace,
        name="No card numbers",
        enforcement=PolicyEnforcement.BLOCK.value,
        rules={
            "conditions": [
                {"signal": "content", "operator": "contains", "value": "4111 1111"}
            ],
            "action": {"message": "Card data may not leave the process."},
        },
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(input={"question": "my card is 4111 1111 1111 1111"}),
                trace(name="harmless"),
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert outcomes(posted) == [
        (0, "blocked", RejectionCode.POLICY_BLOCKED.value),
        (1, "accepted", None),
    ]
    assert body["blocked"] == 1
    assert body["results"][0]["policy_id"] == policy.id
    assert body["results"][0]["reason"] == "Card data may not leave the process."
    assert engine.trace_count(agent.engine_project_name) == 1, "the blocked item never left"

    violations = await db.scalars(
        select(PolicyViolation).where(PolicyViolation.workspace_id == workspace.id)
    )
    assert len(violations) == 1
    assert violations[0].policy_id == policy.id


async def test_a_duplicate_feedback_reference_is_reported_as_a_duplicate(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    event = {
        "kind": "feedback.submitted",
        "ref": "FB-90001",
        "agent": "Support Bot",
        "rating": 5,
        "body": "Solved it first time",
    }

    first = await ingest_client.post("/api/v1/ingest/events", json={"events": [event]})
    second = await ingest_client.post("/api/v1/ingest/events", json={"events": [event]})

    assert first.json()["events_recorded"] == 1
    assert outcomes(second) == [(0, "rejected", RejectionCode.DUPLICATE.value)]


async def test_an_event_naming_a_guardrail_that_does_not_exist_is_rejected(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {"kind": "guardrail.triggered", "guardrail": "PII shield", "score": 0.9},
                {"kind": "guardrail.triggered", "guardrail": "Nonexistent", "score": 0.9},
            ]
        },
    )

    assert outcomes(posted) == [
        (0, "accepted", None),
        (1, "rejected", RejectionCode.UNKNOWN_GUARDRAIL.value),
    ]


# ===========================================================================
# Refusals of the request as a whole
# ===========================================================================


@pytest.mark.parametrize(
    ("body", "because"),
    [
        (b"", "empty"),
        (b"not json at all", "unparsable"),
        (b"[1, 2, 3]", "not an object"),
        (b'{"agent": "Support Bot"}', "no traces array"),
        (b'{"traces": "one please"}', "traces is not an array"),
        (b'{"traces": []}', "traces is empty"),
    ],
    ids=["empty", "unparsable", "not-an-object", "no-array", "not-an-array", "empty-array"],
)
async def test_a_malformed_request_is_refused_outright(ingest_client, body, because):
    """These are problems with the request, not with a row inside it."""
    response = await ingest_client.post(
        "/api/v1/ingest/traces", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, f"{because}: {response.text}"
    assert error_code(response) == "validation_failed"


async def test_a_batch_larger_than_the_cap_is_refused_before_it_is_processed(
    ingest_client, factory, workspace, engine
):
    from fulcrum_ops_api.schemas.ingest import MAX_TRACES_PER_BATCH

    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace() for _ in range(MAX_TRACES_PER_BATCH + 1)],
        },
    )

    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["max_items"] == MAX_TRACES_PER_BATCH
    assert details["received"] == MAX_TRACES_PER_BATCH + 1


async def test_a_body_past_the_transport_limit_is_refused_with_413(ingest_client):
    from fulcrum_ops_api.core.config import settings

    oversized = b'{"traces": [' + b"x" * (settings.ingest_max_body_bytes + 1) + b"]}"

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert error_code(response) == "payload_too_large"


# ===========================================================================
# Auto-registration
# ===========================================================================


async def test_a_key_that_may_register_agents_creates_one_on_first_telemetry(
    registrar_client, db, workspace, engine
):
    posted = await registrar_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Brand New Service", "traces": [trace()]},
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["accepted"] == 1
    registered = body["auto_registered"]
    assert len(registered) == 1
    assert registered[0]["name"] == "Brand New Service"
    assert registered[0]["status"] == AgentStatus.PENDING_REVIEW.value, (
        "something is reporting and nobody has governed it yet"
    )
    assert registered[0]["engine_project_name"] in {
        project["name"] for project in engine.projects.values()
    }

    stored = await db.scalar(select(Agent).where(Agent.name == "Brand New Service"))
    assert stored is not None
    assert "auto-registered" in stored.tags


async def test_auto_registration_is_written_to_the_audit_trail(
    registrar_client, admin_client, workspace
):
    await registrar_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Brand New Service", "traces": [trace()]},
    )

    trail = await admin_client.get("/api/v1/audit")

    actions = [row["action"] for row in trail.json()["items"]]
    assert "agent.auto_registered" in actions


async def test_a_key_without_the_registration_scope_gets_a_rejection_instead(
    ingest_client, db, workspace
):
    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Brand New Service", "traces": [trace()]},
    )

    assert outcomes(posted) == [(0, "rejected", RejectionCode.UNKNOWN_AGENT.value)]
    assert posted.json()["auto_registered"] == []
    assert await db.count(Agent) == 0


# ===========================================================================
# Commercial refusals
# ===========================================================================


async def test_a_plan_that_does_not_include_ingest_stops_the_batch_at_the_door(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.license(workspace, entitlements={"ingest.enabled": False})

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace()]},
    )

    assert response.status_code == 402, response.text
    assert error_code(response) == "quota_exceeded"
    assert "ingest.enabled" in response.text
    assert engine.trace_count() == 0


async def test_a_soft_entitlement_warns_rather_than_refuses(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.license(
        workspace, entitlements={"ingest.enabled": False}, hard_limits=False
    )

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace()]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1


async def test_a_hard_quota_refuses_the_batch_rather_than_billing_the_overage(
    ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Requests per month",
        resource=QuotaResource.REQUESTS.value,
        unit="requests",
        limit_value=10,
        used_value=10,
        enforcement=QuotaEnforcement.BLOCK.value,
    )

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace()]},
    )

    assert response.status_code == 402, response.text
    assert error_code(response) == "quota_exceeded"
    details = response.json()["error"]["details"]
    assert details["quota_id"] == quota.id
    assert details["limit_value"] == 10
    assert details["requested"] == 1
    assert engine.trace_count() == 0, "nothing was stored"

    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 10, "a refused batch consumes nothing"


async def test_a_quota_set_to_warn_lets_the_batch_through_and_counts_it(
    ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Requests per month",
        resource=QuotaResource.REQUESTS.value,
        unit="requests",
        limit_value=10,
        used_value=9,
        enforcement=QuotaEnforcement.WARN.value,
    )

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(), trace(name="second")]},
    )

    assert response.status_code == 200, response.text
    reported = response.json()["quotas"]
    assert len(reported) == 1
    assert reported[0]["id"] == quota.id
    assert reported[0]["used_value"] == 11
    assert reported[0]["status"] == LimitStatus.EXCEEDED.value

    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 11


async def test_token_quotas_are_charged_from_the_spans_own_counters(
    ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Tokens per month",
        resource=QuotaResource.TOKENS.value,
        limit_value=1_000,
        used_value=0,
        enforcement=QuotaEnforcement.WARN.value,
    )

    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    spans=[
                        {
                            "name": "chat",
                            "type": "llm",
                            "start_time": iso(),
                            "end_time": iso(1),
                            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                        }
                    ]
                )
            ],
        },
    )

    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 150


async def test_a_quota_scoped_to_another_agent_does_not_apply(
    ingest_client, db, factory, workspace, engine
):
    reporting = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    other = await factory.provisioned_agent(workspace, engine, name="Sales Bot")
    quota = await factory.quota(
        workspace,
        name="Sales requests",
        resource=QuotaResource.REQUESTS.value,
        scope=LimitScope.AGENT.value,
        scope_ref=other.id,
        limit_value=1,
        used_value=1,
        enforcement=QuotaEnforcement.BLOCK.value,
    )

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": reporting.name, "traces": [trace()]},
    )

    assert response.status_code == 200, response.text
    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 1


async def test_an_unreachable_store_never_consumes_a_tenants_allowance(
    ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Requests per month",
        resource=QuotaResource.REQUESTS.value,
        limit_value=1_000,
        used_value=5,
        enforcement=QuotaEnforcement.WARN.value,
    )
    engine.fail(503)

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace()]},
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"
    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 5


# ===========================================================================
# Free-text vocabulary is normalised before it is stored
# ===========================================================================


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("positive", Sentiment.POSITIVE.value),
        ("POSITIVE", Sentiment.POSITIVE.value),
        ("  Negative  ", Sentiment.NEGATIVE.value),
        ("neutral", Sentiment.NEUTRAL.value),
    ],
    ids=["lowercase", "uppercase", "padded", "already-fine"],
)
async def test_a_free_text_sentiment_is_stored_in_the_vocabulary_readers_expect(
    ingest_client, admin_client, db, factory, workspace, engine, sent, stored
):
    """Regression: an SDK writing "positive" made the whole feedback list unreadable.

    The wire field is free text because SDKs in several languages write it and
    they do not agree on case. Every read parses this column back into the
    ``Sentiment`` enum, so a value outside it is not untidy — it takes the
    Feedback screen down with a serialisation error. What is stored has to be
    what the rest of the system can read.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {
                    "kind": "feedback.submitted",
                    "agent": "Support Bot",
                    "ref": "FB-77001",
                    "rating": 4,
                    "sentiment": sent,
                    "body": "Answered my question",
                }
            ]
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 1

    row = await db.scalar(select(FeedbackItem).where(FeedbackItem.feedback_ref == "FB-77001"))
    assert row.sentiment == stored

    # The read path is the half that used to break.
    listed = await admin_client.get("/api/v1/feedback")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["sentiment"] == stored


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("support ticket", FeedbackSource.SUPPORT_TICKET.value),
        ("End User (In-App)", FeedbackSource.END_USER.value),
        ("MANUAL REVIEW", FeedbackSource.MANUAL_REVIEW.value),
    ],
    ids=["lowercase", "exact", "uppercase"],
)
async def test_a_free_text_source_is_stored_in_the_vocabulary_readers_expect(
    ingest_client, admin_client, db, factory, workspace, engine, sent, stored
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {
                    "kind": "feedback.submitted",
                    "agent": "Support Bot",
                    "ref": "FB-77002",
                    "rating": 2,
                    "source": sent,
                    "body": "Wrong answer",
                }
            ]
        },
    )

    row = await db.scalar(select(FeedbackItem).where(FeedbackItem.feedback_ref == "FB-77002"))
    assert row.source == stored

    listed = await admin_client.get("/api/v1/feedback")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["source"] == stored


async def test_an_unrecognisable_sentiment_falls_back_to_the_rating(
    ingest_client, admin_client, db, factory, workspace, engine
):
    """Storing the submitted text verbatim is what broke every reader."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {
                    "kind": "feedback.submitted",
                    "agent": "Support Bot",
                    "ref": "FB-77003",
                    "rating": 1,
                    "sentiment": "furious",
                    "body": "Hopeless",
                }
            ]
        },
    )

    row = await db.scalar(select(FeedbackItem).where(FeedbackItem.feedback_ref == "FB-77003"))
    assert row.sentiment == Sentiment.NEGATIVE.value
    assert (await admin_client.get("/api/v1/feedback")).status_code == 200


async def test_feedback_with_no_sentiment_at_all_is_derived_from_the_rating(
    ingest_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {"kind": "feedback.submitted", "ref": "a", "rating": 5, "body": "Great"},
                {"kind": "feedback.submitted", "ref": "b", "rating": 3, "body": "Fine"},
                {"kind": "feedback.submitted", "ref": "c", "rating": 1, "body": "Bad"},
            ]
        },
    )

    rows = await db.scalars(select(FeedbackItem).order_by(FeedbackItem.feedback_ref))
    assert [row.sentiment for row in rows] == [
        Sentiment.POSITIVE.value,
        Sentiment.NEUTRAL.value,
        Sentiment.NEGATIVE.value,
    ]


async def test_a_guardrail_event_reported_by_the_sdk_lands_on_the_guardrail_screen(
    ingest_client, admin_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(workspace, name="PII shield")

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={
            "events": [
                {
                    "kind": "guardrail.triggered",
                    "agent": "Support Bot",
                    "guardrail": "PII shield",
                    "action_taken": "Mask",
                    "score": 0.92,
                    "sample": "email removed",
                }
            ]
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 1
    events = await db.scalars(
        select(GuardrailEvent).where(GuardrailEvent.guardrail_id == guardrail.id)
    )
    assert len(events) == 1
    assert events[0].action_taken == "Mask"

    listed = await admin_client.get("/api/v1/guardrails/events")
    assert listed.json()["total"] == 1


# ===========================================================================
# OpenTelemetry
# ===========================================================================


def otlp_json_export(
    *, agent: str, trace_id: str, span_id: str, name: str = "chat gpt-4o"
) -> dict:
    start = int(NOW.timestamp() * 1_000_000_000)
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": agent}},
                        {"key": "deployment.environment", "value": {"stringValue": "prod"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "opentelemetry.instrumentation", "version": "0.1"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": name,
                                "kind": 3,
                                "startTimeUnixNano": str(start),
                                "endTimeUnixNano": str(start + 1_500_000_000),
                                "attributes": [
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "gpt-4o"},
                                    },
                                    {
                                        "key": "gen_ai.system",
                                        "value": {"stringValue": "azure-openai"},
                                    },
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": "120"},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "30"},
                                    },
                                    {
                                        "key": "gen_ai.prompt",
                                        "value": {"stringValue": "Where is my order?"},
                                    },
                                ],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


# -- a protobuf encoder, small enough to read -------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _delimited(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _fixed64(field: int, value: int) -> bytes:
    return _tag(field, 1) + struct.pack("<Q", value)


def _string_attribute(key: str, value: str) -> bytes:
    any_value = _delimited(1, value.encode())
    return _delimited(1, key.encode()) + _delimited(2, any_value)


def _int_attribute(key: str, value: int) -> bytes:
    any_value = _tag(3, 0) + _varint(value)
    return _delimited(1, key.encode()) + _delimited(2, any_value)


def otlp_protobuf_export(*, agent: str, trace_id: bytes, span_id: bytes, name: str) -> bytes:
    """Encode one ``ExportTraceServiceRequest`` by hand.

    Field numbers are the ones the specification fixes: ResourceSpans is field 1
    of the request, Resource is field 1 and ScopeSpans field 2 of ResourceSpans,
    Span is field 2 of ScopeSpans, and inside a Span the ids are 1 and 2, the
    name 5, the timestamps 7 and 8 and the attributes 9.
    """
    start = int(NOW.timestamp() * 1_000_000_000)

    span = b"".join(
        [
            _delimited(1, trace_id),
            _delimited(2, span_id),
            _delimited(5, name.encode()),
            _tag(6, 0) + _varint(3),
            _fixed64(7, start),
            _fixed64(8, start + 1_500_000_000),
            _delimited(9, _string_attribute("gen_ai.request.model", "gpt-4o")),
            _delimited(9, _int_attribute("gen_ai.usage.input_tokens", 120)),
            _delimited(9, _int_attribute("gen_ai.usage.output_tokens", 30)),
        ]
    )
    scope_spans = _delimited(2, span)
    resource = _delimited(1, _string_attribute("service.name", agent))
    resource_spans = _delimited(1, resource) + _delimited(2, scope_spans)
    return _delimited(1, resource_spans)


def decode_partial_success(body: bytes) -> tuple[int, str]:
    """Read ``rejected_spans`` and ``error_message`` out of the OTLP reply."""

    def fields(buf: bytes):
        position = 0
        while position < len(buf):
            key, position = _read_varint(buf, position)
            field, wire = key >> 3, key & 0x07
            if wire == 0:
                value, position = _read_varint(buf, position)
                yield field, value
            elif wire == 2:
                length, position = _read_varint(buf, position)
                yield field, buf[position : position + length]
                position += length
            else:  # pragma: no cover - the reply uses only these two
                raise AssertionError(f"unexpected wire type {wire}")

    rejected, message = 0, ""
    for field, value in fields(body):
        if field == 1 and isinstance(value, bytes):
            for inner_field, inner in fields(value):
                if inner_field == 1 and isinstance(inner, int):
                    rejected = inner
                elif inner_field == 2 and isinstance(inner, bytes):
                    message = inner.decode()
    return rejected, message


def _read_varint(buf: bytes, position: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


async def test_an_otlp_json_export_is_governed_like_any_other_batch(
    ingest_client, admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    span_id = "00f067aa0ba902b7"

    response = await ingest_client.post(
        "/v1/traces",
        json=otlp_json_export(agent="Support Bot", trace_id=trace_id, span_id=span_id),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {}, "no partial success means everything landed"
    assert engine.trace_count(agent.engine_project_name) == 1

    runs = await admin_client.get("/api/v1/runs")
    assert runs.json()["total"] == 1
    row = runs.json()["items"][0]
    assert row["agent"] == "Support Bot"
    assert row["tokens"] == 150


async def test_an_otlp_protobuf_export_lands_the_same_way(
    ingest_client, admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    body = otlp_protobuf_export(
        agent="Support Bot",
        trace_id=bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736"),
        span_id=bytes.fromhex("00f067aa0ba902b7"),
        name="chat gpt-4o",
    )

    response = await ingest_client.post(
        "/v1/traces", content=body, headers={"Content-Type": "application/x-protobuf"}
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-protobuf")
    rejected, message = decode_partial_success(response.content)
    assert (rejected, message) == (0, "")
    assert engine.trace_count(agent.engine_project_name) == 1

    runs = await admin_client.get("/api/v1/runs")
    assert runs.json()["total"] == 1
    assert runs.json()["items"][0]["model"] == "gpt-4o"


async def test_the_same_export_twice_produces_the_same_ids(
    ingest_client, factory, workspace, engine
):
    """Span ids are derived, not minted, so a replayed export is not duplicated."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    export = otlp_json_export(
        agent="Support Bot",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
    )

    await ingest_client.post("/v1/traces", json=export)
    await ingest_client.post("/v1/traces", json=export)

    assert engine.trace_count(agent.engine_project_name) == 1
    assert len(engine.spans) == 1


async def test_an_otlp_export_for_an_unknown_agent_reports_rejected_spans(
    ingest_client, factory, workspace, engine
):
    """This is how an OTel exporter learns not to retry the batch."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    response = await ingest_client.post(
        "/v1/traces",
        json=otlp_json_export(
            agent="Ghost Service",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            span_id="00f067aa0ba902b7",
        ),
    )

    assert response.status_code == 200, response.text
    partial = response.json()["partialSuccess"]
    assert partial["rejectedSpans"] == 1
    assert "Ghost Service" in partial["errorMessage"]


async def test_an_otlp_body_in_a_format_we_do_not_speak_is_refused(ingest_client):
    response = await ingest_client.post(
        "/v1/traces", content=b"<xml/>", headers={"Content-Type": "application/xml"}
    )
    assert response.status_code == 415, response.text
    assert "application/x-protobuf" in response.text


async def test_a_corrupt_protobuf_body_is_a_validation_failure_not_a_crash(ingest_client):
    response = await ingest_client.post(
        "/v1/traces",
        content=b"\x0a\xff\xff\xff\xff\x0f",
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_failed"


async def test_the_otlp_endpoint_wants_an_ingest_key_like_every_other_front_door(
    client, admin_client
):
    anonymous = await client.post("/v1/traces", json={"resourceSpans": []})
    signed_in = await admin_client.post("/v1/traces", json={"resourceSpans": []})

    assert anonymous.status_code == 401
    assert signed_in.status_code == 403
    assert "API keys only" in signed_in.text


async def test_an_otlp_export_is_measured_against_the_same_quota(
    ingest_client, db, factory, workspace, engine
):
    """An OTel-instrumented agent is not a way around the tenant's limits."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Requests per month",
        resource=QuotaResource.REQUESTS.value,
        limit_value=1,
        used_value=1,
        enforcement=QuotaEnforcement.BLOCK.value,
    )

    response = await ingest_client.post(
        "/v1/traces",
        json=otlp_json_export(
            agent="Support Bot",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            span_id="00f067aa0ba902b7",
        ),
    )

    assert response.status_code == 402, response.text
    assert error_code(response) == "quota_exceeded"
    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 1
