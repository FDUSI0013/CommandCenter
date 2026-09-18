"""``@trace`` — the one-line way to instrument a function.

The decorator has to cover four shapes of callable, because an agent codebase
contains all four: a plain function, a coroutine, a generator (a streaming
response), and an async generator (a streaming response from an async client).
Each gets a wrapper of its own kind, because wrapping a generator in a coroutine
— or the reverse — changes the function's contract, and the SDK is not allowed
to change how the customer's code behaves.

What every wrapper guarantees:

* The decorated function's return value, exceptions and type are unchanged. An
  exception is recorded and re-raised, not swallowed.
* A failure *inside the SDK* never reaches the caller. If the instrumentation
  cannot open a span, the function is simply called.
* The first decorated call in a context opens a trace; every decorated call
  nested inside it opens a child span. Nothing is passed between them —
  :mod:`contextvars` carries the parent link.

For generators, the span opens at the first ``next()`` rather than at the call,
and closes when iteration finishes, so its duration is the time the stream took
rather than the microsecond it took to build the generator object.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar, Union

from . import context as _context
from .trace import Span, Trace

__all__ = ["trace", "traced"]

logger = logging.getLogger("fulcrum_ops")

F = TypeVar("F", bound=Callable[..., Any])

#: Yielded values kept as a generator's recorded output. A stream of ten
#: thousand tokens is not telemetry, it is a denial of service on your own
#: ingest quota, so the tail is counted rather than stored.
MAX_YIELDED_ITEMS = 100

class _TypedRoot:
    """A trace together with the one typed span that is its whole body.

    ``@trace(type="llm")`` on a function nobody else has traced opens a run, and
    that run *is* an LLM call. A trace carries no type, no model and no token
    counts -- only a span does -- so recording the trace alone leaves a run with
    a name, a duration and nothing else: no model, no tokens, no cost and an
    empty step list. Opening the span as well is what makes the decorator's
    ``type`` mean something at the root, and gives ``current_span()`` something
    to hang ``set_model()`` and ``set_usage()`` on.
    """

    __slots__ = ("trace", "span")

    def __init__(self, trace: Trace, span: Span) -> None:
        self.trace = trace
        self.span = span

    def set_output(self, value: Any) -> None:
        self.span.set_output(value)
        self.trace.set_output(value)

    def record_exception(self, exc: BaseException) -> None:
        self.span.record_exception(exc)
        self.trace.record_exception(exc)

    def end(self) -> None:
        self.span.end()  # first: it nests itself inside the trace it closes into
        self.trace.end()


_Item = Union[Trace, Span, _TypedRoot]
_Tokens = Tuple[Any, Any]


# --------------------------------------------------------------------- context


def _attach(item: _Item) -> _Tokens:
    """Make ``item`` current, returning the tokens that undo it."""
    if isinstance(item, _TypedRoot):
        return (_context.attach_trace(item.trace), _context.attach_span(item.span))
    if isinstance(item, Trace):
        return (_context.attach_trace(item), _context.attach_span(None))
    return (None, _context.attach_span(item))


def _detach(tokens: Optional[_Tokens]) -> None:
    if not tokens:
        return
    trace_token, span_token = tokens
    _context.detach("span", span_token)
    if trace_token is not None:
        _context.detach("trace", trace_token)


# ------------------------------------------------------------------- capture


def _bind_inputs(
    func: Callable[..., Any],
    args: Sequence[Any],
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Name the arguments the function was called with.

    ``{"question": "...", "top_k": 5}`` is worth reading in the console;
    ``{"args": ["..."], "kwargs": {...}}`` is not, which is why this binds
    against the signature rather than recording the raw tuple. ``self`` and
    ``cls`` are dropped: the receiver is not an input, and serialising it drags
    in whatever the object happens to hold.
    """
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        data = dict(bound.arguments)

        # Flatten **kwargs so its contents read as ordinary fields.
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                extra = data.pop(parameter.name, None)
                if isinstance(extra, dict):
                    data.update(extra)
            elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                extra = data.pop(parameter.name, None)
                if extra:
                    data[parameter.name] = list(extra)
    except Exception:  # noqa: BLE001 - an unbindable signature is not the caller's problem
        data = {}
        if args:
            data["args"] = list(args)
        if kwargs:
            data["kwargs"] = dict(kwargs)

    data.pop("self", None)
    data.pop("cls", None)
    return data


def _stream_output(items: List[Any], total: int, returned: Any) -> Dict[str, Any]:
    output: Dict[str, Any] = {"items": items, "yielded": total}
    if total > len(items):
        output["truncated"] = total - len(items)
    if returned is not None:
        output["returned"] = returned
    return output


# ------------------------------------------------------------------ decorator


def trace(  # noqa: C901 - one function, four wrapper shapes; splitting it hides the symmetry
    func: Optional[F] = None,
    *,
    name: Optional[str] = None,
    type: str = "general",  # noqa: A002 - matches the wire field name
    capture_input: bool = True,
    capture_output: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    agent: Optional[str] = None,
    thread_id: Optional[str] = None,
    client: Optional[Any] = None,
) -> Any:
    """Trace a function, coroutine, generator or async generator.

    ::

        import fulcrum_ops
        from fulcrum_ops import trace

        fulcrum_ops.configure(agent="checkout-agent")

        @trace
        def answer(question: str) -> str:
            docs = retrieve(question)          # a child span, if it is traced too
            return summarise(docs)

        @trace(name="retrieve", type="tool")
        def retrieve(question: str) -> list[str]:
            ...

    :param name: Span name. Defaults to the function's qualified name.
    :param type: ``general``, ``llm``, ``tool`` or ``guardrail``.
    :param capture_input: Record the bound arguments. Off for a function whose
        arguments are large or sensitive; the workspace's redaction rules apply
        either way.
    :param capture_output: Record the return value.
    :param client: Report to this client instead of the default one.
    """

    def decorate(target: F) -> F:
        span_name = name or getattr(target, "__qualname__", None) or getattr(target, "__name__", "traced")

        def resolve_client() -> Optional[Any]:
            if client is not None:
                return client
            from .client import get_client

            try:
                return get_client()
            except Exception:  # noqa: BLE001 - a client that cannot be built is not an error here
                logger.debug("fulcrum-ops: no default client available", exc_info=True)
                return None

        def open_item(active: Any, args: Sequence[Any], kwargs: Dict[str, Any]) -> Optional[_Item]:
            """Open a trace or a child span, whichever the context calls for."""
            try:
                captured = (
                    _bind_inputs(target, args, kwargs)
                    if capture_input and active.capture_input
                    else None
                )
                if _context.current_trace() is None:
                    root = active.trace(
                        span_name,
                        input=captured,
                        metadata=metadata,
                        tags=tags,
                        agent=agent,
                        thread_id=thread_id,
                    )
                    if type == "general":
                        return root
                    # The span is opened with the trace current and no span
                    # current, exactly as it would be inside ``with trace:`` --
                    # otherwise a sibling's leftover span becomes its parent.
                    tokens = _attach(root)
                    try:
                        body = root.span(
                            span_name, type=type, input=captured, metadata=metadata, tags=tags
                        )
                    finally:
                        _detach(tokens)
                    return _TypedRoot(root, body)
                return active.span(
                    span_name,
                    type=type,
                    input=captured,
                    metadata=metadata,
                    tags=tags,
                    agent=agent,
                )
            except Exception:  # noqa: BLE001 - instrumentation must not break the call
                logger.debug("fulcrum-ops: could not open a span for %s", span_name, exc_info=True)
                return None

        def close_item(item: Optional[_Item], output: Any, exc: Optional[BaseException]) -> None:
            if item is None:
                return
            try:
                if exc is not None:
                    item.record_exception(exc)
                elif capture_output and output is not None:
                    item.set_output(output)
                item.end()
            except Exception:  # noqa: BLE001
                logger.debug("fulcrum-ops: could not close the span for %s", span_name, exc_info=True)

        # ------------------------------------------------------ async generator

        if inspect.isasyncgenfunction(target):

            @functools.wraps(target)
            async def async_gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                active = resolve_client()
                if active is None or not active.enabled:
                    async for value in target(*args, **kwargs):
                        yield value
                    return

                item = open_item(active, args, kwargs)
                tokens = _attach(item) if item is not None else None
                collected: List[Any] = []
                total = 0
                failure: Optional[BaseException] = None
                try:
                    async for value in target(*args, **kwargs):
                        total += 1
                        if len(collected) < MAX_YIELDED_ITEMS:
                            collected.append(value)
                        _detach(tokens)
                        tokens = None
                        try:
                            yield value
                        finally:
                            if item is not None:
                                tokens = _attach(item)
                except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
                    failure = exc if not isinstance(exc, GeneratorExit) else None
                    raise
                finally:
                    _detach(tokens)
                    close_item(
                        item,
                        _stream_output(collected, total, None) if capture_output else None,
                        failure,
                    )

            return async_gen_wrapper  # type: ignore[return-value]

        # ------------------------------------------------------------ coroutine

        if inspect.iscoroutinefunction(target):

            @functools.wraps(target)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                active = resolve_client()
                if active is None or not active.enabled:
                    return await target(*args, **kwargs)

                item = open_item(active, args, kwargs)
                tokens = _attach(item) if item is not None else None
                try:
                    result = await target(*args, **kwargs)
                except BaseException as exc:
                    _detach(tokens)
                    close_item(item, None, exc)
                    raise
                _detach(tokens)
                close_item(item, result, None)
                return result

            return async_wrapper  # type: ignore[return-value]

        # ------------------------------------------------------- sync generator

        if inspect.isgeneratorfunction(target):

            @functools.wraps(target)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                active = resolve_client()
                if active is None or not active.enabled:
                    for value in target(*args, **kwargs):
                        yield value
                    return

                item = open_item(active, args, kwargs)
                tokens = _attach(item) if item is not None else None
                collected: List[Any] = []
                total = 0
                returned: Any = None
                failure: Optional[BaseException] = None
                try:
                    iterator = target(*args, **kwargs)
                    while True:
                        try:
                            value = next(iterator)
                        except StopIteration as stop:
                            returned = getattr(stop, "value", None)
                            break
                        total += 1
                        if len(collected) < MAX_YIELDED_ITEMS:
                            collected.append(value)
                        _detach(tokens)
                        tokens = None
                        try:
                            yield value
                        finally:
                            if item is not None:
                                tokens = _attach(item)
                except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
                    failure = exc if not isinstance(exc, GeneratorExit) else None
                    raise
                finally:
                    _detach(tokens)
                    close_item(
                        item,
                        _stream_output(collected, total, returned) if capture_output else None,
                        failure,
                    )

            return gen_wrapper  # type: ignore[return-value]

        # ------------------------------------------------------- plain function

        @functools.wraps(target)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            active = resolve_client()
            if active is None or not active.enabled:
                return target(*args, **kwargs)

            item = open_item(active, args, kwargs)
            tokens = _attach(item) if item is not None else None
            try:
                result = target(*args, **kwargs)
            except BaseException as exc:
                _detach(tokens)
                close_item(item, None, exc)
                raise
            _detach(tokens)
            close_item(item, result, None)
            return result

        return sync_wrapper  # type: ignore[return-value]

    # Used bare (``@trace``) or called (``@trace(name="x")``).
    if func is not None:
        return decorate(func)
    return decorate


#: Alias, for codebases that already have something called ``trace``.
traced = trace
