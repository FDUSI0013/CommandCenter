"""The background batching flusher.

The contract this file owns is short and absolute: **dropping telemetry must
never break the customer's agent.** Everything else follows from it.

* Handing work in never blocks. :meth:`Flusher.submit` appends to an in-memory
  deque and returns; the network happens on a worker thread.
* The queue is bounded. Past ``max_queue_size`` the *oldest* items are dropped,
  because during an outage the newest telemetry is the telemetry someone is
  waiting to look at, and an unbounded queue turns a server outage into
  the customer's own out-of-memory kill.
* No failure escapes. Every send path funnels through :meth:`_send`, which
  converts anything raised into a counted, logged outcome.
* An outage is waited out, not thrown away. A batch that fails for a reason that
  could clear — no connection, a timeout, a 5xx, a 429 — goes back to the front
  of the queue and the worker backs off; only a refusal that retrying cannot
  change (a revoked key, a malformed body), a row that has been failing for ten
  minutes, or the queue ceiling drops anything. The in-request retries cover a
  blip of a few seconds. A deploy takes longer than that.
* Shutdown is best-effort but bounded. An ``atexit`` hook flushes, with a
  timeout, so a process that is exiting does not hang on a server that is
  not answering.

Batches are cut on three triggers — item count, byte budget, and the flush
interval — and the byte budget matters most: ``batch_max_bytes`` is what the
server actually enforces, and a 413 costs a whole round trip. When one arrives
anyway the batch is halved and retried rather than dropped, so one oversized
trace cannot take its neighbours down with it.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ._version import SDK_NAME, SDK_VERSION
from .errors import FulcrumOpsError, PayloadTooLargeError
from .limits import (
    BYTE_BUDGET_RATIO,
    MAX_EVENTS_PER_BATCH,
    MAX_SCORES_PER_BATCH,
    MAX_SPANS_PER_BATCH,
    MAX_TRACES_PER_BATCH,
)
from .options import Options
from .serialize import estimate_bytes
from .transport import Response, Transport

__all__ = ["Flusher", "QueueItem", "KIND_ENDPOINTS"]

logger = logging.getLogger("fulcrum_ops")

#: How long a row may keep failing before it is given up on. Long enough to ride
#: out a deploy or a proxy restart; short enough that a server which is
#: never coming back does not pin ten thousand rows in memory for the life of
#: the process.
REQUEUE_MAX_AGE_SECONDS = 600.0

#: The longest the worker waits between attempts while the server is down.
OUTAGE_MAX_BACKOFF_SECONDS = 60.0

#: kind -> (path, envelope key, hard per-batch ceiling from the contract)
KIND_ENDPOINTS: Dict[str, Tuple[str, str, int]] = {
    "traces": ("ingest/traces", "traces", MAX_TRACES_PER_BATCH),
    "spans": ("ingest/spans", "spans", MAX_SPANS_PER_BATCH),
    "scores": ("ingest/scores", "scores", MAX_SCORES_PER_BATCH),
    "events": ("ingest/events", "events", MAX_EVENTS_PER_BATCH),
}


class QueueItem:
    """One row waiting to be sent, with its measured size."""

    __slots__ = ("payload", "size", "spans", "first_failed_at", "stamp")

    def __init__(
        self, payload: Dict[str, Any], size: int, spans: int = 0, stamp: Optional[int] = None
    ) -> None:
        self.payload = payload
        self.size = size
        self.spans = spans
        #: The owner's mark for "which rules this payload was built under";
        #: ``None`` for a row that carries no captured content.
        self.stamp = stamp
        #: When this row first came back unsent, on the monotonic clock.
        self.first_failed_at: Optional[float] = None


class Flusher:
    """Queues ingest rows and posts them in batches from a worker thread."""

    def __init__(
        self,
        transport: Transport,
        options: Options,
        *,
        on_error: Optional[Callable[[FulcrumOpsError, str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        on_tick: Optional[Callable[[], None]] = None,
        prepare: Optional[Callable[[str, "QueueItem"], None]] = None,
    ) -> None:
        self._transport = transport
        self._options = options
        self._on_error = on_error
        #: Called on the worker thread each time it wakes, before it drains.
        self._on_tick = on_tick
        #: Called for every row just before it is sent, to bring it up to date.
        self._prepare = prepare
        self._sleep = sleep
        self._rng = rng

        self._queues: Dict[str, Deque[QueueItem]] = {kind: deque() for kind in KIND_ENDPOINTS}
        self._bytes: Dict[str, int] = {kind: 0 for kind in KIND_ENDPOINTS}

        self._cond = threading.Condition(threading.Lock())
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._draining = False
        self._flush_requested = False
        self._started = False
        self._exited = False
        # Drain cycles are numbered so ``flush()`` can wait for one that began
        # after it asked, rather than for the queue to be empty — which, during
        # an outage, it never is.
        self._drains_started = 0
        self._drains_finished = 0
        # Batches that were dropped or put back. ``flush()`` compares it across
        # its wait to tell "everything was sent" from "the queue is quiet".
        self._failures = 0
        self._failed_drains = 0
        self._retry_at = 0.0

        self.stats: Dict[str, int] = {
            "submitted": 0,
            "sent": 0,
            "accepted": 0,
            "rejected": 0,
            "blocked": 0,
            "dropped_overflow": 0,
            "dropped_failed": 0,
            "requeued": 0,
            "batches": 0,
            "retries": 0,
            "errors": 0,
        }
        self._last_error: Optional[FulcrumOpsError] = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the worker thread. Idempotent, and safe to call from any thread."""
        with self._cond:
            if self._stopping:
                return
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._exited = False
            thread = threading.Thread(
                target=self._run,
                name="fulcrum-ops-flusher",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def close(self, timeout: Optional[float] = 5.0) -> None:
        """Flush what is queued, then stop the worker. Never raises."""
        try:
            self.flush(timeout=timeout)
        except Exception:  # pragma: no cover - flush already swallows its own failures
            pass
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout if timeout is not None else 5.0)

    def _reset_after_fork(self) -> None:
        """Make this flusher usable in a forked child. Called in the child only.

        ``fork()`` copies the memory and none of the threads. The child inherits
        ``_started = True`` with no worker behind it — so nothing it queued was
        ever sent, and every ``flush()`` waited out its whole timeout — a lock
        that may have been held by a thread that no longer exists, and a copy of
        the parent's queue. The rows in that copy are still in the parent, which
        will send them; sending them from here as well would report every one
        of them twice. The next ``submit()`` starts a fresh worker.
        """
        self._cond = threading.Condition(threading.Lock())
        self._thread = None
        self._started = False
        self._exited = False
        self._draining = False
        self._flush_requested = False
        self._drains_started = 0
        self._drains_finished = 0
        self._failed_drains = 0
        self._retry_at = 0.0
        for kind, queue in self._queues.items():
            queue.clear()
            self._bytes[kind] = 0

    # --------------------------------------------------------------- ingress

    def submit(
        self, kind: str, payload: Dict[str, Any], spans: int = 0, stamp: Optional[int] = None
    ) -> bool:
        """Queue one row. Returns False when it was dropped rather than queued."""
        if kind not in KIND_ENDPOINTS:
            logger.debug("fulcrum-ops: ignoring unknown queue kind %r", kind)
            return False

        try:
            size = estimate_bytes(payload)
        except Exception:
            size = 1_024

        with self._cond:
            queue = self._queues[kind]
            total = sum(len(q) for q in self._queues.values())
            if total >= self._options.max_queue_size:
                # Shed from the front: the oldest row is the least useful one to
                # keep when the server is unreachable.
                shed_from = max(self._queues.values(), key=len)
                if shed_from:
                    dropped = shed_from.popleft()
                    self.stats["dropped_overflow"] += 1
                    for name, q in self._queues.items():
                        if q is shed_from:
                            self._bytes[name] = max(0, self._bytes[name] - dropped.size)
                            break

            queue.append(QueueItem(payload, size, spans, stamp))
            self._bytes[kind] += size
            self.stats["submitted"] += 1
            if self._ready_locked():
                self._cond.notify_all()

        if not self._worker_alive():
            self.start()
        return True

    def _worker_alive(self) -> bool:
        thread = self._thread
        return self._started and thread is not None and thread.is_alive()

    # ----------------------------------------------------------------- flush

    def flush(self, timeout: Optional[float] = 10.0) -> bool:
        """Send everything queued and wait for it.

        ``True`` means every row that was queued has been handed to the server.
        ``False`` means it has not: the wait timed out, or a batch could
        not be delivered — it was put back for a later attempt, or dropped — so
        a caller that flushes before exiting can tell a clean hand-off from a
        server that was not there. It used to answer ``True`` the moment
        the queue was empty, which a dropped batch also leaves it.

        During an outage it returns as soon as the attempt it asked for has
        failed, not after the full timeout: a handler that flushes per request
        must not turn a server restart into ten seconds on every call.

        Callable from any thread, including from inside a traced function. When
        the worker was never started — a client that has only just been
        constructed — the drain happens inline on the calling thread, so a
        short-lived script that submits once and flushes once still reports.
        """
        with self._cond:
            failures = self._failures
            if sum(len(q) for q in self._queues.values()) == 0 and not self._draining:
                return True

        if not self._started:
            self._drain_once()
            return self.pending() == 0 and self._failures == failures
        if not self._worker_alive() and not self._stopping:
            # The thread did not survive — a fork this process never told us
            # about. Waiting on it would only ever time out.
            self.start()

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                # A drain already under way may have counted the queue before
                # the rows this caller cares about arrived; only one that starts
                # from here is known to have seen them.
                wanted = self._drains_started + 1
                self._flush_requested = True
                self._cond.notify_all()
                while self._drains_finished < wanted and not self._exited:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._cond.wait(timeout=remaining if remaining is not None else 1.0)
                if self._failures != failures:
                    return False
                if sum(len(q) for q in self._queues.values()) == 0:
                    return True
                if self._exited:
                    return False
                # Rows arrived while that drain ran. Go round again for them.

    def pending(self) -> int:
        """Rows waiting to be sent, across every kind."""
        with self._cond:
            return sum(len(q) for q in self._queues.values())

    def snapshot(self) -> Dict[str, Any]:
        """Counters plus the last failure, for :meth:`FulcrumOps.stats`."""
        with self._cond:
            out: Dict[str, Any] = dict(self.stats)
            out["pending"] = sum(len(q) for q in self._queues.values())
            out["pending_bytes"] = sum(self._bytes.values())
            out["last_error"] = str(self._last_error) if self._last_error else None
            return out

    # ---------------------------------------------------------------- worker

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._stopping and not self._flush_requested:
                    held = self._retry_at - time.monotonic()
                    if held > 0:
                        # Backing off after a failed drain. A full queue does
                        # not shorten it: "ready" is a reason to send promptly,
                        # not a reason to hammer a server that is down.
                        self._cond.wait(timeout=held)
                    elif not self._ready_locked():
                        self._cond.wait(timeout=self._options.flush_interval_seconds)

            self._tick()

            with self._cond:
                stopping = self._stopping
                if (
                    not stopping
                    and not self._flush_requested
                    and time.monotonic() < self._retry_at
                ):
                    continue  # woken early by a submit; the back-off still stands
                if stopping:
                    # One last attempt at whatever is left — unless the previous
                    # attempt has only just failed, in which case a process on
                    # its way out does not wait on a dead endpoint a second time.
                    if time.monotonic() < self._retry_at:
                        self._abandon_locked()
                    if sum(len(q) for q in self._queues.values()) == 0:
                        self._draining = False
                        self._flush_requested = False
                        self._exited = True
                        self._cond.notify_all()
                        return
                self._drains_started += 1
                cycle = self._drains_started
                self._flush_requested = False
                self._draining = True

            try:
                self._drain_once()
            except Exception as exc:  # pragma: no cover - the drain guards itself
                self._record_error(exc, "flusher")
            finally:
                with self._cond:
                    self._draining = False
                    self._drains_finished = cycle
                    self._cond.notify_all()

    def _tick(self) -> None:
        """Run the owner's periodic work on this thread. Never raises."""
        if self._on_tick is None or self._stopping:
            return
        try:
            self._on_tick()
        except Exception:  # noqa: BLE001 - housekeeping must not stop the worker
            logger.debug("fulcrum-ops: the periodic tick raised", exc_info=True)

    def _abandon_locked(self) -> None:
        """Give up on everything still queued, and count it. Shutdown only."""
        for kind, queue in self._queues.items():
            if queue:
                self.stats["dropped_failed"] += len(queue)
                self._failures += 1
                queue.clear()
            self._bytes[kind] = 0

    def _ready_locked(self) -> bool:
        """Whether any kind has enough queued to be worth a request now."""
        budget = int(self._options.batch_max_bytes * BYTE_BUDGET_RATIO)
        for kind, queue in self._queues.items():
            if len(queue) >= self._options.batch_max_items:
                return True
            if self._bytes[kind] >= budget:
                return True
        return False

    def _drain_once(self) -> bool:
        """Cut and send every batch that is currently queued.

        Bounded by the count observed on entry so a producer thread submitting
        continuously cannot keep this call from ever returning — the next cycle
        picks up whatever arrived meanwhile.

        Stops at the first batch the server could not be reached for, and
        answers ``False``. The other kinds go to the same host, so trying them
        would only spend another full retry cycle each to learn the same thing.
        """
        for kind in KIND_ENDPOINTS:
            with self._cond:
                remaining = len(self._queues[kind])
            while remaining > 0:
                batch = self._take_batch(kind)
                if not batch:
                    break
                remaining -= len(batch)
                if not self._send(kind, batch):
                    return False
        return True

    def _take_batch(self, kind: str) -> List[QueueItem]:
        """Pop one batch: whichever of item count, span count or bytes runs out first."""
        _, _, hard_max = KIND_ENDPOINTS[kind]
        max_items = min(self._options.batch_max_items, hard_max)
        max_spans = self._options.batch_max_spans
        budget = int(self._options.batch_max_bytes * BYTE_BUDGET_RATIO)

        batch: List[QueueItem] = []
        size = 0
        spans = 0
        with self._cond:
            queue = self._queues[kind]
            while queue and len(batch) < max_items:
                item = queue[0]
                # Always take at least one, even an oversized row: it is the
                # server's job to refuse it, and holding it forever would wedge
                # everything queued behind it.
                if batch and (size + item.size > budget or spans + item.spans > max_spans):
                    break
                queue.popleft()
                self._bytes[kind] = max(0, self._bytes[kind] - item.size)
                batch.append(item)
                size += item.size
                spans += item.spans
        return batch

    # ------------------------------------------------------------------ send

    def _envelope(self, kind: str, batch: List[QueueItem]) -> Dict[str, Any]:
        _, key, _ = KIND_ENDPOINTS[kind]
        if self._prepare is not None:
            for item in batch:
                try:
                    self._prepare(kind, item)
                except Exception:  # noqa: BLE001 - one odd row must not cost the batch
                    logger.debug("fulcrum-ops: could not prepare a %s row", kind, exc_info=True)
        body: Dict[str, Any] = {
            "sdk": SDK_NAME,
            "sdk_version": SDK_VERSION,
            key: [item.payload for item in batch],
        }
        if self._options.agent:
            body["agent"] = self._options.agent
        if self._options.environment:
            # Traces also say this per row, in their metadata. Scores and events
            # have no metadata, so a batch of only those had no way to say which
            # environment the process that sent it was configured for.
            body["environment"] = self._options.environment
        return body

    def _send(self, kind: str, batch: List[QueueItem]) -> bool:
        """Post one batch. Never raises; failures are counted and logged.

        ``False`` means the server could not be reached and the caller
        should stop sending for now; a batch that was *refused* is a delivered
        answer, and the next one may well be accepted.
        """
        if not batch:
            return True
        path, _, _ = KIND_ENDPOINTS[kind]
        body = self._envelope(kind, batch)

        try:
            response, error, attempts = self._transport.send_with_retry(
                "POST", path, body=body, sleep=self._sleep, rng=self._rng
            )
        except Exception as exc:  # pragma: no cover - send_with_retry does not raise
            self._record_error(exc, "ingest.{0}".format(kind))
            self._count_dropped(len(batch))
            return True

        with self._cond:
            self.stats["batches"] += 1
            self.stats["retries"] += max(0, attempts - 1)

        if error is not None and isinstance(error, PayloadTooLargeError) and len(batch) > 1:
            # Halve and retry rather than drop: the batch is too big, not wrong.
            middle = len(batch) // 2
            logger.debug(
                "fulcrum-ops: %s batch of %d refused as too large; splitting",
                kind,
                len(batch),
            )
            if not self._send(kind, batch[:middle]):
                self._requeue(kind, batch[middle:], None)
                return False
            return self._send(kind, batch[middle:])

        if response is None or error is not None:
            self._record_error(error, "ingest.{0}".format(kind))
            if error is None or error.retryable:
                # Nothing was refused: the server was not there, or said
                # to come back later. That clears on its own, and these rows
                # are the ones somebody will be looking for when it does.
                self._requeue(kind, batch, error)
                return False
            self._count_dropped(len(batch))
            return True

        with self._cond:
            self._failed_drains = 0
            self._retry_at = 0.0
        self._record_result(kind, len(batch), response)
        return True

    def _requeue(
        self, kind: str, batch: List[QueueItem], error: Optional[FulcrumOpsError]
    ) -> None:
        """Put an undelivered batch back at the front of its queue, and back off.

        At the front, because these are the oldest rows and the queue sheds from
        the front: if the outage outlasts ``max_queue_size`` they are the first
        to go, which is the order the ceiling has always promised. A process
        that is shutting down does not wait for a recovery it will not see, so
        there the batch is dropped as before.
        """
        now = time.monotonic()
        keep: List[QueueItem] = []
        for item in batch:
            if item.first_failed_at is None:
                item.first_failed_at = now
            if now - item.first_failed_at <= REQUEUE_MAX_AGE_SECONDS:
                keep.append(item)

        delay = 0.0
        with self._cond:
            self._failures += 1
            if self._stopping:
                self.stats["dropped_failed"] += len(batch)
                # And the rest of the queue with it: the worker reads this as
                # "the last attempt has only just failed" and does not spend a
                # retry cycle per remaining batch on its way out.
                self._retry_at = now + OUTAGE_MAX_BACKOFF_SECONDS
                return
            self.stats["dropped_failed"] += len(batch) - len(keep)

            queue = self._queues[kind]
            queue.extendleft(reversed(keep))
            self._bytes[kind] += sum(item.size for item in keep)
            self.stats["requeued"] += len(keep)
            overflow = sum(len(q) for q in self._queues.values()) - self._options.max_queue_size
            while overflow > 0 and queue:
                shed = queue.popleft()
                self._bytes[kind] = max(0, self._bytes[kind] - shed.size)
                self.stats["dropped_overflow"] += 1
                overflow -= 1

            # Each failed drain doubles the wait, from one flush interval up to
            # a minute, and a server that named a time is taken at its word.
            self._failed_drains += 1
            interval = self._options.flush_interval_seconds
            delay = min(
                max(OUTAGE_MAX_BACKOFF_SECONDS, interval),
                interval * (2 ** min(self._failed_drains - 1, 10)),
            )
            retry_after = getattr(error, "retry_after_seconds", None)
            if retry_after:
                delay = max(delay, float(retry_after))
            self._retry_at = now + delay

        if keep:
            logger.info(
                "fulcrum-ops: kept %d %s for another attempt in about %.0fs",
                len(keep),
                kind,
                delay,
            )

    def _record_result(self, kind: str, submitted: int, response: Response) -> None:
        """Read the per-item verdicts the ingest endpoint returns.

        A 200 does not mean the rows landed: the batch's fate is in the counters
        and the per-item rows. Surfacing the rejection reason here is the
        difference between "telemetry is missing" and "the agent is not
        provisioned yet, go and provision it".
        """
        body = response.body if isinstance(response.body, dict) else {}
        accepted = int(body.get("accepted") or 0)
        rejected = int(body.get("rejected") or 0)
        blocked = int(body.get("blocked") or 0)

        with self._cond:
            self.stats["sent"] += submitted
            self.stats["accepted"] += accepted
            self.stats["rejected"] += rejected
            self.stats["blocked"] += blocked

        if rejected or blocked:
            reasons: Dict[str, str] = {}
            for row in body.get("results") or []:
                if not isinstance(row, dict) or row.get("outcome") == "accepted":
                    continue
                code = str(row.get("code") or row.get("outcome") or "rejected")
                reasons.setdefault(code, str(row.get("reason") or ""))
            summary = "; ".join(
                "{0}: {1}".format(code, reason) if reason else code
                for code, reason in reasons.items()
            )
            logger.warning(
                "fulcrum-ops: %d of %d %s were not stored (%s)",
                rejected + blocked,
                submitted,
                kind,
                summary or "no reason given",
            )

            # A refused row is data the caller believed they had reported. It
            # never interrupts them, but it must not be visible only to whoever
            # happens to read the log either: a batch answered 200 with every
            # row rejected is exactly the failure that goes unnoticed for a
            # week. One notification per distinct reason, not per row, so a
            # thousand-row batch cannot flood the handler.
            for code, reason in reasons.items():
                self._notify(
                    FulcrumOpsError(
                        reason or "{0} rows were {1}.".format(kind, code),
                        code=code,
                        details={
                            "kind": kind,
                            "submitted": submitted,
                            "rejected": rejected,
                            "blocked": blocked,
                        },
                    ),
                    "ingest.{0}".format(kind),
                )

        for registered in body.get("auto_registered") or []:
            if isinstance(registered, dict):
                logger.info(
                    "fulcrum-ops: registered agent %r in FD AI Command Center",
                    registered.get("name") or registered.get("slug"),
                )

    def _count_dropped(self, count: int) -> None:
        with self._cond:
            self.stats["dropped_failed"] += count
            self._failures += 1

    def _record_error(self, exc: Optional[BaseException], operation: str) -> None:
        """Count a send failure, log it, and hand it to ``on_error``."""
        if exc is None:
            return
        error = exc if isinstance(exc, FulcrumOpsError) else FulcrumOpsError(str(exc))
        with self._cond:
            # ``errors`` counts requests that failed outright. A per-item
            # rejection came back on a successful request and is already counted
            # under ``rejected``/``blocked``, so it goes through ``_notify``
            # instead and leaves this counter alone.
            self.stats["errors"] += 1

        logger.warning("fulcrum-ops: %s failed — %s", operation, error)
        self._notify(error, operation)

    def _notify(self, error: FulcrumOpsError, operation: str) -> None:
        """Hand a failure to the caller's handler, and never let it propagate."""
        with self._cond:
            self._last_error = error
        if self._on_error is None:
            return
        try:
            self._on_error(error, operation)
        except Exception:
            # A broken error handler is not allowed to become the error.
            logger.debug("fulcrum-ops: on_error handler raised", exc_info=True)
