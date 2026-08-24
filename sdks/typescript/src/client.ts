/**
 * `FulcrumOps` — the object a customer's code holds.
 *
 * The contract this class keeps with its caller, which every method below is
 * written to honour:
 *
 *   **Telemetry never changes the behaviour of the code it observes.**
 *
 * Concretely: `trace(name, fn)` returns exactly what `fn` returned, throws
 * exactly what `fn` threw, and swallows everything that goes wrong in the SDK
 * itself. A missing API key, an unreachable control plane, a full queue, a
 * serialisation failure on an exotic argument — none of them reach the caller
 * except through `onError`.
 *
 * The deliberate exception is the pair of lookups whose whole purpose is to
 * return a value — `config()` and `prompts.get()`. Those reject, because there
 * the failure *is* the answer and swallowing it would hand a model an empty
 * system prompt. `flush()` and `close()` are not in that set: they are what a
 * `finally` block and a shutdown hook call, so they absorb everything too.
 */

import { BatchQueue } from './queue.js';
import { ContextManager } from './context.js';
import { PromptClient } from './prompts.js';
import { Span, Trace } from './trace.js';
import { Transport } from './transport.js';
import { applyRedactionToText, compileRedactionRules } from './redaction.js';
import { FulcrumOpsError, toFulcrumError } from './errors.js';
import {
  MAX_ACTION_TAKEN_LENGTH,
  MAX_AGENT_LENGTH,
  MAX_EVENT_BODY_LENGTH,
  MAX_EVENT_SOURCE_LENGTH,
  MAX_GUARDRAIL_LENGTH,
  MAX_ID_LENGTH,
  MAX_POLICY_LENGTH,
  MAX_RATING,
  MAX_REF_LENGTH,
  MAX_SAMPLE_LENGTH,
  MAX_SCORE_CATEGORY_LENGTH,
  MAX_SCORE_NAME_LENGTH,
  MAX_SCORE_REASON_LENGTH,
  MAX_SCORE_SOURCE_LENGTH,
  MAX_SENTIMENT_LENGTH,
  MAX_SEVERITY_LENGTH,
  MAX_SUBMITTED_BY_LENGTH,
  MIN_RATING,
  clampText,
} from './limits.js';
import { normaliseMetadata } from './serialize.js';
import { onPageHide, onProcessExit } from './runtime.js';
import { resolveOptions } from './options.js';
import { nowIso, toIso } from './ids.js';
import type { CompiledRedactionRule } from './redaction.js';
import type { EndOptions, ScoreInput, SpanOptions, TraceOptions, TraceSink } from './trace.js';
import type { FulcrumOpsOptions, ResolvedOptions } from './options.js';
import type { QueueKind, QueueStats } from './queue.js';
import type {
  EventIn,
  IngestBatchResult,
  IngestConfig,
  IngestQuotaState,
  JsonObject,
  ScoreIn,
  ScoreTarget,
  SpanIn,
  TraceIn,
} from './types.js';

/** A traced function's first argument is the span it is running in. */
export type TracedBody<T> = (trace: Trace) => T;
export type SpanBody<T> = (span: Span) => T;

/** What `client.score()` takes. */
export interface FeedbackScoreOptions {
  /** Trace id, span id or thread id, according to `target`. */
  id: string;
  name: string;
  value: number;
  target?: ScoreTarget;
  categoryName?: string;
  reason?: string;
  source?: string;
  agent?: string;
}

/** A guardrail that fired inside the customer's own process. */
export interface GuardrailEventOptions {
  guardrail: string;
  actionTaken?: string;
  score?: number;
  matched?: Record<string, unknown>;
  sample?: string;
  traceId?: string;
  spanId?: string;
  agent?: string;
  occurredAt?: Date | number | string;
  /** Idempotency key; a repeat comes back as a duplicate rather than a second row. */
  ref?: string;
}

/** A policy the agent breached. */
export interface PolicyViolationOptions {
  policy: string;
  severity?: string;
  actionTaken?: string;
  detail?: Record<string, unknown>;
  traceId?: string;
  spanId?: string;
  agent?: string;
  occurredAt?: Date | number | string;
  ref?: string;
}

/** End-user feedback captured in the product. */
export interface FeedbackEventOptions {
  rating?: number;
  sentiment?: string;
  body?: string;
  source?: string;
  submittedBy?: string;
  traceId?: string;
  spanId?: string;
  agent?: string;
  occurredAt?: Date | number | string;
  ref?: string;
}

/** Options for the bootstrap fetch. */
export interface ConfigOptions {
  /** Ignore the cached document and re-read it. */
  refresh?: boolean;
}

/** Counters describing what the client has done so far. */
export interface ClientStats extends QueueStats {
  sampledOut: number;
  contextBackend: string;
}

let defaultClient: FulcrumOps | undefined;

/** The client the module-level `traced()` helper reports to. */
export function getDefaultClient(): FulcrumOps | undefined {
  return defaultClient;
}

/** Point the module-level helpers at a specific client. */
export function setDefaultClient(client: FulcrumOps | undefined): void {
  defaultClient = client;
}

export class FulcrumOps implements TraceSink {
  /** The options actually in force, after environment and defaults. */
  readonly options: ResolvedOptions;
  /** The Prompt Manager, cached. */
  readonly prompts: PromptClient;

  private readonly transport: Transport;
  private readonly queue: BatchQueue;
  private readonly context: ContextManager;

  private compiledRedaction: CompiledRedactionRule[];
  private cachedConfig: IngestConfig | undefined;
  private configEtag: string | undefined;
  private configExpiresAt = 0;
  private configInFlight: Promise<IngestConfig | undefined> | undefined;

  private effectiveSamplingRate: number;
  private effectiveCaptureInput: boolean;
  private effectiveCaptureOutput: boolean;
  private boundAgent: string | undefined;

  private sampledOut = 0;
  private closed = false;
  private readonly unregisterExitHooks: Array<() => void> = [];
  private latestQuotas: IngestQuotaState[] = [];

  constructor(options: FulcrumOpsOptions = {}) {
    this.options = resolveOptions(options);
    this.transport = new Transport(this.options);
    this.context = new ContextManager(this.options.asyncContext);
    this.prompts = new PromptClient(this.transport);

    this.compiledRedaction = compileRedactionRules(this.options.redaction);
    this.effectiveSamplingRate = this.options.samplingRate;
    this.effectiveCaptureInput = this.options.captureInput;
    this.effectiveCaptureOutput = this.options.captureOutput;

    this.queue = new BatchQueue({
      transport: this.transport,
      maxItems: this.options.batch.maxItems,
      maxBytes: this.options.batch.maxBytes,
      flushIntervalMs: this.options.batch.flushIntervalMs,
      maxQueueSize: this.options.batch.maxQueueSize,
      agent: this.options.agent,
      onError: this.options.onError,
      onResult: (kind, result) => this.absorbResult(kind, result),
      debug: this.options.debug,
    });

    if (!this.options.enabled) {
      this.debug('no API key found; reporting is disabled and every call is a no-op');
    }
    if (this.options.enabled && this.options.bootstrap) {
      // Fire-and-forget: start-up must not block on the network, and a failure
      // here only means the SDK keeps its own defaults.
      void this.config().catch(() => undefined);
    }
    if (this.options.flushOnExit) this.installExitHooks();
    if (this.options.setAsDefault && !defaultClient) defaultClient = this;
  }

  /**
   * Build a client and wait until it is fully ready.
   *
   * Only worth using when the very first trace must have `AsyncLocalStorage`
   * nesting, or when the bootstrap document's sampling rate must apply from the
   * first call rather than from the next one. The plain constructor is right
   * everywhere else.
   */
  static async create(options: FulcrumOpsOptions = {}): Promise<FulcrumOps> {
    const client = new FulcrumOps(options);
    await client.ready();
    return client;
  }

  /** Resolves once the async-context backend and bootstrap document have settled. */
  async ready(): Promise<void> {
    await this.context.whenReady();
    if (this.options.enabled && this.options.bootstrap) {
      await this.config().catch(() => undefined);
    }
  }

  // -------------------------------------------------------------------------
  // TraceSink — what `Trace` and `Span` call back into.
  // -------------------------------------------------------------------------

  /** @internal */
  submitTrace(trace: TraceIn): void {
    if (!this.options.enabled || this.closed) return;
    this.queue.enqueue('traces', trace);
  }

  /** @internal */
  submitSpan(span: SpanIn): void {
    if (!this.options.enabled || this.closed) return;
    this.queue.enqueue('spans', span);
  }

  /** @internal */
  redactionRules(): readonly CompiledRedactionRule[] {
    return this.compiledRedaction;
  }

  /** @internal */
  captureInput(): boolean {
    return this.effectiveCaptureInput;
  }

  /** @internal */
  captureOutput(): boolean {
    return this.effectiveCaptureOutput;
  }

  /** @internal */
  streamSpans(): boolean {
    return this.options.streamSpans;
  }

  /** @internal */
  defaultAgent(): string | undefined {
    return this.options.agent ?? this.boundAgent;
  }

  // -------------------------------------------------------------------------
  // Tracing
  // -------------------------------------------------------------------------

  /** The trace currently in scope, if any. */
  currentTrace(): Trace | undefined {
    const active = this.context.active();
    return active?.trace instanceof Trace ? active.trace : undefined;
  }

  /** The innermost span currently in scope, if any. */
  currentSpan(): Span | undefined {
    const active = this.context.active();
    return active?.span instanceof Span ? active.span : undefined;
  }

  /** Which nesting mechanism is in force: `async-local-storage` or `stack`. */
  get contextBackend(): string {
    return this.context.backend;
  }

  /**
   * Open a trace by hand, for a run whose start and end are far apart.
   *
   * The caller owns the returned object and must call `end()`. `trace()` is the
   * right choice whenever the work fits inside one function.
   */
  startTrace(nameOrOptions: string | TraceOptions = {}): Trace {
    const options = typeof nameOrOptions === 'string' ? { name: nameOrOptions } : { ...nameOrOptions };
    const sampled = options.sampled ?? this.shouldSample();
    if (!sampled) this.sampledOut += 1;
    if (this.options.environment) {
      options.metadata = { environment: this.options.environment, ...(options.metadata ?? {}) };
    }
    return new Trace(this, { ...options, sampled });
  }

  /**
   * Run `body` inside a trace.
   *
   * Works for a synchronous or an async body, and returns whichever the body
   * returned: an async body yields a promise the caller awaits normally, and
   * the trace closes when that promise settles rather than when the function
   * returns. A throw is recorded on the trace and then re-thrown untouched.
   */
  trace<T>(name: string, body: TracedBody<T>): T;
  trace<T>(options: TraceOptions, body: TracedBody<T>): T;
  trace<T>(nameOrOptions: string | TraceOptions, body: TracedBody<T>): T {
    const trace = this.startTrace(nameOrOptions);
    return this.context.run({ trace }, () => this.runUnit(trace, body));
  }

  /**
   * Open a span by hand, beneath whatever is currently in scope.
   *
   * With no trace in scope and no explicit `parent`, a trace is opened to hold
   * the span — a stray span with no run to belong to would be dropped by the
   * ingest path, and an implicit trace is more useful than nothing.
   */
  startSpan(nameOrOptions: string | SpanOptions = {}): Span {
    const options: SpanOptions = typeof nameOrOptions === 'string' ? { name: nameOrOptions } : { ...nameOrOptions };
    const explicitParent = options.parent;

    if (explicitParent instanceof Span) return explicitParent.startSpan(options);
    if (explicitParent instanceof Trace) return explicitParent.startSpan(options);

    const activeSpan = this.currentSpan();
    if (activeSpan) return activeSpan.startSpan(options);

    const activeTrace = this.currentTrace();
    if (activeTrace) return activeTrace.startSpan(options);

    const implicit = this.startTrace({ name: options.name ?? 'span', agent: options.agent });
    const span = implicit.startSpan(options);
    // The implicit trace closes with its only span, so the run is complete.
    const originalEnd = span.end.bind(span);
    span.end = (endOptions: EndOptions = {}) => {
      originalEnd(endOptions);
      implicit.end({ output: endOptions.output, error: endOptions.error, endTime: endOptions.endTime });
      return span;
    };
    return span;
  }

  /** Run `body` inside a span. Same return and throw semantics as `trace()`. */
  span<T>(name: string, body: SpanBody<T>): T;
  span<T>(options: SpanOptions, body: SpanBody<T>): T;
  span<T>(nameOrOptions: string | SpanOptions, body: SpanBody<T>): T {
    const span = this.startSpan(nameOrOptions);
    const active = this.context.active();
    const trace = active?.trace ?? this.currentTrace();
    return this.context.run({ trace, span }, () => this.runUnit(span, body));
  }

  /**
   * Wrap a function so every call to it is traced.
   *
   * The wrapper keeps the original's arity and name, so it can be dropped in
   * where the original was without anything downstream noticing.
   */
  traced<A extends unknown[], R>(
    fn: (...args: A) => R,
    options: (TraceOptions & { asSpan?: boolean }) | string = {},
  ): (...args: A) => R {
    const settings = typeof options === 'string' ? { name: options } : { ...options };
    const client = this;
    // `fn.name` is `''` for an anonymous function expression, and `??` would
    // keep the empty string; `||` is what falls through to the fallback.
    const name = settings.name ?? (fn.name || 'anonymous');

    const wrapper = function (this: unknown, ...args: A): R {
      const unitOptions = { ...settings, name };
      // Only capture arguments when the client is recording input at all;
      // serialising them otherwise is pure cost.
      if (client.effectiveCaptureInput && settings.input === undefined && args.length > 0) {
        unitOptions.input = args.length === 1 ? args[0] : { args };
      }
      const body = () => fn.apply(this, args) as R;
      // A nested `traced` function becomes a span of the trace above it, which
      // is what makes decorating a whole call graph produce one tree.
      const nestUnderCurrent = settings.asSpan ?? client.currentTrace() !== undefined;
      return nestUnderCurrent
        ? client.span(unitOptions as SpanOptions, body)
        : client.trace(unitOptions as TraceOptions, body);
    };

    Object.defineProperty(wrapper, 'name', { value: name, configurable: true });
    Object.defineProperty(wrapper, 'length', { value: fn.length, configurable: true });
    return wrapper as (...args: A) => R;
  }

  /**
   * Run a unit's body, closing it on the way out.
   *
   * The two paths are not interchangeable: a synchronous body must close before
   * the value is returned, and an async body must close when its promise
   * settles. Everything the SDK does here is wrapped, so a failure in `end()`
   * cannot turn a working function into a throwing one.
   */
  private runUnit<T, U extends Trace | Span>(unit: U, body: (unit: U) => T): T {
    let result: T;
    try {
      result = body(unit);
    } catch (thrown) {
      this.safely('trace:end', () => unit.end({ error: thrown }));
      throw thrown;
    }

    if (isThenable(result)) {
      return (result as unknown as Promise<unknown>).then(
        (value) => {
          this.safely('trace:end', () => unit.end({ output: value }));
          return value;
        },
        (error: unknown) => {
          this.safely('trace:end', () => unit.end({ error }));
          throw error;
        },
      ) as unknown as T;
    }

    this.safely('trace:end', () => unit.end({ output: result }));
    return result;
  }

  private shouldSample(): boolean {
    const rate = this.effectiveSamplingRate;
    if (rate >= 1) return true;
    if (rate <= 0) return false;
    return Math.random() < rate;
  }

  // -------------------------------------------------------------------------
  // Feedback scores
  // -------------------------------------------------------------------------

  /**
   * Attach a feedback score to a trace, span or thread.
   *
   * Posted separately from the run it describes because it usually arrives
   * later — a thumbs-down minutes after the answer, a judge's verdict after an
   * offline pass.
   */
  score(score: FeedbackScoreOptions): void {
    if (!this.options.enabled || this.closed) return;
    const id = clampText(score.id, MAX_ID_LENGTH);
    const name = clampText(score.name, MAX_SCORE_NAME_LENGTH);
    if (!id || !name || !Number.isFinite(score.value)) {
      this.report('score', new FulcrumOpsError('A feedback score needs an id, a name and a numeric value.'));
      return;
    }
    const payload: ScoreIn = { id, name, value: score.value, target: score.target ?? 'trace' };
    const category = clampText(score.categoryName, MAX_SCORE_CATEGORY_LENGTH);
    if (category) payload.category_name = category;
    const reason = clampText(score.reason, MAX_SCORE_REASON_LENGTH);
    if (reason) payload.reason = reason;
    payload.source = clampText(score.source, MAX_SCORE_SOURCE_LENGTH) ?? 'sdk';
    const agent = clampText(score.agent ?? this.defaultAgent(), MAX_AGENT_LENGTH);
    if (agent) payload.agent = agent;
    this.queue.enqueue('scores', payload);
  }

  /** Attach several scores at once. */
  scores(scores: readonly FeedbackScoreOptions[]): void {
    for (const score of scores) this.score(score);
  }

  /** Score whatever is currently in scope, without naming its id. */
  scoreCurrent(score: Omit<FeedbackScoreOptions, 'id' | 'target'>): void {
    const span = this.currentSpan();
    const trace = this.currentTrace();
    const target = span ?? trace;
    if (!target) {
      this.report('score', new FulcrumOpsError('No trace or span is in scope to score.'));
      return;
    }
    // Attach in place when the unit is still open, so the score travels with
    // the item instead of costing a second request.
    if (!target.isEnded) {
      target.score(score as ScoreInput);
      return;
    }
    this.score({ ...score, id: target.id, target: span ? 'span' : 'trace' });
  }

  // -------------------------------------------------------------------------
  // Governance events
  // -------------------------------------------------------------------------

  private submitEvent(event: EventIn): void {
    if (!this.options.enabled || this.closed) return;
    const agent = clampText(event.agent ?? this.defaultAgent(), MAX_AGENT_LENGTH);
    if (agent) event.agent = agent;
    this.queue.enqueue('events', event);
  }

  private currentIds(traceId?: string, spanId?: string): { trace_id?: string; span_id?: string } {
    const out: { trace_id?: string; span_id?: string } = {};
    const span = this.currentSpan();
    // A span opened with no trace in scope gets an implicit trace that never
    // enters the context, so the span's own `traceId` is the only way to
    // attribute the event to a run rather than leaving it orphaned.
    const resolvedTrace = traceId ?? this.currentTrace()?.id ?? span?.traceId;
    const resolvedSpan = spanId ?? span?.id;
    if (resolvedTrace) out.trace_id = resolvedTrace;
    if (resolvedSpan) out.span_id = resolvedSpan;
    return out;
  }

  /** Record a guardrail firing in the customer's own process. */
  guardrailTriggered(options: GuardrailEventOptions): void {
    const guardrail = clampText(options.guardrail, MAX_GUARDRAIL_LENGTH);
    if (!guardrail) {
      this.report('guardrailTriggered', new FulcrumOpsError('A guardrail event needs a guardrail id or name.'));
      return;
    }
    const event: EventIn = {
      kind: 'guardrail.triggered',
      guardrail,
      ...this.currentIds(options.traceId, options.spanId),
    };
    const actionTaken = clampText(options.actionTaken, MAX_ACTION_TAKEN_LENGTH);
    if (actionTaken) event.action_taken = actionTaken;
    if (Number.isFinite(options.score as number)) event.score = options.score;
    const matched = normaliseMetadata(options.matched);
    if (matched) event.matched = matched;
    if (options.sample) {
      // The sample is a fragment of the content that tripped the rule, so it is
      // the single most likely field to carry exactly what redaction exists to
      // remove. Trim first, then redact — and trim again afterwards, because a
      // replacement token is usually longer than the value it replaced and can
      // push a sample that was just inside the limit back over it.
      const trimmed = options.sample.slice(0, MAX_SAMPLE_LENGTH);
      event.sample = this.redactText(trimmed).slice(0, MAX_SAMPLE_LENGTH);
    }
    if (options.occurredAt) event.occurred_at = toIso(options.occurredAt) ?? nowIso();
    const ref = clampText(options.ref, MAX_REF_LENGTH);
    if (ref) event.ref = ref;
    const agent = clampText(options.agent, MAX_AGENT_LENGTH);
    if (agent) event.agent = agent;
    this.submitEvent(event);
  }

  /** Record a policy the agent breached. */
  policyViolation(options: PolicyViolationOptions): void {
    const policy = clampText(options.policy, MAX_POLICY_LENGTH);
    if (!policy) {
      this.report('policyViolation', new FulcrumOpsError('A policy violation needs a policy id or name.'));
      return;
    }
    const event: EventIn = {
      kind: 'policy.violation',
      policy,
      ...this.currentIds(options.traceId, options.spanId),
    };
    const severity = clampText(options.severity, MAX_SEVERITY_LENGTH);
    if (severity) event.severity = severity;
    const actionTaken = clampText(options.actionTaken, MAX_ACTION_TAKEN_LENGTH);
    if (actionTaken) event.action_taken = actionTaken;
    const detail = normaliseMetadata(options.detail);
    if (detail) event.detail = detail as JsonObject;
    if (options.occurredAt) event.occurred_at = toIso(options.occurredAt) ?? nowIso();
    const ref = clampText(options.ref, MAX_REF_LENGTH);
    if (ref) event.ref = ref;
    const agent = clampText(options.agent, MAX_AGENT_LENGTH);
    if (agent) event.agent = agent;
    this.submitEvent(event);
  }

  /**
   * Record end-user feedback captured in the product.
   *
   * `rating` is a 1–5 star count. A value outside that range is clamped into it
   * rather than sent: the contract rejects the row, and a thumbs-up recorded as
   * `1` is worth more than a rejected row nobody sees.
   */
  submitFeedback(options: FeedbackEventOptions): void {
    const event: EventIn = {
      kind: 'feedback.submitted',
      ...this.currentIds(options.traceId, options.spanId),
    };
    if (Number.isFinite(options.rating as number)) {
      const rounded = Math.round(options.rating as number);
      event.rating = Math.min(MAX_RATING, Math.max(MIN_RATING, rounded));
    }
    const sentiment = clampText(options.sentiment, MAX_SENTIMENT_LENGTH);
    if (sentiment) event.sentiment = sentiment;
    if (options.body) event.body = this.redactText(options.body).slice(0, MAX_EVENT_BODY_LENGTH);
    const source = clampText(options.source, MAX_EVENT_SOURCE_LENGTH);
    if (source) event.source = source;
    const submittedBy = clampText(options.submittedBy, MAX_SUBMITTED_BY_LENGTH);
    if (submittedBy) event.submitted_by = submittedBy;
    if (options.occurredAt) event.occurred_at = toIso(options.occurredAt) ?? nowIso();
    const ref = clampText(options.ref, MAX_REF_LENGTH);
    if (ref) event.ref = ref;
    const agent = clampText(options.agent, MAX_AGENT_LENGTH);
    if (agent) event.agent = agent;
    this.submitEvent(event);
  }

  private redactText(text: string): string {
    return applyRedactionToText(text, this.compiledRedaction, 'input');
  }

  // -------------------------------------------------------------------------
  // Bootstrap configuration
  // -------------------------------------------------------------------------

  /**
   * Fetch `GET /ingest/config` and adopt what it says.
   *
   * Cached for the lifetime the document itself declares
   * (`refresh_after_seconds`) and revalidated with an `If-None-Match`, which is
   * how a fleet restart costs one conditional request per process rather than a
   * full read each. Concurrent callers share one in-flight request.
   *
   * The adopted values are floors, not overrides: a caller who asked for a
   * 10% sampling rate keeps it even if the deployment allows 100%, because the
   * caller's number is a preference the deployment has no reason to widen.
   */
  async config(options: ConfigOptions = {}): Promise<IngestConfig | undefined> {
    if (!this.options.enabled) return undefined;
    if (!options.refresh && this.cachedConfig && Date.now() < this.configExpiresAt) {
      return this.cachedConfig;
    }
    if (this.configInFlight) return this.configInFlight;

    this.configInFlight = (async () => {
      try {
        const headers: Record<string, string> = {};
        if (this.configEtag && !options.refresh) headers['if-none-match'] = this.configEtag;

        const response = await this.transport.requestRaw<IngestConfig>({
          method: 'GET',
          path: '/ingest/config',
          headers,
          allowNotModified: true,
        });

        if (response.status === 304 && this.cachedConfig) {
          this.configExpiresAt = Date.now() + this.cachedConfig.refresh_after_seconds * 1000;
          return this.cachedConfig;
        }

        const config = response.data;
        if (!config) return this.cachedConfig;

        this.configEtag = response.headers.get('etag') ?? this.configEtag;
        this.cachedConfig = config;
        this.configExpiresAt = Date.now() + Math.max(1, config.refresh_after_seconds) * 1000;
        this.applyConfig(config);
        return config;
      } catch (thrown) {
        const error = toFulcrumError(thrown, 'Could not fetch the SDK configuration.');
        this.report('config', error);
        // Retry sooner than the document's own lifetime would allow, but not on
        // every trace — a control plane that is down should not be hammered.
        this.configExpiresAt = Date.now() + 30_000;
        if (this.cachedConfig) return this.cachedConfig;
        throw error;
      } finally {
        this.configInFlight = undefined;
      }
    })();

    return this.configInFlight;
  }

  /** The most recently fetched bootstrap document, without triggering a fetch. */
  getCachedConfig(): IngestConfig | undefined {
    return this.cachedConfig;
  }

  private applyConfig(config: IngestConfig): void {
    // Server-side redaction rules come first so a workspace rule cannot be
    // shadowed by a locally configured one with the same shape.
    this.compiledRedaction = compileRedactionRules([...(config.redaction ?? []), ...this.options.redaction]);

    if (Number.isFinite(config.sampling_rate)) {
      this.effectiveSamplingRate = Math.min(this.options.samplingRate, Math.max(0, Math.min(1, config.sampling_rate)));
    }
    if (config.capture_input === false) this.effectiveCaptureInput = false;
    if (config.capture_output === false) this.effectiveCaptureOutput = false;
    if (config.agent_bound && config.agent_name) this.boundAgent = config.agent_name;

    this.queue.reconfigure({
      maxItems: Math.min(this.options.batch.maxItems, Math.max(1, config.batch_max_spans)),
      maxBytes: Math.min(this.options.batch.maxBytes, Math.max(1_024, config.batch_max_bytes)),
      flushIntervalMs: Math.min(this.options.batch.flushIntervalMs, Math.max(50, config.flush_interval_seconds * 1000)),
      maxQueueSize: Math.min(this.options.batch.maxQueueSize, Math.max(1, config.max_queue_size)),
    });

    this.debug(
      `config revision ${config.revision}: sampling ${this.effectiveSamplingRate}, ${config.redaction?.length ?? 0} redaction rule(s), ${config.guardrails?.length ?? 0} guardrail(s)`,
    );
  }

  /** Quota states from the most recent batch, as the ingest path reported them. */
  getQuotas(): readonly IngestQuotaState[] {
    return this.latestQuotas;
  }

  private absorbResult(kind: QueueKind, result: IngestBatchResult): void {
    if (result.quotas?.length) this.latestQuotas = result.quotas;
    if (result.auto_registered?.length) {
      for (const agent of result.auto_registered) {
        this.debug(`ingest registered a new agent: ${agent.name} (${agent.id}) in ${agent.environment}`);
      }
    }
    // A rejected row is data the caller believed they had reported. It never
    // interrupts them, but it must not vanish either.
    const failures = (result.results ?? []).filter((item) => item.outcome !== 'accepted');
    for (const failure of failures) {
      this.report(
        `ingest:${kind}`,
        new FulcrumOpsError(
          failure.reason ?? `Item ${failure.index} was ${failure.outcome}.`,
          {
            code: failure.code ?? failure.outcome,
            details: { kind, index: failure.index, policyId: failure.policy_id, guardrailId: failure.guardrail_id },
          },
        ),
      );
    }
  }

  // -------------------------------------------------------------------------
  // Lifecycle
  // -------------------------------------------------------------------------

  /** Send everything queued and wait for it. Never throws. */
  async flush(): Promise<void> {
    try {
      await this.queue.flush();
    } catch (thrown) {
      this.report('flush', toFulcrumError(thrown, 'Flush failed.'));
    }
  }

  /**
   * Flush, stop the timers, and release the exit hooks.
   *
   * After this the client is inert: further calls are no-ops rather than
   * errors, because a shutdown path that traces one last thing should not
   * become the reason a process fails to exit cleanly.
   */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    for (const unregister of this.unregisterExitHooks.splice(0)) unregister();
    try {
      await this.queue.close();
    } catch (thrown) {
      this.report('close', toFulcrumError(thrown, 'Close failed.'));
    }
    if (defaultClient === this) defaultClient = undefined;
  }

  /** Counters for a health endpoint or a test. */
  getStats(): ClientStats {
    return { ...this.queue.getStats(), sampledOut: this.sampledOut, contextBackend: this.context.backend };
  }

  /**
   * Flush what is queued when the host is going away.
   *
   * On Node that is `beforeExit`, which still allows an await — deliberately
   * not `SIGINT`/`SIGTERM`, because installing a handler for those stops the
   * default "terminate now" behaviour the host expects, and a telemetry library
   * has no business changing how an application responds to a kill signal.
   *
   * In a browser it is `pagehide`, where nothing can be awaited at all; the
   * flush goes out with `keepalive` so the host finishes it after the document
   * is gone.
   */
  private installExitHooks(): void {
    this.unregisterExitHooks.push(
      onProcessExit(() => {
        void this.flush();
      }),
    );
    this.unregisterExitHooks.push(
      onPageHide(() => {
        void this.queue.flush({ keepalive: true }).catch(() => undefined);
      }),
    );
  }

  /** Run an SDK-internal step, routing any failure to `onError`. */
  private safely(operation: string, action: () => void): void {
    try {
      action();
    } catch (thrown) {
      this.report(operation, toFulcrumError(thrown, `${operation} failed.`));
    }
  }

  private report(operation: string, error: FulcrumOpsError): void {
    this.debug(`${operation}: ${error.message}`);
    if (!this.options.onError) return;
    try {
      this.options.onError(error, { operation });
    } catch {
      // A throwing error handler is the caller's bug; it does not get to break
      // the path that was reporting to it.
    }
  }

  private debug(message: string): void {
    if (!this.options.debug) return;
    // eslint-disable-next-line no-console
    console.debug(`[fulcrum-ops] ${message}`);
  }
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (
    value !== null &&
    (typeof value === 'object' || typeof value === 'function') &&
    typeof (value as PromiseLike<unknown>).then === 'function'
  );
}

/**
 * Wrap a function so every call is traced by the default client.
 *
 * The module-level twin of `client.traced()`, for code that does not have the
 * client to hand. With no client configured it returns the function unchanged,
 * so a library can decorate its own functions without forcing its consumers to
 * set up telemetry.
 */
export function traced<A extends unknown[], R>(
  fn: (...args: A) => R,
  options: (TraceOptions & { asSpan?: boolean; client?: FulcrumOps }) | string = {},
): (...args: A) => R {
  const { client: explicit, ...settings } =
    typeof options === 'string' ? { client: undefined, name: options } : { ...options };
  const name = settings.name ?? (fn.name || 'anonymous');

  // Which client to report to is only known at call time — the default may be
  // set after this wrapper is built, which is the whole reason this function
  // exists. The per-client wrapper is therefore memoised rather than rebuilt on
  // every call, so decorating a hot function costs one map lookup.
  const perClient = new WeakMap<FulcrumOps, (...args: A) => R>();

  const wrapper = function (this: unknown, ...args: A): R {
    const client = explicit ?? defaultClient;
    if (!client) return fn.apply(this, args);
    let bound = perClient.get(client);
    if (!bound) {
      bound = client.traced(fn, settings);
      perClient.set(client, bound);
    }
    return bound.apply(this, args);
  };

  // Same identity contract as `client.traced()`: a decorated function still
  // reports its own name and arity. Callers dispatch on both — Express reads
  // middleware arity to tell a handler from an error handler, and DI containers
  // read the name — so losing them changes behaviour, which telemetry may not.
  Object.defineProperty(wrapper, 'name', { value: name, configurable: true });
  Object.defineProperty(wrapper, 'length', { value: fn.length, configurable: true });
  return wrapper as (...args: A) => R;
}
