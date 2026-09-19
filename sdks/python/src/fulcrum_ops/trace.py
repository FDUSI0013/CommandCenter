"""Traces and spans — the objects a caller holds while work is in flight.

A :class:`Trace` is one end-to-end agent invocation. A :class:`Span` is one unit
of work inside it: a retrieval, a model call, a tool. Spans nest, and the
nesting is discovered from :mod:`contextvars` rather than passed around, so a
function three layers down does not need a handle on anything to land in the
right place in the tree.

Nothing here touches the network. Both classes build a payload on ``end()`` and
hand it to the client, which queues it. That separation is what lets the
capture flags and the redaction rules from ``GET /ingest/config`` apply to work
that started before the config document arrived: the payload is not built until
the span closes.
"""

from __future__ import annotations

import datetime as dt
import traceback as _traceback
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union

from . import context as _context
from .ids import new_id
from .limits import (
    MAX_AGENT_LENGTH,
    MAX_EXCEPTION_MESSAGE_LENGTH,
    MAX_EXCEPTION_TYPE_LENGTH,
    MAX_METADATA_KEYS,
    MAX_MODEL_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PROVIDER_LENGTH,
    MAX_SCORE_CATEGORY_LENGTH,
    MAX_SCORE_NAME_LENGTH,
    MAX_SCORE_REASON_LENGTH,
    MAX_SCORE_SOURCE_LENGTH,
    MAX_SCORES_PER_ITEM,
    MAX_SPANS_PER_TRACE,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    MAX_THREAD_ID_LENGTH,
    MAX_TRACEBACK_LENGTH,
    MAX_USAGE_KEYS,
    clamp_required_text,
    clamp_text,
)
from .serialize import to_json_safe, to_payload

if TYPE_CHECKING:  # pragma: no cover
    from .client import FulcrumOps

__all__ = ["Span", "Trace", "NoopSpan", "NOOP_SPAN", "SPAN_TYPES"]

#: The span classes the telemetry engine models separately.
SPAN_TYPES = ("general", "llm", "tool", "guardrail")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_tags(tags: Optional[Sequence[str]]) -> List[str]:
    if not tags:
        return []
    out: List[str] = []
    for tag in tags:
        cleaned = clamp_text(str(tag), MAX_TAG_LENGTH)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= MAX_TAGS:
            break
    return out


def _clean_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not metadata:
        return {}
    items = list(metadata.items())[:MAX_METADATA_KEYS]
    return {str(key): to_json_safe(value) for key, value in items}


def _clean_usage(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """``usage`` is typed as integer counters; anything else is dropped, not coerced badly."""
    if not usage:
        return None
    out: Dict[str, int] = {}
    for key, value in list(usage.items())[:MAX_USAGE_KEYS]:
        try:
            if value is None or isinstance(value, bool):
                continue
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out or None


def _error_info(exc: BaseException) -> Dict[str, Any]:
    """Capture a failure in the shape ``ErrorInfoIn`` expects."""
    try:
        text = "".join(
            _traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except Exception:  # pragma: no cover
        text = ""
    info: Dict[str, Any] = {
        "exception_type": clamp_required_text(
            type(exc).__name__, MAX_EXCEPTION_TYPE_LENGTH, "Exception"
        )
    }
    message = clamp_text(str(exc), MAX_EXCEPTION_MESSAGE_LENGTH)
    if message:
        info["message"] = message
    trimmed = clamp_text(text, MAX_TRACEBACK_LENGTH)
    if trimmed:
        info["traceback"] = trimmed
    return info


def _score_payload(
    name: str,
    value: float,
    *,
    reason: Optional[str] = None,
    category: Optional[str] = None,
    source: str = "sdk",
) -> Optional[Dict[str, Any]]:
    cleaned_name = clamp_text(name, MAX_SCORE_NAME_LENGTH)
    if not cleaned_name:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    payload: Dict[str, Any] = {
        "name": cleaned_name,
        "value": numeric,
        "source": clamp_text(source, MAX_SCORE_SOURCE_LENGTH) or "sdk",
    }
    category_name = clamp_text(category, MAX_SCORE_CATEGORY_LENGTH)
    if category_name:
        payload["category_name"] = category_name
    reason_text = clamp_text(reason, MAX_SCORE_REASON_LENGTH)
    if reason_text:
        payload["reason"] = reason_text
    return payload


class _Recordable:
    """The fields and setters a trace and a span have in common."""

    def __init__(
        self,
        client: "FulcrumOps",
        name: str,
        *,
        input: Any = None,  # noqa: A002 - matches the wire field name
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        agent: Optional[str] = None,
    ) -> None:
        self._client = client
        self.name = clamp_required_text(name, MAX_NAME_LENGTH, "unnamed")
        self.start_time = _now()
        self.end_time: Optional[dt.datetime] = None
        self.input = input
        self.output: Any = None
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self.tags: List[str] = list(tags or [])
        self.agent = agent
        self.error: Optional[BaseException] = None
        self.feedback_scores: List[Dict[str, Any]] = []
        self._logs: List[Dict[str, Any]] = []
        self._ended = False

    # ------------------------------------------------------------- mutation

    def set_input(self, value: Any) -> "_Recordable":
        """Record what this unit of work was given."""
        self.input = value
        return self

    def set_output(self, value: Any) -> "_Recordable":
        """Record what this unit of work produced."""
        self.output = value
        return self

    def set_metadata(self, values: Optional[Dict[str, Any]] = None, **kwargs: Any) -> "_Recordable":
        """Merge extra structured fields onto the item."""
        if values:
            self.metadata.update(values)
        if kwargs:
            self.metadata.update(kwargs)
        return self

    def add_tags(self, *tags: str) -> "_Recordable":
        """Add free-text tags, deduplicated and length-capped on the way out."""
        self.tags.extend(str(tag) for tag in tags)
        return self

    def log(self, message: Optional[str] = None, **fields: Any) -> "_Recordable":
        """Attach a timestamped note to this item.

        Notes ride along in ``metadata.logs``, which is where the run detail
        screen reads them from. This is deliberately not a logging framework:
        it exists so the one value that explains a run — the retrieved chunk
        count, the tool's raw arguments, the branch the agent took — is visible
        next to the span it belongs to instead of in a log file nobody
        correlates.
        """
        entry: Dict[str, Any] = {"at": _iso(_now())}
        if message is not None:
            entry["message"] = str(message)
        if fields:
            entry.update(fields)
        self._logs.append(entry)
        return self

    def score(
        self,
        name: str,
        value: float,
        *,
        reason: Optional[str] = None,
        category: Optional[str] = None,
        source: str = "sdk",
    ) -> "_Recordable":
        """Attach a feedback score, evaluated inline while the work is still in scope."""
        payload = _score_payload(name, value, reason=reason, category=category, source=source)
        if payload and len(self.feedback_scores) < MAX_SCORES_PER_ITEM:
            self.feedback_scores.append(payload)
        return self

    def record_exception(self, exc: BaseException) -> "_Recordable":
        """Mark this item as failed, keeping the type, message and traceback."""
        self.error = exc
        return self

    # --------------------------------------------------------------- payload

    def _payload_common(self) -> Dict[str, Any]:
        client = self._client
        body: Dict[str, Any] = {
            "name": self.name,
            "start_time": _iso(self.start_time),
        }
        end = _iso(self.end_time)
        if end:
            body["end_time"] = end

        metadata = dict(self.metadata)
        if self._logs:
            metadata["logs"] = self._logs
        if client.environment and "environment" not in metadata:
            metadata["environment"] = client.environment
        metadata = _clean_metadata(metadata)
        if metadata:
            # ``metadata`` is the one captured field the contract does not type
            # as nullable, so a redaction failure — which returns None rather
            # than risk leaking — has to drop the key instead of writing a null
            # the endpoint would refuse for the whole row.
            redacted = client._redact(metadata, "metadata")
            if redacted is not None:
                body["metadata"] = redacted

        tags = _clean_tags(self.tags)
        if tags:
            body["tags"] = tags

        if client.capture_input and self.input is not None:
            payload = to_payload(self.input)
            if payload is not None:
                body["input"] = client._redact(payload, "input")
        if client.capture_output and self.output is not None:
            payload = to_payload(self.output)
            if payload is not None:
                body["output"] = client._redact(payload, "output")

        if self.error is not None:
            body["error_info"] = _error_info(self.error)
        if self.feedback_scores:
            body["feedback_scores"] = self.feedback_scores[:MAX_SCORES_PER_ITEM]

        agent = clamp_text(self.agent, MAX_AGENT_LENGTH)
        if agent:
            body["agent"] = agent
        return body


class Span(_Recordable):
    """One unit of work inside a trace."""

    def __init__(
        self,
        client: "FulcrumOps",
        name: str,
        *,
        trace: Optional["Trace"] = None,
        trace_id: Optional[str] = None,
        parent: Optional["Span"] = None,
        type: str = "general",  # noqa: A002 - matches the wire field name
        input: Any = None,  # noqa: A002
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        super().__init__(client, name, input=input, metadata=metadata, tags=tags, agent=agent)
        self.id = new_id()
        self.trace = trace
        self.trace_id = trace_id or (trace.id if trace is not None else new_id())
        self.parent = parent
        self.parent_span_id = parent.id if parent is not None else None
        self.type = type if type in SPAN_TYPES else "general"
        self.model = model
        self.provider = provider
        self.usage: Optional[Dict[str, Any]] = None
        self.total_estimated_cost: Optional[float] = None
        self._span_token: Any = None
        self._owns_trace = False

    # ------------------------------------------------------------- mutation

    def set_model(self, model: str, provider: Optional[str] = None) -> "Span":
        """Name the model this span called, which is what the cost view groups by."""
        self.model = model
        if provider:
            self.provider = provider
        return self

    def set_usage(self, usage: Optional[Dict[str, Any]] = None, **counters: Any) -> "Span":
        """Record token counters. Non-integer values are dropped rather than coerced."""
        merged = dict(self.usage or {})
        if usage:
            merged.update(usage)
        if counters:
            merged.update(counters)
        self.usage = merged
        return self

    def set_cost(self, cost: float) -> "Span":
        """Record an estimated cost the caller computed itself."""
        try:
            value = float(cost)
        except (TypeError, ValueError):
            return self
        self.total_estimated_cost = max(0.0, value)
        return self

    # -------------------------------------------------------------- lifetime

    def end(self, output: Any = None, *, error: Optional[BaseException] = None) -> "Span":
        """Close the span and hand it to the client. Idempotent."""
        if self._ended:
            return self
        self._ended = True
        self.end_time = _now()
        if output is not None:
            self.output = output
        if error is not None:
            self.error = error

        if self.trace is not None and not self._client.stream_spans:
            self.trace._add_span(self)
        else:
            self._client._report_span(self)

        if self._owns_trace and self.trace is not None:
            # A span that is the whole run *is* the run: the console reads a
            # run's input and output off the trace, so a trace opened only to
            # carry this span would otherwise list as a run that took nothing
            # in and gave nothing back.
            if self.trace.input is None:
                self.trace.input = self.input
            if self.trace.output is None:
                self.trace.output = self.output
            # ``self.error``, not just the argument: the provider wrappers mark
            # a failure with ``record_exception()`` and then call ``end()``
            # bare, and a model call that raised would otherwise list as a run
            # that completed.
            #
            # Through ``__exit__`` rather than ``end()`` so that, when this span
            # was entered as a context manager and took the trace into the
            # context with it, the trace's tokens come back down too. A span
            # that was never entered has none, and this is then just ``end()``.
            self.trace.__exit__(None, self.error, None)
        return self

    def to_payload(self, *, include_trace_id: bool) -> Dict[str, Any]:
        """Build the ``SpanIn`` body."""
        body = self._payload_common()
        body["id"] = self.id
        body["type"] = self.type
        if include_trace_id:
            body["trace_id"] = self.trace_id
        if self.parent_span_id:
            body["parent_span_id"] = self.parent_span_id
        model = clamp_text(self.model, MAX_MODEL_LENGTH)
        if model:
            body["model"] = model
        provider = clamp_text(self.provider, MAX_PROVIDER_LENGTH)
        if provider:
            body["provider"] = provider
        usage = _clean_usage(self.usage)
        if usage:
            body["usage"] = usage
        if self.total_estimated_cost is not None:
            body["total_estimated_cost"] = self.total_estimated_cost
        return body

    # -------------------------------------------------------- context manager

    def __enter__(self) -> "Span":
        # A lone span carries its trace into the context here, not when it is
        # created. ``with`` pairs this with ``__exit__`` in the same context, so
        # whatever is attached is always detached. Attaching at creation had no
        # such partner: a span opened in one context and closed in another -- a
        # provider call awaited under ``asyncio.gather``, a stream drained by a
        # thread pool -- left its finished trace current in the first one, and
        # every later span there was appended to a run already sent.
        if self._owns_trace and self.trace is not None and not self.trace._ended:
            self.trace.__enter__()
        self._span_token = _context.attach_span(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        _context.detach("span", self._span_token)
        self._span_token = None
        self.end(error=exc if isinstance(exc, BaseException) else None)
        return False

    async def __aenter__(self) -> "Span":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Span(name={0!r}, type={1!r}, id={2!r})".format(self.name, self.type, self.id)


class NoopSpan:
    """What ``fulcrum_ops.current_span()`` answers when no span is open.

    The body of a traced function reports what only it knows with
    ``fulcrum_ops.current_span().set_usage(...)``. With reporting switched off —
    no API key on a laptop, a run that was not sampled in, a function called
    from outside anything traced — there is no span, and ``None`` there turns
    the SDK being *off* into an ``AttributeError`` in the customer's agent.
    Every recording method is accepted and discarded instead. It is falsy, so
    ``if span:`` still tells the two apart.
    """

    __slots__ = ()

    id = None
    trace = None
    trace_id = None

    def __bool__(self) -> bool:
        return False

    def _discard(self, *args: Any, **kwargs: Any) -> "NoopSpan":
        return self

    set_input = set_output = set_metadata = add_tags = log = score = _discard
    record_exception = set_model = set_usage = set_cost = end = _discard

    def __enter__(self) -> "NoopSpan":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    async def __aenter__(self) -> "NoopSpan":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "NoopSpan()"


#: There is only ever a need for one.
NOOP_SPAN = NoopSpan()


class Trace(_Recordable):
    """One end-to-end agent invocation, with its spans nested inside it."""

    def __init__(
        self,
        client: "FulcrumOps",
        name: str,
        *,
        input: Any = None,  # noqa: A002
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        agent: Optional[str] = None,
        thread_id: Optional[str] = None,
        sampled: bool = True,
    ) -> None:
        super().__init__(client, name, input=input, metadata=metadata, tags=tags, agent=agent)
        self.id = new_id()
        self.thread_id = thread_id
        self.sampled = sampled
        self.spans: List[Span] = []
        self._trace_token: Any = None
        self._span_token: Any = None

    def set_thread_id(self, thread_id: Optional[str]) -> "Trace":
        """File this run under a conversation.

        The Sessions, Conversation State and Memory views are built from
        threads, and a thread exists only for runs that name one. The payload is
        not built until the trace ends, so this can be called at any point while
        the run is open — typically once the code has parsed whatever carries
        the conversation id.
        """
        self.thread_id = None if thread_id is None else str(thread_id)
        return self

    # ----------------------------------------------------------------- spans

    def span(
        self,
        name: str,
        *,
        type: str = "general",  # noqa: A002
        input: Any = None,  # noqa: A002
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        parent: Optional[Span] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Span:
        """Open a child span. Its parent is the innermost open span, unless one is named."""
        if parent is None:
            # Only a span of *this* trace. The context may hold a span from
            # another run -- this trace was built without being entered, or the
            # caller is inside somebody else's -- and a parent id that points
            # into a different trace is a step the console can never place.
            ambient = _context.current_span()
            if ambient is not None and ambient.trace is self:
                parent = ambient
        return Span(
            self._client,
            name,
            trace=self,
            trace_id=self.id,
            parent=parent,
            type=type,
            input=input,
            metadata=metadata,
            tags=tags,
            agent=self.agent,
            model=model,
            provider=provider,
        )

    def _add_span(self, span: Span) -> None:
        if self._ended:
            # The trace has already been built and queued, so appending now
            # would add the span to a list nobody reads again. It happens
            # legitimately -- a handler that returns a streamed completion
            # closes its trace long before the stream is drained -- so the span
            # goes out on its own, carrying the id of the trace it belongs to.
            self._client._report_span(span)
        elif len(self.spans) < MAX_SPANS_PER_TRACE:
            self.spans.append(span)
        else:
            # Past the contract's ceiling the whole trace would be refused, so
            # the overflow is streamed as its own row instead of lost.
            self._client._report_span(span)

    # -------------------------------------------------------------- lifetime

    def end(self, output: Any = None, *, error: Optional[BaseException] = None) -> "Trace":
        """Close the trace and hand it to the client. Idempotent."""
        if self._ended:
            return self
        self._ended = True
        self.end_time = _now()
        if output is not None:
            self.output = output
        if error is not None:
            self.error = error
        self._client._report_trace(self)
        return self

    def to_payload(self) -> Dict[str, Any]:
        """Build the ``TraceIn`` body, with its spans nested inside it."""
        body = self._payload_common()
        body["id"] = self.id
        thread_id = clamp_text(self.thread_id, MAX_THREAD_ID_LENGTH)
        if thread_id:
            body["thread_id"] = thread_id
        if self.spans:
            body["spans"] = [
                span.to_payload(include_trace_id=False) for span in self.spans[:MAX_SPANS_PER_TRACE]
            ]
        return body

    # -------------------------------------------------------- context manager

    def __enter__(self) -> "Trace":
        self._trace_token = _context.attach_trace(self)
        # A trace is not a span, so nothing inherits it as a parent; clearing
        # the span variable is what stops a *previous* sibling's span from
        # becoming this trace's root parent.
        self._span_token = _context.attach_span(None)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        _context.detach("span", self._span_token)
        _context.detach("trace", self._trace_token)
        self._span_token = None
        self._trace_token = None
        self.end(error=exc if isinstance(exc, BaseException) else None)
        return False

    async def __aenter__(self) -> "Trace":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Trace(name={0!r}, id={1!r}, spans={2})".format(self.name, self.id, len(self.spans))


#: What a decorated function's wrapper is handed, whichever kind it opened.
Recordable = Union[Trace, Span]
