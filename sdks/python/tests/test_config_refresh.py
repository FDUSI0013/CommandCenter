"""The configuration document has to keep arriving, not arrive once.

It used to be read exactly once, on a start-up thread, with no retry. An agent
that started while the control plane was restarting ran for the rest of its life
with no workspace redaction rules — every email body it handled went out as
written — and a Mask guardrail or a lower sampling rate set in the console
reached no running agent until somebody redeployed it. And because payloads are
built when a run ends, a run that ended before the rules arrived was queued, and
later sent, without them.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from fulcrum_ops import FulcrumOps
from fulcrum_ops import client as client_module

from .conftest import build_client, sent
from .stub_server import StubServer

CONFIG = "/api/v1/ingest/config"
CARD = "4111 1111 1111 1111"
MASK_CARDS = {
    "id": "gr-cards",
    "name": "Card numbers",
    "source": "guardrail",
    "entity_types": ["credit_card"],
    "pattern": None,
    "replacement": "[redacted by policy]",
    "applies_to": ["input", "output"],
}


def eventually(condition: Callable[[], Any], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real schedule — minutes — at a pace a test can watch."""
    monkeypatch.setattr(client_module, "CONFIG_RETRY_DELAYS", (0.05, 0.05))
    monkeypatch.setattr(client_module, "CONFIG_MIN_REFRESH_SECONDS", 0.05)


def config_reads(stub: StubServer) -> list:
    return [r for r in stub.requests if r.path == CONFIG]


def test_a_start_up_read_that_fails_is_tried_again(
    stub: StubServer, shared_http_client, quick: None
) -> None:
    stub.config["redaction"] = [MASK_CARDS]
    stub.inject("GET", CONFIG, 503, {"error": {"code": "unavailable"}}, times=1)
    seen: list = []
    client = build_client(
        stub,
        shared_http_client,
        bootstrap=True,
        flush_interval_seconds=0.05,
        on_error=lambda error, operation: seen.append(operation),
    )
    try:
        assert eventually(lambda: client.stats()["redaction_rules"] == 1), (
            "the control plane came back and the SDK never asked again"
        )
        assert seen.count("config") == 1, "one failure is one notification"

        with client.trace("run", input={"card": CARD}):
            pass
        assert client.flush(timeout=5) is True
        assert CARD not in str(sent(stub, "traces"))
    finally:
        client.close(timeout=2)


def test_a_rule_added_in_the_console_reaches_a_running_agent(
    stub: StubServer, shared_http_client, quick: None
) -> None:
    stub.config["refresh_after_seconds"] = 0.05
    client = build_client(stub, shared_http_client, bootstrap=True, flush_interval_seconds=0.05)
    try:
        assert eventually(lambda: len(config_reads(stub)) >= 2), "the document was never re-read"
        assert config_reads(stub)[1].headers.get("if-none-match") == stub.config_etag, (
            "a refresh that finds nothing new must cost a 304, not a full read"
        )
        assert client.stats()["redaction_rules"] == 0

        # An operator switches a guardrail to Mask and lowers sampling.
        stub.config["redaction"] = [MASK_CARDS]
        stub.config["sampling_rate"] = 0.0
        stub.config_etag = 'W/"rev-2"'

        assert eventually(lambda: client.stats()["redaction_rules"] == 1)
        assert client.options.sampling_rate == 0.0
    finally:
        client.close(timeout=2)


def test_a_limit_the_console_relaxes_is_relaxed_but_never_past_the_constructor(
    stub: StubServer, shared_http_client
) -> None:
    client = build_client(stub, shared_http_client, sampling_rate=0.5, capture_input=True)
    try:
        stub.config.update({"sampling_rate": 0.1, "capture_input": False, "batch_max_spans": 50})
        client.config()
        assert client.options.sampling_rate == 0.1
        assert client.options.capture_input is False
        assert client.options.batch_max_spans == 50

        stub.config.update({"sampling_rate": 1.0, "capture_input": True, "batch_max_spans": 500})
        stub.config_etag = 'W/"rev-2"'
        client.config(refresh=True)
        assert client.options.sampling_rate == 0.5, "the ratchet only ever went one way"
        assert client.options.capture_input is True
        assert client.options.batch_max_spans == 500
    finally:
        client.close(timeout=2)


def test_a_run_queued_before_the_rules_arrived_is_redacted_before_it_is_sent(
    stub: StubServer, shared_http_client
) -> None:
    stub.config["redaction"] = [MASK_CARDS]
    client = build_client(stub, shared_http_client)
    try:
        with client.trace("run", input={"card": CARD}, metadata={"note": "plain"}) as run:
            with client.span("charge", type="tool", input={"pan": CARD}) as span:
                span.set_output("charged " + CARD)
            run.set_output({"receipt": CARD})
        client.log_guardrail_event("pii-filter", action_taken="Masked", sample="card " + CARD)
        assert client.stats()["pending"] == 2  # built, queued, not sent

        client.config()  # the rules arrive
        assert client.flush(timeout=5) is True

        (row,) = sent(stub, "traces")
        assert row["input"] == {"card": "[redacted by policy]"}
        assert row["output"] == {"receipt": "[redacted by policy]"}
        assert row["spans"][0]["input"] == {"pan": "[redacted by policy]"}
        assert row["spans"][0]["output"] == {"value": "charged [redacted by policy]"}
        assert row["metadata"]["note"] == "plain"
        (event,) = sent(stub, "events")
        assert event["sample"] == "card [redacted by policy]"
        for request in stub.requests:
            assert CARD not in str(request.body or "")
    finally:
        client.close(timeout=2)


def test_the_first_send_waits_for_a_start_up_read_that_is_still_in_flight(
    stub: StubServer, shared_http_client
) -> None:
    """A script that traces once and flushes at once must not beat its own rules out."""
    stub.config["redaction"] = [MASK_CARDS]
    release = threading.Event()
    original = stub.handle

    def slow_config(request: Any) -> Any:
        if request.path == CONFIG:
            release.wait(timeout=5)
        return original(request)

    stub.handle = slow_config  # type: ignore[method-assign]
    client = build_client(stub, shared_http_client, bootstrap=True)
    try:
        with client.trace("run", input={"card": CARD}):
            pass
        threading.Timer(0.3, release.set).start()
        assert client.flush(timeout=5) is True
        assert CARD not in str(sent(stub, "traces"))
    finally:
        release.set()
        client.close(timeout=2)


def test_a_start_up_read_that_hangs_holds_back_only_the_first_send(
    stub: StubServer, shared_http_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grace is spent once, not once per flush.

    A control plane that accepts the connection and then says nothing keeps the
    start-up read open for its whole timeout. Waiting the grace out on every
    wake of the worker put it on every ``flush()`` in that window -- and the
    agent this was written for flushes after every call.
    """
    monkeypatch.setattr(client_module, "BOOTSTRAP_GRACE_SECONDS", 1.0)
    release = threading.Event()
    original = stub.handle

    def hung_config(request: Any) -> Any:
        if request.path == CONFIG:
            release.wait(timeout=10)
        return original(request)

    stub.handle = hung_config  # type: ignore[method-assign]
    client = build_client(stub, shared_http_client, bootstrap=True)
    try:
        with client.trace("first"):
            pass
        assert client.flush(timeout=5) is True  # this one may wait the grace out

        with client.trace("second"):
            pass
        began = time.monotonic()
        assert client.flush(timeout=5) is True
        assert time.monotonic() - began < 0.7, "the grace was waited out a second time"
        assert {row["name"] for row in sent(stub, "traces")} == {"first", "second"}
    finally:
        release.set()
        client.close(timeout=2)


def test_a_client_that_does_not_bootstrap_never_asks(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, flush_interval_seconds=0.05)
    try:
        with client.trace("run"):
            pass
        client.flush(timeout=5)
        time.sleep(0.2)
        assert config_reads(stub) == []
    finally:
        client.close(timeout=2)


def test_a_forked_child_does_not_wait_on_the_parents_start_up_read(stub: StubServer) -> None:
    client = FulcrumOps(
        api_key="fo_test_key", base_url=stub.base_url, set_as_default=False, flush_on_exit=False
    )
    try:
        client._reset_after_fork()
        assert client._bootstrapped.is_set()
    finally:
        client.close(timeout=2)
