"""The one property that outranks every other: losing telemetry must never break
the caller's agent.

Each test here breaks something different — the network, the key, the payload,
the caller's own error handler — and asserts the same thing: the decorated
function still returned, and it returned the right value.
"""

from __future__ import annotations

import socket
import threading

import pytest

from fulcrum_ops import FulcrumOps, trace

from .conftest import build_client
from .stub_server import StubServer


def free_port() -> int:
    """A port with nothing listening on it, for the dead-endpoint tests."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


def test_a_dead_control_plane_does_not_stop_the_agent(shared_http_client) -> None:
    """Point the SDK at a closed port and the caller's function still returns."""
    errors: list = []
    client = FulcrumOps(
        api_key="fo_test_key",
        base_url="http://127.0.0.1:{0}/api/v1".format(free_port()),
        agent="checkout-agent",
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        flush_interval_seconds=30.0,
        retry_max_attempts=1,
        retry_backoff_seconds=0.001,
        retry_max_backoff_seconds=0.002,
        http_client=shared_http_client,
        on_error=lambda error, operation: errors.append((error, operation)),
    )
    try:
        with client.trace("support-question", input={"q": "hi"}) as run:
            with client.span("retrieval", type="tool") as span:
                span.set_output({"chunks": 3})
            run.set_output({"answer": "still computed"})

        assert client.flush(timeout=10) is True, "flush must settle rather than hang"

        stats = client.stats()
        assert stats["dropped_failed"] == 1
        assert stats["accepted"] == 0
        assert errors, "the failure must reach on_error rather than vanishing"
        assert errors[-1][1] == "ingest.traces"
    finally:
        client.close(timeout=2)


def test_a_decorated_function_returns_its_value_with_a_dead_endpoint(
    shared_http_client,
) -> None:
    client = FulcrumOps(
        api_key="fo_test_key",
        base_url="http://127.0.0.1:{0}/api/v1".format(free_port()),
        bootstrap=False,
        flush_on_exit=False,
        retry_max_attempts=0,
        http_client=shared_http_client,
    )
    try:

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        @trace
        def stream(n: int):
            for i in range(n):
                yield i

        assert add(2, 3) == 5
        assert list(stream(3)) == [0, 1, 2]

        # And an exception the caller raised is still the exception they get.
        @trace
        def explode() -> None:
            raise KeyError("caller's own bug")

        with pytest.raises(KeyError, match="caller's own bug"):
            explode()

        client.flush(timeout=10)
    finally:
        client.close(timeout=2)


def test_close_and_flush_never_raise_against_a_dead_endpoint(shared_http_client) -> None:
    """They are what ends up in a ``finally`` block and a shutdown hook."""
    client = FulcrumOps(
        api_key="fo_test_key",
        base_url="http://127.0.0.1:{0}/api/v1".format(free_port()),
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        retry_max_attempts=0,
        http_client=shared_http_client,
    )
    with client.trace("run"):
        pass

    assert client.flush(timeout=10) in (True, False)
    client.close(timeout=5)  # must not raise


def test_a_missing_api_key_disables_reporting_without_changing_behaviour() -> None:
    """A missing environment variable must not change what the program does."""
    client = FulcrumOps(bootstrap=False, set_as_default=True, flush_on_exit=False)
    try:
        assert client.enabled is False

        @trace
        def answer(question: str) -> str:
            return "42"

        assert answer("meaning of life") == "42"

        with client.trace("run") as run:
            with client.span("step", type="tool") as span:
                span.set_output({"ok": True})
            run.set_output({"done": True})

        assert client.score("t-1", "helpfulness", 1.0) is False
        assert client.log_feedback(rating=5) is False
        assert client.flush(timeout=1) is True
        # And nothing above reached out: a disabled client makes no requests at
        # all, including the bootstrap read.
        assert client.config() == {}
    finally:
        client.close(timeout=1)


def test_an_unserialisable_argument_does_not_break_the_call(
    stub: StubServer, shared_http_client
) -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("broken repr")

    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        def handle(payload: object) -> str:
            return "handled"

        assert handle(Hostile()) == "handled"
        client.flush(timeout=5)
        assert client.stats()["accepted"] == 1
    finally:
        client.close(timeout=2)


def test_a_throwing_error_handler_is_the_callers_bug_not_the_sdks(
    shared_http_client,
) -> None:
    def broken_handler(error, operation):
        raise RuntimeError("my handler is broken too")

    client = FulcrumOps(
        api_key="fo_test_key",
        base_url="http://127.0.0.1:{0}/api/v1".format(free_port()),
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        retry_max_attempts=0,
        http_client=shared_http_client,
        on_error=broken_handler,
    )
    try:
        with client.trace("run"):
            pass
        assert client.flush(timeout=10) in (True, False)
    finally:
        client.close(timeout=2)


def test_a_control_plane_that_dies_mid_run_does_not_take_the_agent_with_it(
    shared_http_client,
) -> None:
    """The realistic outage: it was up when the process started, and then it was not."""
    stub = StubServer().start()
    client = build_client(stub, shared_http_client, retry_max_attempts=0)
    try:
        with client.trace("before-the-outage"):
            pass
        assert client.flush(timeout=5) is True
        assert client.stats()["accepted"] == 1

        stub.stop()

        results = []
        for index in range(3):
            with client.trace("during-the-outage-{0}".format(index)):
                results.append(index)
        assert results == [0, 1, 2], "the agent kept working"

        client.flush(timeout=10)
        assert client.stats()["dropped_failed"] == 3
        assert client.stats()["accepted"] == 1
    finally:
        client.close(timeout=2)
        stub.stop()


def test_flush_returns_false_rather_than_hanging_when_it_cannot_finish(
    shared_http_client,
) -> None:
    """A caller who asked for a bounded wait gets one."""
    blocked = threading.Event()
    stub = StubServer().start()

    original = stub.handle

    def slow_handle(request):
        if request.path.startswith("/api/v1/ingest/"):
            blocked.wait(timeout=5)
        return original(request)

    stub.handle = slow_handle  # type: ignore[method-assign]

    client = build_client(stub, shared_http_client, retry_max_attempts=0)
    try:
        with client.trace("slow"):
            pass
        assert client.flush(timeout=0.2) is False
    finally:
        blocked.set()
        client.close(timeout=5)
        stub.stop()
