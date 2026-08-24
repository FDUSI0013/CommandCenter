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
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from .trace import Span, Trace

__all__ = [
    "current_span",
    "current_trace",
    "attach_span",
    "attach_trace",
    "detach",
    "span_stack",
]

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
    """The trace this thread or task is currently inside, if any."""
    return _current_trace.get()


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
