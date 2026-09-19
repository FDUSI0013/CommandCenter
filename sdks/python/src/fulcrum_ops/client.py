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
import dataclasses
import json
import logging
import os
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
    MAX_ISSUE_TITLE_LENGTH,
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

#: The feedback source an agent-raised issue is filed under. The control plane
#: reads ``source`` through a closed list and files anything else as end-user
#: feedback, which an agent judging its own answer is not; of the sources it
#: knows, this is the one that says the verdict is about an agent response.
ISSUE_SOURCE = "Agent Response Rating"

#: How long the configuration document is trusted when it does not say. The
#: control plane sends ``refresh_after_seconds``; the floor keeps a document that
#: says ``0`` from turning every flush interval into a request.
CONFIG_DEFAULT_REFRESH_SECONDS = 300.0
CONFIG_MIN_REFRESH_SECONDS = 30.0
#: How long to wait before asking again after a read that failed, by attempt.
CONFIG_RETRY_DELAYS = (5.0, 30.0, 60.0, 120.0, 300.0)
#: How long the first send waits for the start-up read, so that a run which ends
#: in the process's first moments is not posted before the workspace's redaction
#: rules have had a chance to arrive. Waited on the worker thread, never on the
#: caller's, and bounded: a control plane that is down must not hold telemetry
#: back for ever on the strength of rules nobody can fetch.
BOOTSTRAP_GRACE_SECONDS = 5.0

#: Every live client, so the atexit hook can flush all of them without keeping
#: any of them alive past their last strong reference.
_live_clients: "weakref.WeakSet[FulcrumOps]" = weakref.WeakSet()
_default_client: Optional["FulcrumOps"] = None
_default_lock = threading.Lock()
_atexit_registered = False
#: Whether this process has already been told that it has no API key.
_warned_no_key = False


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

        # What the constructor asked for, kept apart from what is in force. The
        # configuration document narrows the live options, and it is read again
        # every few minutes; narrowing from the already-narrowed values would
        # make every limit a ratchet that an operator could tighten from the
        # console and never loosen again without redeploying the fleet.
        self._baseline: Options = dataclasses.replace(self._options)

        self._transport = Transport(self._options)
        self._flusher = Flusher(
            self._transport,
            self._options,
            on_error=self._handle_error,
            sleep=sleep if callable(sleep) else time.sleep,
            rng=rng,
            on_tick=self._refresh_config_if_due,
            prepare=self._prepare_row,
        )
        self._prompts = PromptCache(self._transport)
        self.datasets = Datasets(self._transport)
        self.experiments = Experiments(self._transport, self)

        self._rules: List[CompiledRule] = compile_rules(self._options.redaction)
        self._config: Optional[Dict[str, Any]] = None
        self._config_etag: Optional[str] = None
        self._config_lock = threading.Lock()
        # Bumped whenever the redaction rules change. A queued row remembers the
        # revision it was built under, so one that was built before the rules in
        # force arrived is put through them on its way out.
        self._rules_revision = 0
        self._rules_key = "[]"
        self._config_due = 0.0  # monotonic; 0 means nothing is scheduled yet
        self._config_failures = 0
        self._bootstrapped = threading.Event()
        self._grace_spent = False
        self._closed = False
        self._random = random.Random()

        _live_clients.add(self)
        _register_atexit()

        if self._options.set_as_default:
            set_default_client(self)

        if not self._options.enabled:
            self._explain_disabled()
            return

        if self._options.base_url_defaulted:
            # Said once, here, because nothing later can say it: every request
            # from now on fails as a plain connection error (or, worse, succeeds
            # against some other service that happens to own the port), and
            # neither outcome mentions that the address was never chosen.
            logger.warning(
                "fulcrum-ops: an API key is set but no control plane address is. Telemetry, "
                "and the key with it, will be sent to the built-in default %s. Set "
                "FULCRUM_OPS_BASE_URL (or pass base_url=) to your control plane, e.g. "
                "https://controlplane.example.com.",
                self._options.base_url,
            )

        self._flusher.start()
        if self._options.bootstrap:
            # Fire-and-forget: start-up must not block on the control plane. A
            # document that arrives a moment late still applies — to payloads
            # not built yet, and to rows already queued, which are put through
            # the new rules before they are sent.
            threading.Thread(
                target=self._bootstrap,
                name="fulcrum-ops-bootstrap",
                daemon=True,
            ).start()
        else:
            self._bootstrapped.set()

    def _explain_disabled(self) -> None:
        """Say why nothing will be reported — loudly, when it looks like a mistake.

        Switched off on purpose is not news. No key at all usually is: the
        variable is mistyped or was never exported, the agent runs perfectly,
        ``flush()`` answers ``True``, and the first anybody hears of it is an
        empty console a week later. Once per process, not per client, so a
        codebase that builds clients freely does not repeat itself.
        """
        global _warned_no_key
        if self._options.disabled_on_purpose or self._options.api_key or _warned_no_key:
            logger.debug("fulcrum-ops: reporting is disabled for this client.")
            return
        _warned_no_key = True
        logger.warning(
            "fulcrum-ops: no API key found (api_key= or FULCRUM_OPS_API_KEY), so nothing will "
            "be reported. The agent is unaffected. Set FULCRUM_OPS_DISABLED=1 to switch "
            "reporting off on purpose and silence this message."
        )

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
        id: Optional[str] = None,  # noqa: A002 - matches the wire field name
        sampled: Optional[bool] = None,
    ) -> Trace:
        """Open a trace: one end-to-end agent invocation.

        Use it as a context manager. The trace closes on exit, capturing an
        exception as the run's failure and re-raising it unchanged.

        ``id`` reports the run under an id somebody else issued — the ``run_id``
        the console answers a manual run with, which stays ``Running`` there
        until a runtime reports under it::

            with client.trace("handle", id=job.run_id, thread_id=job.session_id, sampled=True):
                ...

        It has to be a version 7 UUID, which is what the console and
        :func:`fulcrum_ops.new_id` mint; anything else is replaced, with a
        warning, so read the id in use back from ``.id``. Sampling still applies
        to a run that was handed its id, and a run the console is waiting on is
        not one to leave to a dice roll: ``sampled=True`` reports it regardless
        of the rate, and ``sampled=False`` drops it.
        """
        return Trace(
            self,
            name,
            input=input,
            metadata=metadata,
            tags=tags,
            agent=agent or self._options.agent,
            thread_id=thread_id,
            sampled=self._should_sample() if sampled is None else bool(sampled),
            id=id,
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
        thread_id: Optional[str] = None,
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

        That implicit trace is built here but not made current here: the span
        takes it into the context when it is entered (``with client.span(...)``)
        and out again when it exits. A span that is only ever ``end()``-ed —
        which is how the provider wrappers use this — never touches the context
        at all, so it cannot leave anything behind in it.

        ``thread_id`` names the conversation for that implicit run, which has no
        other place to be told; a run that is already open and has no thread
        takes it too, and one that has a thread keeps its own.
        """
        trace = _context.current_trace()
        owns_trace = trace is None
        if trace is None:
            trace = self.trace(
                name, agent=agent, metadata=metadata, tags=tags, thread_id=thread_id
            )
        elif thread_id and not trace.thread_id:
            trace.set_thread_id(thread_id)

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
            revision = self._rules_revision  # read first: a change mid-build is redone
            payload = trace.to_payload()
            spans = len(payload.get("spans") or [])
            self._flusher.submit("traces", payload, spans=spans, stamp=revision)
        except Exception as exc:  # noqa: BLE001 - a broken payload must not break the agent
            self._handle_error(to_fulcrum_error(exc, "Failed to serialise a trace."), "trace")

    def _report_span(self, span: Span) -> None:
        """Queue a span on its own, for the streaming mode and for trace overflow."""
        if not self.enabled:
            return
        if span.trace is not None and not span.trace.sampled:
            return
        try:
            revision = self._rules_revision
            self._flusher.submit(
                "spans", span.to_payload(include_trace_id=True), spans=1, stamp=revision
            )
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

        It travels as negative feedback. The ingest contract is a closed set —
        guardrail, policy and feedback events — and refuses the whole row for
        any other kind or any field it does not know, so an ``issue.reported``
        event with a ``title`` was queued, answered ``True`` here, and then
        thrown away by the control plane at flush. An agent complaining about
        its own answer *is* feedback, so that is the kind it goes out as, and
        the Feedback inbox is where it lands. The title leads the body, because
        the body is what that screen lists; the structured copy rides in
        ``detail``, which the control plane stores verbatim, so nobody has to
        parse the severity back out of prose.
        """
        cleaned = clamp_text(title, MAX_ISSUE_TITLE_LENGTH)
        if not cleaned:
            return False
        allowed = ("Critical", "High", "Medium", "Low")
        chosen = str(severity or "Medium").strip().title()
        chosen = chosen if chosen in allowed else "Medium"
        explanation = clamp_text(detail, MAX_EVENT_BODY_LENGTH)
        event: Dict[str, Any] = {
            "kind": "feedback.submitted",
            "sentiment": "negative",
            "severity": chosen,
            "source": ISSUE_SOURCE,
            "body": clamp_text(
                "{0}\n\n{1}".format(cleaned, explanation) if explanation else cleaned,
                MAX_EVENT_BODY_LENGTH,
            ),
            "detail": {
                "reported_as": "issue",
                "reported_by": "agent",
                "title": cleaned,
                "severity": chosen,
            },
        }
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
        revision = self._rules_revision
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
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref, stamp=revision)

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
        revision = self._rules_revision
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
        return self._submit_event(event, trace_id=trace_id, agent=agent, ref=ref, stamp=revision)

    def _submit_event(
        self,
        event: Dict[str, Any],
        *,
        trace_id: Optional[str],
        agent: Optional[str],
        ref: Optional[str],
        stamp: Optional[int] = None,
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
        return self._flusher.submit("events", event, stamp=stamp)

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
            self._schedule_config(cached)
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
            self._config_failed(exc)
        finally:
            self._bootstrapped.set()

    def _refresh_config_if_due(self) -> None:
        """Keep the configuration current. Runs on the flusher thread, each time it wakes.

        The document used to be read exactly once, at start-up, with no second
        try. A process that started while the control plane was restarting ran
        for the rest of its life with no redaction rules at all, and a Mask
        guardrail or a lower sampling rate set in the console reached no agent
        until somebody redeployed it. The document says how long it is good for
        (``refresh_after_seconds``); a read that fails is tried again after 5 s,
        30 s, a minute, and so on. Revalidated with the ETag, a refresh that
        finds nothing changed is one 304.
        """
        if not self._options.bootstrap or not self.enabled:
            return
        if not self._bootstrapped.is_set() and not self._grace_spent:
            # Once. The grace is for the run that ends in the process's first
            # moments; a start-up read that is still hanging after it -- a
            # control plane that accepts the connection and then says nothing
            # for thirty seconds -- must not cost every flush in that window
            # another five.
            self._grace_spent = True
            self._bootstrapped.wait(timeout=BOOTSTRAP_GRACE_SECONDS)
        due = self._config_due
        if not due or time.monotonic() < due:
            return
        try:
            self.config(refresh=True, timeout=min(self._options.timeout_seconds, 10.0))
        except Exception as exc:  # noqa: BLE001 - housekeeping never raises into the worker
            self._config_failed(exc)

    def _schedule_config(self, document: Mapping[str, Any]) -> None:
        try:
            lifetime = float(document.get("refresh_after_seconds") or CONFIG_DEFAULT_REFRESH_SECONDS)
        except (TypeError, ValueError):
            lifetime = CONFIG_DEFAULT_REFRESH_SECONDS
        if self._config_failures:
            logger.info("fulcrum-ops: the SDK configuration was read after %d failed attempt(s)",
                        self._config_failures)
        self._config_failures = 0
        self._config_due = time.monotonic() + max(CONFIG_MIN_REFRESH_SECONDS, lifetime)

    def _config_failed(self, exc: BaseException) -> None:
        delay = CONFIG_RETRY_DELAYS[min(self._config_failures, len(CONFIG_RETRY_DELAYS) - 1)]
        self._config_failures += 1
        self._config_due = time.monotonic() + delay
        if self._config_failures == 1:
            # Once per outage. The retries that follow are routine, and a
            # warning every minute for as long as the control plane is away
            # would teach people to filter this logger out.
            self._handle_error(
                to_fulcrum_error(exc, "Could not read the SDK configuration."), "config"
            )
        else:
            logger.debug("fulcrum-ops: the SDK configuration is still unreadable", exc_info=exc)

    def _apply_config(self, document: Mapping[str, Any], etag: Optional[str]) -> None:
        """Narrow the local options to whatever the deployment enforces.

        Always from what the constructor asked for, never from the values
        already in force, so that a limit the console relaxes is relaxed here
        at the next refresh — up to the constructor's own value and no further.
        """
        options = self._options
        baseline = self._baseline

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

        options.sampling_rate = stricter_number("sampling_rate", baseline.sampling_rate)
        options.batch_max_spans = int(
            stricter_number("batch_max_spans", baseline.batch_max_spans, 1)
        )
        options.batch_max_bytes = int(
            stricter_number("batch_max_bytes", baseline.batch_max_bytes, 1_024)
        )
        options.flush_interval_seconds = stricter_number(
            "flush_interval_seconds", baseline.flush_interval_seconds, 0.05
        )
        options.max_queue_size = int(stricter_number("max_queue_size", baseline.max_queue_size, 1))
        options.retry_max_attempts = int(
            stricter_number("retry_max_attempts", baseline.retry_max_attempts, 0)
        )
        options.retry_backoff_seconds = max(
            0.001, float(document.get("retry_backoff_seconds") or baseline.retry_backoff_seconds)
        )

        # Capture is a conjunction: either side may switch content off, and
        # neither may switch on what the other has off.
        options.capture_input = baseline.capture_input and document.get("capture_input") is not False
        options.capture_output = (
            baseline.capture_output and document.get("capture_output") is not False
        )

        if not baseline.agent and document.get("agent_name"):
            options.agent = str(document["agent_name"])
        if not baseline.environment and document.get("environment"):
            options.environment = str(document["environment"])

        served = list(document.get("redaction") or [])
        rules = compile_rules(served + list(options.redaction))
        try:
            rules_key = json.dumps(served, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rules_key = repr(served)
        with self._config_lock:
            self._rules = rules
            self._config = dict(document)
            self._config_etag = etag
            if rules_key != self._rules_key:
                self._rules_key = rules_key
                self._rules_revision += 1
        self._schedule_config(document)

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

    def _prepare_row(self, kind: str, item: Any) -> None:
        """Put a queued row through the rules in force, if it was built under older ones.

        Payloads are built, and redacted, on the caller's thread when a run
        ends — which can be before the start-up read has answered, or while the
        control plane was unreachable and the queue was holding rows for it.
        Those rows went out carrying whatever the *server's* rules would have
        removed. Called by the flusher for every row just before it is sent.
        """
        revision = self._rules_revision
        if item.stamp is None or item.stamp == revision:
            return
        payload = item.payload
        try:
            if kind in ("traces", "spans"):
                self._redact_row(payload)
                for span in payload.get("spans") or []:
                    self._redact_row(span)
            elif kind == "events":
                if payload.get("sample") is not None:
                    payload["sample"] = self._redact(payload["sample"], "input")
                if payload.get("kind") == "policy.violation" and payload.get("detail"):
                    payload["detail"] = self._redact(payload["detail"], "metadata") or {}
        except Exception:  # noqa: BLE001 - fail closed, as ``_redact`` does
            logger.warning(
                "fulcrum-ops: could not re-apply redaction to a queued row; "
                "sending it without its content"
            )
            for row in [payload] + [r for r in payload.get("spans") or [] if isinstance(r, dict)]:
                for field in ("input", "output", "metadata", "sample", "detail"):
                    row.pop(field, None)
        item.stamp = revision

    def _redact_row(self, row: Dict[str, Any]) -> None:
        for field in ("input", "output"):
            if row.get(field) is not None:
                row[field] = self._redact(row[field], field)
        if row.get("metadata") is not None:
            redacted = self._redact(row["metadata"], "metadata")
            if redacted is None:
                # Not nullable on the wire; see ``_payload_common``.
                row.pop("metadata", None)
            else:
                row["metadata"] = redacted

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

    def _reset_after_fork(self) -> None:
        """Make this client usable in a forked child. Called in the child only."""
        self._config_lock = threading.Lock()
        self._bootstrapped = threading.Event()
        self._bootstrapped.set()
        self._flusher._reset_after_fork()
        self._transport._reset_after_fork()

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


def _after_fork_in_child() -> None:
    """Rebuild what ``fork()`` does not carry over.

    A pre-forking server -- gunicorn with ``--preload``, Celery's prefork pool --
    that calls ``configure()`` at import hands every worker a client whose
    flusher thread exists only in the parent. Nothing the workers traced was
    ever sent, nothing was logged, and each ``flush()`` stalled its request for
    the full timeout waiting on a thread that was not there.
    """
    global _default_lock
    _default_lock = threading.Lock()
    for client in list(_live_clients):
        try:
            client._reset_after_fork()
        except Exception:  # noqa: BLE001 - a fork hook must never raise into the child
            pass


if hasattr(os, "register_at_fork"):  # not on Windows, which cannot fork
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    # Bounded on purpose. A process on its way out must not hang waiting for a
    # control plane that is not answering; two seconds is enough to drain a
    # healthy queue and short enough that nobody notices when it is not.
    atexit.register(lambda: shutdown(timeout=2.0))
