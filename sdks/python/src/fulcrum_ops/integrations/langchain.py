"""LangChain callback handler.

LangChain reports its own execution as a stream of paired start/end callbacks,
each carrying a ``run_id`` and the ``parent_run_id`` it hangs beneath. That is
already a tree, so this handler does not use :mod:`contextvars` at all: it keeps
its own map from ``run_id`` to the span it opened and resolves a parent by
looking ``parent_run_id`` up in that map.

Relying on the ambient context here would be wrong. LangChain runs callbacks
from its own executor — ``on_llm_end`` for a batched call can fire on a
different thread from the ``on_llm_start`` that paired with it — and by then the
context that started the call is long gone. The explicit map is the only thing
that survives that.

``langchain_core`` is never imported at module load. LangChain dispatches
callbacks with ``getattr(handler, event_name)``, so a plain object carrying
these method names is a valid ``callbacks`` entry, and the common case costs
nothing. :func:`create_langchain_handler` additionally splices the real
``BaseCallbackHandler`` in beneath the class for the code paths that
``isinstance``-check it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "FulcrumOpsCallbackHandler",
    "create_langchain_handler",
    "LangChainCallbackHandler",
]

logger = logging.getLogger("fulcrum_ops")

#: Documents, prompts and messages are lists; only the head is worth recording.
MAX_RECORDED_ITEMS = 20


def _component_name(serialized: Any, fallback: str) -> str:
    """The readable name of whatever LangChain is about to run."""
    if isinstance(serialized, dict):
        name = serialized.get("name")
        if isinstance(name, str) and name:
            return name
        kwargs = serialized.get("kwargs")
        if isinstance(kwargs, dict):
            for key in ("name", "_type"):
                value = kwargs.get(key)
                if isinstance(value, str) and value:
                    return value
        identifier = serialized.get("id")
        if isinstance(identifier, (list, tuple)) and identifier:
            return str(identifier[-1])
    return fallback


def _model_name(serialized: Any, kwargs: Dict[str, Any]) -> Optional[str]:
    """The model a run used, from wherever this LangChain version put it."""
    params = kwargs.get("invocation_params")
    if isinstance(params, dict):
        for key in ("model", "model_name", "model_id", "deployment_name"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(serialized, dict):
        inner = serialized.get("kwargs")
        if isinstance(inner, dict):
            for key in ("model", "model_name", "model_id"):
                value = inner.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _provider_name(kwargs: Dict[str, Any]) -> Optional[str]:
    params = kwargs.get("invocation_params")
    if isinstance(params, dict):
        provider = params.get("_type") or params.get("ls_provider")
        if isinstance(provider, str) and provider:
            return provider
    return None


def _snake(key: str) -> str:
    """LangChain reports ``promptTokens`` in places; the contract wants ``prompt_tokens``."""
    out: List[str] = []
    for index, char in enumerate(key):
        if char.isupper() and index > 0:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _usage_from(response: Any) -> Optional[Dict[str, int]]:
    """Token counters, from any of the four places LangChain has kept them.

    ``llm_output["token_usage"]`` is the old OpenAI shape,
    ``usage_metadata`` on the generated message is the current one, and both are
    still produced depending on the integration package's version. Whichever is
    found first is normalised onto the contract's names.
    """
    candidates: List[Any] = []

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        candidates.extend(
            [llm_output.get("token_usage"), llm_output.get("usage"), llm_output.get("usage_metadata")]
        )

    generations = getattr(response, "generations", None)
    if isinstance(generations, (list, tuple)):
        for batch in generations:
            if not isinstance(batch, (list, tuple)):
                continue
            for generation in batch:
                message = getattr(generation, "message", None)
                if message is not None:
                    candidates.append(getattr(message, "usage_metadata", None))
                    response_metadata = getattr(message, "response_metadata", None)
                    if isinstance(response_metadata, dict):
                        candidates.append(response_metadata.get("token_usage"))

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        counters: Dict[str, int] = {}
        for key, value in candidate.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            name = _snake(str(key))
            # ``usage_metadata`` calls them input/output; normalise onto the
            # names the console's token chart reads.
            if name == "input_tokens":
                name = "prompt_tokens"
            elif name == "output_tokens":
                name = "completion_tokens"
            counters[name] = int(value)
        if counters:
            if "total_tokens" not in counters:
                counters["total_tokens"] = counters.get("prompt_tokens", 0) + counters.get(
                    "completion_tokens", 0
                )
            return counters
    return None


def _generation_text(response: Any) -> Optional[str]:
    """The text a model run produced, for the span's output."""
    generations = getattr(response, "generations", None)
    if not isinstance(generations, (list, tuple)):
        return None
    parts: List[str] = []
    for batch in generations:
        if not isinstance(batch, (list, tuple)):
            continue
        for generation in batch:
            text = getattr(generation, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            message = getattr(generation, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if isinstance(content, str) and content:
                parts.append(content)
    return "\n".join(parts) if parts else None


def _summarise(value: Any) -> Any:
    """Trim a list argument down to something worth storing."""
    if isinstance(value, (list, tuple)) and len(value) > MAX_RECORDED_ITEMS:
        return {
            "items": list(value[:MAX_RECORDED_ITEMS]),
            "count": len(value),
            "truncated": len(value) - MAX_RECORDED_ITEMS,
        }
    return value


class FulcrumOpsCallbackHandler:
    """Report a LangChain run to Fulcrum Ops as a trace with nested spans.

    ::

        from fulcrum_ops.integrations import FulcrumOpsCallbackHandler

        handler = FulcrumOpsCallbackHandler(agent="research-agent")
        chain.invoke(question, config={"callbacks": [handler]})

    The outermost run becomes the trace; every run beneath it becomes a span of
    the appropriate type — ``llm`` for models, ``tool`` for tools and
    retrievers, ``general`` for chains.

    :param fulcrum: Report to this client rather than the default one.
    :param agent: Attribute the run to this agent.
    :param trace_name: Name for the trace. Defaults to the root component's name.
    :param tags: Tags copied onto the trace and every span in it.
    """

    #: LangChain identifies handlers by this attribute in its logs.
    name = "fulcrum_ops_callback_handler"

    #: LangChain reads all of these off a handler before dispatching. Answering
    #: them here is what lets a plain object serve as a callback handler.
    raise_error = False
    run_inline = False
    ignore_llm = False
    ignore_chain = False
    ignore_agent = False
    ignore_retriever = False
    ignore_chat_model = False
    ignore_retry = False
    ignore_custom_event = False

    def __init__(
        self,
        fulcrum: Optional[Any] = None,
        *,
        agent: Optional[str] = None,
        trace_name: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> None:
        if fulcrum is None:
            from ..client import get_client

            fulcrum = get_client()
        self._client = fulcrum
        self._agent = agent
        self._trace_name = trace_name
        self._tags = list(tags or [])
        self._capture_input = capture_input
        self._capture_output = capture_output

        # LangChain can dispatch these callbacks from more than one thread, so
        # the run map is guarded. Contention is nil — the critical sections are
        # a dict lookup — and losing a parent link to a race would silently
        # flatten the tree.
        self._lock = threading.Lock()
        self._runs: Dict[str, Tuple[Any, Any]] = {}

    # ------------------------------------------------------------- internals

    def _open(
        self,
        run_id: Any,
        parent_run_id: Any,
        name: str,
        span_type: str,
        payload: Any,
        tags: Optional[Sequence[str]] = None,
    ) -> Optional[Any]:
        """Open a span for a run, rooting a trace when the run has no parent."""
        if self._client is None:
            return None
        key = str(run_id)
        parent_key = str(parent_run_id) if parent_run_id is not None else None
        all_tags = self._tags + [str(tag) for tag in (tags or [])]

        try:
            with self._lock:
                parent = self._runs.get(parent_key) if parent_key else None

            captured = _summarise(payload) if self._capture_input else None

            if parent is not None:
                parent_span, _ = parent
                span = parent_span.trace.span(
                    name,
                    type=span_type,
                    input=captured,
                    tags=all_tags or None,
                    parent=parent_span,
                )
                with self._lock:
                    self._runs[key] = (span, None)
                return span

            # No parent: this run is the whole invocation as far as the console
            # is concerned, so it becomes the trace.
            trace = self._client.trace(
                self._trace_name or name,
                input=captured,
                agent=self._agent,
                tags=all_tags or None,
            )
            span = trace.span(name, type=span_type, input=captured, tags=all_tags or None)
            with self._lock:
                self._runs[key] = (span, trace)
            return span
        except Exception:  # noqa: BLE001 - a callback that raises looks like a chain failure
            logger.debug("fulcrum-ops: could not open a span for %s", name, exc_info=True)
            return None

    def _close(self, run_id: Any, output: Any, error: Optional[BaseException] = None) -> None:
        """Close a run's span, and its trace when the run was the root."""
        key = str(run_id)
        try:
            with self._lock:
                entry = self._runs.pop(key, None)
            if entry is None:
                return
            span, trace = entry
            payload = _summarise(output) if self._capture_output else None
            span.end(payload, error=error if isinstance(error, BaseException) else None)
            if trace is not None:
                trace.end(payload, error=error if isinstance(error, BaseException) else None)
        except Exception:  # noqa: BLE001 - never raise into LangChain's dispatcher
            logger.debug("fulcrum-ops: could not close the span for run %s", key, exc_info=True)

    def _span_for(self, run_id: Any) -> Optional[Any]:
        with self._lock:
            entry = self._runs.get(str(run_id))
        return entry[0] if entry else None

    # ---------------------------------------------------------------- chains

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        self._open(run_id, parent_run_id, _component_name(serialized, "chain"), "general", inputs, tags)

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, outputs)

    def on_chain_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, None, error)

    # ---------------------------------------------------------------- models

    def on_llm_start(
        self,
        serialized: Any,
        prompts: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        span = self._open(
            run_id,
            parent_run_id,
            _component_name(serialized, "llm"),
            "llm",
            {"prompts": prompts},
            tags,
        )
        self._stamp_model(span, serialized, kwargs)

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        span = self._open(
            run_id,
            parent_run_id,
            _component_name(serialized, "chat_model"),
            "llm",
            {"messages": messages},
            tags,
        )
        self._stamp_model(span, serialized, kwargs)

    def _stamp_model(self, span: Optional[Any], serialized: Any, kwargs: Dict[str, Any]) -> None:
        if span is None:
            return
        try:
            model = _model_name(serialized, kwargs)
            if model:
                span.set_model(model, _provider_name(kwargs))
        except Exception:  # noqa: BLE001
            logger.debug("fulcrum-ops: could not record the model name", exc_info=True)

    def on_llm_new_token(self, token: str, *, run_id: Any = None, **kwargs: Any) -> None:
        """Count streamed tokens without storing them.

        Storing every token would mean a span carrying the whole response twice
        over; the count is what makes a streaming span legible, and the text
        arrives intact on ``on_llm_end``.
        """
        span = self._span_for(run_id)
        if span is None:
            return
        try:
            streamed = int(span.metadata.get("streamed_tokens") or 0)
            span.set_metadata(streamed_tokens=streamed + 1)
        except Exception:  # noqa: BLE001
            pass

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is not None:
            try:
                usage = _usage_from(response)
                if usage:
                    span.set_usage(usage)
                model = getattr(response, "llm_output", None)
                if isinstance(model, dict) and isinstance(model.get("model_name"), str):
                    span.set_model(model["model_name"], span.provider)
            except Exception:  # noqa: BLE001
                logger.debug("fulcrum-ops: could not record token usage", exc_info=True)
        text = _generation_text(response)
        self._close(run_id, {"text": text} if text is not None else response)

    def on_llm_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, None, error)

    # ----------------------------------------------------------------- tools

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        self._open(
            run_id, parent_run_id, _component_name(serialized, "tool"), "tool", {"input": input_str}, tags
        )

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, {"output": output})

    def on_tool_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, None, error)

    # ------------------------------------------------------------ retrievers

    def on_retriever_start(
        self,
        serialized: Any,
        query: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        self._open(
            run_id,
            parent_run_id,
            _component_name(serialized, "retriever"),
            "tool",
            {"query": query},
            tags,
        )

    def on_retriever_end(self, documents: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is not None and isinstance(documents, (list, tuple)):
            try:
                span.set_metadata(document_count=len(documents))
            except Exception:  # noqa: BLE001
                pass
        self._close(run_id, {"documents": _summarise(documents)})

    def on_retriever_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id, None, error)

    # ---------------------------------------------------------------- agents

    def on_agent_action(self, action: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is None:
            return
        try:
            tool = getattr(action, "tool", None)
            span.log("agent action", tool=tool if isinstance(tool, str) else "action")
        except Exception:  # noqa: BLE001
            pass

    def on_agent_finish(self, finish: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is None:
            return
        try:
            span.log("agent finished", log=getattr(finish, "log", None))
        except Exception:  # noqa: BLE001
            pass

    def on_text(self, text: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is not None:
            try:
                span.log(str(text)[:500])
            except Exception:  # noqa: BLE001
                pass

    def on_retry(self, retry_state: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        span = self._span_for(run_id)
        if span is not None:
            try:
                span.log("retry", attempt=getattr(retry_state, "attempt_number", None))
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- lifetime

    def flush_open_runs(self) -> None:
        """Close anything still open, after a run was abandoned mid-flight.

        A chain that is cancelled fires no terminal callback, so without this
        its spans would sit in the map until the process ended and never reach
        the console at all.
        """
        with self._lock:
            keys = list(self._runs)
        for key in keys:
            self._close(key, None)

    @property
    def open_runs(self) -> int:
        """How many runs are still waiting for their end callback."""
        with self._lock:
            return len(self._runs)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "FulcrumOpsCallbackHandler(open_runs={0})".format(self.open_runs)


#: Alias, for codebases that prefer the provider-prefixed name.
LangChainCallbackHandler = FulcrumOpsCallbackHandler

_subclass_lock = threading.Lock()
_subclass: Optional[type] = None


def create_langchain_handler(
    fulcrum: Optional[Any] = None, **options: Any
) -> FulcrumOpsCallbackHandler:
    """Build a handler that also passes an ``isinstance(BaseCallbackHandler)`` check.

    Some LangChain code paths type-check the base class rather than duck-typing
    the methods. This imports ``langchain_core`` lazily and returns an instance
    of a subclass when it is installed, and the plain handler above when it is
    not — which every documented ``callbacks`` list accepts either way.
    """
    global _subclass
    with _subclass_lock:
        if _subclass is None:
            try:
                from langchain_core.callbacks import BaseCallbackHandler  # type: ignore

                _subclass = type(
                    "FulcrumOpsCallbackHandler",
                    (FulcrumOpsCallbackHandler, BaseCallbackHandler),
                    {},
                )
            except Exception:  # noqa: BLE001 - not installed, or a version that moved it
                logger.debug(
                    "fulcrum-ops: langchain_core is not importable; using the plain handler",
                    exc_info=True,
                )
                _subclass = FulcrumOpsCallbackHandler
        handler_class = _subclass

    return handler_class(fulcrum, **options)  # type: ignore[return-value]
