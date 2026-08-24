"""The background batching flusher.

The contract this file owns is short and absolute: **dropping telemetry must
never break the customer's agent.** Everything else follows from it.

* Handing work in never blocks. :meth:`Flusher.submit` appends to an in-memory
  deque and returns; the network happens on a worker thread.
* The queue is bounded. Past ``max_queue_size`` the *oldest* items are dropped,
  because during an outage the newest telemetry is the telemetry someone is
  waiting to look at, and an unbounded queue turns a control-plane outage into
  the customer's own out-of-memory kill.
* No failure escapes. Every send path funnels through :meth:`_send`, which
  converts anything raised into a counted, logged drop.
* Shutdown is best-effort but bounded. An ``atexit`` hook flushes, with a
  timeout, so a process that is exiting does not hang on a control plane that is
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

#: kind -> (path, envelope key, hard per-batch ceiling from the contract)
KIND_ENDPOINTS: Dict[str, Tuple[str, str, int]] = {
    "traces": ("ingest/traces", "traces", MAX_TRACES_PER_BATCH),
    "spans": ("ingest/spans", "spans", MAX_SPANS_PER_BATCH),
    "scores": ("ingest/scores", "scores", MAX_SCORES_PER_BATCH),
    "events": ("ingest/events", "events", MAX_EVENTS_PER_BATCH),
}


class QueueItem:
    """One row waiting to be sent, with its measured size."""

    __slots__ = ("payload", "size", "spans")

    def __init__(self, payload: Dict[str, Any], size: int, spans: int = 0) -> None:
        self.payload = payload
        self.size = size
        self.spans = spans


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
    ) -> None:
        self._transport = transport
        self._options = options
        self._on_error = on_error
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

        self.stats: Dict[str, int] = {
            "submitted": 0,
            "sent": 0,
            "accepted": 0,
            "rejected": 0,
            "blocked": 0,
            "dropped_overflow": 0,
            "dropped_failed": 0,
            "batches": 0,
            "retries": 0,
            "errors": 0,
        }
        self._last_error: Optional[FulcrumOpsError] = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the worker thread. Idempotent, and safe to call from any thread."""
        with self._cond:
            if self._started or self._stopping:
                return
            self._started = True
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

    # --------------------------------------------------------------- ingress

    def submit(self, kind: str, payload: Dict[str, Any], spans: int = 0) -> bool:
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
                # keep when the control plane is unreachable.
                shed_from = max(self._queues.values(), key=len)
                if shed_from:
                    dropped = shed_from.popleft()
                    self.stats["dropped_overflow"] += 1
                    for name, q in self._queues.items():
                        if q is shed_from:
                            self._bytes[name] = max(0, self._bytes[name] - dropped.size)
                            break

            queue.append(QueueItem(payload, size, spans))
            self._bytes[kind] += size
            self.stats["submitted"] += 1
            if self._ready_locked():
                self._cond.notify_all()

        if not self._started:
            self.start()
        return True

    # ----------------------------------------------------------------- flush

    def flush(self, timeout: Optional[float] = 10.0) -> bool:
        """Send everything queued and wait for it. Returns False on timeout.

        Callable from any thread, including from inside a traced function. When
        the worker was never started — a client that has only just been
        constructed — the drain happens inline on the calling thread, so a
        short-lived script that submits once and flushes once still reports.
        """
        if self.pending() == 0 and not self._draining:
            return True

        if not self._started:
            self._drain_once()
            return self.pending() == 0

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            self._flush_requested = True
            self._cond.notify_all()
            while True:
                if self._idle_locked():
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining if remaining is not None else 1.0)

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
                if not self._stopping and not self._flush_requested and not self._ready_locked():
                    self._cond.wait(timeout=self._options.flush_interval_seconds)
                stopping = self._stopping
                empty = sum(len(q) for q in self._queues.values()) == 0
                if stopping and empty:
                    self._draining = False
                    self._flush_requested = False
                    self._cond.notify_all()
                    return
                self._draining = True

            try:
                self._drain_once()
            except Exception as exc:  # pragma: no cover - the drain guards itself
                self._record_error(exc, "flusher")
            finally:
                with self._cond:
                    self._draining = False
                    if sum(len(q) for q in self._queues.values()) == 0:
                        self._flush_requested = False
                    self._cond.notify_all()

    def _ready_locked(self) -> bool:
        """Whether any kind has enough queued to be worth a request now."""
        budget = int(self._options.batch_max_bytes * BYTE_BUDGET_RATIO)
        for kind, queue in self._queues.items():
            if len(queue) >= self._options.batch_max_items:
                return True
            if self._bytes[kind] >= budget:
                return True
        return False

    def _idle_locked(self) -> bool:
        return (
            sum(len(q) for q in self._queues.values()) == 0
            and not self._draining
            and not self._flush_requested
        )

    def _drain_once(self) -> None:
        """Cut and send every batch that is currently queued.

        Bounded by the count observed on entry so a producer thread submitting
        continuously cannot keep this call from ever returning — the next cycle
        picks up whatever arrived meanwhile.
        """
        for kind in KIND_ENDPOINTS:
            with self._cond:
                remaining = len(self._queues[kind])
            while remaining > 0:
                batch = self._take_batch(kind)
                if not batch:
                    break
                remaining -= len(batch)
                self._send(kind, batch)

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
        body: Dict[str, Any] = {
            "sdk": SDK_NAME,
            "sdk_version": SDK_VERSION,
            key: [item.payload for item in batch],
        }
        if self._options.agent:
            body["agent"] = self._options.agent
        return body

    def _send(self, kind: str, batch: List[QueueItem]) -> None:
        """Post one batch. Never raises; failures are counted and logged."""
        if not batch:
            return
        path, _, _ = KIND_ENDPOINTS[kind]
        body = self._envelope(kind, batch)

        try:
            response, error, attempts = self._transport.send_with_retry(
                "POST", path, body=body, sleep=self._sleep, rng=self._rng
            )
        except Exception as exc:  # pragma: no cover - send_with_retry does not raise
            self._record_error(exc, "ingest.{0}".format(kind))
            self._count_dropped(len(batch))
            return

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
            self._send(kind, batch[:middle])
            self._send(kind, batch[middle:])
            return

        if response is None or error is not None:
            self._record_error(error, "ingest.{0}".format(kind))
            self._count_dropped(len(batch))
            return

        self._record_result(kind, len(batch), response)

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
                    "fulcrum-ops: registered agent %r in the control plane",
                    registered.get("name") or registered.get("slug"),
                )

    def _count_dropped(self, count: int) -> None:
        with self._cond:
            self.stats["dropped_failed"] += count

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
