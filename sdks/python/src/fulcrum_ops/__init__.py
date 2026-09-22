"""``fulcrum_ops`` — report agent telemetry and governance events to FD AI Command Center.

::

    import fulcrum_ops
    from fulcrum_ops import trace

    fulcrum_ops.configure(agent="checkout-agent", environment="Production")

    @trace
    def answer(question: str) -> str:
        with fulcrum_ops.get_client().span("retrieval", type="tool") as span:
            docs = search(question)
            span.set_output({"chunks": len(docs)})
        return summarise(docs)

The one promise this package makes, which every module in it is written to
keep: **losing telemetry never breaks the caller's agent.** A missing API key,
an unreachable server, a full queue, a value that will not serialise —
none of them reach the calling code. They are counted, logged, and handed to the
``on_error`` hook.

The deliberate exceptions are the calls whose whole purpose is to return a
value: :meth:`FulcrumOps.config`, :meth:`FulcrumOps.get_prompt` and the dataset
helpers do raise, because there the failure *is* the answer and swallowing it
would hand a model an empty system prompt.

The provider wrappers live in :mod:`fulcrum_ops.integrations` and are imported
lazily, so the base package installs and imports cleanly with neither OpenAI,
Anthropic nor LangChain present.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from . import context as _context
from ._version import SDK_NAME, SDK_VERSION, USER_AGENT
from .client import FulcrumOps, configure, get_client, set_default_client, shutdown
from .context import TracedThreadPoolExecutor, propagate
from .datasets import (
    DatasetItem,
    Datasets,
    ExperimentResult,
    ExperimentRow,
    Experiments,
)
from .decorators import trace, traced
from .errors import (
    ApiError,
    AuthenticationError,
    ConfigurationError,
    EntitlementError,
    FulcrumOpsError,
    NetworkError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    ServerError,
    TelemetryUnavailableError,
    TimeoutError,
    TransportError,
    ValidationError,
)
from .ids import is_valid_id, new_id
from .options import DEFAULT_BASE_URL, Options, normalise_base_url, resolve_options
from .prompts import Prompt, render_template, template_variables
from .redaction import SUPPORTED_ENTITY_TYPES, RedactionRule
from .trace import NOOP_SPAN, SPAN_TYPES, NoopSpan, Span, Trace

__version__ = SDK_VERSION

__all__ = [
    # Client and module-level surface.
    "FulcrumOps",
    "configure",
    "get_client",
    "set_default_client",
    "shutdown",
    "flush",
    "score",
    "thumbs_up",
    "thumbs_down",
    "rate",
    "report_issue",
    # Tracing.
    "trace",
    "traced",
    "span",
    "current_span",
    "current_trace",
    "set_thread_id",
    "propagate",
    "TracedThreadPoolExecutor",
    "Trace",
    "Span",
    "NoopSpan",
    "SPAN_TYPES",
    # Prompts.
    "Prompt",
    "get_prompt",
    "render_template",
    "template_variables",
    # Offline evaluation.
    "Datasets",
    "DatasetItem",
    "Experiments",
    "ExperimentResult",
    "ExperimentRow",
    # Options and redaction.
    "Options",
    "resolve_options",
    "normalise_base_url",
    "DEFAULT_BASE_URL",
    "RedactionRule",
    "SUPPORTED_ENTITY_TYPES",
    # Identifiers.
    "new_id",
    "is_valid_id",
    # Errors.
    "FulcrumOpsError",
    "ConfigurationError",
    "ApiError",
    "AuthenticationError",
    "PermissionDeniedError",
    "QuotaExceededError",
    "EntitlementError",
    "ValidationError",
    "NotFoundError",
    "PayloadTooLargeError",
    "RateLimitError",
    "ServerError",
    "TelemetryUnavailableError",
    "TransportError",
    "NetworkError",
    "TimeoutError",
    # Identity.
    "SDK_NAME",
    "SDK_VERSION",
    "USER_AGENT",
    "__version__",
]


# ---------------------------------------------------------------------------
# Module-level conveniences.
#
# Each one is a thin forward to the default client, so a script can report
# something without holding a client — which is the whole point of ``configure``
# existing. They are functions rather than re-exported bound methods because the
# default client can be replaced at any time, and a bound method captured at
# import would keep pointing at the client that existed then.
# ---------------------------------------------------------------------------


def span(name: str, **kwargs: Any) -> Span:
    """Open a span on the default client.

    ::

        with fulcrum_ops.span("retrieval", type="tool") as s:
            s.set_output({"chunks": len(chunks)})
    """
    return get_client().span(name, **kwargs)  # type: ignore[union-attr]


def current_span() -> Union[Span, NoopSpan]:
    """The innermost open span here. Never ``None``.

    This is how the body of a decorated function reports what only it knows::

        @trace(name="answer", type="llm")
        def answer(question: str) -> str:
            reply = model.respond(question)
            fulcrum_ops.current_span().set_model(reply.model, "openai").set_usage(
                prompt_tokens=reply.usage.input_tokens,
                completion_tokens=reply.usage.output_tokens,
            )
            return reply.text

    It needs no client: the span is carried by the execution context.

    With no span open — reporting is off, the run was not sampled in, the
    function was called from outside anything traced, or the enclosing
    ``@trace`` is a plain one, which opens a run and no step — the answer is a
    :class:`NoopSpan` that accepts the same calls and records nothing. ``None``
    there would turn "telemetry is switched off on this laptop" into an
    ``AttributeError`` inside the customer's agent, which is the one thing this
    package promises never to do. It is falsy, so ``if fulcrum_ops.current_span():``
    still tells the two apart; :meth:`FulcrumOps.current_span` keeps answering
    ``None`` for code that wants the bare fact.
    """
    span = _context.current_span()
    return span if span is not None else NOOP_SPAN


def current_trace() -> Optional[Trace]:
    """The trace this thread or task is inside, or ``None`` outside one."""
    return _context.current_trace()


def set_thread_id(thread_id: Optional[str]) -> bool:
    """File the run this code is inside under a conversation.

    ::

        @trace(name="underwriting_insight", type="llm")
        def get_insight(self, email):
            fulcrum_ops.set_thread_id(email.conversation_id)
            ...

    Runs that share a ``thread_id`` are one session in the console; a run with
    none belongs to no session, and the Sessions and Memory views stay empty for
    that agent. Returns ``False`` when no run is open here — tracing is off, or
    this is not inside a traced call — and never raises. For an id that is one
    of the function's arguments, ``@trace(thread_id=lambda ...)`` says the same
    thing in one line.
    """
    run = _context.current_trace()
    if run is None:
        return False
    run.set_thread_id(thread_id)
    return True


def score(trace_id: str, name: str, value: float, **kwargs: Any) -> bool:
    """Attach a feedback score to a trace, span or thread, via the default client."""
    client = get_client()
    return client.score(trace_id, name, value, **kwargs) if client else False


def thumbs_up(trace_id: str = None, comment: str = None, **kwargs: Any) -> bool:
    """Record positive feedback on a run, via the default client."""
    client = get_client()
    return client.thumbs_up(trace_id, comment, **kwargs) if client else False


def thumbs_down(trace_id: str = None, comment: str = None, **kwargs: Any) -> bool:
    """Record negative feedback on a run, via the default client."""
    client = get_client()
    return client.thumbs_down(trace_id, comment, **kwargs) if client else False


def rate(stars: int, trace_id: str = None, comment: str = None, **kwargs: Any) -> bool:
    """Record a star rating on a run, via the default client."""
    client = get_client()
    return client.rate(stars, trace_id, comment, **kwargs) if client else False


def report_issue(title: str, **kwargs: Any) -> bool:
    """Raise a quality issue from inside the agent, via the default client."""
    client = get_client()
    return client.report_issue(title, **kwargs) if client else False


def get_prompt(name: str, **kwargs: Any) -> Prompt:
    """Fetch a prompt from Prompt Studio, via the default client."""
    return get_client().get_prompt(name, **kwargs)  # type: ignore[union-attr]


def flush(timeout: float = 10.0) -> bool:
    """Send everything the default client has queued. Returns False on timeout."""
    client = get_client(create=False)
    return client.flush(timeout=timeout) if client else True
