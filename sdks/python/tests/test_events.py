"""Feedback scores and the three governance event kinds."""

from __future__ import annotations

from fulcrum_ops import FulcrumOps

from .conftest import build_client, sent
from .stub_server import StubServer


def test_a_score_names_what_it_scores(client: FulcrumOps, stub: StubServer) -> None:
    assert client.score("trace-1", "helpfulness", 0.9, reason="resolved") is True
    client.flush(timeout=5)

    score = sent(stub, "scores")[0]
    assert score == {
        "name": "helpfulness",
        "value": 0.9,
        "id": "trace-1",
        "target": "trace",
        "source": "sdk",
        "reason": "resolved",
        "agent": "checkout-agent",
    }


def test_scores_can_target_a_span_or_a_thread(client: FulcrumOps, stub: StubServer) -> None:
    client.score("span-1", "grounded", 1.0, target="span")
    client.score("thread-1", "csat", 4.0, target="thread")
    client.score("trace-1", "nonsense", 1.0, target="galaxy")
    client.flush(timeout=5)

    targets = [score["target"] for score in sent(stub, "scores")]
    assert targets == ["span", "thread", "trace"], "an unknown target falls back to trace"


def test_a_score_with_no_id_or_no_name_is_refused_quietly(
    client: FulcrumOps, stub: StubServer
) -> None:
    assert client.score("", "helpfulness", 1.0) is False
    assert client.score("trace-1", "  ", 1.0) is False
    assert client.score("trace-1", "helpfulness", "not a number") is False  # type: ignore[arg-type]
    client.flush(timeout=5)

    assert sent(stub, "scores") == []


def test_an_inline_score_travels_with_the_item_it_scores(
    client: FulcrumOps, stub: StubServer
) -> None:
    """A score on an open unit costs no second request."""
    with client.trace("run") as run:
        run.score("answered", 1.0, category="binary")
    client.flush(timeout=5)

    assert sent(stub, "scores") == [], "an inline score must not become its own request"
    assert sent(stub, "traces")[0]["feedback_scores"] == [
        {"name": "answered", "value": 1.0, "source": "sdk", "category_name": "binary"}
    ]


def test_feedback_events_carry_the_rating_and_the_body(
    client: FulcrumOps, stub: StubServer
) -> None:
    assert client.log_feedback(
        trace_id="trace-1",
        rating=5,
        sentiment="positive",
        body="exactly what I needed",
        source="in-product",
        submitted_by="user-42",
    ) is True
    client.flush(timeout=5)

    event = sent(stub, "events")[0]
    assert event["kind"] == "feedback.submitted"
    assert event["rating"] == 5
    assert event["sentiment"] == "positive"
    assert event["body"] == "exactly what I needed"
    assert event["source"] == "in-product"
    assert event["submitted_by"] == "user-42"
    assert event["trace_id"] == "trace-1"
    assert event["agent"] == "checkout-agent"


def test_a_rating_outside_the_range_is_clamped_not_rejected(
    client: FulcrumOps, stub: StubServer
) -> None:
    """The contract refuses 0 and 9; a clamped row beats a row nobody sees."""
    client.log_feedback(trace_id="t-1", rating=0)
    client.log_feedback(trace_id="t-2", rating=9)
    client.flush(timeout=5)

    assert [event["rating"] for event in sent(stub, "events")] == [1, 5]


def test_guardrail_events_carry_the_rule_and_the_action(
    client: FulcrumOps, stub: StubServer
) -> None:
    assert client.log_guardrail_event(
        "pii-filter",
        action_taken="Masked",
        score=0.98,
        matched={"entity": "email", "count": 2},
        sample="contact ada@example.com",
        trace_id="trace-1",
        span_id="span-1",
        ref="idempotency-key-1",
    ) is True
    client.flush(timeout=5)

    event = sent(stub, "events")[0]
    assert event["kind"] == "guardrail.triggered"
    assert event["guardrail"] == "pii-filter"
    assert event["action_taken"] == "Masked"
    assert event["score"] == 0.98
    assert event["matched"] == {"entity": "email", "count": 2}
    assert event["span_id"] == "span-1"
    assert event["ref"] == "idempotency-key-1"


def test_a_guardrail_sample_is_redacted_like_any_other_content(
    stub: StubServer, shared_http_client
) -> None:
    """The sample is the field most likely to hold what redaction exists to remove."""
    stub.config["redaction"] = [
        {
            "id": "r1",
            "name": "PII",
            "source": "guardrail",
            "entity_types": ["email"],
            "replacement": "[redacted]",
            "applies_to": ["input"],
        }
    ]
    client = build_client(stub, shared_http_client)
    try:
        client.config()
        client.log_guardrail_event("pii-filter", sample="contact ada@example.com now")
        client.flush(timeout=5)

        assert sent(stub, "events")[0]["sample"] == "contact [redacted] now"
        for request in stub.requests:
            assert "ada@example.com" not in str(request.body or "")
    finally:
        client.close(timeout=2)


def test_a_guardrail_event_needs_a_guardrail(client: FulcrumOps, stub: StubServer) -> None:
    assert client.log_guardrail_event("") is False
    assert client.log_policy_violation("   ") is False
    client.flush(timeout=5)
    assert sent(stub, "events") == []


def test_policy_violations_carry_severity_and_detail(
    client: FulcrumOps, stub: StubServer
) -> None:
    assert client.log_policy_violation(
        "no-medical-advice",
        severity="high",
        action_taken="Blocked",
        detail={"matched_phrase": "dosage"},
        trace_id="trace-1",
    ) is True
    client.flush(timeout=5)

    event = sent(stub, "events")[0]
    assert event["kind"] == "policy.violation"
    assert event["policy"] == "no-medical-advice"
    assert event["severity"] == "high"
    assert event["action_taken"] == "Blocked"
    assert event["detail"] == {"matched_phrase": "dosage"}


def test_an_event_inside_a_trace_attaches_to_it_without_being_told(
    client: FulcrumOps, stub: StubServer
) -> None:
    with client.trace("run") as run:
        client.log_policy_violation("no-medical-advice", severity="low")
        expected = run.id
    client.flush(timeout=5)

    assert sent(stub, "events")[0]["trace_id"] == expected


def test_over_long_event_fields_are_trimmed_to_the_contract(
    client: FulcrumOps, stub: StubServer
) -> None:
    """Trimming here turns a rejected row into a slightly shortened one."""
    client.log_guardrail_event(
        "g" * 400,
        action_taken="Blocked with a very long explanation that will not fit",
        sample="x" * 900,
    )
    client.flush(timeout=5)

    event = sent(stub, "events")[0]
    assert len(event["guardrail"]) == 160
    assert len(event["action_taken"]) == 24
    assert len(event["sample"]) == 500
