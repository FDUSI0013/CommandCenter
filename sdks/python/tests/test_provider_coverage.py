"""Every way of calling a model must produce a span that says which model and what it cost.

The wrappers covered ``create()`` answering with a whole response. Everything
else an agent does with a provider client fell through a gap: a stream kept its
text and lost its model and token counts, so a chat UI's runs all cost $0.00;
``parse()``, ``stream()``, ``with_options()`` and ``with_raw_response`` were
handed back unwrapped and produced no span at all; and an Azure client was
priced as OpenAI, which the price table does not file it under.

As in ``test_integrations``, every fake is a plain object with the provider's
shape. The shapes are the documented wire events, not a convenience.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Dict, List

import fulcrum_ops
from fulcrum_ops import FulcrumOps
from fulcrum_ops.integrations import FulcrumOpsCallbackHandler, track_anthropic, track_openai

from .conftest import sent
from .stub_server import StubServer


def obj(**fields: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**fields)


USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def only_span(stub: StubServer) -> Dict[str, Any]:
    (row,) = sent(stub, "traces")
    (span,) = row["spans"]
    return span


# ---------------------------------------------------------------- the shapes


def chat_chunks() -> List[Any]:
    """Chat Completions with ``stream_options={"include_usage": True}``."""
    return [
        obj(model="gpt-4o-mini-2024", usage=None, choices=[obj(delta=obj(content="Hel"))]),
        obj(model="gpt-4o-mini-2024", usage=None, choices=[obj(delta=obj(content="lo"))]),
        obj(
            model="gpt-4o-mini-2024",
            choices=[],
            usage=obj(prompt_tokens=12, completion_tokens=5, total_tokens=17),
        ),
    ]


def response_object(text: str = "Hello") -> Any:
    return obj(
        id="resp_1",
        model="gpt-5-2026-01-01",
        output_text=text,
        usage=obj(input_tokens=12, output_tokens=5, total_tokens=17),
    )


def response_events() -> List[Any]:
    """The Responses API: the model arrives first, the usage last."""
    return [
        obj(type="response.created", response=obj(model="gpt-5-2026-01-01", usage=None)),
        obj(type="response.function_call_arguments.delta", delta='{"city":'),
        obj(type="response.output_text.delta", delta="Hel"),
        obj(type="response.output_text.delta", delta="lo"),
        obj(type="response.completed", response=response_object()),
    ]


def anthropic_events() -> List[Any]:
    return [
        obj(
            type="message_start",
            message=obj(model="claude-sonnet-4-5", usage=obj(input_tokens=30, output_tokens=1)),
        ),
        obj(type="content_block_delta", delta=obj(type="text_delta", text="Hi ")),
        obj(type="content_block_delta", delta=obj(type="text_delta", text="there")),
        obj(type="message_delta", delta=obj(stop_reason="end_turn"), usage=obj(output_tokens=9)),
    ]


class Manager:
    """What ``.stream()`` returns: iterable only once entered."""

    def __init__(self, events: List[Any]) -> None:
        self._events = events

    def __enter__(self) -> Any:
        return iter(self._events)

    def __exit__(self, *args: Any) -> bool:
        return False


class RawResponse:
    """``with_raw_response``: the HTTP response, with the parsed body one call away."""

    def __init__(self, parsed: Any) -> None:
        self.headers = {"x-request-id": "req_1"}
        self._parsed = parsed

    def parse(self) -> Any:
        return self._parsed


class Responses:
    def create(self, **kwargs: Any) -> Any:
        return iter(response_events()) if kwargs.get("stream") else response_object()

    def parse(self, **kwargs: Any) -> Any:
        return response_object('{"decision": "refer"}')

    def stream(self, **kwargs: Any) -> Any:
        return Manager(response_events())

    @property
    def with_raw_response(self) -> Any:
        inner = self
        return obj(
            create=lambda **kwargs: RawResponse(inner.create(**kwargs)),
        )


class OpenAI:
    def __init__(self, base_url: str = "https://api.openai.com/v1") -> None:
        self.base_url = base_url
        self.responses = Responses()
        self.chat = obj(
            completions=obj(
                create=lambda **kwargs: iter(chat_chunks()),
                parse=lambda **kwargs: obj(
                    model="gpt-4o-mini-2024",
                    usage=obj(prompt_tokens=12, completion_tokens=5, total_tokens=17),
                    choices=[
                        obj(message=obj(content="Hello", parsed={"a": 1}), finish_reason="stop")
                    ],
                ),
            )
        )
        self.options: Dict[str, Any] = {}

    def with_options(self, **options: Any) -> "OpenAI":
        clone = OpenAI(self.base_url)
        clone.options = options
        return clone


class AzureOpenAI(OpenAI):
    pass


# ------------------------------------------------------------------ streaming


def test_a_streamed_chat_completion_records_model_and_usage(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    chunks = list(
        openai.chat.completions.create(
            messages=[], stream=True, stream_options={"include_usage": True}
        )
    )
    assert len(chunks) == 3
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["model"] == "gpt-4o-mini-2024"
    assert span["usage"] == USAGE
    assert span["output"] == {"text": "Hello", "streamed": True}


def test_a_streamed_responses_call_records_model_and_usage(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    list(openai.responses.create(input="hi", stream=True))
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["model"] == "gpt-5-2026-01-01"
    assert span["usage"] == USAGE
    # Tool arguments stream as string deltas too; they are not what the model said.
    assert span["output"] == {"text": "Hello", "streamed": True}


def test_a_streamed_anthropic_call_records_model_and_usage(
    client: FulcrumOps, stub: StubServer
) -> None:
    messages = obj(create=lambda **kwargs: iter(anthropic_events()))
    claude = track_anthropic(obj(messages=messages), fulcrum=client)

    list(claude.messages.create(model="claude-sonnet-4-5", messages=[], max_tokens=64, stream=True))
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["model"] == "claude-sonnet-4-5"
    assert span["usage"] == {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39}
    assert span["output"] == {"text": "Hi there", "streamed": True}


def test_an_async_stream_records_model_and_usage(client: FulcrumOps, stub: StubServer) -> None:
    class AsyncStream:
        def __init__(self) -> None:
            self._events = iter(response_events())

        def __aiter__(self) -> "AsyncStream":
            return self

        async def __anext__(self) -> Any:
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration from None

    class AsyncResponses:
        async def create(self, **kwargs: Any) -> Any:
            return AsyncStream()

    openai = track_openai(obj(responses=AsyncResponses()), fulcrum=client)

    async def run() -> int:
        stream = await openai.responses.create(input="hi", stream=True)
        return len([event async for event in stream])

    assert asyncio.run(run()) == 5
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["model"] == "gpt-5-2026-01-01"
    assert span["usage"] == USAGE


def test_a_consumer_that_stops_reading_has_not_failed(client: FulcrumOps, stub: StubServer) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    stream = iter(openai.responses.create(input="hi", stream=True))
    next(stream)
    stream.close()
    client.flush(timeout=5)

    assert "error_info" not in only_span(stub)


# ------------------------------------------------------------ the other doors


def test_parse_is_a_model_call_too(client: FulcrumOps, stub: StubServer) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    parsed = openai.responses.parse(input="hi", text_format=dict)
    assert parsed.output_text == '{"decision": "refer"}'
    assert openai.chat.completions.parse(messages=[]).choices[0].message.parsed == {"a": 1}
    client.flush(timeout=5)

    spans = {row["name"]: row["spans"][0] for row in sent(stub, "traces")}
    assert set(spans) == {"openai.responses", "openai.chat.completions"}
    for span in spans.values():
        assert span["type"] == "llm"
        assert span["usage"] == USAGE
    assert spans["openai.responses"]["model"] == "gpt-5-2026-01-01"


def test_the_stream_helper_is_traced_for_its_whole_life(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    with openai.responses.stream(input="hi") as stream:
        assert len(list(stream)) == 5
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["name"] == "openai.responses"
    assert span["model"] == "gpt-5-2026-01-01"
    assert span["usage"] == USAGE
    assert span["output"] == {"text": "Hello", "streamed": True}


def test_with_options_returns_a_client_that_is_still_traced(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    patient = openai.with_options(timeout=120)
    assert patient.options == {"timeout": 120}, "the provider's own clone, configured as asked"
    patient.responses.create(input="hi")
    client.flush(timeout=5)

    assert only_span(stub)["usage"] == USAGE


def test_with_raw_response_is_traced_and_hands_back_the_raw_response(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    raw = openai.responses.with_raw_response.create(input="hi")
    assert isinstance(raw, RawResponse), "the caller asked for the HTTP response and gets it"
    assert raw.headers["x-request-id"] == "req_1"
    assert raw.parse().output_text == "Hello"
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["name"] == "openai.responses"
    assert span["model"] == "gpt-5-2026-01-01"
    assert span["usage"] == USAGE
    assert span["output"] == {"text": "Hello"}


def test_a_raw_streamed_call_is_traced_through_its_parse(
    client: FulcrumOps, stub: StubServer
) -> None:
    openai = track_openai(OpenAI(), fulcrum=client)

    raw = openai.responses.with_raw_response.create(input="hi", stream=True)
    assert raw.headers["x-request-id"] == "req_1"
    assert len(list(raw.parse())) == 5
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["usage"] == USAGE
    assert span["output"] == {"text": "Hello", "streamed": True}


def test_a_turn_that_only_calls_tools_records_what_it_asked_for(
    client: FulcrumOps, stub: StubServer
) -> None:
    call = obj(function=obj(name="lookup_policy", arguments='{"id": "P-1"}'))
    reply = obj(
        model="gpt-4o",
        usage=None,
        choices=[obj(message=obj(content=None, tool_calls=[call]), finish_reason="tool_calls")],
    )
    openai = track_openai(
        obj(chat=obj(completions=obj(create=lambda **kwargs: reply))), fulcrum=client
    )

    openai.chat.completions.create(model="gpt-4o", messages=[])
    client.flush(timeout=5)

    assert only_span(stub)["output"] == {
        "tool_calls": [{"name": "lookup_policy", "arguments": '{"id": "P-1"}'}]
    }


# ------------------------------------------------------------------ provider


def test_an_azure_client_is_priced_as_azure(client: FulcrumOps, stub: StubServer) -> None:
    by_class = track_openai(AzureOpenAI(), fulcrum=client)
    # What an Azure AI Foundry project's ``get_openai_client()`` returns: a plain
    # OpenAI client, already pointed at the project.
    by_host = track_openai(
        OpenAI("https://uw-bridge.services.ai.azure.com/api/projects/uw/openai"), fulcrum=client
    )
    plain = track_openai(OpenAI(), fulcrum=client)
    told = track_openai(OpenAI(), fulcrum=client, provider="groq")

    for wrapped in (by_class, by_host, plain, told):
        wrapped.responses.create(input="hi")
    client.flush(timeout=5)

    providers = [row["spans"][0]["provider"] for row in sent(stub, "traces")]
    assert providers == ["azure", "azure", "openai", "groq"]


def test_a_foundry_agent_call_names_no_model_so_the_wrapper_can(
    client: FulcrumOps, stub: StubServer
) -> None:
    """``agent_reference`` addresses an agent, not a model; a stream may never name one."""
    silent = obj(responses=obj(create=lambda **kwargs: obj(output_text="refer", usage=None)))
    openai = track_openai(silent, fulcrum=client, model="gpt-5", provider="azure")

    openai.responses.create(input=[], extra_body={"agent_reference": {"name": "uw-agent"}})
    client.flush(timeout=5)

    span = only_span(stub)
    assert span["model"] == "gpt-5"
    assert span["provider"] == "azure"


def test_bedrock_and_vertex_are_not_priced_as_anthropic(
    client: FulcrumOps, stub: StubServer
) -> None:
    reply = obj(model="claude-sonnet-4-5", usage=None, content=[obj(type="text", text="hi")])

    class AnthropicBedrock:
        messages = obj(create=lambda **kwargs: reply)

    class AnthropicVertex:
        messages = obj(create=lambda **kwargs: reply)

    for provider_client in (AnthropicBedrock(), AnthropicVertex()):
        track_anthropic(provider_client, fulcrum=client).messages.create(messages=[])
    client.flush(timeout=5)

    providers = [row["spans"][0]["provider"] for row in sent(stub, "traces")]
    assert providers == ["bedrock", "google_vertexai"]


def test_langchain_reports_the_provider_not_the_class_family(
    client: FulcrumOps, stub: StubServer
) -> None:
    handler = FulcrumOpsCallbackHandler(client)

    handler.on_chain_start({"name": "Chain"}, {}, run_id="root")
    for run_id, kwargs in (
        (
            "a",
            {
                "metadata": {"ls_provider": "azure"},
                "invocation_params": {"_type": "azure-openai-chat"},
            },
        ),
        ("b", {"invocation_params": {"_type": "openai-chat"}}),
        ("c", {"invocation_params": {"_type": "azure-openai-chat"}}),
        ("d", {"invocation_params": {"_type": "anthropic-chat"}}),
    ):
        kwargs["invocation_params"]["model"] = "m"
        handler.on_chat_model_start(
            {"name": run_id}, [[]], run_id=run_id, parent_run_id="root", **kwargs
        )
        handler.on_llm_end(obj(generations=[], llm_output=None), run_id=run_id)
    handler.on_chain_end({}, run_id="root")
    client.flush(timeout=5)

    spans = {span["name"]: span for span in sent(stub, "traces")[0]["spans"]}
    assert [spans[name]["provider"] for name in "abcd"] == ["azure", "openai", "azure", "anthropic"]


# ----------------------------------------------------------- the stale client


def test_a_wrapper_built_before_configure_reports_to_the_configured_client(
    stub: StubServer,
) -> None:
    """``openai = track_openai(OpenAI())`` at import time is how everybody writes it."""
    openai = track_openai(OpenAI())  # no default client exists yet
    handler = FulcrumOpsCallbackHandler()

    fulcrum_ops.configure(
        api_key="fo_test_key", base_url=stub.base_url, agent="bridge", bootstrap=False
    )
    try:
        openai.responses.create(input="hi")
        handler.on_chain_start({"name": "Chain"}, {}, run_id="root")
        handler.on_chain_end({}, run_id="root")
        fulcrum_ops.flush()

        assert sorted(row["name"] for row in sent(stub, "traces")) == ["Chain", "openai.responses"]
    finally:
        fulcrum_ops.shutdown()
