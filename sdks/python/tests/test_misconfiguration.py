"""A client that is set up wrong has to say so, once, where a developer will see it.

Two set-ups used to be silent. With no key at all the SDK switched itself off at
DEBUG, so a mistyped variable name produced an agent that ran perfectly and a
console that stayed empty. With a key and no address, the key and every prompt
were posted to ``127.0.0.1:8080`` — whatever happened to be listening there —
and the only clue was a generic connection failure that never mentioned the
address had not been chosen by anybody.
"""

from __future__ import annotations

import logging

import pytest

from fulcrum_ops import DEFAULT_BASE_URL, FulcrumOps
from fulcrum_ops import client as client_module

from .conftest import build_client
from .stub_server import StubServer


@pytest.fixture(autouse=True)
def first_client_of_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_warned_no_key", False)


def warnings(caplog: pytest.LogCaptureFixture) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_key_with_no_address_is_called_out_once_at_configure_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        client = FulcrumOps(
            api_key="fo_test_key", bootstrap=False, set_as_default=False, flush_on_exit=False
        )
    try:
        assert client.options.base_url == DEFAULT_BASE_URL
        (message,) = warnings(caplog)
        assert "FULCRUM_OPS_BASE_URL" in message
        assert DEFAULT_BASE_URL in message
        assert "fo_test_key" not in message, "the key itself is never logged"
    finally:
        client.close(timeout=0)


def test_an_address_from_the_environment_counts_as_chosen(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FULCRUM_OPS_BASE_URL", stub.base_url)
    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        client = FulcrumOps(
            api_key="fo_test_key", bootstrap=False, set_as_default=False, flush_on_exit=False
        )
    try:
        assert warnings(caplog) == []
    finally:
        client.close(timeout=0)


def test_an_explicit_address_says_nothing(
    stub: StubServer, shared_http_client, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        client = build_client(stub, http_client=shared_http_client)
    try:
        assert warnings(caplog) == []
    finally:
        client.close(timeout=0)


def test_a_missing_key_is_said_out_loud_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The typo that started this: the right prefix, the wrong name.
    monkeypatch.setenv("FULCRUM_API_KEY", "fo_live_key")
    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        first = FulcrumOps(set_as_default=False)
        second = FulcrumOps(set_as_default=False)
    assert first.enabled is False and second.enabled is False
    (message,) = warnings(caplog)
    assert "FULCRUM_OPS_API_KEY" in message
    assert "FULCRUM_OPS_DISABLED" in message


def test_reporting_switched_off_on_purpose_is_not_news(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        FulcrumOps(enabled=False, set_as_default=False)
        monkeypatch.setenv("FULCRUM_OPS_DISABLED", "1")
        FulcrumOps(set_as_default=False)
        FulcrumOps(api_key="fo_test_key", set_as_default=False)
    assert warnings(caplog) == []


def test_a_redirect_is_followed_even_when_the_injected_http_client_would_not(
    stub: StubServer, shared_http_client, caplog: pytest.LogCaptureFixture
) -> None:
    """``http://`` in front of a proxy that upgrades to ``https://``.

    The SDK's own client follows redirects; httpx does not by default, so a
    caller-supplied one met a 308 on every batch. 308 is not retryable, and the
    batch was dropped with ``The API returned HTTP 308``.
    """
    path = "/api/v1/ingest/traces"
    client = build_client(stub, http_client=shared_http_client)
    try:
        with caplog.at_level("WARNING", logger="fulcrum_ops"):
            for name in ("first", "second"):
                stub.inject("POST", path, 308, None, headers={"location": path})
                with client.trace(name):
                    pass
                assert client.flush(timeout=5) is True
        stats = client.stats()
        assert stats["accepted"] == 2 and stats["dropped_failed"] == 0
        redirects = [m for m in warnings(caplog) if "redirects to" in m]
        assert len(redirects) == 1, "said once, not on every batch"
        assert "follow_redirects=True" in redirects[0]
    finally:
        client.close(timeout=0)


def test_a_redirect_to_another_host_is_not_followed_with_the_api_key(
    stub: StubServer, shared_http_client, caplog: pytest.LogCaptureFixture
) -> None:
    other = StubServer().start()
    try:
        stub.inject(
            "POST",
            "/api/v1/ingest/traces",
            308,
            None,
            headers={"location": "http://localhost:{0}/api/v1/ingest/traces".format(other.port)},
        )
        client = build_client(stub, http_client=shared_http_client)
        try:
            with caplog.at_level("WARNING", logger="fulcrum_ops"):
                with client.trace("run"):
                    pass
                assert client.flush(timeout=5) is False
            assert other.requests == [], "the key went to a host nobody configured"
            assert any("another host" in m for m in warnings(caplog))
        finally:
            client.close(timeout=0)
    finally:
        other.stop()


def test_the_environment_rides_on_the_envelope(stub: StubServer, shared_http_client) -> None:
    """Scores and events have no metadata to carry it in, as traces do."""
    client = build_client(stub, http_client=shared_http_client, environment="Production")
    try:
        client.score("trace-1", "helpfulness", 1.0)
        assert client.flush(timeout=5) is True
        (request,) = [r for r in stub.requests if r.path == "/api/v1/ingest/scores"]
        assert request.body["environment"] == "Production"
    finally:
        client.close(timeout=0)
