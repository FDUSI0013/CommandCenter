"""Provider wrappers.

Every fake below is a plain object with the shape the real provider has. That is
not a shortcut — it is the property under test. The wrappers never import
``openai``, ``anthropic`` or ``langchain_core``, and work on whatever object
they are handed, which is what makes them cover Azure OpenAI, Bedrock, the async
clients and every version bump without a code change here.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from fulcrum_ops import FulcrumOps
from fulcrum_ops.integrations import (
    FulcrumOpsCallbackHandler,
    create_langchain_handler,
    track_anthropic,
    track_openai,
)

from .conftest import build_client, sent
from .stub_server import StubServer


def obj(**fields) -> types.SimpleNamespace:
    return types.SimpleNamespace(**fields)


# ------------------------------------------------------------------- OpenAI


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(
                [
                    obj(choices=[obj(delta=obj(content="Hel"))]),
                    obj(choices=[obj(delta=obj(content="lo"))]),
                ]
            )
        return obj(
            model="gpt-4o-mini-2024",
            usage=obj(prompt_tokens=12, completion_tokens=5, total_tokens=17),
            choices=[obj(message=obj(content="Hello"), finish_reason="stop")],
        )


class FakeOpenAI:
    def __init__(self) -> None:
        self.completions_impl = FakeCompletions()
        self.chat = obj(completions=self.completions_impl)
        self.embeddings = obj(create=self._embed)
        self.api_key = "sk-not-real"

    def _embed(self, **kwargs):
        return obj(model="text-embedding-3-small", data=[obj(embedding=[0.1, 0.2])], usage=None)


def test_openai_calls_become_llm_spans(client: FulcrumOps, stub: StubServer) -> None:
    openai = track_openai(FakeOpenAI(), fulcrum=client)

    with client.trace("run"):
        response = openai.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )

    assert response.choices[0].message.content == "Hello", "the provider's value is untouched"
    client.flush(timeout=5)

    span = sent(stub, "traces")[0]["spans"][0]
    assert span["name"] == "openai.chat.completions"
    assert span["type"] == "llm"
    assert span["provider"] == "openai"
    assert span["model"] == "gpt-4o-mini-2024", "the response's model beats the request's"
    assert span["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    assert span["output"] == {"text": "Hello"}
    assert span["metadata"]["finish_reason"] == "stop"


def test_untracked_attributes_fall_straight_through() -> None:
    real = FakeOpenAI()
    openai = track_openai(real, fulcrum=None)

    assert openai.api_key == "sk-not-real"
    assert openai.chat.completions is not None


def test_a_streamed_openai_call_keeps_its_span_open(
    client: FulcrumOps, stub: StubServer
) -> None:
    """Closing at hand-over would report a long generation as taking microseconds."""
    openai = track_openai(FakeOpenAI(), fulcrum=client)

    with client.trace("run"):
        stream = openai.chat.completions.create(model="gpt-4o-mini", messages=[], stream=True)
        chunks = list(stream)

    assert len(chunks) == 2, "the caller still sees every chunk"
    client.flush(timeout=5)

    span = sent(stub, "traces")[0]["spans"][0]
    assert span["output"] == {"text": "Hello", "streamed": True}


def test_a_provider_error_is_recorded_and_re_raised(
    client: FulcrumOps, stub: StubServer
) -> None:
    class Failing:
        def create(self, **kwargs):
            raise RuntimeError("rate limited by the provider")

    fake = FakeOpenAI()
    fake.chat = obj(completions=Failing())
    openai = track_openai(fake, fulcrum=client)

    with pytest.raises(RuntimeError, match="rate limited"):
        with client.trace("run"):
            openai.chat.completions.create(model="gpt-4o-mini", messages=[])

    client.flush(timeout=5)
    span = sent(stub, "traces")[0]["spans"][0]
    assert span["error_info"]["exception_type"] == "RuntimeError"


def test_embeddings_record_the_count_not_the_vectors(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(FakeOpenAI(), fulcrum=client)

    with client.trace("run"):
        openai.embeddings.create(model="text-embedding-3-small", input=["a"])

    client.flush(timeout=5)
    span = sent(stub, "traces")[0]["spans"][0]
    assert span["output"] == {"vectors": 1}


def test_an_async_openai_client_is_traced(client: FulcrumOps, stub: StubServer) -> None:
    class AsyncCompletions:
        async def create(self, **kwargs):
            await asyncio.sleep(0)
            return obj(
                model="gpt-4o",
                usage=obj(prompt_tokens=1, completion_tokens=2, total_tokens=3),
                choices=[obj(message=obj(content="async hello"), finish_reason="stop")],
            )

    fake = FakeOpenAI()
    fake.chat = obj(completions=AsyncCompletions())
    openai = track_openai(fake, fulcrum=client)

    async def run() -> str:
        with client.trace("run"):
            response = await openai.chat.completions.create(model="gpt-4o", messages=[])
            return response.choices[0].message.content

    assert asyncio.run(run()) == "async hello"
    client.flush(timeout=5)

    span = sent(stub, "traces")[0]["spans"][0]
    assert span["usage"]["total_tokens"] == 3


# ---------------------------------------------------------------- Anthropic


class FakeMessages:
    def create(self, **kwargs):
        return obj(
            model="claude-sonnet-4-5",
            usage=obj(input_tokens=30, output_tokens=9),
            content=[obj(type="text", text="Hi there")],
            stop_reason="end_turn",
        )

    def stream(self, **kwargs):
        events = [obj(delta=obj(text="Hi ")), obj(delta=obj(text="there"))]

        class Manager:
            def __enter__(self):
                return iter(events)

            def __exit__(self, *args):
                return False

        return Manager()


class FakeAnthropic:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def test_anthropic_token_names_are_normalised(client: FulcrumOps, stub: StubServer) -> None:
    """The console charts one token series; both providers must feed the same names."""
    claude = track_anthropic(FakeAnthropic(), fulcrum=client)

    with client.trace("run"):
        response = claude.messages.create(model="claude-sonnet-4-5", messages=[], max_tokens=64)

    assert response.content[0].text == "Hi there"
    client.flush(timeout=5)

    span = sent(stub, "traces")[0]["spans"][0]
    assert span["name"] == "anthropic.messages.create"
    assert span["provider"] == "anthropic"
    assert span["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 9,
        "total_tokens": 39,
    }
    assert span["output"] == {"text": "Hi there"}
    assert span["metadata"]["stop_reason"] == "end_turn"


def test_anthropic_messages_stream_is_traced_for_its_whole_life(
    client: FulcrumOps, stub: StubServer
) -> None:
    """``messages.stream()`` never takes ``stream=True``; the method is the declaration."""
    claude = track_anthropic(FakeAnthropic(), fulcrum=client)

    with client.trace("run"):
        with claude.messages.stream(model="claude-sonnet-4-5", messages=[]) as stream:
            text = "".join(event.delta.text for event in stream)

    assert text == "Hi there"
    client.flush(timeout=5)

    span = sent(stub, "traces")[0]["spans"][0]
    assert span["name"] == "anthropic.messages.stream"
    assert span["output"] == {"text": "Hi there", "streamed": True}


# ---------------------------------------------------------------- LangChain


def test_the_langchain_handler_rebuilds_the_run_tree(
    client: FulcrumOps, stub: StubServer
) -> None:
    handler = FulcrumOpsCallbackHandler(client, agent="research-agent")

    handler.on_chain_start({"name": "AgentExecutor"}, {"question": "why"}, run_id="root")
    handler.on_chat_model_start(
        {"name": "ChatOpenAI"},
        [[{"role": "user", "content": "why"}]],
        run_id="llm-1",
        parent_run_id="root",
        invocation_params={"model": "gpt-4o-mini", "_type": "openai-chat"},
    )
    handler.on_llm_end(
        obj(
            generations=[[obj(text="because", message=obj(usage_metadata={"input_tokens": 7, "output_tokens": 3}))]],
            llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        ),
        run_id="llm-1",
    )
    handler.on_tool_start({"name": "search"}, "orders", run_id="tool-1", parent_run_id="root")
    handler.on_tool_end("3 results", run_id="tool-1")
    handler.on_chain_end({"answer": "because"}, run_id="root")

    assert handler.open_runs == 0
    client.flush(timeout=5)

    row = sent(stub, "traces")[0]
    assert row["name"] == "AgentExecutor"
    assert row["agent"] == "research-agent"

    spans = {span["name"]: span for span in row["spans"]}
    assert set(spans) == {"AgentExecutor", "ChatOpenAI", "search"}
    assert spans["ChatOpenAI"]["type"] == "llm"
    assert spans["ChatOpenAI"]["model"] == "gpt-4o-mini"
    assert spans["ChatOpenAI"]["usage"]["total_tokens"] == 10
    assert spans["search"]["type"] == "tool"
    # Both children hang off the root run, not off each other.
    root_id = spans["AgentExecutor"]["id"]
    assert spans["ChatOpenAI"]["parent_span_id"] == root_id
    assert spans["search"]["parent_span_id"] == root_id


def test_a_langchain_error_is_recorded(client: FulcrumOps, stub: StubServer) -> None:
    handler = FulcrumOpsCallbackHandler(client)

    handler.on_chain_start({"name": "Chain"}, {}, run_id="root")
    handler.on_tool_start({"name": "search"}, "q", run_id="tool-1", parent_run_id="root")
    handler.on_tool_error(ValueError("the tool broke"), run_id="tool-1")
    handler.on_chain_end({}, run_id="root")

    client.flush(timeout=5)
    spans = {span["name"]: span for span in sent(stub, "traces")[0]["spans"]}
    assert spans["search"]["error_info"]["exception_type"] == "ValueError"


def test_a_retriever_run_records_its_document_count(
    client: FulcrumOps, stub: StubServer
) -> None:
    handler = FulcrumOpsCallbackHandler(client)

    handler.on_chain_start({"name": "RAG"}, {}, run_id="root")
    handler.on_retriever_start({"name": "VectorStore"}, "why", run_id="r1", parent_run_id="root")
    handler.on_retriever_end([obj(page_content="a"), obj(page_content="b")], run_id="r1")
    handler.on_chain_end({}, run_id="root")

    client.flush(timeout=5)
    spans = {span["name"]: span for span in sent(stub, "traces")[0]["spans"]}
    assert spans["VectorStore"]["type"] == "tool"
    assert spans["VectorStore"]["metadata"]["document_count"] == 2


def test_abandoned_runs_can_be_closed(client: FulcrumOps, stub: StubServer) -> None:
    """A cancelled chain fires no terminal callback; its spans must not be stranded."""
    handler = FulcrumOpsCallbackHandler(client)
    handler.on_chain_start({"name": "Chain"}, {}, run_id="root")
    assert handler.open_runs == 1

    handler.flush_open_runs()
    assert handler.open_runs == 0

    client.flush(timeout=5)
    assert sent(stub, "traces")[0]["name"] == "Chain"


def test_an_unknown_run_id_is_ignored_rather_than_raising(client: FulcrumOps) -> None:
    """LangChain dispatches into the handler; the handler may never raise back."""
    handler = FulcrumOpsCallbackHandler(client)
    handler.on_chain_end({"a": 1}, run_id="never-started")
    handler.on_llm_error(ValueError("x"), run_id="never-started")
    handler.on_agent_action(obj(tool="search"), run_id="never-started")
    assert handler.open_runs == 0


def test_the_handler_factory_works_without_langchain_installed(client: FulcrumOps) -> None:
    handler = create_langchain_handler(client, agent="research-agent")
    assert isinstance(handler, FulcrumOpsCallbackHandler)


def test_a_traced_provider_call_with_no_open_trace_still_reports(
    stub: StubServer, shared_http_client
) -> None:
    client = build_client(stub, shared_http_client)
    try:
        openai = track_openai(FakeOpenAI(), fulcrum=client)
        openai.chat.completions.create(model="gpt-4o-mini", messages=[])
        client.flush(timeout=5)

        row = sent(stub, "traces")[0]
        assert row["name"] == "openai.chat.completions"
        assert row["spans"][0]["type"] == "llm"
    finally:
        client.close(timeout=2)
