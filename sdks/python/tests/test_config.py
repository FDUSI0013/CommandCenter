"""``GET /ingest/config``: what the SDK adopts from it, and what it refuses to."""

from __future__ import annotations

import time

import pytest

from fulcrum_ops import FulcrumOps
from fulcrum_ops.errors import AuthenticationError

from .conftest import build_client, sent
from .stub_server import StubServer


def config_requests(stub: StubServer) -> list:
    return [r for r in stub.requests if r.path == "/api/v1/ingest/config"]


def test_config_is_read_and_returned(client: FulcrumOps, stub: StubServer) -> None:
    document = client.config()

    assert document["workspace"] == "acme"
    assert document["revision"] == "rev-1"
    assert config_requests(stub)[0].headers["authorization"] == "Bearer fo_test_key"


def test_the_document_is_cached_and_revalidated_with_an_etag(
    client: FulcrumOps, stub: StubServer
) -> None:
    first = client.config()
    # A second call inside the cache window costs nothing at all.
    assert client.config() is first
    assert len(config_requests(stub)) == 1

    # A refresh revalidates rather than re-reading, which is what keeps a fleet
    # restart to one conditional request per process.
    refreshed = client.config(refresh=True)
    requests = config_requests(stub)
    assert len(requests) == 2
    assert requests[1].headers.get("if-none-match") == stub.config_etag
    assert refreshed["workspace"] == "acme"


def test_a_304_keeps_the_cached_document(client: FulcrumOps, stub: StubServer) -> None:
    client.config()
    stub.inject("GET", "/api/v1/ingest/config", 304, None, {"etag": stub.config_etag})

    document = client.config(refresh=True)
    assert document["workspace"] == "acme"


def test_the_stricter_of_the_two_sampling_rates_wins(stub: StubServer, shared_http_client) -> None:
    """A workspace cap is a limit, not a preference a constructor may widen."""
    stub.config["sampling_rate"] = 0.25
    client = build_client(stub, shared_http_client, sampling_rate=1.0)
    try:
        client.config()
        assert client.options.sampling_rate == 0.25
    finally:
        client.close(timeout=2)


def test_a_locally_narrower_setting_is_not_widened(stub: StubServer, shared_http_client) -> None:
    stub.config["sampling_rate"] = 1.0
    client = build_client(stub, shared_http_client, sampling_rate=0.1)
    try:
        client.config()
        assert client.options.sampling_rate == pytest.approx(0.1)
    finally:
        client.close(timeout=2)


def test_batching_limits_are_narrowed_to_what_the_deployment_enforces(
    stub: StubServer, shared_http_client
) -> None:
    stub.config.update(
        {
            "batch_max_spans": 50,
            "batch_max_bytes": 100_000,
            "flush_interval_seconds": 1.0,
            "max_queue_size": 250,
            "retry_max_attempts": 1,
        }
    )
    client = build_client(
        stub,
        shared_http_client,
        batch_max_bytes=8_000_000,
        max_queue_size=10_000,
        retry_max_attempts=5,
        flush_interval_seconds=30.0,
    )
    try:
        client.config()
        options = client.options
        assert options.batch_max_spans == 50
        assert options.batch_max_bytes == 100_000
        assert options.flush_interval_seconds == 1.0
        assert options.max_queue_size == 250
        assert options.retry_max_attempts == 1
    finally:
        client.close(timeout=2)


def test_the_server_may_switch_capture_off_but_not_on(
    stub: StubServer, shared_http_client
) -> None:
    stub.config.update({"capture_input": False, "capture_output": True})
    client = build_client(stub, shared_http_client, capture_input=True, capture_output=False)
    try:
        client.config()
        assert client.options.capture_input is False, "the server may switch capture off"
        assert client.options.capture_output is False, "the server may not switch it back on"
    finally:
        client.close(timeout=2)


def test_server_redaction_rules_are_applied_before_content_leaves(
    stub: StubServer, shared_http_client
) -> None:
    stub.config["redaction"] = [
        {
            "id": "gr-pii",
            "name": "PII masking",
            "source": "guardrail",
            "entity_types": ["email"],
            "pattern": None,
            "replacement": "[redacted by policy]",
            "applies_to": ["input", "output"],
        }
    ]
    client = build_client(stub, shared_http_client)
    try:
        client.config()
        with client.trace("run", input={"question": "email ada@example.com about it"}) as run:
            run.set_output({"reply": "sent to ada@example.com"})
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert row["input"]["question"] == "email [redacted by policy] about it"
        assert row["output"]["reply"] == "sent to [redacted by policy]"
        # And the raw address never appeared anywhere on the wire.
        for request in stub.requests:
            assert "ada@example.com" not in str(request.body or "")
    finally:
        client.close(timeout=2)


def test_a_local_rule_and_a_server_rule_both_apply(
    stub: StubServer, shared_http_client
) -> None:
    from fulcrum_ops import RedactionRule

    stub.config["redaction"] = [
        {
            "id": "gr-pii",
            "name": "PII",
            "source": "guardrail",
            "entity_types": ["email"],
            "replacement": "[email]",
            "applies_to": ["input"],
        }
    ]
    client = build_client(
        stub,
        shared_http_client,
        redaction=[
            RedactionRule(
                name="employee ids",
                pattern=r"EMP-\d{6}",
                replacement="[emp]",
                applies_to=["input"],
            )
        ],
    )
    try:
        client.config()
        with client.trace("run", input={"note": "ada@example.com is EMP-123456"}):
            pass
        client.flush(timeout=5)

        assert sent(stub, "traces")[0]["input"]["note"] == "[email] is [emp]"
    finally:
        client.close(timeout=2)


def test_an_unmatchable_entity_type_warns_once_rather_than_failing(
    stub: StubServer, shared_http_client, caplog
) -> None:
    """A rule this version cannot match is the server's job, and worth saying so."""
    stub.config["redaction"] = [
        {
            "id": "r1",
            "name": "future entity",
            "source": "workspace",
            "entity_types": ["email", "medical_record_number"],
            "applies_to": ["input"],
        }
    ]
    client = build_client(stub, shared_http_client)
    try:
        with caplog.at_level("WARNING", logger="fulcrum_ops"):
            client.config()
        assert "medical_record_number" in caplog.text
        # The half of the rule it does understand still protects content.
        with client.trace("run", input={"note": "ada@example.com"}):
            pass
        client.flush(timeout=5)
        assert "ada@example.com" not in str(sent(stub, "traces")[0]["input"])
    finally:
        client.close(timeout=2)


def test_bootstrap_never_blocks_start_up_and_never_raises(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("GET", "/api/v1/ingest/config", 500, {"error": {"code": "boom"}}, times=10)
    started = time.perf_counter()
    client = build_client(stub, shared_http_client, bootstrap=True)
    try:
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, "construction waited on the network"

        # And tracing works regardless of what the bootstrap found.
        with client.trace("run"):
            pass
        client.flush(timeout=5)
        assert len(sent(stub, "traces")) == 1
    finally:
        client.close(timeout=2)


def test_config_raises_because_it_is_a_lookup(stub: StubServer, shared_http_client) -> None:
    """Telemetry is absorbed; a call made for its return value is not."""
    stub.inject(
        "GET",
        "/api/v1/ingest/config",
        401,
        {"error": {"code": "unauthenticated", "message": "That key was revoked."}},
        times=5,
    )
    client = build_client(stub, shared_http_client)
    try:
        with pytest.raises(AuthenticationError, match="revoked"):
            client.config()
    finally:
        client.close(timeout=2)


def test_the_agent_from_the_document_is_used_when_none_was_given(
    stub: StubServer, shared_http_client
) -> None:
    client = build_client(stub, shared_http_client, agent=None)
    try:
        client.config()
        assert client.options.agent == "support-copilot"
        assert client.options.environment == "Production"
    finally:
        client.close(timeout=2)
