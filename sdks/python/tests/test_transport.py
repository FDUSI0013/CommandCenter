"""The HTTP layer, and the package's public surface."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

import fulcrum_ops
from fulcrum_ops import FulcrumOps, is_valid_id, new_id
from fulcrum_ops._version import SDK_NAME, SDK_VERSION, USER_AGENT
from fulcrum_ops.options import resolve_options
from fulcrum_ops.transport import Transport

from .stub_server import StubServer


# ------------------------------------------------------------------ requests


def test_every_request_identifies_the_sdk_and_the_key(
    client: FulcrumOps, stub: StubServer
) -> None:
    client.config()
    headers = stub.requests[0].headers

    assert headers["authorization"] == "Bearer fo_test_key"
    assert headers["x-fulcrum-sdk"] == "python"
    assert headers["x-fulcrum-sdk-version"] == SDK_VERSION
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept"] == "application/json"


def test_a_workspace_and_extra_headers_are_sent(stub: StubServer, shared_http_client) -> None:
    client = FulcrumOps(
        api_key="k",
        base_url=stub.base_url,
        workspace="acme",
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        headers={"X-Tenant": "eu-1"},
        http_client=shared_http_client,
    )
    try:
        client.config()
        headers = stub.requests[0].headers
        assert headers["x-fulcrum-workspace"] == "acme"
        assert headers["x-tenant"] == "eu-1"
    finally:
        client.close(timeout=2)


def test_paths_are_joined_onto_the_versioned_root() -> None:
    transport = Transport(resolve_options(base_url="https://example.com/api/v1"))

    assert transport._url("ingest/traces") == "https://example.com/api/v1/ingest/traces"
    assert transport._url("/ingest/traces") == "https://example.com/api/v1/ingest/traces"
    # The config document names endpoints with the full prefix already on them.
    assert transport._url("/api/v1/ingest/spans") == "https://example.com/api/v1/ingest/spans"
    assert transport._url("https://elsewhere.test/x") == "https://elsewhere.test/x"


def test_the_http_client_is_built_lazily_and_only_once(stub: StubServer) -> None:
    """No key, no call, no connection pool: constructing a client costs nothing."""
    transport = Transport(resolve_options(api_key="k", base_url=stub.base_url))
    assert transport._client is None

    transport.request("GET", "ingest/config")
    first = transport._client
    assert first is not None

    transport.request("GET", "ingest/config")
    assert transport._client is first

    transport.close()
    assert transport._client is None


def test_an_injected_client_is_not_closed_by_the_sdk(
    stub: StubServer, shared_http_client
) -> None:
    """It belongs to the caller, who may well still be using it."""
    transport = Transport(
        resolve_options(api_key="k", base_url=stub.base_url, http_client=shared_http_client)
    )
    transport.request("GET", "ingest/config")
    transport.close()

    assert shared_http_client.is_closed is False


def test_a_closed_transport_refuses_further_requests(stub: StubServer) -> None:
    transport = Transport(resolve_options(api_key="k", base_url=stub.base_url))
    transport.close()
    with pytest.raises(fulcrum_ops.FulcrumOpsError, match="closed"):
        transport.request("GET", "ingest/config")


def test_a_non_json_response_body_is_returned_rather_than_raising(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("GET", "/api/v1/ingest/config", 200, "plain text, not JSON")
    transport = Transport(
        resolve_options(api_key="k", base_url=stub.base_url, http_client=shared_http_client)
    )
    response = transport.request("GET", "ingest/config")
    assert response.ok
    assert response.body == "plain text, not JSON"


# ---------------------------------------------------------------------- ids


def test_ids_are_well_formed_uuids() -> None:
    """The control plane validates the shape and refuses anything else."""
    for _ in range(200):
        value = new_id()
        parsed = uuid.UUID(value)
        assert parsed.version == 7
        assert parsed.variant == uuid.RFC_4122
        assert is_valid_id(value)


def test_ids_are_unique_and_time_ordered() -> None:
    """Time-ordered keys cluster by insert time instead of scattering an index."""
    values = [new_id() for _ in range(2_000)]
    assert len(set(values)) == 2_000
    assert values == sorted(values)


def test_is_valid_id_rejects_what_the_contract_would() -> None:
    assert is_valid_id("a" * 64) is True
    assert is_valid_id("a" * 65) is False
    assert is_valid_id("") is False
    assert is_valid_id("   ") is False
    assert is_valid_id(None) is False
    assert is_valid_id(42) is False


# ----------------------------------------------------------- public surface


def test_everything_advertised_is_importable() -> None:
    missing = [name for name in fulcrum_ops.__all__ if not hasattr(fulcrum_ops, name)]
    assert missing == []


def test_the_sdk_identifies_itself_consistently() -> None:
    assert SDK_NAME == "python"
    assert USER_AGENT == "fulcrum-ops-sdk-python/{0}".format(SDK_VERSION)
    assert fulcrum_ops.__version__ == SDK_VERSION


def test_the_declared_version_matches_the_packaging_metadata() -> None:
    """One version, in two files that drift apart the moment nobody checks."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert declared is not None
    assert declared.group(1) == SDK_VERSION


def test_the_package_ships_its_typing_marker() -> None:
    marker = Path(fulcrum_ops.__file__).resolve().parent / "py.typed"
    assert marker.exists(), "pyproject advertises Typing :: Typed"


def test_importing_the_package_pulls_in_nothing_provider_shaped() -> None:
    """Most agents use one provider; none of them may be a dependency of this SDK.

    Run in a clean interpreter on purpose. Asserting against ``sys.modules`` in
    this process would only prove that whichever test ran first happened not to
    import them, which is a fact about test ordering rather than about the
    package.
    """
    import subprocess
    import sys

    script = (
        "import sys, json;"
        "import fulcrum_ops;"
        "import fulcrum_ops.integrations as i;"
        "before = [m for m in ('openai','anthropic','langchain_core','langchain')"
        " if m in sys.modules];"
        "lazy = 'fulcrum_ops.integrations.anthropic' not in sys.modules;"
        "fn = i.track_anthropic;"
        "loaded = 'fulcrum_ops.integrations.anthropic' in sys.modules;"
        "print(json.dumps({'providers': before, 'lazy': lazy, 'loaded': loaded}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr

    import json

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["providers"] == [], "importing fulcrum_ops pulled in a provider package"
    assert result["lazy"] is True, "the integrations package imported a wrapper eagerly"
    assert result["loaded"] is True, "the lazy attribute did not load its module"


def test_an_unknown_integration_name_raises_attribute_error() -> None:
    import fulcrum_ops.integrations as integrations

    with pytest.raises(AttributeError):
        integrations.not_a_real_integration
