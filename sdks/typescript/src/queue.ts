/**
 * The batching queue: what stands between an agent's hot path and the network.
 *
 * Design constraints, in the order they mattered:
 *
 * 1. **Enqueueing is synchronous and cannot fail.** A `trace()` that finishes
 *    hands its payload over and returns. No promise, no throw, no await.
 * 2. **The queue is bounded.** If the control plane is unreachable for an hour,
 *    an agent under load must not accumulate an hour of traces in memory. Past
 *    `maxQueueSize` the *oldest* items are dropped, because in an outage the
 *    freshest telemetry is the telemetry worth having.
 * 3. **One flight at a time per kind.** Sends are serialised so a retry storm
 *    cannot fan out into hundreds of concurrent requests.
 * 4. **Nothing escapes.** Every failure is routed to `onError`; none of them
 *    reach the caller, and none of them become an unhandled rejection.
 */

import { BYTE_BUDGET_RATIO, MAX_EVENTS_PER_BATCH, MAX_SCORES_PER_BATCH, MAX_SPANS_PER_BATCH, MAX_TRACES_PER_BATCH } from './limits.js';
import { FulcrumOpsError, PayloadTooLargeError, toFulcrumError } from './errors.js';
import { approximateBytes } from './serialize.js';
import { unrefTimer } from './runtime.js';
import { SDK_NAME, SDK_VERSION } from './version.js';
import type { Transport } from './transport.js';
import type { ErrorHandler } from './options.js';
import type { EventIn, IngestBatchResult, ScoreIn, SpanIn, TraceIn } from './types.js';

/** The four ingest endpoints, each with its own buffer. */
export type QueueKind = 'traces' | 'spans' | 'scores' | 'events';

interface QueuedItem {
  payload: TraceIn | SpanIn | ScoreIn | EventIn;
  bytes: number;
  /** Spans this item adds to a request: a trace's nested spans, or 1 for a span. */
  spans: number;
}

interface Buffer {
  items: QueuedItem[];
  bytes: number;
  spans: number;
  /** Serialises sends for this kind. */
  inFlight: Promise<void>;
}

/** Counters a caller can inspect, mostly for tests and health endpoints. */
export interface QueueStats {
  pending: number;
  dropped: number;
  sent: number;
  accepted: number;
  rejected: number;
  blocked: number;
  failedBatches: number;
}

export interface QueueOptions {
  transport: Transport;
  maxItems: number;
  maxBytes: number;
  /**
   * Spans one request may carry, summed across every trace in it. The server
   * counts them that way and refuses the whole request past its ceiling, so a
   * hundred tool-heavy traces are not "100 items" to it. Defaults to the
   * server's own limit.
   */
  maxSpans?: number | undefined;
  flushIntervalMs: number;
  maxQueueSize: number;
  /** Batch-level default agent, written into every body envelope. */
  agent?: string | undefined;
  onError?: ErrorHandler | undefined;
  /** Handed every successful batch result, for quota and auto-registration news. */
  onResult?: ((kind: QueueKind, result: IngestBatchResult) => void) | undefined;
  debug?: boolean;
}

/** The server's own per-batch ceilings, which the SDK must not exceed. */
const HARD_LIMITS: Record<QueueKind, number> = {
  traces: MAX_TRACES_PER_BATCH,
  spans: MAX_SPANS_PER_BATCH,
  scores: MAX_SCORES_PER_BATCH,
  events: MAX_EVENTS_PER_BATCH,
};

export class BatchQueue {
  private readonly buffers: Record<QueueKind, Buffer> = {
    traces: { items: [], bytes: 0, spans: 0, inFlight: Promise.resolve() },
    spans: { items: [], bytes: 0, spans: 0, inFlight: Promise.resolve() },
    scores: { items: [], bytes: 0, spans: 0, inFlight: Promise.resolve() },
    events: { items: [], bytes: 0, spans: 0, inFlight: Promise.resolve() },
  };

  private timer: ReturnType<typeof setTimeout> | undefined;
  private closed = false;
  private readonly stats: QueueStats = {
    pending: 0,
    dropped: 0,
    sent: 0,
    accepted: 0,
    rejected: 0,
    blocked: 0,
    failedBatches: 0,
  };

  constructor(private options: QueueOptions) {}

  /** Update batching limits in place, after `/ingest/config` narrows them. */
  reconfigure(
    patch: Partial<Pick<QueueOptions, 'maxItems' | 'maxBytes' | 'maxSpans' | 'flushIntervalMs' | 'maxQueueSize' | 'agent'>>,
  ): void {
    this.options = { ...this.options, ...patch };
  }

  /** A snapshot of the counters. */
  getStats(): QueueStats {
    return { ...this.stats, pending: this.pendingCount() };
  }

  /** Items per request: what the caller asked for, capped by what the server takes. */
  private batchSize(kind: QueueKind): number {
    return Math.max(1, Math.min(this.options.maxItems, HARD_LIMITS[kind]));
  }

  /** Spans per request, summed across the items in it. */
  private spanLimit(): number {
    return Math.max(1, Math.min(this.options.maxSpans ?? MAX_SPANS_PER_BATCH, MAX_SPANS_PER_BATCH));
  }

  /** Bytes per request the queue aims for, leaving headroom under the server's ceiling. */
  private byteBudget(): number {
    return this.options.maxBytes * BYTE_BUDGET_RATIO;
  }

  private pendingCount(): number {
    return (Object.keys(this.buffers) as QueueKind[]).reduce(
      (total, kind) => total + this.buffers[kind].items.length,
      0,
    );
  }

  /**
   * Add one item. Never throws, never returns a promise.
   *
   * A flush triggered by this call is deliberately not awaited — the caller is
   * in the middle of their own work. Its rejection is already handled inside
   * `flushKind`, so nothing dangles.
   */
  enqueue(kind: QueueKind, payload: TraceIn | SpanIn | ScoreIn | EventIn): void {
    if (this.closed) {
      this.report('enqueue', new FulcrumOpsError('The client is closed; the item was dropped.'));
      this.stats.dropped += 1;
      return;
    }

    const buffer = this.buffers[kind];
    const bytes = approximateBytes(payload);
    const spans = spansIn(kind, payload);
    buffer.items.push({ payload, bytes, spans });
    buffer.bytes += bytes;
    buffer.spans += spans;

    this.enforceQueueCeiling(kind);

    if (
      buffer.items.length >= this.batchSize(kind) ||
      buffer.bytes >= this.byteBudget() ||
      buffer.spans >= this.spanLimit()
    ) {
      void this.flushKind(kind);
      return;
    }
    this.scheduleTimer();
  }

  /**
   * Drop from the front once the buffer is over depth.
   *
   * Oldest-first because during an outage the newest traces describe the
   * problem being diagnosed; the ones from twenty minutes ago do not.
   */
  private enforceQueueCeiling(kind: QueueKind): void {
    const buffer = this.buffers[kind];
    const ceiling = this.options.maxQueueSize;
    if (buffer.items.length <= ceiling) return;

    const excess = buffer.items.length - ceiling;
    const removed = buffer.items.splice(0, excess);
    for (const item of removed) {
      buffer.bytes -= item.bytes;
      buffer.spans -= item.spans;
    }
    this.stats.dropped += removed.length;
    this.report(
      'enqueue',
      new FulcrumOpsError(
        `The ${kind} queue is at its ceiling of ${ceiling}; dropped ${removed.length} of the oldest items.`,
        { code: 'queue_overflow', details: { kind, dropped: removed.length } },
      ),
    );
  }

  private scheduleTimer(): void {
    if (this.timer !== undefined || this.closed) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.flush();
    }, this.options.flushIntervalMs);
    // A pending flush must never be the reason a short-lived script stays alive.
    unrefTimer(this.timer);
  }

  private clearTimer(): void {
    if (this.timer === undefined) return;
    clearTimeout(this.timer);
    this.timer = undefined;
  }

  /** Send everything currently buffered, for every kind. */
  async flush(options: { keepalive?: boolean } = {}): Promise<void> {
    this.clearTimer();
    const kinds = Object.keys(this.buffers) as QueueKind[];
    await Promise.all(kinds.map((kind) => this.flushKind(kind, options)));
  }

  /**
   * Send one kind's buffer.
   *
   * Chained onto `inFlight` so two overlapping calls queue rather than race,
   * and the chain is repaired on failure so one bad batch does not poison every
   * later flush.
   */
  private flushKind(kind: QueueKind, options: { keepalive?: boolean } = {}): Promise<void> {
    const buffer = this.buffers[kind];
    const next = buffer.inFlight.then(() => this.drain(kind, options));
    buffer.inFlight = next.catch(() => undefined);
    return buffer.inFlight;
  }

  /**
   * Cut one request's worth off the front of a buffer.
   *
   * Whichever of three budgets runs out first ends the chunk: item count, spans
   * summed across the items, or bytes. The server enforces all three on the
   * request as a whole and answers 413 for the lot, so cutting by count alone
   * turns "this agent's traces carry fifteen spans each" into a hundred runs
   * lost per request. At least one item is always taken, even an oversized
   * one: it is the server's job to refuse it, and holding it back would wedge
   * everything queued behind it.
   */
  private takeChunk(kind: QueueKind): QueuedItem[] {
    const buffer = this.buffers[kind];
    const maxItems = this.batchSize(kind);
    const maxSpans = this.spanLimit();
    const budget = this.byteBudget();

    let count = 0;
    let bytes = 0;
    let spans = 0;
    while (count < buffer.items.length && count < maxItems) {
      const item = buffer.items[count]!;
      if (count > 0 && (bytes + item.bytes > budget || spans + item.spans > maxSpans)) break;
      bytes += item.bytes;
      spans += item.spans;
      count += 1;
    }

    const chunk = buffer.items.splice(0, count);
    buffer.bytes -= bytes;
    buffer.spans -= spans;
    if (buffer.items.length === 0) {
      buffer.bytes = 0;
      buffer.spans = 0;
    }
    return chunk;
  }

  private async drain(kind: QueueKind, options: { keepalive?: boolean }): Promise<void> {
    const buffer = this.buffers[kind];

    while (buffer.items.length > 0) {
      // Chunk by the configured batch size, not just the server's ceiling.
      // A flush triggered by `maxItems` cannot send synchronously — it is
      // chained onto `inFlight` — so by the time it runs, a caller in a tight
      // loop may have enqueued well past the limit. Cutting here is what keeps
      // "batch of 100" meaning batches of 100 rather than one request with
      // everything that piled up in the meantime.
      await this.send(kind, this.takeChunk(kind), options);
    }
  }

  private async send(kind: QueueKind, chunk: QueuedItem[], options: { keepalive?: boolean }): Promise<void> {
    if (chunk.length === 0) return;
    const body = {
      agent: this.options.agent ?? null,
      sdk: SDK_NAME,
      sdk_version: SDK_VERSION,
      [kind]: chunk.map((item) => item.payload),
    };

    try {
      const result = await this.options.transport.request<IngestBatchResult>({
        method: 'POST',
        path: `/ingest/${kind}`,
        body,
        keepalive: options.keepalive === true,
      });
      this.stats.sent += chunk.length;
      this.stats.accepted += result?.accepted ?? 0;
      this.stats.rejected += result?.rejected ?? 0;
      this.stats.blocked += result?.blocked ?? 0;
      if (result) this.options.onResult?.(kind, result);
      this.debug(
        `${kind}: sent ${chunk.length}, accepted ${result?.accepted ?? 0}, rejected ${result?.rejected ?? 0}, blocked ${result?.blocked ?? 0}`,
      );
    } catch (thrown) {
      // "Too large" is the one refusal that is about the request rather than
      // about what is in it, and the server says so: "Split it and retry". The
      // budgets above are estimates — the deployment's limits may be tighter
      // than the defaults and the bootstrap document not yet read — so a 413
      // on several items is halved and resent instead of costing every run in
      // it. The recursion is bounded by log2 of the chunk, and a single item
      // that is still too large falls through to be dropped and reported.
      if (thrown instanceof PayloadTooLargeError && chunk.length > 1) {
        const middle = Math.ceil(chunk.length / 2);
        this.debug(`${kind}: ${chunk.length} items were refused as too large; resending as two halves`);
        await this.send(kind, chunk.slice(0, middle), options);
        await this.send(kind, chunk.slice(middle), options);
        return;
      }
      // The transport already exhausted its retries. Re-queueing here would
      // build an unbounded retry loop on top of a bounded one, so the batch
      // is counted as lost and reported.
      this.stats.failedBatches += 1;
      this.stats.dropped += chunk.length;
      this.report(`flush:${kind}`, toFulcrumError(thrown, `Could not report ${chunk.length} ${kind}.`));
    }
  }

  /** Flush and stop. Idempotent; safe to call from an exit handler. */
  async close(options: { keepalive?: boolean } = {}): Promise<void> {
    if (this.closed) {
      await this.settle();
      return;
    }
    this.closed = true;
    this.clearTimer();
    await this.flush(options);
    await this.settle();
  }

  /** Wait for any in-flight send to finish, without starting a new one. */
  private async settle(): Promise<void> {
    const kinds = Object.keys(this.buffers) as QueueKind[];
    await Promise.all(kinds.map((kind) => this.buffers[kind].inFlight));
  }

  private report(operation: string, error: FulcrumOpsError): void {
    this.debug(`${operation}: ${error.message}`);
    if (!this.options.onError) return;
    try {
      this.options.onError(error, { operation });
    } catch {
      // A throwing error handler is the caller's bug, not a reason to crash
      // the flush that was reporting to it.
    }
  }

  private debug(message: string): void {
    if (!this.options.debug) return;
    // eslint-disable-next-line no-console
    console.debug(`[fulcrum-ops] ${message}`);
  }
}

/** How many spans an item contributes to the request it travels in. */
function spansIn(kind: QueueKind, payload: TraceIn | SpanIn | ScoreIn | EventIn): number {
  if (kind === 'spans') return 1;
  if (kind !== 'traces') return 0;
  const nested = (payload as TraceIn).spans;
  return Array.isArray(nested) ? nested.length : 0;
}
