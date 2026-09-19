"""A provider call must never leave anything behind in the caller's context.

The provider wrappers open a span where the call is *made* and close it where
the call *finishes*, and those are not always the same context: an async call
awaited under ``asyncio.gather`` finishes in a task's copy, a stream is drained
by whoever iterates it. A lone span used to make its implicit trace current the
moment it was created, so closing it elsewhere left a finished trace current in
the caller's context for good -- and every later span there was appended to a
run that had already been sent. No error, no log line; a poller or a consumer
loop simply stopped reporting until the process was restarted.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, List

import pytest

from fulcrum_ops import FulcrumOps, trace
from fulcrum_ops.integrations import track_openai

from .conftest import sent
from .stub_server import StubServer


def obj(**fields: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**fields)


def response(text: str = "refer") -> types.SimpleNamespace:
    """A Responses-API result, as an Azure AI Foundry agent returns it."""
    return obj(
        id="resp_1",
        model="gpt-5-2026-01-01",
        output_text=text,
        usage=obj(input_tokens=900, output_tokens=210, total_tokens=1110),
    )


class AsyncResponses:
    async def create(self, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return response(str(kwargs.get("input")))


class SyncResponses:
    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return iter([obj(type="response.output_text.delta", delta="re"),
                         obj(type="response.output_text.delta", delta="fer")])
        return response()


class Failing:
    def create(self, **kwargs: Any) -> Any:
        raise RuntimeError("429 from the model")


def test_gathered_provider_calls_are_two_runs_and_leave_the_context_clean(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(obj(responses=AsyncResponses()), fulcrum=client)
    seen: List[Any] = []

    async def run() -> None:
        await asyncio.gather(
            openai.responses.create(input="first"), openai.responses.create(input="second")
        )
        # The gather ran each call in a task's copy of this context. Nothing
        # they did may still be current here ...
        seen.append(client.current_trace())
        # ... so the next call is a run of its own, not a step on a dead one.
        await asyncio.gather(openai.responses.create(input="third"))

    asyncio.run(run())
    assert seen == [None]
    assert client.flush(timeout=5) is True

    rows = sent(stub, "traces")
    assert sorted(row["spans"][0]["input"]["input"] for row in rows) == ["first", "second", "third"]
    assert all(len(row["spans"]) == 1 for row in rows), "each call is its own run"
    assert sent(stub, "spans") == []


def test_an_unfinished_stream_does_not_capture_the_callers_context(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(obj(responses=SyncResponses()), fulcrum=client)

    stream = openai.responses.create(input="hello", stream=True)
    # The stream's span is open, and will be for as long as somebody else takes
    # to drain it. That is its business, not the context's.
    assert client.current_trace() is None
    assert client.current_span() is None

    with client.trace("unrelated") as run:
        pass
    assert "".join(chunk.delta for chunk in stream) == "refer"
    client.flush(timeout=5)

    rows = {row["name"]: row for row in sent(stub, "traces")}
    assert set(rows) == {"unrelated", "openai.responses"}
    assert "spans" not in rows["unrelated"], "the stream's span belongs to its own run"
    assert rows["unrelated"]["id"] == run.id


def test_a_provider_failure_outside_a_trace_fails_the_run(
    client: FulcrumOps, stub: StubServer
) -> None:
    """The span recorded the error and the run it was the whole of listed as completed."""
    openai = track_openai(obj(responses=Failing()), fulcrum=client)

    with pytest.raises(RuntimeError, match="429"):
        openai.responses.create(input="hello")
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["spans"][0]["error_info"]["exception_type"] == "RuntimeError"
    assert row["error_info"]["exception_type"] == "RuntimeError"
    assert row["error_info"]["message"] == "429 from the model"


def test_a_span_that_outlives_its_trace_is_streamed_not_lost(
    client: FulcrumOps, stub: StubServer
) -> None:
    """A handler that returns a streamed completion ends its trace before the stream is read."""
    openai = track_openai(obj(responses=SyncResponses()), fulcrum=client)

    @trace(name="chat-handler", client=client)
    def handler() -> Any:
        return openai.responses.create(input="hello", stream=True)

    stream = handler()  # the trace is closed and queued here
    assert [chunk.delta for chunk in stream] == ["re", "fer"]
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    (late,) = sent(stub, "spans")
    assert late["trace_id"] == row["id"]
    assert late["name"] == "openai.responses"
    assert late["output"] == {"text": "refer", "streamed": True}


def test_a_lone_span_is_still_current_inside_its_with_block(
    client: FulcrumOps, stub: StubServer
) -> None:
    with client.span("outer", type="tool") as outer:
        assert client.current_trace() is outer.trace
        with client.span("inner") as inner:
            assert inner.trace is outer.trace
    assert client.current_trace() is None
    assert client.current_span() is None
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    spans = {span["name"]: span for span in row["spans"]}
    assert spans["inner"]["parent_span_id"] == spans["outer"]["id"]


def test_a_span_is_never_parented_into_another_trace(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("outer"):
        with client.span("step") as step:
            other = client.trace("detached")  # built, never entered
            child = other.span("root-of-the-other")
            child.end()
            other.end()
    client.flush(timeout=5)

    rows = {row["name"]: row for row in sent(stub, "traces")}
    assert "parent_span_id" not in rows["detached"]["spans"][0]
    assert rows["outer"]["spans"][0]["id"] == step.id


def test_the_customers_one_line_integration(client: FulcrumOps, stub: StubServer) -> None:
    """``@trace(type="llm")`` on a method that calls a Foundry agent inside ``asyncio.to_thread``."""
    openai = track_openai(obj(responses=SyncResponses()), fulcrum=client)

    class AgentClient:
        @trace(name="underwriting_insight", type="llm", client=client)
        def get_insight(self, email_body: str) -> str:
            reply = openai.responses.create(
                input=[{"role": "user", "content": email_body}],
                extra_body={"agent_reference": {"name": "uw-agent", "type": "agent_reference"}},
            )
            return reply.output_text

    async def poll_once() -> str:
        result = await asyncio.to_thread(AgentClient().get_insight, "please quote")
        assert client.current_trace() is None
        return result

    assert asyncio.run(poll_once()) == "refer"
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    spans = {span["name"]: span for span in row["spans"]}
    assert set(spans) == {"underwriting_insight", "openai.responses"}
    call = spans["openai.responses"]
    assert call["parent_span_id"] == spans["underwriting_insight"]["id"]
    assert call["type"] == "llm"
    assert call["model"] == "gpt-5-2026-01-01"
    assert call["usage"] == {"prompt_tokens": 900, "completion_tokens": 210, "total_tokens": 1110}
    assert call["output"] == {"text": "refer"}
