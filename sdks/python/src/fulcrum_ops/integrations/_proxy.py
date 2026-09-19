"""The machinery the provider wrappers share.

A provider SDK is a tree of namespaces — ``client.chat.completions.create`` —
and the goal is to trace the leaves without the customer editing a single call
site and without the SDK inheriting the provider's surface, which changes every
few weeks.

Monkey-patching the provider's classes would do it, but it is a global mutation
of somebody else's library: it breaks a second, untraced client in the same
process, and it survives past the point where anyone remembers doing it. So the
wrapper returns a *proxy* instead. Attribute access falls through to the real
object; only the specific methods named in ``tracked`` are wrapped, and only on
the instance the caller handed in.
"""

from __future__ import annotations

import functools
import inspect
import logging
import types
from typing import Any, Callable, Dict, Optional, Sequence, Set

logger = logging.getLogger("fulcrum_ops")

__all__ = ["TracedProxy", "MethodTracer", "extract_text", "as_int"]

#: ``client.responses.with_raw_response.create(...)`` and its streaming twin: the
#: same methods, answering with the HTTP response instead of the parsed one.
RAW_VIEW = "with_raw_response"
STREAMING_VIEW = "with_streaming_response"
#: Methods that return a new client configured differently from this one.
CLONING_METHODS = ("with_options", "copy")


def as_int(value: Any) -> Optional[int]:
    """Read a token counter that may be an int, a float, or absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_text(value: Any, limit: int = 16_000) -> Optional[str]:
    """Pull human-readable text out of a provider response, whatever its shape.

    Structural rather than typed, because the two providers disagree and each
    changes its own response class between minor versions. Everything here is
    ``getattr`` with a default; nothing raises.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]

    # OpenAI Responses API.
    text = getattr(value, "output_text", None)
    if isinstance(text, str) and text:
        return text[:limit]

    # Chat Completions.
    choices = getattr(value, "choices", None)
    if choices:
        parts = []
        for choice in choices:
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if content is None:
                content = getattr(choice, "text", None)
            if isinstance(content, str):
                parts.append(content)
        if parts:
            return "\n".join(parts)[:limit]

    # Anthropic Messages.
    content = getattr(value, "content", None)
    if isinstance(content, list):
        parts = []
        for block in content:
            block_text = getattr(block, "text", None)
            if block_text is None and isinstance(block, dict):
                block_text = block.get("text")
            if isinstance(block_text, str):
                parts.append(block_text)
        if parts:
            return "\n".join(parts)[:limit]
    if isinstance(content, str):
        return content[:limit]

    return None


class MethodTracer:
    """Wraps one provider method so that calling it opens and closes a span."""

    def __init__(
        self,
        client: Any,
        provider: str,
        *,
        agent: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        span_type: str = "llm",
        name_for: Optional[Callable[[str], str]] = None,
        describe: Optional[Callable[[Any], Dict[str, Any]]] = None,
        always_streams: Optional[Set[str]] = None,
        model: Optional[str] = None,
    ) -> None:
        # ``None`` means "whichever client is the default when the call is
        # made". A wrapper is typically built at import -- ``openai =
        # track_openai(OpenAI())`` at module level -- which is before
        # ``configure()`` has run, and resolving the default there bound the
        # wrapper for life to a throwaway client that ``configure()`` then
        # closed: every span it opened afterwards was discarded without a word.
        self._client = client
        self._provider = provider
        # The model to report when neither the call nor the response names one.
        # An Azure AI Foundry agent call is addressed by ``agent_reference``,
        # not by ``model``, so the request has nothing to offer.
        self._model = model
        self._agent = agent
        self._tags = list(tags or [])
        self._span_type = span_type
        self._name_for = name_for or (lambda path: "{0}.{1}".format(provider, path))
        self._describe = describe or (lambda response: {})
        # Paths that stream whatever their arguments say. ``messages.stream()``
        # is the case that needs this: it never takes ``stream=True``, so
        # inferring from the keyword would close its span before the first
        # token and report a 12-second generation as taking 200 microseconds.
        self._always_streams = set(always_streams or ())

    # ------------------------------------------------------------------ spans

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        from ..client import get_client

        return get_client()

    def _open(self, path: str, kwargs: Dict[str, Any]) -> Any:
        try:
            client = self._resolve_client()
            if client is None or not client.enabled:
                return None
            requested = kwargs.get("model")
            # ``client.span()`` builds a run around the span when none is open,
            # and leaves the context alone: this span is closed with ``end()``,
            # possibly in another task or thread, never with ``with``.
            return client.span(
                self._name_for(path),
                type=self._span_type,
                input=dict(kwargs) if kwargs else None,
                tags=self._tags or None,
                agent=self._agent,
                model=requested if isinstance(requested, str) and requested else self._model,
                provider=self._provider,
            )
        except Exception:  # noqa: BLE001 - instrumentation must not break the call
            logger.debug("fulcrum-ops: could not open a %s span", self._provider, exc_info=True)
            return None

    def _close(self, span: Any, response: Any, error: Optional[BaseException]) -> None:
        if span is None:
            return
        try:
            if error is not None:
                span.record_exception(error)
            else:
                for key, value in self._describe(response).items():
                    if key == "usage" and value:
                        span.set_usage(value)
                    elif key == "model" and value:
                        span.set_model(str(value), self._provider)
                    elif key == "output" and value is not None:
                        span.set_output(value)
                    elif key == "metadata" and value:
                        span.set_metadata(value)
            span.end()
        except Exception:  # noqa: BLE001
            logger.debug("fulcrum-ops: could not close a %s span", self._provider, exc_info=True)

    # ----------------------------------------------------------------- wrapper

    def wrap(self, path: str, method: Callable[..., Any], view: str = "") -> Callable[..., Any]:
        tracer = self

        streams_always = path in self._always_streams

        @functools.wraps(method)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            span = tracer._open(path, kwargs)
            try:
                result = method(*args, **kwargs)
            except BaseException as exc:
                tracer._close(span, None, exc)
                raise

            streaming = streams_always or bool(kwargs.get("stream"))
            if inspect.isawaitable(result):
                return _finish_awaitable(tracer, span, result, streaming, view)
            return _settle(tracer, span, result, streaming, streams_always, view)

        return wrapper


def _is_stream(value: Any, allow_context_manager: bool = False) -> bool:
    """A provider stream iterates but is not one of the plain containers.

    ``allow_context_manager`` covers Anthropic's ``messages.stream()``, which
    hands back a manager that only becomes iterable inside a ``with`` block. The
    proxy forwards ``__enter__`` either way, so accepting it here is what keeps
    the span open for the length of the generation.
    """
    if isinstance(value, (str, bytes, dict, list, tuple)):
        return False
    if hasattr(value, "__iter__") or hasattr(value, "__aiter__"):
        return True
    if allow_context_manager:
        return hasattr(value, "__enter__") or hasattr(value, "__aenter__")
    return False


def _settle(
    tracer: MethodTracer,
    span: Any,
    result: Any,
    streaming: bool,
    allow_context_manager: bool,
    view: str,
) -> Any:
    """Close the span now, or hand back something that closes it when the call really ends."""
    if view == RAW_VIEW:
        # ``with_raw_response`` answers with the HTTP response; the object that
        # knows the model and the token counts is one ``parse()`` away. For a
        # stream that ``parse()`` is the caller's to make, so it is proxied; for
        # a plain call the provider caches the parse, and asking first costs
        # the caller nothing.
        if streaming:
            return _RawResponseProxy(tracer, span, result)
        tracer._close(span, _parsed(result), None)
        return result
    if view == STREAMING_VIEW:
        # ``with_streaming_response`` is a manager around a body nobody has
        # read yet. Reading it here to find the usage would take the stream
        # away from the caller, so this span records the call and how long it
        # was open, and nothing it would have to consume the body to learn.
        if hasattr(result, "__enter__") or hasattr(result, "__aenter__"):
            return _StreamProxy(tracer, span, result)
        tracer._close(span, None, None)
        return result
    if streaming and _is_stream(result, allow_context_manager=allow_context_manager):
        return _StreamProxy(tracer, span, result)
    tracer._close(span, result, None)
    return result


async def _finish_awaitable(
    tracer: MethodTracer, span: Any, awaitable: Any, streaming: bool, view: str = ""
) -> Any:
    try:
        result = await awaitable
    except BaseException as exc:
        tracer._close(span, None, exc)
        raise
    return _settle(tracer, span, result, streaming, False, view)


def _parsed(raw: Any) -> Any:
    """The parsed body behind a raw response, or ``None`` when it cannot be had for free."""
    parse = getattr(raw, "parse", None)
    if not callable(parse):
        return None
    try:
        parsed = parse()
    except Exception:  # noqa: BLE001 - the caller will meet the same failure on their own parse()
        return None
    if inspect.isawaitable(parsed):
        # An async ``parse()`` cannot be awaited from here. Close the coroutine
        # so it does not warn about never having been awaited.
        close = getattr(parsed, "close", None)
        if callable(close):
            close()
        return None
    return parsed


class _RawResponseProxy:
    """A raw streaming response whose ``parse()`` hands back a traced stream."""

    def __init__(self, tracer: MethodTracer, span: Any, raw: Any) -> None:
        self._tracer = tracer
        self._span = span
        self._raw = raw
        self._parsed: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_raw"), name)

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        if self._parsed is None:
            parsed = self._raw.parse(*args, **kwargs)
            if _is_stream(parsed):
                parsed = _StreamProxy(self._tracer, self._span, parsed)
            else:
                self._tracer._close(self._span, parsed, None)
            self._parsed = parsed
        return self._parsed


#: The token counters either provider puts on a ``usage`` object.
_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class _StreamProxy:
    """Keeps a streaming call's span open until the stream is exhausted.

    A streamed completion's span should last as long as the stream does — that
    duration is the number a latency dashboard is actually about. The proxy also
    accumulates the chunks so the span records what the model said, which a
    naive wrapper loses entirely: the response object a streaming call returns
    is empty at the moment it is returned.

    The same goes for the model and the token counts. A stream has no response
    object to read them off; they arrive *in* the stream, on whichever event
    each API chose, and a span that only kept the text reported every streamed
    generation as costing nothing.
    """

    def __init__(self, tracer: MethodTracer, span: Any, stream: Any) -> None:
        self._tracer = tracer
        self._span = span
        self._stream = stream
        # What ``__enter__`` handed back, when the provider's stream is really a
        # manager. Anthropic's ``messages.stream()`` returns one of those: the
        # manager itself is not iterable, and the object that is only exists
        # once the ``with`` block has been entered.
        self._entered: Any = None
        self._chunks: list = []
        self._model: Optional[str] = None
        self._usage: Dict[str, int] = {}
        self._final_text: Optional[str] = None
        self._closed = False

    @property
    def _iterable(self) -> Any:
        """Whichever object actually yields chunks."""
        return self._entered if self._entered is not None else self._stream

    # Anything the provider exposes on its stream object — ``.response``,
    # ``.get_final_message()`` — falls through untouched.
    def __getattr__(self, name: str) -> Any:
        entered = object.__getattribute__(self, "_entered")
        if entered is not None and hasattr(entered, name):
            return getattr(entered, name)
        return getattr(object.__getattribute__(self, "_stream"), name)

    def __enter__(self) -> "_StreamProxy":
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            self._entered = enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._finish(exc if isinstance(exc, BaseException) else None)
        exit_ = getattr(self._stream, "__exit__", None)
        return exit_(exc_type, exc, tb) if exit_ is not None else False

    async def __aenter__(self) -> "_StreamProxy":
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            self._entered = await enter()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._finish(exc if isinstance(exc, BaseException) else None)
        exit_ = getattr(self._stream, "__aexit__", None)
        return await exit_(exc_type, exc, tb) if exit_ is not None else False

    def __iter__(self) -> Any:
        try:
            for chunk in self._iterable:
                self._collect(chunk)
                yield chunk
        except GeneratorExit:
            # The consumer stopped reading. That is a decision, not a failure.
            self._finish(None)
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._finish(None)

    def __aiter__(self) -> "_StreamProxy":
        self._aiterator = self._iterable.__aiter__()
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._aiterator.__anext__()
        except StopAsyncIteration:
            self._finish(None)
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._collect(chunk)
        return chunk

    def close(self) -> Any:
        """Closing the stream early is how a caller abandons it; the span ends with it."""
        self._finish(None)
        return self._iterable.close()

    async def aclose(self) -> Any:
        self._finish(None)
        return await self._iterable.aclose()

    def _collect(self, chunk: Any) -> None:
        try:
            self._sniff(chunk)
        except Exception:  # noqa: BLE001 - a chunk of an unexpected shape is not the caller's problem
            pass
        if len(self._chunks) >= 2_000:
            return
        text = _chunk_text(chunk)
        if text:
            self._chunks.append(text)

    def _sniff(self, chunk: Any) -> None:
        """Keep the model and the token counts from whichever event carries them.

        By structure, not by event name, because there are five shapes:

        * a Chat Completions chunk carries ``model`` itself, and ``usage`` on
          the last one when the call asked for ``include_usage``;
        * the ``chat.completions.stream()`` helper wraps those in ``.chunk``;
        * the Responses API puts both on ``.response`` — of ``response.created``
          first, and complete on ``response.completed``;
        * Anthropic names the model and the input tokens on ``message_start``'s
          ``.message``, and the running output count on each ``message_delta``.

        Later values replace earlier ones, which is right for all of them: every
        counter that repeats is cumulative.
        """
        for carrier in (
            chunk,
            getattr(chunk, "chunk", None),
            getattr(chunk, "response", None),
            getattr(chunk, "message", None),
        ):
            if carrier is None:
                continue
            model = getattr(carrier, "model", None)
            if isinstance(model, str) and model:
                self._model = model
            usage = getattr(carrier, "usage", None)
            if usage is not None:
                for name in _USAGE_FIELDS:
                    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
                    count = as_int(value)
                    if count is not None:
                        self._usage[name] = count
            text = getattr(carrier, "output_text", None)
            if isinstance(text, str) and text:
                self._final_text = text

    def _finish(self, error: Optional[BaseException]) -> None:
        if self._closed:
            return
        self._closed = True
        response = "".join(self._chunks) if self._chunks else self._final_text
        if self._span is not None and error is None and response:
            try:
                self._span.set_output({"text": response, "streamed": True})
            except Exception:  # noqa: BLE001
                pass
        self._tracer._close(
            self._span, None if error else _Streamed(self._model, self._usage), error
        )


class _Streamed:
    """What a finished stream amounted to, in the shape of a response.

    It has a ``model`` and a ``usage`` so the provider's own ``describe`` reads
    them exactly as it would off a unary response, and no text: that was
    accumulated chunk by chunk and is already on the span.
    """

    def __init__(self, model: Optional[str] = None, usage: Optional[Dict[str, int]] = None) -> None:
        self.model = model
        self.usage = types.SimpleNamespace(**usage) if usage else None


def _chunk_text(chunk: Any) -> Optional[str]:
    """The text carried by one streamed chunk, in either provider's shape."""
    kind = getattr(chunk, "type", None)
    if isinstance(kind, str) and kind.startswith("response.") and kind != "response.output_text.delta":
        # The Responses API streams tool arguments, reasoning summaries and
        # base64 audio as string ``delta``s too. None of them is what the model
        # said.
        return None
    delta = getattr(chunk, "delta", None)
    if isinstance(delta, str):
        return delta
    if delta is not None:
        text = getattr(delta, "text", None)
        if isinstance(text, str):
            return text
    choices = getattr(chunk, "choices", None)
    if choices:
        first = choices[0]
        choice_delta = getattr(first, "delta", None)
        content = getattr(choice_delta, "content", None) if choice_delta is not None else None
        if isinstance(content, str):
            return content
    return None


class TracedProxy:
    """Attribute-forwarding proxy that wraps only the methods it was told to."""

    def __init__(
        self, target: Any, tracer: MethodTracer, tracked: Set[str], path: str = "", view: str = ""
    ) -> None:
        object.__setattr__(self, "_fulcrum_target", target)
        object.__setattr__(self, "_fulcrum_tracer", tracer)
        object.__setattr__(self, "_fulcrum_tracked", tracked)
        object.__setattr__(self, "_fulcrum_path", path)
        object.__setattr__(self, "_fulcrum_view", view)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_fulcrum_target")
        tracer = object.__getattribute__(self, "_fulcrum_tracer")
        tracked = object.__getattribute__(self, "_fulcrum_tracked")
        path = object.__getattribute__(self, "_fulcrum_path")
        view = object.__getattribute__(self, "_fulcrum_view")

        value = getattr(target, name)

        # Two hops hand back the same surface with different plumbing, and
        # returning them raw is how a traced client quietly stopped being one:
        # ``client.with_options(timeout=5)`` is a new client, and
        # ``responses.with_raw_response`` is the same namespace answering with
        # HTTP responses. Neither is part of a method's path, so the path stands
        # still across them and ``...with_raw_response.create`` is still
        # ``responses.create``.
        if name in (RAW_VIEW, STREAMING_VIEW):
            return TracedProxy(value, tracer, tracked, path, name)
        if name in CLONING_METHODS and callable(value):

            @functools.wraps(value)
            def clone(*args: Any, **kwargs: Any) -> Any:
                return TracedProxy(value(*args, **kwargs), tracer, tracked, path, view)

            return clone

        full = "{0}.{1}".format(path, name) if path else name

        if full in tracked and callable(value):
            return tracer.wrap(full, value, view)
        prefix = full + "."
        if any(candidate.startswith(prefix) for candidate in tracked):
            return TracedProxy(value, tracer, tracked, full, view)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_fulcrum_target"), name, value)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "TracedProxy({0!r})".format(object.__getattribute__(self, "_fulcrum_target"))
