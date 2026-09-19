"""Anthropic — tracing without touching a call site.

Same shape as the OpenAI wrapper and for the same reasons: ``anthropic`` is
never imported, the wrapper works structurally on whatever object it is handed,
and it therefore covers ``Anthropic``, ``AsyncAnthropic`` and the Bedrock and
Vertex clients without knowing they exist.

The differences that matter are in the payload. Anthropic reports
``input_tokens`` / ``output_tokens`` where OpenAI reports ``prompt_tokens`` /
``completion_tokens``. Both are normalised onto the same two names here, because
the console charts one token series across every provider and a run that used
both would otherwise show two half-filled charts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ._proxy import MethodTracer, TracedProxy, as_int, extract_text

__all__ = ["track_anthropic"]

#: The leaves worth a span. ``messages.stream`` returns a context manager rather
#: than an iterator, which the stream proxy handles by forwarding ``__enter__``
#: and closing the span on ``__exit__``.
TRACKED_METHODS = {
    "messages.create",
    "messages.stream",
    "completions.create",
    "beta.messages.create",
    "beta.messages.stream",
}

#: ``stream()`` never takes ``stream=True``; the method itself is the
#: declaration, so the tracer is told which paths always stream.
ALWAYS_STREAMS = {"messages.stream", "beta.messages.stream"}


def _detect_provider(client: Any) -> str:
    """Which price list these calls belong to.

    The same Claude model is billed differently by Anthropic, by Bedrock and by
    Vertex, and cost is priced from the model *and* the provider. All three
    clients used to be reported as ``anthropic``.
    """
    name = type(client).__name__.lower()
    if "bedrock" in name:
        return "bedrock"
    if "vertex" in name:
        return "google_vertexai"
    return "anthropic"


def _tool_calls(response: Any) -> List[Dict[str, Any]]:
    """The ``tool_use`` blocks of a message: what the model asked to have run."""
    calls: List[Dict[str, Any]] = []
    content = getattr(response, "content", None)
    if isinstance(content, list):
        for block in content:
            if getattr(block, "type", None) == "tool_use" and isinstance(
                getattr(block, "name", None), str
            ):
                calls.append({"name": block.name, "arguments": getattr(block, "input", None)})
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
        # Anthropic's names on the left, the SDK's normalised names on the
        # right, so the per-model cost breakdown adds up across providers.
        for source, target in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
            ("cache_creation_input_tokens", "cache_creation_input_tokens"),
            ("cache_read_input_tokens", "cache_read_input_tokens"),
        ):
            value = as_int(getattr(usage, source, None))
            if value is not None:
                counters[target] = value
        if counters:
            counters["total_tokens"] = counters.get("prompt_tokens", 0) + counters.get(
                "completion_tokens", 0
            )
            described["usage"] = counters

    text = extract_text(response)
    calls = _tool_calls(response)
    if text is not None:
        described["output"] = {"text": text, "tool_calls": calls} if calls else {"text": text}
    elif calls:
        # A turn that only calls tools has no text, and used to be recorded as
        # having produced nothing.
        described["output"] = {"tool_calls": calls}

    metadata: Dict[str, Any] = {}
    stop_reason = getattr(response, "stop_reason", None)
    if isinstance(stop_reason, str):
        metadata["stop_reason"] = stop_reason
    stop_sequence = getattr(response, "stop_sequence", None)
    if isinstance(stop_sequence, str):
        metadata["stop_sequence"] = stop_sequence
    if metadata:
        described["metadata"] = metadata

    return described


def track_anthropic(
    client: Any,
    *,
    fulcrum: Optional[Any] = None,
    agent: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Any:
    """Return a traced view of an Anthropic client.

    ::

        from anthropic import Anthropic
        from fulcrum_ops.integrations import track_anthropic

        claude = track_anthropic(Anthropic())
        claude.messages.create(model="claude-sonnet-4-5", messages=messages, max_tokens=512)

    Every call becomes an ``llm`` span carrying the model, the token counters
    and the response text, nested under whatever trace is open. A streaming call
    keeps its span open until the stream is exhausted, so the recorded duration
    is how long the generation took rather than how long it took to hand the
    stream over. The model and the token counts of a streamed call are read from
    the ``message_start`` and ``message_delta`` events that carry them.

    :param client: An ``Anthropic``, ``AsyncAnthropic``, Bedrock or Vertex instance.
    :param fulcrum: Report to this client rather than the default one. Left out,
        the default client is looked up on each call, so the wrapper may be
        built before ``fulcrum_ops.configure()`` runs.
    :param agent: Override the agent these spans are attributed to.
    :param provider: The provider to price these calls under. Detected when left
        out: ``bedrock`` or ``google_vertexai`` for those clients, ``anthropic``
        otherwise.
    :param model: The model to report when neither the call nor the response
        names one.
    """
    tracer = MethodTracer(
        fulcrum,
        provider or _detect_provider(client),
        agent=agent,
        tags=tags,
        span_type="llm",
        name_for=lambda path: "anthropic.{0}".format(path),
        describe=_describe,
        always_streams=ALWAYS_STREAMS,
        model=model,
    )
    return TracedProxy(client, tracer, TRACKED_METHODS)
