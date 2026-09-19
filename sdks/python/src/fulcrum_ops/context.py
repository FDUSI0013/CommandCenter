"""Where the SDK keeps "the span we are currently inside".

``contextvars`` is the only mechanism that gets this right in every execution
model Python offers at once: it is thread-local for threads, task-local for
asyncio, and — unlike a plain ``threading.local`` — it is *copied* into a new
task rather than shared with it, so two concurrent requests never see each
other's spans. That is why nesting works with no argument passing: a decorated
function called from inside another decorated function finds its parent by
reading the same variable the parent wrote.

Generators complicate this. A generator body runs in whatever context is active
at each ``next()``, so a ``set()`` inside one leaks out to the consumer. The
tracing code therefore detaches around every ``yield`` and re-attaches after,
and every reset goes through :func:`detach`, which tolerates the ``ValueError``
raised when a token is reset in a context other than the one that created it.

Threads complicate it the other way. A new *task* starts with a copy of its
creator's context, and so does ``asyncio.to_thread``; a plain worker thread
starts with an empty one. Work handed to ``ThreadPoolExecutor.submit`` or
``loop.run_in_executor`` therefore cannot see the run it was started from, and
every step it opens becomes a run of its own. :func:`propagate` and
:class:`TracedThreadPoolExecutor` carry the two variables across that hop.
"""

from __future__ import annotations

import contextvars
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, TypeVar

if TYPE_CHECKING:  # pragma: no cover
    from .trace import Span, Trace

__all__ = [
    "current_span",
    "current_trace",
    "attach_span",
    "attach_trace",
    "detach",
    "span_stack",
    "propagate",
    "TracedThreadPoolExecutor",
]

_F = TypeVar("_F", bound=Callable[..., Any])

_current_span: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar(
    "fulcrum_ops_current_span", default=None
)
_current_trace: contextvars.ContextVar[Optional["Trace"]] = contextvars.ContextVar(
    "fulcrum_ops_current_trace", default=None
)


def current_span() -> Optional["Span"]:
    """The innermost span in this thread or task, if any."""
    return _current_span.get()


def current_trace() -> Optional["Trace"]:
    """The trace this thread or task is currently inside, if any.

    A trace that has ended is not one anybody is inside. It can still be in the
    variable -- :func:`detach` cannot reset a token made in another context, so
    a trace entered in one task and closed in another stays behind in the first
    -- and handing it out would hang every later span on a run already sent.
    """
    trace = _current_trace.get()
    if trace is not None and getattr(trace, "_ended", False):
        return None
    return trace


def attach_span(span: Optional["Span"]) -> Any:
    """Make ``span`` the current one; returns a token for :func:`detach`."""
    return _current_span.set(span)


def attach_trace(trace: Optional["Trace"]) -> Any:
    """Make ``trace`` the current one; returns a token for :func:`detach`."""
    return _current_trace.set(trace)


def detach(variable: str, token: Any) -> None:
    """Undo an attach, tolerating a token created in a different context.

    Resetting a token from another context raises ``ValueError``. That happens
    when a generator is advanced from two different tasks, which is unusual but
    entirely legal, and losing the reset is a cosmetic problem while raising
    here would corrupt the caller's control flow.
    """
    if token is None:
        return
    var = _current_span if variable == "span" else _current_trace
    try:
        var.reset(token)
    except (ValueError, RuntimeError):
        var.set(None)


def span_stack() -> List[Tuple[str, str]]:
    """The (name, id) chain from the root span down to the current one.

    Only used for diagnostics; the parent link on the wire comes from
    ``parent_span_id``, not from this.
    """
    stack: List[Tuple[str, str]] = []
    span = _current_span.get()
    seen = set()
    while span is not None and span.id not in seen:
        seen.add(span.id)
        stack.append((span.name, span.id))
        span = span.parent
    stack.reverse()
    return stack


def propagate(fn: _F) -> _F:
    """Bind ``fn`` to the run that is open *here*, for a thread that would not inherit it.

    ::

        with ThreadPoolExecutor(4) as pool:
            pages = list(pool.map(fulcrum_ops.propagate(extract), attachments))

        await loop.run_in_executor(None, fulcrum_ops.propagate(poll_once), mailbox)

    The trace and span current at the moment ``propagate`` is called are made
    current again around every call of the returned function, on whichever
    thread that happens, and taken down afterwards — pool threads are reused,
    and a worker that kept one job's run would hang the next job's steps on it.
    Only this SDK's own two variables travel; the rest of the worker's context
    is left exactly as the customer's code finds it today.

    Not needed for ``asyncio`` tasks or ``asyncio.to_thread``, which already
    copy the context, and harmless there.
    """
    trace = _current_trace.get()
    span = _current_span.get()

    @functools.wraps(fn)
    def bound(*args: Any, **kwargs: Any) -> Any:
        trace_token = _current_trace.set(trace)
        span_token = _current_span.set(span)
        try:
            return fn(*args, **kwargs)
        finally:
            detach("span", span_token)
            detach("trace", trace_token)

    return bound  # type: ignore[return-value]


class TracedThreadPoolExecutor(ThreadPoolExecutor):
    """A ``ThreadPoolExecutor`` whose jobs stay inside the run that submitted them.

    A drop-in: ``submit`` and ``map`` wrap each callable in :func:`propagate`
    at the moment it is handed over, so a fan-out from a traced function shows
    up as steps of that run rather than as a scatter of one-step runs named
    after the helper. It can be given to ``loop.run_in_executor`` as well.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return super().submit(propagate(fn), *args, **kwargs)
