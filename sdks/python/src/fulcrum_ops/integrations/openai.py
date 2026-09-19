"""OpenAI — tracing without touching a call site.

``openai`` is never imported here. The wrapper works on whatever object it is
handed, by structure, which means it also covers Azure OpenAI clients, the
client an Azure AI Foundry project hands out (``get_openai_client()``), the
async client, and any drop-in with the same method names — and it means
installing this SDK does not put a version constraint on the customer's OpenAI
package.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from ._proxy import MethodTracer, TracedProxy, as_int, extract_text

__all__ = ["track_openai"]

#: The leaves worth a span. Everything else on the client is forwarded untouched.
#:
#: ``parse`` and ``stream`` are the same calls as ``create`` with a different
#: return type — structured output, and a stream manager — and an agent built on
#: either used to produce no model span at all.
TRACKED_METHODS = {
    "chat.completions.create",
    "chat.completions.parse",
    "chat.completions.stream",
    "beta.chat.completions.parse",
    "beta.chat.completions.stream",
    "completions.create",
    "responses.create",
    "responses.parse",
    "responses.stream",
    "embeddings.create",
    "moderations.create",
}

#: ``stream()`` never takes ``stream=True``; the method is the declaration.
ALWAYS_STREAMS = {
    "chat.completions.stream",
    "beta.chat.completions.stream",
    "responses.stream",
}

#: Hosts that serve OpenAI models from Azure: Azure OpenAI, Azure AI Services
#: and Azure AI Foundry projects.
AZURE_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
)


def _detect_provider(client: Any) -> str:
    """``azure`` for a client that talks to Azure, ``openai`` otherwise.

    Cost is priced from the model *and* the provider, and the price table files
    Azure-served OpenAI models under ``azure``. Every client used to be reported
    as ``openai``, so an Azure or Foundry agent's tokens arrived and its cost
    stayed at zero. The class name catches ``AzureOpenAI``; the host catches the
    plain ``OpenAI`` client that a Foundry project returns, already pointed at
    the project's endpoint.
    """
    try:
        if "azure" in type(client).__name__.lower():
            return "azure"
        base_url = getattr(client, "base_url", None)
        host = getattr(base_url, "host", None)  # an ``httpx.URL`` on the real client
        if not isinstance(host, str):
            host = urlsplit(str(base_url or "")).hostname or ""
        if host.lower().endswith(AZURE_HOST_SUFFIXES):
            return "azure"
    except Exception:  # noqa: BLE001 - a client of an unexpected shape is still an OpenAI client
        pass
    return "openai"


def _tool_calls(response: Any) -> List[Dict[str, Any]]:
    """The tools a response asked for, in either API's shape."""
    calls: List[Dict[str, Any]] = []
    # Chat Completions: ``choices[].message.tool_calls[].function``.
    for choice in getattr(response, "choices", None) or []:
        message = getattr(choice, "message", None)
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            name = getattr(function, "name", None)
            if isinstance(name, str):
                calls.append({"name": name, "arguments": getattr(function, "arguments", None)})
    # Responses API: ``output[]`` items of type ``function_call``.
    output = getattr(response, "output", None)
    if isinstance(output, (list, tuple)):
        for item in output:
            if getattr(item, "type", None) == "function_call" and isinstance(
                getattr(item, "name", None), str
            ):
                calls.append({"name": item.name, "arguments": getattr(item, "arguments", None)})
    return calls


def _describe(response: Any) -> Dict[str, Any]:
    """Read model, usage and text off a response, in whichever shape it arrived."""
    if response is None:
        return {}

    described: Dict[str, Any] = {}

    model = getattr(response, "model", None)
    if isinstance(model, str):
        described["model"] = model

    usage = getattr(response, "usage", None)
    if usage is not None:
        counters: Dict[str, int] = {}
        # Chat Completions and the Responses API name the same two numbers
        # differently; the console charts one series, so both are normalised.
        for source, target in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        ):
            value = as_int(getattr(usage, source, None))
            if value is not None:
                counters.setdefault(target, value)
        if "total_tokens" not in counters and counters:
            counters["total_tokens"] = counters.get("prompt_tokens", 0) + counters.get(
                "completion_tokens", 0
            )
        if counters:
            described["usage"] = counters

    text = extract_text(response)
    # A turn that only calls tools says nothing, and used to be recorded as
    # producing nothing — in the one turn of an agent loop where what the model
    # decided is the whole story.
    calls = [] if text else _tool_calls(response)
    if calls:
        described["output"] = {"tool_calls": calls}
    elif text is not None:
        described["output"] = {"text": text}
    elif getattr(response, "data", None) is not None:
        # Embeddings: the vectors themselves are not telemetry.
        data = response.data
        described["output"] = {"vectors": len(data) if hasattr(data, "__len__") else None}

    finish_reasons = []
    for choice in getattr(response, "choices", None) or []:
        reason = getattr(choice, "finish_reason", None)
        if isinstance(reason, str):
            finish_reasons.append(reason)
    if finish_reasons:
        described["metadata"] = {"finish_reason": finish_reasons[0]}

    return described


def track_openai(
    client: Any,
    *,
    fulcrum: Optional[Any] = None,
    agent: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Any:
    """Return a traced view of an OpenAI client.

    ::

        from openai import OpenAI
        from fulcrum_ops.integrations import track_openai

        openai = track_openai(OpenAI())
        openai.chat.completions.create(model="gpt-4o-mini", messages=messages)

    Every completion becomes an ``llm`` span carrying the model, the token
    counters and the response text, nested under whatever trace is open — Chat
    Completions and the Responses API alike, ``create``, ``parse`` and
    ``stream``, through ``with_options()`` and ``with_raw_response`` too.
    Streaming calls keep their span open until the stream is exhausted; the
    accumulated text is what the span records, and the model and token counts
    are read from the events that carry them. Chat Completions only sends usage
    on a stream when asked: pass ``stream_options={"include_usage": True}``.

    An Azure AI Foundry agent is the same call::

        openai = track_openai(project_client.get_openai_client(), model="gpt-5")
        openai.responses.create(input=[...], extra_body={"agent_reference": {...}})

    :param client: An OpenAI, AsyncOpenAI or AzureOpenAI instance, or the client
        an Azure AI Foundry project returns.
    :param fulcrum: Report to this client rather than the default one. Left out,
        the default client is looked up on each call, so the wrapper may be
        built before ``fulcrum_ops.configure()`` runs.
    :param agent: Override the agent these spans are attributed to.
    :param provider: The provider to price these calls under. Detected when left
        out: ``azure`` for an Azure client or endpoint, ``openai`` otherwise.
    :param model: The model to report when neither the call nor the response
        names one, as with a Foundry agent addressed by ``agent_reference``.
    """
    resolved_provider = provider or _detect_provider(client)
    tracer = MethodTracer(
        fulcrum,
        resolved_provider,
        agent=agent,
        tags=tags,
        span_type="llm",
        name_for=lambda path: "openai.{0}".format(path.rsplit(".", 1)[0] if "." in path else path),
        describe=_describe,
        always_streams=ALWAYS_STREAMS,
        model=model,
    )
    return TracedProxy(client, tracer, TRACKED_METHODS)
