"""``FulcrumOps`` — the client a customer's agent holds.

Everything the SDK does passes through here: tracing, feedback, prompts,
datasets, the bootstrap configuration, and the queue that carries all of it to
the control plane. The client is deliberately the only object with a network
connection, so there is exactly one place where "what happens when the control
plane is down" has to be answered.

The answer is always the same. Reporting is best-effort and never raises into
the caller; the calls a developer explicitly makes and waits for —
:meth:`config`, :meth:`get_prompt`, the dataset helpers — do raise, because
those are not telemetry and a silent failure there would be worse.
"""

from __future__ import annotations

import atexit
import logging
import random
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import context as _context
from ._version import SDK_NAME, SDK_VERSION
from .datasets import Datasets, Experiments, ExperimentResult
from .errors import FulcrumOpsError, to_fulcrum_error
from .limits import (
    MAX_ACTION_TAKEN_LENGTH,
    MAX_AGENT_LENGTH,
    MAX_EVENT_BODY_LENGTH,
    MAX_EVENT_SOURCE_LENGTH,
    MAX_GUARDRAIL_LENGTH,
    MAX_POLICY_LENGTH,
    MAX_RATING,
    MAX_REF_LENGTH,
    MAX_SAMPLE_LENGTH,
    MAX_SCORE_CATEGORY_LENGTH,
    MAX_SCORE_NAME_LENGTH,
    MAX_SCORE_REASON_LENGTH,
    MAX_SCORE_SOURCE_LENGTH,
    MAX_SEVERITY_LENGTH,
    MAX_SUBMITTED_BY_LENGTH,
    MAX_TARGET_ID_LENGTH,
    MIN_RATING,
    clamp_text,
)
from .options import Options, resolve_options
from .prompts import Prompt, PromptCache
from .queue import Flusher
from .redaction import CompiledRule, compile_rules, redact_field
from .serialize import to_json_safe
from .trace import Span, Trace
from .transport import Transport

__all__ = ["FulcrumOps", "configure", "get_client", "set_default_client", "shutdown"]

logger = logging.getLogger("fulcrum_ops")

#: Every live client, so the atexit hook can flush all of them without keeping
#: any of them alive past their last strong reference.
_live_clients: "weakref.WeakSet[FulcrumOps]" = weakref.WeakSet()
_default_client: Optional["FulcrumOps"] = None
_default_lock = threading.Lock()
_atexit_registered = False


class FulcrumOps:
    """Report agent telemetry and governance events to the Fulcrum Ops control plane.

    ::

        from fulcrum_ops import FulcrumOps

        fulcrum = FulcrumOps(agent="checkout-agent", environment="Production")

        with fulcrum.trace("support-question", input={"question": question}) as run:
            with fulcrum.span("retrieval", type="tool") as span:
                docs = search(question)
                span.set_output({"chunks": len(docs)})
            run.set_output(answer(docs))

        fulcrum.close()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        environment: Optional[str] = None,
        agent: Optional[str] = None,
        workspace: Optional[str] = None,
        **options: Any,
    ) -> None:
        # Test seams, kept out of the public option surface: a sleep function so
        # the retry backoff does not make a test suite slow, and a seeded RNG so
        # sampling decisions can be made deterministic.
        sleep = options.pop("sleep", time.sleep)
        rng = options.pop("rng", None)

        self._options: Options = resolve_options(
            api_key=api_key,
            base_url=base_url,
            environment=environment,
            agent=agent,
            workspace=workspace,
            **options,
        )

        if self._options.debug:
            logging.getLogger("fulcrum_ops").setLevel(logging.DEBUG)

        self._transport = Transport(self._options)
        self._flusher = Flusher(
            self._transport,
            self._options,
            on_error=self._handle_error,
            sleep=sleep if callable(sleep) else time.sleep,
            rng=rng,
        )
        self._prompts = PromptCache(self._transport)
        self.datasets = Datasets(self._transport)
        self.experiments = Experiments(self._transport, self)

        self._rules: List[CompiledRule] = compile_rules(self._options.redaction)
        self._config: Optional[Dict[str, Any]] = None
        self._config_etag: Optional[str] = None
        self._config_lock = threading.Lock()
        self._closed = False
        self._random = random.Random()

        _live_clients.add(self)
        _register_atexit()

        if self._options.set_as_default:
            set_default_client(self)

        if not self._options.enabled:
            logger.debug(
                "fulcrum-ops: reporting is disabled (no API key found in the argument, "
                "FULCRUM_OPS_API_KEY, or FULCRUM_OPS_DISABLED is set)."
            )
            return

        self._flusher.start()
        if self._options.bootstrap:
            # Fire-and-forget: start-up must not block on the control plane, and
            # a config document that arrives a moment late still applies,
            # because payloads are not built until each span closes.
            threading.Thread(
                target=self._bootstrap,
                name="fulcrum-ops-bootstrap",
                daemon=True,
            ).start()

    # ------------------------------------------------------------ properties

    @property
    def options(self) -> Options:
        """The resolved settings this client is running with."""
        return self._options

    @property
    def enabled(self) -> bool:
        """False when no API key was found, or reporting was switched off."""
        return self._options.enabled and not self._closed

    @property
    def environment(self) -> Optional[str]:
        return self._options.environment

    @property
    def agent(self) -> Optional[str]:
        return self._options.agent

    @property
    def capture_input(self) -> bool:
        return self._options.capture_input

    @property
    def capture_output(self) -> bool:
        return self._options.capture_output

    @property
    def stream_spans(self) -> bool:
        return self._options.stream_spans

    @property
    def prompts(self) -> PromptCache:
        """The Prompt Manager, cached."""
        return self._prompts

    # ---------------------------------------------------------------- tracing

    def trace(
        self,
        name: str,
        *,
        input: Any = None,  # noqa: A002 - matches the wire field name
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        agent: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Trace:
        """Open a trace: one end-to-end agent invocation.

        Use it as a context manager. The trace closes on exit, capturing an
        exception as the run's failure and re-raising it unchanged.
        """
        sampled = self._should_sample()
        return Trace(
            self,
            name,
            input=input,
            metadata=metadata,
            tags=tags,
            agent=agent or self._options.agent,
            thread_id=thread_id,
            sampled=sampled,
        )

    def span(
        self,
        name: str,
        *,
        type: str = "general",  # noqa: A002 - matches the wire field name
        input: Any = None,  # noqa: A002
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Sequence[str]] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Span:
        """Open a span inside the current trace.

        ::

            with client.span("retrieval", type="tool") as span:
                span.log("querying the index", filters=filters)
                span.set_output({"chunks": len(chunks)})
                span.score("recall", 0.82, reason="8 of 10 gold chunks returned")

        Called with no trace open, it starts one named after the span and closes
        it at the same time, so a single instrumented function is still a
        complete run rather than an orphan.
        """
        trace = _context.current_trace()
        owns_trace = trace is None
        if trace is None:
            trace = self.trace(name, agent=agent, metadata=metadata, tags=tags)
            trace.__enter__()

        span = trace.span(
            name,
            type=type,
            input=input,
            metadata=metadata,
            tags=tags,
            model=model,
            provider=provider,
        )
        if agent:
            span.agent = agent
        span._owns_trace = owns_trace
        return span

    def current_trace(self) -> Optional[Trace]:
        """The trace this thread or task is inside, if any."""
        return _context.current_trace()

    def current_span(self) -> Optional[Span]:
        """The innermost open span in this thread or task, if any."""
        return _context.current_span()

    def _should_sample(self) -> bool:
        rate = self._options.sampling_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return self._random.random() < rate

    # ------------------------------------------------------------- reporting

    def _report_trace(self, trace: Trace) -> None:
        """Queue a finished trace. Never raises."""
        if not self.enabled or not trace.sampled:
            return
        try:
            payload = trace.to_payload()
            spans = len(payload.get("spans") or [])
            self._flusher.submit("traces", payload, spans=spans)
        except Exception as exc:  # noqa: BLE001 - a broken payload must not break the agent
            self._handle_error(to_fulcrum_error(exc, "Failed to serialise a trace."), "trace")

    def _report_span(self, span: Span) -> None:
        """Queue a span on its own, for the streaming mode and for trace overflow."""
        if not self.enabled:
            return
        if span.trace is not None and not span.trace.sampled:
            return
        try:
            self._flusher.submit("spans", span.to_payload(include_trace_id=True), spans=1)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(to_fulcrum_error(exc, "Failed to serialise a span."), "span")

    # -------------------------------------------------------------- feedback

    def score(
        self,
        trace_id: str,
        name: str,
        value: float,
        *,
        reason: Optional[str] = None,
        category: Optional[str] = None,
        target: str = "trace",
        source: str = "sdk",
        agent: Optional[str] = None,
    ) -> bool:
        """Attach a feedback score to a trace, a span or a conversation thread.

        Scores usually arrive long after the run they describe — a thumbs-down
        two minutes later, a judge's verdict from an offline pass — which is why
        they are posted on their own rather than folded into the trace.
        """
        if not self.enabled:
            return False

        target_id = clamp_text(trace_id, MAX_TARGET_ID_LENGTH)
        score_name = clamp_text(name, MAX_SCORE_NAME_LENGTH)
        if not target_id or not score_name:
            logger.debug("fulcrum-ops: ignoring a score with no id or no name")
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            logger.debug("fulcrum-ops: ignoring a non-numeric score %r", value)
            return False

        payload: Dict[str, Any] = {
            "name": score_name,
            "value": numeric,
            "id": target_id,
            "target": target if target in ("trace", "span", "thread") else "trace",
            "source": clamp_text(source, MAX_SCORE_SOURCE_LENGTH) or "sdk",
        }
        reason_text = clamp_text(reason, MAX_SCORE_REASON_LENGTH)
        if reason_text:
            payload["reason"] = reason_text
        category_name = clamp_text(category, MAX_SCORE_CATEGORY_LENGTH)
        if category_name:
            payload["category_name"] = category_name
        resolved_agent = clamp_text(agent or self._options.agent, MAX_AGENT_LENGTH)
        if resolved_agent:
            payload["agent"] = resolved_agent

        return self._flusher.submit("scores", payload)

    # ---------------------------------------------------------------- events

    def log_feedback(
        self,
        *,
        rating: Optional[int] = None,
        body: Optional[str] = None,
        sentiment: Optional[str] = None,
        trace_id: Optional[str] = None,
        source: Optional[str] = None,
        submitted_by: Optional[str] = None,
        agent: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Report end-user feedback captured inside the customer's own product."""
        event: Dict[str, Any] = {"kind": "feedback.submitted"}
        if rating is not None:
            try:
                event["rating"] = max(MIN_RATING, min(MAX_RATING, int(rating)))
            except (TypeError, ValueError):
                pass
        for key, value, cap in (
            ("body", body, MAX_EVENT_BODY_LENGTH),
            ("sentiment", sentiment, 16),
            ("source", source, MAX_EVENT_SOURCE_LENGTH),
            ("submitted_by", submitted_by, MAX_SUBMITTED_BY_LENGTH),
        ):
            cleaned = clamp_text(value, cap)
            if cleaned:
                event[key] = cleaned
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref)

    # -- feedback conveniences ------------------------------------------
    #
    # `log_feedback` below is the general form and stays the only thing that
    # talks to the wire. These wrap it in the shapes products actually collect,
    # so a caller never has to remember that sentiment is a string, that a
    # rating is 1-5, or which of the two a thumb is.

    def thumbs_up(
        self, trace_id: Optional[str] = None, comment: Optional[str] = None, **kwargs: Any
    ) -> bool:
        """The single most common signal a product collects."""
        return self.log_feedback(
            rating=MAX_RATING, sentiment="positive", body=comment, trace_id=trace_id, **kwargs
        )

    def thumbs_down(
        self, trace_id: Optional[str] = None, comment: Optional[str] = None, **kwargs: Any
    ) -> bool:
        """The other one, and the one worth asking a reason for."""
        return self.log_feedback(
            rating=MIN_RATING, sentiment="negative", body=comment, trace_id=trace_id, **kwargs
        )

    def rate(
        self,
        stars: int,
        trace_id: Optional[str] = None,
        comment: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """A star rating. Sentiment is derived so the console can group it.

        The midpoint is neutral rather than silently positive: three out of
        five is not praise, and counting it as praise would flatter every
        quality report built on this signal.
        """
        try:
            value = max(MIN_RATING, min(MAX_RATING, int(stars)))
        except (TypeError, ValueError):
            value = MIN_RATING
        midpoint = (MIN_RATING + MAX_RATING) / 2
        sentiment = "positive" if value > midpoint else "negative" if value < midpoint else "neutral"
        return self.log_feedback(
            rating=value, sentiment=sentiment, body=comment, trace_id=trace_id, **kwargs
        )

    def report_issue(
        self,
        title: str,
        *,
        severity: str = "Medium",
        detail: Optional[str] = None,
        trace_id: Optional[str] = None,
        agent: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Raise a quality issue from inside the agent.

        Use this when the agent itself can tell something went wrong — a
        retrieval that returned nothing, a tool that answered nonsense — rather
        than waiting for a person to notice and complain.
        """
        event: Dict[str, Any] = {"kind": "issue.reported"}
        cleaned = clamp_text(title, MAX_EVENT_BODY_LENGTH)
        if not cleaned:
            return False
        event["title"] = cleaned
        allowed = ("Critical", "High", "Medium", "Low")
        chosen = str(severity or "Medium").strip().title()
        event["severity"] = chosen if chosen in allowed else "Medium"
        body = clamp_text(detail, MAX_EVENT_BODY_LENGTH)
        if body:
            event["body"] = body
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref)

    def log_guardrail_event(
        self,
        guardrail: str,
        *,
        action_taken: Optional[str] = None,
        score: Optional[float] = None,
        matched: Optional[Dict[str, Any]] = None,
        sample: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        agent: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Report a guardrail the SDK enforced locally, in the customer's process."""
        event: Dict[str, Any] = {"kind": "guardrail.triggered"}
        name = clamp_text(guardrail, MAX_GUARDRAIL_LENGTH)
        if not name:
            return False
        event["guardrail"] = name
        action = clamp_text(action_taken, MAX_ACTION_TAKEN_LENGTH)
        if action:
            event["action_taken"] = action
        if score is not None:
            try:
                event["score"] = float(score)
            except (TypeError, ValueError):
                pass
        if matched:
            event["matched"] = to_json_safe(matched)
        # The sample is a slice of the content that tripped the rule, so it goes
        # through redaction like any other captured content would.
        sample_text = clamp_text(sample, MAX_SAMPLE_LENGTH)
        if sample_text:
            event["sample"] = self._redact(sample_text, "input")
        if span_id:
            event["span_id"] = clamp_text(span_id, MAX_TARGET_ID_LENGTH)
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref)

    def log_policy_violation(
        self,
        policy: str,
        *,
        severity: Optional[str] = None,
        action_taken: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        agent: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Report a policy the SDK enforced locally."""
        name = clamp_text(policy, MAX_POLICY_LENGTH)
        if not name:
            return False
        event: Dict[str, Any] = {"kind": "policy.violation", "policy": name}
        for key, value, cap in (
            ("severity", severity, MAX_SEVERITY_LENGTH),
            ("action_taken", action_taken, MAX_ACTION_TAKEN_LENGTH),
        ):
            cleaned = clamp_text(value, cap)
            if cleaned:
                event[key] = cleaned
        if detail:
            event["detail"] = self._redact(to_json_safe(detail), "metadata")
        if span_id:
            event["span_id"] = clamp_text(span_id, MAX_TARGET_ID_LENGTH)
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref)

    def _submit_event(
        self,
        event: Dict[str, Any],
        *,
        trace_id: Optional[str],
        agent: Optional[str],
        ref: Optional[str],
    ) -> bool:
        if not self.enabled:
            return False
        target = clamp_text(trace_id, MAX_TARGET_ID_LENGTH)
        if target:
            event["trace_id"] = target
        elif _context.current_trace() is not None:
            event["trace_id"] = _context.current_trace().id  # type: ignore[union-attr]
        resolved_agent = clamp_text(agent or self._options.agent, MAX_AGENT_LENGTH)
        if resolved_agent:
            event["agent"] = resolved_agent
        reference = clamp_text(ref, MAX_REF_LENGTH)
        if reference:
            event["ref"] = reference
        return self._flusher.submit("events", event)

    # --------------------------------------------------------------- prompts

    def get_prompt(
        self,
        name: str,
        *,
        commit: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        refresh: bool = False,
    ) -> Prompt:
        """Fetch a prompt from the Prompt Manager, cached locally.

        Raises :class:`NotFoundError` when nothing matches. This one *does*
        raise: a missing system prompt is a broken agent, not a degraded one.
        """
        return self._prompts.get(name, commit=commit, ttl_seconds=ttl_seconds, refresh=refresh)

    # ---------------------------------------------------------- evaluation

    def evaluate(
        self,
        dataset: str,
        task: Callable[..., Any],
        *,
        scorers: Optional[Sequence[Callable[..., Any]]] = None,
        name: Optional[str] = None,
        items: Optional[Sequence[Any]] = None,
        agent: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> ExperimentResult:
        """Run ``task`` over a dataset locally, tracing and scoring every case."""
        return self.experiments.evaluate(
            dataset, task, scorers=scorers, name=name, items=items, agent=agent, tags=tags
        )

    # ---------------------------------------------------------------- config

    def config(self, *, refresh: bool = False, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Read ``GET /ingest/config`` and adopt it.

        Sampling, batching, the flush interval, the queue ceiling and the
        redaction rules all come from this document. Where it and the local
        options disagree the *stricter* value wins, because the document
        expresses a limit the deployment enforces rather than a preference the
        developer expressed — a workspace capped at 25% sampling is not raised
        to 100% by a constructor argument.

        Revalidated with an ETag, so a fleet restart costs one conditional
        request per process rather than one full read.

        A client with no API key returns an empty document instead of calling
        out. Reporting is already off for such a client, so a request here could
        only ever produce a 401 — and it would be a request the developer never
        asked for, made from a process they thought had telemetry disabled.
        """
        if not self._options.enabled or self._closed:
            return {}

        with self._config_lock:
            cached = self._config
            etag = self._config_etag
        if cached is not None and not refresh:
            return cached

        headers = {"if-none-match": etag} if etag else None
        response = self._transport.request(
            "GET", "ingest/config", headers=headers, timeout=timeout
        )
        if response.status == 304 and cached is not None:
            return cached
        if not response.ok:
            from .errors import error_from_response

            raise error_from_response(response.status, response.body, response.headers)

        document = response.body if isinstance(response.body, dict) else {}
        self._apply_config(document, response.headers.get("etag"))
        # Return the cached copy rather than the freshly parsed body, so every
        # call — the first one included — hands back the same object. Returning
        # one on the first call and the other afterwards would make "is the
        # document cached?" answerable only by watching the network.
        with self._config_lock:
            return self._config if self._config is not None else document

    def _bootstrap(self) -> None:
        """Fetch the config document at start-up, absorbing every failure."""
        try:
            self.config()
        except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
            self._handle_error(
                to_fulcrum_error(exc, "Could not read the SDK configuration."), "config"
            )

    def _apply_config(self, document: Mapping[str, Any], etag: Optional[str]) -> None:
        """Narrow the local options to whatever the deployment enforces."""
        options = self._options

        def stricter_number(key: str, current: float, minimum: float = 0.0) -> float:
            raw = document.get(key)
            if raw is None:
                return current
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return current
            if value < minimum:
                return current
            return min(current, value)

        options.sampling_rate = stricter_number("sampling_rate", options.sampling_rate)
        options.batch_max_spans = int(
            stricter_number("batch_max_spans", options.batch_max_spans, 1)
        )
        options.batch_max_bytes = int(
            stricter_number("batch_max_bytes", options.batch_max_bytes, 1_024)
        )
        options.flush_interval_seconds = stricter_number(
            "flush_interval_seconds", options.flush_interval_seconds, 0.05
        )
        options.max_queue_size = int(stricter_number("max_queue_size", options.max_queue_size, 1))
        options.retry_max_attempts = int(
            stricter_number("retry_max_attempts", options.retry_max_attempts, 0)
        )
        options.retry_backoff_seconds = max(
            0.001, float(document.get("retry_backoff_seconds") or options.retry_backoff_seconds)
        )

        # Capture is a conjunction: either side may switch content off, neither
        # may switch it back on.
        if document.get("capture_input") is False:
            options.capture_input = False
        if document.get("capture_output") is False:
            options.capture_output = False

        if not options.agent and document.get("agent_name"):
            options.agent = str(document["agent_name"])
        if not options.environment and document.get("environment"):
            options.environment = str(document["environment"])

        rules = compile_rules(list(document.get("redaction") or []) + list(options.redaction))
        with self._config_lock:
            self._rules = rules
            self._config = dict(document)
            self._config_etag = etag

        unsupported = sorted(
            {
                entity
                for rule in rules
                for entity in rule.unsupported_entity_types
            }
        )
        if unsupported:
            logger.warning(
                "fulcrum-ops: this SDK version cannot match the redaction entity types %s; "
                "content matching them is sent unredacted and masked server-side instead.",
                ", ".join(unsupported),
            )
        logger.debug(
            "fulcrum-ops: configuration applied (workspace=%s, sampling=%.3f, rules=%d)",
            document.get("workspace"),
            options.sampling_rate,
            len(rules),
        )

    def _redact(self, value: Any, field: str) -> Any:
        """Apply the redaction rules that cover one field. Never raises."""
        with self._config_lock:
            rules = self._rules
        if not rules:
            return value
        try:
            return redact_field(value, field, rules)
        except Exception:  # noqa: BLE001 - redaction failing open would be worse than a lost span
            logger.warning("fulcrum-ops: redaction failed; dropping the field rather than sending it")
            return None

    # -------------------------------------------------------------- lifetime

    def flush(self, timeout: Optional[float] = 10.0) -> bool:
        """Send everything queued and wait for it. Returns False on timeout.

        Call it before a short-lived process exits — a Lambda handler, a CLI, a
        test — where the flush interval may never come round.
        """
        if not self._options.enabled:
            return True
        try:
            return self._flusher.flush(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(to_fulcrum_error(exc, "Flush failed."), "flush")
            return False

    def close(self, timeout: Optional[float] = 5.0) -> None:
        """Flush, stop the worker thread and release the connection pool.

        Idempotent, and safe to call from an ``atexit`` hook or a signal
        handler. Never raises.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._flusher.close(timeout=timeout)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("fulcrum-ops: the flusher did not stop cleanly", exc_info=True)
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001
            logger.debug("fulcrum-ops: the transport did not close cleanly", exc_info=True)

        global _default_client
        with _default_lock:
            if _default_client is self:
                _default_client = None

    def stats(self) -> Dict[str, Any]:
        """Counters for what this client has queued, sent, retried and dropped.

        Worth exporting to the customer's own metrics: ``dropped_overflow``
        rising means the queue ceiling is too low for the traffic, and
        ``rejected`` rising means the control plane is refusing rows for a
        reason worth reading.
        """
        snapshot = self._flusher.snapshot()
        snapshot.update(
            {
                "enabled": self.enabled,
                "sdk": SDK_NAME,
                "sdk_version": SDK_VERSION,
                "sampling_rate": self._options.sampling_rate,
                "redaction_rules": len(self._rules),
                "base_url": self._options.base_url,
            }
        )
        return snapshot

    def _handle_error(self, error: FulcrumOpsError, operation: str) -> None:
        """Route an absorbed failure to the caller's handler, then to the log."""
        handler = self._options.on_error
        if handler is not None:
            try:
                handler(error, operation)
                return
            except Exception:  # noqa: BLE001 - a broken handler is not the error
                logger.debug("fulcrum-ops: on_error handler raised", exc_info=True)
        logger.warning("fulcrum-ops: %s — %s", operation, error)

    # -------------------------------------------------------- context manager

    def __enter__(self) -> "FulcrumOps":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "FulcrumOps(base_url={0!r}, agent={1!r}, enabled={2})".format(
            self._options.base_url, self._options.agent, self.enabled
        )


# ------------------------------------------------------------- module surface


def configure(api_key: Optional[str] = None, **options: Any) -> FulcrumOps:
    """Build the default client, the one the bare ``@trace`` decorator uses.

    ::

        import fulcrum_ops

        fulcrum_ops.configure(agent="checkout-agent", environment="Production")

        @fulcrum_ops.trace
        def answer(question: str) -> str:
            ...

    Calling it twice replaces the default client and closes the previous one, so
    a re-configure in a notebook does not leave a worker thread behind.
    """
    options.setdefault("set_as_default", True)
    client = FulcrumOps(api_key, **options)
    return client


def get_client(create: bool = True) -> Optional[FulcrumOps]:
    """The default client, constructing one from the environment if needed."""
    global _default_client
    with _default_lock:
        existing = _default_client
    if existing is not None and not existing._closed:
        return existing
    if not create:
        return None
    return FulcrumOps()


def set_default_client(client: Optional[FulcrumOps]) -> None:
    """Make ``client`` the one the module-level decorators use."""
    global _default_client
    with _default_lock:
        previous = _default_client
        _default_client = client
    if previous is not None and previous is not client and not previous._closed:
        previous.close(timeout=1.0)


def shutdown(timeout: Optional[float] = 5.0) -> None:
    """Flush and close every live client. Called automatically at interpreter exit.

    A client constructed with ``flush_on_exit=False`` is closed without the
    flush, which is what a caller wants when they have already decided that a
    fast exit matters more than the last few spans.
    """
    for client in list(_live_clients):
        try:
            client.close(timeout=timeout if client.options.flush_on_exit else 0.0)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    # Bounded on purpose. A process on its way out must not hang waiting for a
    # control plane that is not answering; two seconds is enough to drain a
    # healthy queue and short enough that nobody notices when it is not.
    atexit.register(lambda: shutdown(timeout=2.0))
