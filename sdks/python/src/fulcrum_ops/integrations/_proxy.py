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
from typing import Any, Callable, Dict, Optional, Sequence, Set

logger = logging.getLogger("fulcrum_ops")

__all__ = ["TracedProxy", "MethodTracer", "extract_text", "as_int"]


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
    ) -> None:
        self._client = client
        self._provider = provider
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

    def _open(self, path: str, kwargs: Dict[str, Any]) -> Any:
        try:
            return self._client.span(
                self._name_for(path),
                type=self._span_type,
                input=dict(kwargs) if kwargs else None,
                tags=self._tags or None,
                agent=self._agent,
                model=kwargs.get("model") if isinstance(kwargs.get("model"), str) else None,
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

    def wrap(self, path: str, method: Callable[..., Any]) -> Callable[..., Any]:
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
                return _finish_awaitable(tracer, span, result, streaming)
            if streaming and _is_stream(result, allow_context_manager=streams_always):
                return _StreamProxy(tracer, span, result)
            tracer._close(span, result, None)
            return result

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


async def _finish_awaitable(
    tracer: MethodTracer, span: Any, awaitable: Any, streaming: bool
) -> Any:
    try:
        result = await awaitable
    except BaseException as exc:
        tracer._close(span, None, exc)
        raise
    if streaming and _is_stream(result):
        return _StreamProxy(tracer, span, result)
    tracer._close(span, result, None)
    return result


class _StreamProxy:
    """Keeps a streaming call's span open until the stream is exhausted.

    A streamed completion's span should last as long as the stream does — that
    duration is the number a latency dashboard is actually about. The proxy also
    accumulates the chunks so the span records what the model said, which a
    naive wrapper loses entirely: the response object a streaming call returns
    is empty at the moment it is returned.
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
        self._closed = False

    @property
    def _iterable(self) -> Any:
        """Whichever object actually yields chunks."""
        return self._entered if self._entered is not None else self._stream

    # Anything the provider exposes on its stream object — ``.response``,
    # ``.close()``, ``.get_final_message()`` — falls through untouched.
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

    def _collect(self, chunk: Any) -> None:
        if len(self._chunks) >= 2_000:
            return
        text = _chunk_text(chunk)
        if text:
            self._chunks.append(text)

    def _finish(self, error: Optional[BaseException]) -> None:
        if self._closed:
            return
        self._closed = True
        response = "".join(self._chunks) if self._chunks else None
        if self._span is not None and error is None and response is not None:
            try:
                self._span.set_output({"text": response, "streamed": True})
            except Exception:  # noqa: BLE001
                pass
        self._tracer._close(self._span, None if error else _Streamed(), error)


class _Streamed:
    """Placeholder response for a stream, whose content was accumulated instead."""


def _chunk_text(chunk: Any) -> Optional[str]:
    """The text carried by one streamed chunk, in either provider's shape."""
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

    def __init__(self, target: Any, tracer: MethodTracer, tracked: Set[str], path: str = "") -> None:
        object.__setattr__(self, "_fulcrum_target", target)
        object.__setattr__(self, "_fulcrum_tracer", tracer)
        object.__setattr__(self, "_fulcrum_tracked", tracked)
        object.__setattr__(self, "_fulcrum_path", path)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_fulcrum_target")
        tracer = object.__getattribute__(self, "_fulcrum_tracer")
        tracked = object.__getattribute__(self, "_fulcrum_tracked")
        path = object.__getattribute__(self, "_fulcrum_path")

        value = getattr(target, name)
        full = "{0}.{1}".format(path, name) if path else name

        if full in tracked and callable(value):
            return tracer.wrap(full, value)
        prefix = full + "."
        if any(candidate.startswith(prefix) for candidate in tracked):
            return TracedProxy(value, tracer, tracked, full)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_fulcrum_target"), name, value)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "TracedProxy({0!r})".format(object.__getattribute__(self, "_fulcrum_target"))
