"""``@trace(type=...)`` on the outermost call: the run must not arrive empty.

A customer instrumented their agent with a single decorator on the one method
that calls the model -- ``@trace(name="underwriting_insight", type="llm")`` --
and every run reached the console with a name, a duration and nothing else: no
steps, no model, no tokens, no cost. At the root the decorator opened a trace
and threw ``type`` away, and a trace cannot carry any of those. These pin the
shape a run takes on the wire when its only instrumentation is that one line.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Iterator, List

import pytest

import fulcrum_ops
from fulcrum_ops import FulcrumOps, trace

from .conftest import sent
from .stub_server import StubServer


@dataclasses.dataclass
class Insight:
    decision: str
    confidence: float


def test_a_root_llm_call_is_a_run_with_one_llm_step(client: FulcrumOps, stub: StubServer) -> None:
    class AgentClient:
        @trace(name="underwriting_insight", type="llm", client=client)
        def get_insight(self, email_body: str, attachments: List[str]) -> Insight:
            span = client.current_span()
            assert span is not None, "a typed root must give the body a span to report on"
            span.set_model("gpt-5", "azure-openai")
            span.set_usage(prompt_tokens=900, completion_tokens=210)
            return Insight(decision="refer", confidence=0.65)

    result = AgentClient().get_insight("please quote", ["application.docx"])
    assert result == Insight(decision="refer", confidence=0.65)  # the caller's contract is untouched
    assert client.flush(timeout=5) is True

    (row,) = sent(stub, "traces")
    assert row["name"] == "underwriting_insight"
    # The run reads as a run: the receiver is dropped, the dataclass is unpacked.
    assert row["input"] == {"email_body": "please quote", "attachments": ["application.docx"]}
    assert row["output"] == {"decision": "refer", "confidence": 0.65}

    (step,) = row["spans"]
    assert step["name"] == "underwriting_insight"
    assert step["type"] == "llm"
    assert step["model"] == "gpt-5"
    assert step["provider"] == "azure-openai"
    assert step["usage"] == {"prompt_tokens": 900, "completion_tokens": 210}
    assert step["input"] == row["input"]
    assert step["output"] == row["output"]
    assert "parent_span_id" not in step


def test_a_general_root_is_still_just_a_trace(client: FulcrumOps, stub: StubServer) -> None:
    @trace(client=client)
    def answer(question: str) -> str:
        assert client.current_span() is None
        return "42"

    answer("what is six by nine?")
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert "spans" not in row


def test_spans_opened_inside_a_typed_root_nest_under_it(client: FulcrumOps, stub: StubServer) -> None:
    @trace(name="lookup", type="tool", client=client)
    def lookup(key: str) -> str:
        return key.upper()

    @trace(name="agent-turn", type="llm", client=client)
    def turn(question: str) -> str:
        return lookup(question)

    turn("abc")
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    spans = {s["name"]: s for s in row["spans"]}
    assert set(spans) == {"agent-turn", "lookup"}
    assert spans["lookup"]["parent_span_id"] == spans["agent-turn"]["id"]


def test_a_failing_typed_root_fails_both_the_step_and_the_run(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(name="call-model", type="llm", client=client)
    def call_model() -> str:
        raise TimeoutError("model did not answer")

    with pytest.raises(TimeoutError):
        call_model()
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["error_info"]["exception_type"] == "TimeoutError"
    assert row["spans"][0]["error_info"]["exception_type"] == "TimeoutError"


def test_nothing_is_left_current_after_a_typed_root(client: FulcrumOps) -> None:
    @trace(name="call-model", type="llm", client=client)
    def call_model() -> str:
        return "ok"

    call_model()
    assert client.current_trace() is None
    assert client.current_span() is None


def test_an_async_typed_root(client: FulcrumOps, stub: StubServer) -> None:
    @trace(name="call-model", type="llm", client=client)
    async def call_model(prompt: str) -> str:
        fulcrum_ops_span = client.current_span()
        assert fulcrum_ops_span is not None
        fulcrum_ops_span.set_model("claude-sonnet-5", "anthropic")
        return prompt[::-1]

    assert asyncio.run(call_model("abc")) == "cba"
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["spans"][0]["type"] == "llm"
    assert row["spans"][0]["model"] == "claude-sonnet-5"


def test_a_streaming_typed_root_keeps_the_callers_context_clean_between_yields(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(name="stream-model", type="llm", client=client)
    def stream() -> Iterator[str]:
        yield "a"
        yield "b"

    seen = []
    for token in stream():
        # Between yields the consumer is running, not the traced generator.
        assert client.current_trace() is None
        seen.append(token)
    assert seen == ["a", "b"]
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["spans"][0]["type"] == "llm"
    assert row["output"]["items"] == ["a", "b"]


def test_a_lone_span_gives_its_run_an_input_and_output(client: FulcrumOps, stub: StubServer) -> None:
    """``client.span()`` with no trace open starts one; that run must not list empty."""
    with client.span("classify", type="llm", input={"text": "hello"}) as span:
        span.set_output({"label": "greeting"})
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["input"] == {"text": "hello"}
    assert row["output"] == {"label": "greeting"}


def test_the_module_level_decorator_uses_the_default_client(stub: StubServer) -> None:
    fulcrum_ops.configure(api_key="fo_test_key", base_url=stub.base_url, agent="bridge")
    try:

        @trace(name="underwriting_insight", type="llm")
        def insight() -> str:
            fulcrum_ops.current_span().set_usage(prompt_tokens=5, completion_tokens=7)
            return "refer"

        insight()
        fulcrum_ops.flush()
        (row,) = sent(stub, "traces")
        assert row["spans"][0]["usage"] == {"prompt_tokens": 5, "completion_tokens": 7}
    finally:
        fulcrum_ops.shutdown()


def test_reporting_usage_never_breaks_an_agent_whose_telemetry_is_off() -> None:
    """The documented one-liner, run on a laptop with no API key.

    ``fulcrum_ops.current_span()`` answered ``None`` whenever there was no span
    -- no key, a run not sampled in, a plain ``@trace`` root -- so the line the
    docs tell a customer to add raised ``AttributeError`` inside their own
    function exactly where the SDK was supposed to be doing nothing.
    """

    @trace(name="underwriting_insight", type="llm")
    def insight() -> str:
        step = fulcrum_ops.current_span()
        step.set_model("gpt-5", "azure").set_usage(prompt_tokens=5, completion_tokens=7)
        step.set_output({"decision": "refer"}).log("reported")
        assert not step, "a span that records nothing must still read as absent"
        return "refer"

    try:
        assert insight() == "refer"
        # Outside anything traced, and as a context manager, it is just as inert.
        with fulcrum_ops.current_span() as nothing:
            nothing.set_cost(0.2)
        assert isinstance(fulcrum_ops.current_span(), fulcrum_ops.NoopSpan)
    finally:
        fulcrum_ops.shutdown()


def test_a_real_span_is_still_handed_out_when_there_is_one(client: FulcrumOps) -> None:
    with client.span("classify", type="llm") as span:
        assert fulcrum_ops.current_span() is span
        assert fulcrum_ops.current_span()
