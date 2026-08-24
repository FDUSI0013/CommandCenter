"""OpenAI — tracing without touching a call site.

``openai`` is never imported here. The wrapper works on whatever object it is
handed, by structure, which means it also covers Azure OpenAI clients, the async
client, and any drop-in with the same method names — and it means installing
this SDK does not put a version constraint on the customer's OpenAI package.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ._proxy import MethodTracer, TracedProxy, as_int, extract_text

__all__ = ["track_openai"]

#: The leaves worth a span. Everything else on the client is forwarded untouched.
TRACKED_METHODS = {
    "chat.completions.create",
    "completions.create",
    "responses.create",
    "embeddings.create",
    "moderations.create",
}


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
    if text is not None:
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
) -> Any:
    """Return a traced view of an OpenAI client.

    ::

        from openai import OpenAI
        from fulcrum_ops.integrations import track_openai

        openai = track_openai(OpenAI())
        openai.chat.completions.create(model="gpt-4o-mini", messages=messages)

    Every completion becomes an ``llm`` span carrying the model, the token
    counters and the response text, nested under whatever trace is open. Streaming
    calls keep their span open until the stream is exhausted, and the accumulated
    text is what the span records.

    :param client: An OpenAI, AsyncOpenAI or AzureOpenAI instance.
    :param fulcrum: Report to this client rather than the default one.
    :param agent: Override the agent these spans are attributed to.
    """
    if fulcrum is None:
        from ..client import get_client

        fulcrum = get_client()

    tracer = MethodTracer(
        fulcrum,
        "openai",
        agent=agent,
        tags=tags,
        span_type="llm",
        name_for=lambda path: "openai.{0}".format(path.rsplit(".", 1)[0] if "." in path else path),
        describe=_describe,
    )
    return TracedProxy(client, tracer, TRACKED_METHODS)
