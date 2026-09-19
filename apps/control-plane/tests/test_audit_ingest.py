"""Regressions from the 2026-09-18 audit of the ingest path.

Everything a customer's agent reports comes through here, so each of these was
a way for telemetry to be refused, lost or miscounted without anybody being
told. They go in through the same HTTP surface an SDK uses and are checked at
the far end: in the engine double, or in the rows the console reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid

import httpx
from sqlalchemy import event, select, update

from conftest import APP_BASE_URL, error_code
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.db.base import new_id
from fulcrum_ops_api.models.governance import Policy, PolicyEnforcement, PolicyStatus
from fulcrum_ops_api.models.operations import (
    Alert,
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
    QuotaResource,
)
from fulcrum_ops_api.models.quality import FeedbackItem, GuardrailConfig
from fulcrum_ops_api.models.registry import Agent, Connector

NOW = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)


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
# n=55 -- a rule about a signal the item does not carry must not fire
# ===========================================================================


def safety_floor(**extra) -> dict:
    """The body the Policy Center used to derive for a blank Guardrails form."""
    body = {"conditions": [{"signal": "safety_score", "operator": "lt", "value": 0.82}]}
    body.update(extra)
    return body


async def test_a_score_threshold_does_not_refuse_a_trace_nobody_scored(
    ingest_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(workspace, name="Safety floor", rules=safety_floor(fail_mode="closed"))

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(),
                trace(feedback_scores=[{"name": "safety_score", "value": 0.4}]),
                trace(feedback_scores=[{"name": "safety_score", "value": 0.95}]),
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert outcomes(posted) == [
        (0, "accepted", None),
        (1, "blocked", "policy_blocked"),
        (2, "accepted", None),
    ]
    assert engine.trace_count(agent.engine_project_name) == 2
    assert posted.json()["violations_recorded"] == 1


async def test_a_masking_rule_on_an_absent_signal_leaves_the_run_readable(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(
        workspace,
        name="Mask slow tools",
        enforcement=PolicyEnforcement.MASK.value,
        rules={
            "conditions": [{"signal": "tool_name", "operator": "ne", "value": "search"}],
            "action": {"mode": "Mask"},
        },
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    row = posted.json()["results"][0]
    assert (row["outcome"], row["masked"]) == ("accepted", False)
    stored = next(iter(engine.traces.values()))
    assert stored["input"] == {"question": "Where is my order?"}


async def test_a_lone_span_is_not_refused_for_a_tool_it_did_not_call(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(
        workspace,
        name="No record deletion",
        rules={"conditions": [{"signal": "tool_name", "operator": "eq", "value": "delete"}]},
    )
    trace_id = "01932c1e-7a40-7c55-9f6b-3d2a1b0c9e8f"

    posted = await ingest_client.post(
        "/api/v1/ingest/spans",
        json={
            "agent": "Support Bot",
            "spans": [
                span(trace_id=trace_id, name="chat", type="llm", model="gpt-4o"),
                span(trace_id=trace_id, name="delete", type="tool"),
            ],
        },
    )

    assert outcomes(posted) == [(0, "accepted", None), (1, "blocked", "policy_blocked")]


async def test_a_tool_rule_reaches_the_spans_nested_inside_a_trace(
    ingest_client, factory, workspace, engine
):
    """Both SDKs nest spans in their trace; a tool rule has to look inside."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policy = await factory.policy(
        workspace,
        name="No record deletion",
        rules={"conditions": [{"signal": "tool_name", "operator": "eq", "value": "delete"}]},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(spans=[span(name="search", type="tool"), span(name="chat", type="llm")]),
                trace(spans=[span(name="delete", type="tool"), span(name="delete", type="tool")]),
            ],
        },
    )

    assert outcomes(posted) == [(0, "accepted", None), (1, "blocked", "policy_blocked")]
    assert posted.json()["results"][1]["policy_id"] == policy.id
    # One policy, one run, one violation -- however many spans broke it.
    assert posted.json()["violations_recorded"] == 1
    assert engine.trace_count(agent.engine_project_name) == 1


# ===========================================================================
# n=58 -- the store only addresses version 7 UUIDs, and refuses the whole batch
# ===========================================================================


def otlp_span(
    trace_id: str,
    span_id: str,
    *,
    name: str = "chat gpt-4o",
    parent: str | None = None,
    offset: float = 0.0,
) -> dict:
    start = int((NOW + dt.timedelta(seconds=offset)).timestamp() * 1_000_000_000)
    item = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 3,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 500_000_000),
        "attributes": [],
    }
    if parent:
        item["parentSpanId"] = parent
    return item


def otlp_export(service: str, spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"scope": {"name": "otel"}, "spans": spans}],
            }
        ]
    }


OTEL_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_an_otlp_export_is_stored_under_version_7_ids(
    ingest_client, factory, workspace, engine
):
    """An OTel id read as a UUID is version 7 about one time in a hundred."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.strict_writes = True
    export = otlp_export(
        "Support Bot",
        [
            otlp_span(OTEL_TRACE, "00f067aa0ba902b7", name="handle request"),
            otlp_span(
                OTEL_TRACE, "53995c3f42cd8ad8", name="chat", parent="00f067aa0ba902b7", offset=0.1
            ),
        ],
    )

    first = await ingest_client.post("/v1/traces", json=export)
    again = await ingest_client.post("/v1/traces", json=export)

    assert first.status_code == 200, first.text
    assert first.json() == {}, "nothing was refused"
    assert again.json() == {}
    # Derived, not minted: the replay overwrote the same rows.
    assert engine.trace_count(agent.engine_project_name) == 1
    assert len(engine.spans) == 2

    (stored,) = engine.traces.values()
    assert uuid.UUID(stored["id"]).version == 7
    assert stored["metadata"]["otel.trace_id"] == OTEL_TRACE
    # The id says when the trace started, as a version 7 id is read to.
    millis = int.from_bytes(uuid.UUID(stored["id"]).bytes[:6], "big")
    assert abs(millis - int(NOW.timestamp() * 1000)) <= 1

    by_name = {row["name"]: row for row in engine.spans.values()}
    assert {uuid.UUID(row["id"]).version for row in by_name.values()} == {7}
    assert by_name["chat"]["parent_span_id"] == by_name["handle request"]["id"]
    assert by_name["chat"]["trace_id"] == stored["id"]


async def test_a_key_bound_to_an_agent_can_export_otlp_whatever_the_service_is_called(
    app, factory, workspace, engine
):
    """``service.name`` names a process; OTel sends one whether anybody set it or not."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    token, _ = await factory.api_key(workspace, scopes=["ingest"], agent_id=agent.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as bound:
        bound.headers["Authorization"] = f"Bearer {token}"

        exported = await bound.post(
            "/v1/traces",
            json=otlp_export("unknown_service:python", [otlp_span(OTEL_TRACE, "00f067aa0ba902b7")]),
        )
        # The SDK endpoints keep the rule: there, the name is a claim.
        forged = await bound.post(
            "/api/v1/ingest/traces", json={"agent": "Sales Bot", "traces": [trace()]}
        )

    assert exported.status_code == 200, exported.text
    assert exported.json() == {}
    assert engine.trace_count(agent.engine_project_name) == 1
    assert outcomes(forged) == [(0, "rejected", "agent_not_permitted")]


# ===========================================================================
# n=59 -- one row the store refuses must not cost the rows sent with it
# ===========================================================================


async def test_one_id_the_store_refuses_costs_only_its_own_trace(
    ingest_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.REQUESTS.value, limit_value=1_000, used_value=0
    )
    engine.strict_writes = True
    batch = [trace(id=new_id(), name=f"run {n}") for n in range(9)]
    batch.insert(4, trace(id=str(uuid.uuid4()), name="not a v7 id"))

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": batch}
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert (body["accepted"], body["rejected"]) == (9, 1)
    refused = body["results"][4]
    assert (refused["outcome"], refused["code"]) == ("rejected", "telemetry_rejected")
    assert "version 7" in refused["reason"]
    assert engine.trace_count(agent.engine_project_name) == 9
    # ...and the tenant is billed for the nine that landed.
    assert (await db.get(Quota, quota.id)).used_value == 9


async def test_spans_the_store_refuses_are_reported_and_not_billed(
    ingest_client, factory, workspace, engine, db
):
    """They used to be a log line under an item that still said "accepted"."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.TOKENS.value, limit_value=10_000, used_value=0
    )
    engine.strict_writes = True
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    id=new_id(),
                    spans=[
                        span(id=new_id(), name="plan", type="llm", usage=usage),
                        span(id=str(uuid.uuid4()), name="answer", type="llm", usage=usage),
                    ],
                ),
                trace(id=new_id(), spans=[span(id=new_id(), name="chat", type="llm", usage=usage)]),
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["spans_accepted"] == 2
    first, second = body["results"]
    assert (first["outcome"], first["spans"], first["spans_rejected"]) == ("accepted", 2, 1)
    assert "version 7" in first["reason"]
    assert (second["outcome"], second["spans_rejected"], second["reason"]) == ("accepted", 0, None)
    assert engine.trace_count(agent.engine_project_name) == 2
    assert sorted(row["name"] for row in engine.spans.values()) == ["chat", "plan"]
    assert (await db.get(Quota, quota.id)).used_value == 240


async def test_an_otlp_exporter_is_told_about_spans_the_store_refused(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail_path("/spans/batch", 400)

    exported = await ingest_client.post(
        "/v1/traces",
        json=otlp_export("Support Bot", [otlp_span(OTEL_TRACE, "00f067aa0ba902b7")]),
    )

    assert exported.status_code == 200, exported.text
    assert exported.json()["partialSuccess"]["rejectedSpans"] == 1


async def test_an_error_without_a_traceback_and_a_bad_parent_do_not_cost_the_batch(
    ingest_client, factory, workspace, engine
):
    """The store wants a traceback on every error and a UUID in every parent link."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.strict_writes = True

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(
                    id=new_id(),
                    error_info={"exception_type": "TimeoutError", "message": "upstream timed out"},
                    spans=[
                        span(
                            id=new_id(),
                            parent_span_id="root",
                            error_info={"exception_type": "TimeoutError", "traceback": None},
                        )
                    ],
                ),
                trace(id=new_id()),
            ],
        },
    )

    assert outcomes(posted) == [(0, "accepted", None), (1, "accepted", None)]
    assert posted.json()["spans_accepted"] == 1
    assert engine.trace_count(agent.engine_project_name) == 2
    (stored,) = engine.spans.values()
    assert "parent_span_id" not in stored
    assert stored["error_info"]["exception_type"] == "TimeoutError"
    assert stored["error_info"]["traceback"]


# ===========================================================================
# n=29, 30, 172 -- quota metering: no lost updates, one threshold, an alert
# ===========================================================================


async def test_usage_another_worker_counted_meanwhile_is_not_overwritten(
    ingest_client, factory, workspace, engine, db
):
    """The counter was read before the store round trip and written back after it."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.REQUESTS.value, limit_value=1_000_000, used_value=900
    )

    async def another_worker_commits_a_batch(method: str, path: str) -> None:
        if path.endswith("/traces/batch"):
            engine.before_dispatch = None
            await db.execute(
                update(Quota).where(Quota.id == quota.id).values(used_value=Quota.used_value + 40)
            )

    engine.before_dispatch = another_worker_commits_a_batch

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace() for _ in range(25)]},
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["quotas"][0]["used_value"] == 965
    assert (await db.get(Quota, quota.id)).used_value == 965


async def test_a_quota_somebody_disabled_meanwhile_is_not_switched_back_on(
    ingest_client, factory, workspace, engine, db
):
    """The status was written from what the request had loaded before the round trip."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.REQUESTS.value, limit_value=100, used_value=60
    )

    async def an_admin_disables_it(method: str, path: str) -> None:
        if path.endswith("/traces/batch"):
            engine.before_dispatch = None
            await db.execute(
                update(Quota)
                .where(Quota.id == quota.id)
                .values(status=LimitStatus.DISABLED.value)
            )

    engine.before_dispatch = an_admin_disables_it

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace() for _ in range(15)]},
    )

    assert posted.status_code == 200, posted.text
    stored = await db.get(Quota, quota.id)
    # Still counted -- the store took the batch -- but the switch stays where it was put,
    # and nobody is alerted about a quota that is no longer in force.
    assert (stored.used_value, stored.status) == (75, LimitStatus.DISABLED.value)
    assert posted.json()["quotas"][0]["status"] == LimitStatus.DISABLED.value
    assert await db.count(Alert) == 0


async def test_metering_a_quota_does_not_count_as_somebody_editing_it(
    ingest_client, admin_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.REQUESTS.value, limit_value=1_000, used_value=0
    )

    await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    stored = await db.get(Quota, quota.id)
    assert stored.used_value == 1
    assert stored.updated_at == quota.updated_at
    # ...so the form that was open while the agent reported can still save.
    saved = await admin_client.patch(
        f"/api/v1/quota/{quota.id}",
        json={
            "limit_value": 2_000,
            "expected_updated_at": quota.updated_at.isoformat(),
        },
    )
    assert saved.status_code == 200, saved.text


async def test_crossing_the_watch_threshold_turns_the_quota_amber_and_raises_the_alert(
    ingest_client, factory, workspace, engine, db
):
    """Ingest kept its own 80% beside the Quota screen's 70%, and never alerted."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        name="Requests per month",
        resource=QuotaResource.REQUESTS.value,
        unit="requests",
        limit_value=100,
        used_value=60,
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace() for _ in range(15)]},
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["quotas"][0]["status"] == LimitStatus.WARNING.value
    assert (await db.get(Quota, quota.id)).status == LimitStatus.WARNING.value
    alerts = await db.scalars(select(Alert).where(Alert.source_entity_id == quota.id))
    assert [alert.title for alert in alerts] == ["Quota approaching limit: Requests per month"]

    # Staying amber is not news; going red is.
    await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace() for _ in range(30)]},
    )
    alerts = await db.scalars(select(Alert).where(Alert.source_entity_id == quota.id))
    assert sorted(alert.title for alert in alerts) == [
        "Quota approaching limit: Requests per month",
        "Quota exceeded: Requests per month",
    ]


async def test_a_quota_on_one_agent_is_charged_for_that_agents_items_only(
    ingest_client, factory, workspace, engine, db
):
    """One batch from a fleet key can carry several agents."""
    support = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.provisioned_agent(workspace, engine, name="Sales Bot")
    quota = await factory.quota(
        workspace,
        resource=QuotaResource.REQUESTS.value,
        scope=LimitScope.AGENT.value,
        scope_ref=support.id,
        limit_value=1_000,
        used_value=0,
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "traces": [
                trace(agent="Support Bot"),
                trace(agent="Sales Bot"),
                trace(agent="Sales Bot"),
            ]
        },
    )

    assert posted.json()["accepted"] == 3
    assert (await db.get(Quota, quota.id)).used_value == 1


async def test_a_period_that_elapsed_is_rolled_once_and_the_batch_counted_in_the_new_one(
    ingest_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace,
        resource=QuotaResource.REQUESTS.value,
        limit_value=10,
        used_value=10,
        status=LimitStatus.EXCEEDED.value,
        enforcement=QuotaEnforcement.BLOCK.value,
        resets_at=NOW - dt.timedelta(days=1),
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace(), trace()]}
    )

    assert posted.status_code == 200, posted.text
    stored = await db.get(Quota, quota.id)
    assert (stored.used_value, stored.status) == (2, LimitStatus.ACTIVE.value)
    assert stored.resets_at > dt.datetime.now(dt.UTC)


# ===========================================================================
# n=48, 56 -- feedback is mirrored once per batch, and never at the batch's expense
# ===========================================================================


def feedback(trace_id: str, n: int, **overrides) -> dict:
    item = {
        "kind": "feedback.submitted",
        "ref": f"fb-{n}",
        "trace_id": trace_id,
        "rating": 5 if n % 2 else 1,
        "body": f"comment {n}",
    }
    item.update(overrides)
    return item


async def test_a_batch_of_feedback_is_mirrored_in_one_store_call(
    ingest_client, factory, workspace, engine, db
):
    """It was one SELECT and one store call per event, inside the loop."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    ids = [new_id() for _ in range(30)]
    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(id=trace_id) for trace_id in ids]},
    )
    engine.reset_calls()

    events = [feedback(trace_id, n) for n, trace_id in enumerate(ids)]
    # A reference that is not a trace id costs its own mirror, not the batch's.
    events.append(feedback("order-5512", 30))
    posted = await ingest_client.post(
        "/api/v1/ingest/events", json={"agent": "Support Bot", "events": events}
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 31
    assert len(engine.calls_to("/traces/feedback-scores")) == 1
    scored = engine.traces[ids[0]]["feedback_scores"]
    assert [(score["name"], score["value"]) for score in scored] == [("user_feedback", 1.0)]
    assert engine.traces[ids[0]]["project_name"] == agent.engine_project_name

    items = await db.scalars(select(FeedbackItem).order_by(FeedbackItem.feedback_ref))
    mirrored = {item.feedback_ref: item.engine_feedback_score_id for item in items}
    assert mirrored["fb-0"] == f"{ids[0]}/user_feedback"
    assert mirrored["fb-30"] is None


async def test_feedback_is_kept_when_the_store_will_not_take_its_mirror(
    ingest_client, factory, workspace, engine, db
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail_path("/traces/feedback-scores", 503)

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={"agent": "Support Bot", "events": [feedback(new_id(), 1)]},
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 1
    (item,) = await db.scalars(select(FeedbackItem))
    assert item.engine_feedback_score_id is None


async def test_feedback_from_no_known_agent_is_kept_and_filed_under_no_project(
    ingest_client, factory, workspace, engine, db
):
    """The workspace's name is not a project; a score sent under it is on no run."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.reset_calls()

    posted = await ingest_client.post(
        "/api/v1/ingest/events",
        json={"events": [feedback(new_id(), 1), feedback(new_id(), 2, agent="Nobody")]},
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 2
    assert engine.calls_to("/traces/feedback-scores") == []
    items = list(await db.scalars(select(FeedbackItem)))
    assert [item.engine_feedback_score_id for item in items] == [None, None]


# ===========================================================================
# n=56 -- a slow store is answered with "retry" inside the reporter's own timeout
# ===========================================================================


async def test_a_store_that_does_not_answer_in_time_costs_a_retry_not_a_minute(
    ingest_client, factory, workspace, engine, db, monkeypatch
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    quota = await factory.quota(
        workspace, resource=QuotaResource.REQUESTS.value, limit_value=1_000, used_value=5
    )
    monkeypatch.setattr(settings, "ingest_budget_seconds", 0.2)
    answered = asyncio.Event()

    async def a_store_that_is_behind(method: str, path: str) -> None:
        # Holds its answer until the reporter has had ours -- or, without a
        # budget, for as long as the request is prepared to wait, and then serves.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(answered.wait(), timeout=8)

    engine.before_dispatch = a_store_that_is_behind

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    answered.set()

    assert posted.status_code == 503, posted.text
    assert error_code(posted) == "telemetry_unavailable"
    # Nothing is billed for a batch the reporter was told to send again.
    assert (await db.get(Quota, quota.id)).used_value == 5


# ===========================================================================
# n=81, 171 -- the run row reads its tools and its verdict off the trace
# ===========================================================================


async def test_a_run_lists_its_tools_and_a_warned_run_says_so(
    ingest_client, admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.policy(
        workspace,
        name="Card numbers",
        enforcement=PolicyEnforcement.WARN.value,
        rules={
            "conditions": [{"signal": "content", "operator": "contains", "value": "4111 1111"}],
            "action": {"mode": "Warn"},
        },
    )
    tools = [
        span(name="search orders", type="tool"),
        span(name="chat", type="llm", model="gpt-4o"),
        span(name="search orders", type="tool", start_time=iso(0.6), end_time=iso(0.7)),
        span(name="refund", type="tool", start_time=iso(0.8), end_time=iso(0.9)),
    ]

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                trace(name="clean", spans=tools),
                trace(name="flagged", input={"question": "my card is 4111 1111 1111 1111"}),
            ],
        },
    )

    assert outcomes(posted) == [(0, "accepted", None), (1, "accepted", None)]
    stored = {row["name"]: row["metadata"] for row in engine.traces.values()}
    assert stored["clean"]["tools"] == ["search orders", "refund"]
    assert "policy" not in stored["clean"]
    assert stored["flagged"]["policy"] == "Warned"

    listed = (await admin_client.get("/api/v1/runs")).json()["items"]
    rows = {bool(row["tools"]): row for row in listed}
    assert rows[True]["tools"] == ["search orders", "refund"]
    assert (rows[True]["policy"], rows[True]["status"]) == ("Allowed", "Completed")
    assert (rows[False]["policy"], rows[False]["status"]) == ("Warned", "Warned")


# ===========================================================================
# n=104 -- a policy flagged "Warning" is still in force
# ===========================================================================


async def test_a_policy_in_warning_keeps_enforcing(ingest_client, factory, workspace, engine):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    rules = {"conditions": [{"signal": "content", "operator": "contains", "value": "4111 1111"}]}
    await factory.policy(workspace, name="Noisy", status=PolicyStatus.WARNING.value, rules=rules)
    await factory.policy(
        workspace,
        name="Parked",
        status=PolicyStatus.PENDING_REVIEW.value,
        rules={"conditions": [{"signal": "content", "operator": "contains", "value": "order"}]},
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(), trace(input={"question": "card 4111 1111 1111 1111"})],
        },
    )

    assert outcomes(posted) == [(0, "accepted", None), (1, "blocked", "policy_blocked")]


# ===========================================================================
# n=97 -- a connector's Last Used is written by somebody
# ===========================================================================


async def test_a_tool_call_by_a_granted_agent_stamps_the_connector_without_editing_it(
    ingest_client, admin_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    idle = await factory.provisioned_agent(workspace, engine, name="Sales Bot")
    github = await factory.connector(workspace, name="GitHub MCP")
    jira = await factory.connector(workspace, name="search issues")
    crm = await factory.connector(workspace, name="CRM")
    for connector, holder in ((github, agent), (jira, agent), (crm, idle)):
        granted = await admin_client.post(
            f"/api/v1/connectors/{connector.id}/grants", json={"agent_id": holder.id}
        )
        assert granted.status_code == 201, granted.text
    before = {row.id: row for row in await db.scalars(select(Connector))}

    # No tool was called: nothing was used.
    await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert {row.last_used_at for row in await db.scalars(select(Connector))} == {None}

    # A span named after a connector stamps that connector, and only that one.
    await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(spans=[span(name="Search Issues", type="tool")])],
        },
    )
    after = {row.id: row for row in await db.scalars(select(Connector))}
    assert after[jira.id].last_used_at is not None
    assert after[github.id].last_used_at is None
    assert after[crm.id].last_used_at is None, "its agent reported nothing"
    assert after[jira.id].updated_at == before[jira.id].updated_at, "being used is not an edit"

    # A span named after a function, as most are: every grant the agent holds.
    await ingest_client.post(
        "/api/v1/ingest/spans",
        json={
            "agent": "Support Bot",
            "spans": [span(trace_id=new_id(), name="list_pull_requests", type="tool")],
        },
    )
    after = {row.id: row for row in await db.scalars(select(Connector))}
    assert after[github.id].last_used_at is not None
    assert after[crm.id].last_used_at is None


# ===========================================================================
# n=14, 16, 67 -- reporting is not an edit to the agent or to the policy
# ===========================================================================


async def test_telemetry_does_not_move_the_token_an_open_edit_form_holds(
    ingest_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policy = await factory.policy(
        workspace,
        name="Card numbers",
        enforcement=PolicyEnforcement.WARN.value,
        rules={
            "conditions": [{"signal": "content", "operator": "contains", "value": "order"}],
            "action": {"mode": "Warn"},
        },
    )

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    assert posted.json()["violations_recorded"] == 1
    stored_agent = await db.get(Agent, agent.id)
    stored_policy = await db.get(Policy, policy.id)
    assert stored_agent.last_used_at > agent.last_used_at
    assert stored_agent.updated_at == agent.updated_at
    assert stored_policy.last_triggered_at is not None
    assert stored_policy.updated_at == policy.updated_at


async def test_a_batch_of_reported_hits_stamps_each_control_once_with_the_latest_hit(
    ingest_client, factory, workspace, engine, db, db_engine
):
    """It was one UPDATE per event, and the last event in the batch won, not the latest."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policy = await factory.policy(workspace, name="Card numbers")
    guardrail = await factory.guardrail(workspace, name="PII watch")
    # Reported newest first, the way a queue drained after an outage can be.
    events = [
        {
            "kind": "policy.violation" if n % 2 else "guardrail.triggered",
            "policy" if n % 2 else "guardrail": "Card numbers" if n % 2 else "PII watch",
            "occurred_at": iso(-n),
        }
        for n in range(40)
    ]
    stamped: list[str] = []

    def watch(_conn, _cursor, statement, *_rest) -> None:
        head = statement.lstrip().upper()
        if head.startswith(("UPDATE POLICIES", "UPDATE GUARDRAIL_CONFIGS")):
            stamped.append(head.split()[1])

    event.listen(db_engine.sync_engine, "before_cursor_execute", watch)
    try:
        posted = await ingest_client.post(
            "/api/v1/ingest/events", json={"agent": "Support Bot", "events": events}
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", watch)

    assert posted.status_code == 200, posted.text
    assert posted.json()["events_recorded"] == 40
    assert sorted(stamped) == ["GUARDRAIL_CONFIGS", "POLICIES"]
    stored_policy = await db.get(Policy, policy.id)
    stored_guardrail = await db.get(GuardrailConfig, guardrail.id)
    assert stored_policy.last_triggered_at.replace(tzinfo=dt.UTC) == NOW - dt.timedelta(seconds=1)
    assert stored_guardrail.last_triggered_at.replace(tzinfo=dt.UTC) == NOW
    assert stored_policy.updated_at == policy.updated_at
    assert stored_guardrail.updated_at == guardrail.updated_at
