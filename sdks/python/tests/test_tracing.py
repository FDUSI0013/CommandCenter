"""Tracing: what lands on the wire, and what the caller sees while it happens."""

from __future__ import annotations

import asyncio

import pytest

import fulcrum_ops
from fulcrum_ops import FulcrumOps, trace

from .conftest import build_client, sent
from .stub_server import StubServer


def test_trace_posts_one_row_with_its_spans_nested(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("support-question", input={"question": "where is my order?"}) as run:
        with client.span("retrieval", type="tool") as span:
            span.set_output({"chunks": 3})
        with client.span("answer", type="llm") as span:
            span.set_model("gpt-4o-mini", "openai")
            span.set_usage(prompt_tokens=120, completion_tokens=40)
        run.set_output({"answer": "it shipped"})

    assert client.flush(timeout=5) is True

    traces = sent(stub, "traces")
    assert len(traces) == 1
    row = traces[0]

    assert row["name"] == "support-question"
    assert row["input"] == {"question": "where is my order?"}
    assert row["output"] == {"answer": "it shipped"}
    assert row["agent"] == "checkout-agent"
    assert row["start_time"].endswith("Z") and row["end_time"].endswith("Z")

    spans = row["spans"]
    assert [s["name"] for s in spans] == ["retrieval", "answer"]
    assert [s["type"] for s in spans] == ["tool", "llm"]
    # A nested span carries no trace_id of its own: it is already inside its trace.
    assert "trace_id" not in spans[0]
    assert spans[1]["model"] == "gpt-4o-mini"
    assert spans[1]["provider"] == "openai"
    assert spans[1]["usage"] == {"prompt_tokens": 120, "completion_tokens": 40}


def test_spans_nest_without_being_passed_around(client: FulcrumOps, stub: StubServer) -> None:
    """The parent link comes from contextvars, not from an argument."""

    def inner() -> None:
        with client.span("inner", type="tool"):
            pass

    with client.trace("outer"):
        with client.span("middle"):
            inner()

    client.flush(timeout=5)
    spans = {s["name"]: s for s in sent(stub, "traces")[0]["spans"]}

    assert spans["middle"].get("parent_span_id") is None
    assert spans["inner"]["parent_span_id"] == spans["middle"]["id"]


def test_an_exception_is_recorded_and_re_raised_unchanged(
    client: FulcrumOps, stub: StubServer
) -> None:
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom, match="the tool failed"):
        with client.trace("failing-run"):
            with client.span("tool-call", type="tool"):
                raise Boom("the tool failed")

    client.flush(timeout=5)
    row = sent(stub, "traces")[0]

    assert row["error_info"]["exception_type"] == "Boom"
    assert row["error_info"]["message"] == "the tool failed"
    assert "Boom" in row["error_info"]["traceback"]
    assert row["spans"][0]["error_info"]["exception_type"] == "Boom"


def test_span_log_set_output_and_score(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("scored-run"):
        with client.span("retrieval", type="tool") as span:
            span.log("querying the index", filters={"tier": "gold"})
            span.set_output({"chunks": 8})
            span.score("recall", 0.82, reason="8 of 10 gold chunks")

    client.flush(timeout=5)
    span = sent(stub, "traces")[0]["spans"][0]

    assert span["output"] == {"chunks": 8}
    assert span["feedback_scores"] == [
        {"name": "recall", "value": 0.82, "source": "sdk", "reason": "8 of 10 gold chunks"}
    ]
    logs = span["metadata"]["logs"]
    assert logs[0]["message"] == "querying the index"
    assert logs[0]["filters"] == {"tier": "gold"}


def test_a_span_with_no_trace_opens_one_around_itself(
    client: FulcrumOps, stub: StubServer
) -> None:
    """A stray span would be dropped by the ingest path; an implicit trace is better."""
    with client.span("lonely-tool", type="tool") as span:
        span.set_output({"ok": True})

    client.flush(timeout=5)
    traces = sent(stub, "traces")

    assert len(traces) == 1
    assert traces[0]["name"] == "lonely-tool"
    assert [s["name"] for s in traces[0]["spans"]] == ["lonely-tool"]


def test_stream_spans_posts_each_span_as_it_closes(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, stream_spans=True)
    try:
        with client.trace("long-job"):
            with client.span("step-1", type="tool"):
                pass
            with client.span("step-2", type="tool"):
                pass
        client.flush(timeout=5)

        spans = sent(stub, "spans")
        assert [s["name"] for s in spans] == ["step-1", "step-2"]
        # Posted on their own, so each one has to name its trace.
        assert all(s["trace_id"] for s in spans)
        # And the trace must not carry them a second time.
        assert "spans" not in sent(stub, "traces")[0]
    finally:
        client.close(timeout=2)


def test_environment_is_stamped_on_every_item(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, environment="Staging")
    try:
        with client.trace("run"):
            pass
        client.flush(timeout=5)
        assert sent(stub, "traces")[0]["metadata"]["environment"] == "Staging"
    finally:
        client.close(timeout=2)


def test_sampling_zero_reports_nothing_but_still_runs_the_body(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, sampling_rate=0.0)
    try:
        ran = []
        with client.trace("run"):
            ran.append(True)
        client.flush(timeout=5)

        assert ran == [True]
        assert sent(stub, "traces") == []
    finally:
        client.close(timeout=2)


def test_capture_off_keeps_the_run_and_drops_the_content(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, capture_input=False, capture_output=False)
    try:
        with client.trace("run", input={"secret": "value"}) as run:
            run.set_output({"answer": "also secret"})
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert "input" not in row and "output" not in row
        # The run itself, its timings and its name still arrive.
        assert row["name"] == "run" and row["end_time"]
    finally:
        client.close(timeout=2)


def test_thread_id_and_tags_reach_the_wire(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("turn-2", thread_id="conversation-9", tags=["beta", "beta", "vip"]) as run:
        run.add_tags("escalated")

    client.flush(timeout=5)
    row = sent(stub, "traces")[0]

    assert row["thread_id"] == "conversation-9"
    assert row["tags"] == ["beta", "vip", "escalated"]


# --------------------------------------------------------------- decorators


def test_decorator_traces_a_plain_function(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        def answer(question: str, top_k: int = 3) -> str:
            return "because {0}".format(question)

        assert answer("why", top_k=5) == "because why"
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert row["name"].endswith("answer")
        # Arguments are bound by name, which is what makes them readable.
        assert row["input"] == {"question": "why", "top_k": 5}
        assert row["output"] == {"value": "because why"}
    finally:
        client.close(timeout=2)


def test_decorated_calls_nest_into_one_tree(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace(name="retrieve", type="tool")
        def retrieve(question: str) -> list:
            return ["a", "b"]

        @trace(name="plan")
        def plan(question: str) -> str:
            retrieve(question)
            return "planned"

        plan("why")
        client.flush(timeout=5)

        traces = sent(stub, "traces")
        assert len(traces) == 1, "a nested decorated call must not open a second trace"
        assert traces[0]["name"] == "plan"
        assert [s["name"] for s in traces[0]["spans"]] == ["retrieve"]
        assert traces[0]["spans"][0]["type"] == "tool"
    finally:
        client.close(timeout=2)


def test_decorator_records_and_re_raises(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        def explode() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            explode()
        client.flush(timeout=5)

        assert sent(stub, "traces")[0]["error_info"]["exception_type"] == "ValueError"
    finally:
        client.close(timeout=2)


def test_decorator_traces_a_coroutine(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        async def fetch(url: str) -> str:
            await asyncio.sleep(0)
            return "body of {0}".format(url)

        result = asyncio.run(fetch("/orders"))
        client.flush(timeout=5)

        assert result == "body of /orders"
        row = sent(stub, "traces")[0]
        assert row["input"] == {"url": "/orders"}
        assert row["output"] == {"value": "body of /orders"}
    finally:
        client.close(timeout=2)


def test_decorator_preserves_the_generator_contract(stub: StubServer, shared_http_client) -> None:
    """A traced generator is still a generator, and its span covers the streaming."""
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        def stream(count: int):
            for index in range(count):
                yield index
            return "done"

        generator = stream(3)
        assert hasattr(generator, "send"), "wrapping a generator must not make it a coroutine"
        assert list(generator) == [0, 1, 2]
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert row["output"]["items"] == [0, 1, 2]
        assert row["output"]["yielded"] == 3
        assert row["output"]["returned"] == "done"
    finally:
        client.close(timeout=2)


def test_decorator_traces_an_async_generator(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        async def stream(count: int):
            for index in range(count):
                await asyncio.sleep(0)
                yield index

        async def drain() -> list:
            return [value async for value in stream(2)]

        assert asyncio.run(drain()) == [0, 1]
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert row["output"]["items"] == [0, 1]
        assert row["output"]["yielded"] == 2
    finally:
        client.close(timeout=2)


def test_a_generator_that_raises_is_recorded(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace
        def stream():
            yield 1
            raise RuntimeError("mid-stream")

        with pytest.raises(RuntimeError, match="mid-stream"):
            list(stream())
        client.flush(timeout=5)

        assert sent(stub, "traces")[0]["error_info"]["exception_type"] == "RuntimeError"
    finally:
        client.close(timeout=2)


def test_concurrent_traces_do_not_see_each_others_spans(stub: StubServer, shared_http_client) -> None:
    """contextvars are copied into a task, not shared with it."""
    client = build_client(stub, shared_http_client, set_as_default=True)
    try:

        @trace(name="task")
        async def work(label: str) -> str:
            with client.span("step-{0}".format(label), type="tool"):
                await asyncio.sleep(0.01)
            return label

        async def both() -> list:
            return await asyncio.gather(work("a"), work("b"))

        asyncio.run(both())
        client.flush(timeout=5)

        traces = sent(stub, "traces")
        assert len(traces) == 2
        for row in traces:
            assert len(row["spans"]) == 1, "a concurrent trace picked up the other one's span"
    finally:
        client.close(timeout=2)


def test_decorator_is_a_no_op_without_a_configured_client() -> None:
    """A library may decorate its own functions without forcing telemetry on anyone."""

    @trace
    def double(value: int) -> int:
        return value * 2

    assert double(21) == 42


def test_module_level_helpers_use_the_default_client(stub: StubServer) -> None:
    client = fulcrum_ops.configure(
        api_key="fo_test_key",
        base_url=stub.base_url,
        agent="checkout-agent",
        bootstrap=False,
        flush_on_exit=False,
        flush_interval_seconds=30.0,
    )
    try:
        with fulcrum_ops.span("module-level", type="tool") as span:
            span.set_output({"ok": True})
        assert fulcrum_ops.flush(timeout=5) is True
        assert sent(stub, "traces")[0]["name"] == "module-level"
    finally:
        client.close(timeout=2)
