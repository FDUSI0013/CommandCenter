"""Shared fixtures.

Every fixture that builds a client passes ``set_as_default=False`` unless the
test is specifically about the default client. Module-level state that leaks
between tests is the classic way an SDK test suite starts passing for the wrong
reason, and ``configure()`` installs exactly that kind of state.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import pytest

from fulcrum_ops import FulcrumOps
from fulcrum_ops import client as client_module

from .stub_server import StubServer

ENV_VARS = [
    "FULCRUM_OPS_API_KEY",
    "FULCRUM_OPS_BASE_URL",
    "FULCRUM_OPS_WORKSPACE",
    "FULCRUM_OPS_ENVIRONMENT",
    "FULCRUM_OPS_AGENT",
    "FULCRUM_OPS_DEBUG",
    "FULCRUM_OPS_DISABLED",
    "FULCRUM_OPS_SAMPLING_RATE",
    "FULCRUM_OPS_TIMEOUT_SECONDS",
    "FULCRUM_OPS_CAPTURE_INPUT",
    "FULCRUM_OPS_CAPTURE_OUTPUT",
]


@pytest.fixture(autouse=True)
def clean_environment() -> Iterator[None]:
    """Run every test as though the developer's shell were empty.

    Without this the suite's result depends on whoever ran it: a real
    ``FULCRUM_OPS_API_KEY`` in the environment would turn the "no key disables
    reporting" tests green for the wrong reason, and a real ``BASE_URL`` would
    point the whole suite at somebody's control plane.
    """
    saved = {name: os.environ.pop(name, None) for name in ENV_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def clean_default_client() -> Iterator[None]:
    """Make sure no test inherits a default client from the one before it."""
    client_module.set_default_client(None)
    yield
    client_module.set_default_client(None)


@pytest.fixture
def stub() -> Iterator[StubServer]:
    server = StubServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def shared_http_client() -> Iterator[Any]:
    """One httpx client for the whole suite, injected via the ``http_client`` option.

    Constructing an ``httpx.Client`` builds an SSL context, which costs the best
    part of a second on a machine whose trust store is large. The SDK pays that
    once, lazily, on its worker thread — so it never lands on a caller — but a
    suite that builds thirty clients would pay it thirty times. Sharing one here
    keeps the suite quick and exercises the documented injection point at the
    same time. ``test_transport`` covers the lazy default path on its own.
    """
    import httpx

    with httpx.Client(timeout=httpx.Timeout(10.0)) as instance:
        yield instance


def build_client(stub: StubServer, http_client: Any = None, **overrides: Any) -> FulcrumOps:
    """A client wired to the stub, with the settings a test almost always wants."""
    options: dict = {
        "api_key": "fo_test_key",
        "base_url": stub.base_url,
        "agent": "checkout-agent",
        "bootstrap": False,
        "set_as_default": False,
        "flush_on_exit": False,
        # Long enough that nothing flushes on the timer; the tests flush by hand
        # so an assertion never races the worker thread.
        "flush_interval_seconds": 30.0,
        "retry_backoff_seconds": 0.001,
        "retry_max_backoff_seconds": 0.01,
        "http_client": http_client,
    }
    options.update(overrides)
    return FulcrumOps(**options)


@pytest.fixture
def client(stub: StubServer, shared_http_client: Any) -> Iterator[FulcrumOps]:
    instance = build_client(stub, http_client=shared_http_client)
    try:
        yield instance
    finally:
        instance.close(timeout=2.0)


def sent(stub: StubServer, kind: str) -> list:
    """Every item of ``kind`` the stub received, flattened across batches."""
    out: list = []
    for request in stub.requests:
        if request.path == "/api/v1/ingest/{0}".format(kind) and isinstance(request.body, dict):
            out.extend(request.body.get(kind) or [])
    return out
