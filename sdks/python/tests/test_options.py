"""Option resolution: argument, then environment, then default."""

from __future__ import annotations

import os

import pytest

from fulcrum_ops import DEFAULT_BASE_URL, ConfigurationError, FulcrumOps, normalise_base_url
from fulcrum_ops.options import resolve_options


def test_arguments_win_over_the_environment() -> None:
    os.environ["FULCRUM_OPS_API_KEY"] = "from-env"
    os.environ["FULCRUM_OPS_AGENT"] = "env-agent"

    options = resolve_options(api_key="from-argument", agent="argument-agent")
    assert options.api_key == "from-argument"
    assert options.agent == "argument-agent"


def test_the_environment_wins_over_the_defaults() -> None:
    os.environ["FULCRUM_OPS_API_KEY"] = "from-env"
    os.environ["FULCRUM_OPS_ENVIRONMENT"] = "Staging"
    os.environ["FULCRUM_OPS_SAMPLING_RATE"] = "0.25"
    os.environ["FULCRUM_OPS_CAPTURE_INPUT"] = "false"

    options = resolve_options()
    assert options.api_key == "from-env"
    assert options.environment == "Staging"
    assert options.sampling_rate == 0.25
    assert options.capture_input is False
    assert options.enabled is True


def test_no_key_anywhere_disables_reporting() -> None:
    options = resolve_options()
    assert options.api_key is None
    assert options.enabled is False


def test_the_disabled_switch_overrides_a_present_key() -> None:
    os.environ["FULCRUM_OPS_API_KEY"] = "k"
    os.environ["FULCRUM_OPS_DISABLED"] = "1"
    assert resolve_options().enabled is False


def test_enabled_can_be_forced_on_to_make_a_missing_key_loud() -> None:
    assert resolve_options(enabled=True).enabled is True


def test_the_default_base_url_points_at_a_local_control_plane() -> None:
    assert resolve_options().base_url == DEFAULT_BASE_URL


def test_a_bare_host_is_pointed_at_the_versioned_api_root() -> None:
    """The commonest mistake, and one that fails silently as a 404 on every call."""
    assert normalise_base_url("https://controlplane.example.com") == (
        "https://controlplane.example.com/api/v1"
    )
    assert normalise_base_url("https://controlplane.example.com/") == (
        "https://controlplane.example.com/api/v1"
    )


def test_a_trailing_slash_is_removed() -> None:
    assert normalise_base_url("https://example.com/api/v1/") == "https://example.com/api/v1"


def test_an_explicit_path_is_left_alone() -> None:
    assert normalise_base_url("https://example.com/proxy/api/v1") == (
        "https://example.com/proxy/api/v1"
    )


@pytest.mark.parametrize(
    "value", ["", "   ", "ftp://example.com", "not a url", "https://", "//example.com"]
)
def test_an_unusable_base_url_is_a_construction_error(value: str) -> None:
    """Worth raising for: nothing the SDK does afterwards could work."""
    with pytest.raises(ConfigurationError):
        normalise_base_url(value)


def test_numeric_options_are_clamped_into_their_legal_range() -> None:
    options = resolve_options(
        sampling_rate=5.0,
        timeout_seconds=0.0,
        batch_max_items=0,
        batch_max_bytes=1,
        max_queue_size=0,
        retry_max_attempts=99,
        flush_interval_seconds=0.0,
    )
    assert options.sampling_rate == 1.0
    assert options.timeout_seconds == 0.1
    assert options.batch_max_items == 1
    assert options.batch_max_bytes == 1_024
    assert options.max_queue_size == 1
    assert options.retry_max_attempts == 10
    assert options.flush_interval_seconds == 0.05

    assert resolve_options(sampling_rate=-1.0).sampling_rate == 0.0


def test_the_max_backoff_never_sits_below_the_first_step() -> None:
    options = resolve_options(retry_backoff_seconds=10.0, retry_max_backoff_seconds=1.0)
    assert options.retry_max_backoff_seconds == 10.0


def test_a_malformed_environment_value_falls_back_rather_than_crashing() -> None:
    os.environ["FULCRUM_OPS_SAMPLING_RATE"] = "not a number"
    os.environ["FULCRUM_OPS_CAPTURE_OUTPUT"] = "perhaps"
    options = resolve_options()
    assert options.sampling_rate == 1.0
    assert options.capture_output is True


def test_the_redacted_view_never_carries_the_key() -> None:
    """``options`` ends up in debug logs and bug reports."""
    options = resolve_options(api_key="fo_live_supersecret", on_error=lambda e, o: None)
    view = options.redacted()

    assert view["api_key"] == "set"
    assert "fo_live_supersecret" not in str(view)
    assert "on_error" not in view and "http_client" not in view


def test_the_client_exposes_what_it_resolved(stub, shared_http_client) -> None:
    client = FulcrumOps(
        api_key="k",
        base_url=stub.base_url,
        environment="Production",
        agent="checkout-agent",
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        http_client=shared_http_client,
    )
    try:
        assert client.environment == "Production"
        assert client.agent == "checkout-agent"
        assert client.enabled is True
        assert client.options.base_url == stub.base_url
    finally:
        client.close(timeout=2)
