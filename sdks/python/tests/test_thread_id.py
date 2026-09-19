"""A decorated agent must be able to say which conversation each call belongs to.

The Sessions, Conversation State and Memory views are built from threads, and a
thread exists only for runs that carry a ``thread_id``. The decorator took one —
as a string, evaluated once when the module is imported. So an agent
instrumented with ``@trace`` had two options: no thread at all, and every one of
those views empty; or one constant, and every customer's conversation filed
under the same session.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from typing import Iterator

import fulcrum_ops
from fulcrum_ops import FulcrumOps, trace

from .conftest import sent
from .stub_server import StubServer


@dataclasses.dataclass
class Email:
    conversation_id: str
    body: str


def test_the_thread_is_worked_out_from_each_calls_own_arguments(
    client: FulcrumOps, stub: StubServer
) -> None:
    class AgentClient:
        @trace(
            name="underwriting_insight",
            type="llm",
            thread_id=lambda self, email, **_: email.conversation_id,
            client=client,
        )
        def get_insight(self, email: Email, *, urgent: bool = False) -> str:
            return "refer"

    agent = AgentClient()
    assert agent.get_insight(Email("submission-17", "please quote")) == "refer"
    assert agent.get_insight(Email("submission-18", "renewal"), urgent=True) == "refer"
    client.flush(timeout=5)

    assert [row["thread_id"] for row in sent(stub, "traces")] == ["submission-17", "submission-18"]
    # The caller's function is still the caller's function.
    assert list(inspect.signature(AgentClient.get_insight).parameters) == ["self", "email", "urgent"]


def test_a_constant_thread_id_still_works(client: FulcrumOps, stub: StubServer) -> None:
    @trace(thread_id="nightly-batch", client=client)
    def run_batch() -> None:
        return None

    run_batch()
    client.flush(timeout=5)
    assert sent(stub, "traces")[0]["thread_id"] == "nightly-batch"


def test_a_thread_id_callable_that_raises_costs_the_thread_not_the_call(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(thread_id=lambda payload: payload["conversation"], client=client)
    def handle(payload: dict) -> str:
        return "handled"

    assert handle({}) == "handled"  # KeyError inside the lambda; the agent never sees it
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert "thread_id" not in row


def test_the_body_can_name_the_thread_once_it_knows_it(stub: StubServer) -> None:
    """The id is often inside what the function parses, not among its arguments."""
    fulcrum_ops.configure(
        api_key="fo_test_key", base_url=stub.base_url, agent="bridge", bootstrap=False
    )
    try:

        @trace(name="underwriting_insight", type="llm")
        def get_insight(raw: str) -> str:
            conversation = raw.split(":", 1)[0]
            assert fulcrum_ops.set_thread_id(conversation) is True
            return "refer"

        get_insight("submission-17:please quote")
        fulcrum_ops.flush()
        assert sent(stub, "traces")[0]["thread_id"] == "submission-17"
        assert fulcrum_ops.set_thread_id("nobody-is-listening") is False
    finally:
        fulcrum_ops.shutdown()


def test_an_inner_step_can_name_the_thread_for_a_run_that_has_none(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(name="reply", type="llm", thread_id=lambda session, text: session, client=client)
    def reply(session: str, text: str) -> str:
        return text.upper()

    @trace(name="turn", client=client)
    def turn(session: str, text: str) -> str:
        return reply(session, text)

    turn("chat-9", "hello")
    client.flush(timeout=5)
    assert sent(stub, "traces")[0]["thread_id"] == "chat-9"


def test_coroutines_and_generators_resolve_the_thread_too(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(thread_id=lambda session: session, client=client)
    async def answer(session: str) -> str:
        return "ok"

    @trace(thread_id=lambda session: session, client=client)
    def stream(session: str) -> Iterator[str]:
        yield "a"

    asyncio.run(answer("chat-1"))
    list(stream("chat-2"))
    client.flush(timeout=5)
    assert sorted(row["thread_id"] for row in sent(stub, "traces")) == ["chat-1", "chat-2"]


def test_a_lone_span_can_name_the_thread_of_the_run_it_opens(
    client: FulcrumOps, stub: StubServer
) -> None:
    """``client.span()`` outside a trace opens the run itself, and had no way to file it."""
    with client.span("classify", type="llm", thread_id="chat-3"):
        pass

    with client.trace("turn", thread_id="chat-4"):
        with client.span("step", thread_id="not-this-one"):
            pass
    with client.trace("untold"):
        with client.span("step", thread_id="chat-5"):
            pass

    client.flush(timeout=5)
    threads = {row["name"]: row.get("thread_id") for row in sent(stub, "traces")}
    assert threads == {"classify": "chat-3", "turn": "chat-4", "untold": "chat-5"}
