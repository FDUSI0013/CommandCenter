"""The Prompt Manager client, and its cache."""

from __future__ import annotations

import pytest

from fulcrum_ops import FulcrumOps, render_template, template_variables
from fulcrum_ops.errors import NotFoundError

from .stub_server import StubServer


def prompt_requests(stub: StubServer) -> list:
    return [r for r in stub.requests if r.path.startswith("/api/v1/prompts")]


def test_a_prompt_is_fetched_by_name_and_rendered(client: FulcrumOps) -> None:
    prompt = client.get_prompt("support-system")

    assert prompt.id == "prompt-1"
    assert prompt.name == "support-system"
    assert prompt.version == "v3"
    assert prompt.variables == ["customer_name", "topic"]
    assert prompt.format(customer_name="Ada", topic="returns") == (
        "You are helping Ada with returns."
    )


def test_a_prompt_is_fetched_by_id_without_a_search(
    client: FulcrumOps, stub: StubServer
) -> None:
    prompt = client.get_prompt("prompt-1")

    assert prompt.name == "support-system"
    assert [r.path for r in prompt_requests(stub)] == ["/api/v1/prompts/prompt-1"]


def test_a_missing_placeholder_is_left_visible(client: FulcrumOps) -> None:
    """A visible placeholder is a bug someone notices; an empty one is not."""
    prompt = client.get_prompt("support-system")
    assert prompt.format(customer_name="Ada") == "You are helping Ada with {{topic}}."
    assert prompt.render({"customer_name": None, "topic": "returns"}) == (
        "You are helping {{customer_name}} with returns."
    )


def test_an_unpinned_lookup_is_cached_for_a_ttl(client: FulcrumOps, stub: StubServer) -> None:
    client.get_prompt("support-system")
    before = len(prompt_requests(stub))
    client.get_prompt("support-system")
    assert len(prompt_requests(stub)) == before, "the second lookup hit the network"

    client.get_prompt("support-system", refresh=True)
    assert len(prompt_requests(stub)) > before


def test_the_cache_can_be_cleared(client: FulcrumOps, stub: StubServer) -> None:
    client.get_prompt("support-system")
    before = len(prompt_requests(stub))
    client.prompts.clear()
    client.get_prompt("support-system")
    assert len(prompt_requests(stub)) > before


def test_a_pinned_commit_is_fetched_and_cached_forever(
    client: FulcrumOps, stub: StubServer
) -> None:
    pinned = client.get_prompt("support-system", commit="deadbee")

    assert pinned.commit == "deadbee"
    assert pinned.version == "v2"
    assert pinned.template == "Older text for {{customer_name}}."

    before = len(prompt_requests(stub))
    client.get_prompt("support-system", commit="deadbee")
    assert len(prompt_requests(stub)) == before


def test_a_missing_prompt_raises_rather_than_returning_nothing(client: FulcrumOps) -> None:
    """An empty system prompt is a broken agent, not a degraded one."""
    with pytest.raises(NotFoundError, match="no-such-prompt"):
        client.get_prompt("no-such-prompt")


def test_a_missing_commit_raises(client: FulcrumOps) -> None:
    with pytest.raises(NotFoundError):
        client.get_prompt("support-system", commit="nosuchcommit")


def test_template_helpers_stand_alone() -> None:
    template = "Hi {{ name }}, about {{topic}} — {{name}} again."

    assert template_variables(template) == ["name", "topic"]
    assert render_template(template, {"name": "Ada"}) == (
        "Hi Ada, about {{topic}} — Ada again."
    )
    assert render_template(template, {"name": 42, "topic": "orders"}) == (
        "Hi 42, about orders — 42 again."
    )
    assert render_template("", {"a": 1}) == ""
