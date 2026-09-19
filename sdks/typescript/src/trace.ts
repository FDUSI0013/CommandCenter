/**
 * `Trace` and `Span` — the two objects a caller actually holds.
 *
 * Both are mutable builders that convert to the wire contract exactly once, on
 * `end()`. That ordering matters: it means a caller can set an output, attach a
 * score, add a tag and correct the model name in any order, at any point before
 * the unit closes, without the SDK sending four partial updates.
 *
 * A trace owns its spans and is posted as a single nested document, which is
 * what `POST /ingest/traces` wants and what keeps a run atomic in the console.
 * The exception is `streamSpans`, where each span goes to `POST /ingest/spans`
 * as it closes — for traces long enough that waiting for the end would mean
 * seeing nothing for minutes.
 */

import { isValidId, newId, nowIso, toIso } from './ids.js';
import {
  MAX_AGENT_LENGTH,
  MAX_MODEL_LENGTH,
  MAX_NAME_LENGTH,
  MAX_PROVIDER_LENGTH,
  MAX_SCORES_PER_ITEM,
  MAX_SCORE_CATEGORY_LENGTH,
  MAX_SCORE_NAME_LENGTH,
  MAX_SCORE_REASON_LENGTH,
  MAX_SCORE_SOURCE_LENGTH,
  MAX_SPANS_PER_TRACE,
  MAX_THREAD_ID_LENGTH,
  clampText,
} from './limits.js';
import { applyRedaction } from './redaction.js';
import { unrefTimer } from './runtime.js';
import { normaliseMetadata, normaliseTags, normaliseUsage, toErrorInfo, toJsonObject } from './serialize.js';
import type { CompiledRedactionRule } from './redaction.js';
import type {
  ErrorInfoIn,
  FeedbackScoreIn,
  JsonObject,
  SpanIn,
  SpanType,
  TraceIn,
} from './types.js';

/** What the trace and span builders need from the client, and nothing more. */
export interface TraceSink {
  /** Post a finished trace, spans nested inside it. */
  submitTrace(trace: TraceIn): void;
  /** Post one span on its own, for `streamSpans`. */
  submitSpan(span: SpanIn): void;
  /** Redaction rules currently in force. */
  redactionRules(): readonly CompiledRedactionRule[];
  /** Whether `input` / `output` are recorded at all. */
  captureInput(): boolean;
  captureOutput(): boolean;
  /** Post spans as they close rather than nesting them. */
  streamSpans(): boolean;
  /** Default agent for items that do not name one. */
  defaultAgent(): string | undefined;
  /**
   * A closed trace is waiting on a deferred span before it reports. Optional:
   * a sink that does not track them only loses the ability to push them out
   * early on shutdown.
   */
  holdTrace?(trace: Trace): void;
  /** The trace from `holdTrace` has now reported. */
  releaseTrace?(trace: Trace): void;
}

/**
 * How long a closed trace waits for a deferred span before reporting without it.
 *
 * Long enough for any generation a person is still reading; short enough that a
 * stream somebody abandoned without closing does not cost the run.
 */
export const DEFERRED_SPAN_GRACE_MS = 5 * 60_000;

/** A score attached to a trace or span before it closes. */
export interface ScoreInput {
  name: string;
  value: number;
  categoryName?: string;
  reason?: string;
  source?: string;
}

/** What every unit of work accepts. */
interface CommonOptions {
  name?: string;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
  tags?: string[];
  agent?: string;
  startTime?: Date | number | string;
  /**
   * Supply an id to make a retry idempotent. Must be a version 7 UUID — mint it
   * with `newId()`. The telemetry store takes no other kind, and refuses the
   * whole request over one that is not, so anything else (a `randomUUID()`
   * included) is replaced; read the id actually in use back from `.id`.
   */
  id?: string;
}

/** Options for opening a trace. */
export interface TraceOptions extends CommonOptions {
  /** Groups traces into one conversation in Memory & State. */
  threadId?: string;
  /** Skip reporting this trace entirely. */
  sampled?: boolean;
}

/** Options for opening a span. */
export interface SpanOptions extends CommonOptions {
  type?: SpanType;
  model?: string;
  provider?: string;
  usage?: Record<string, number>;
  cost?: number;
  /** Attach to this parent explicitly, instead of to whatever is in scope. */
  parent?: Trace | Span;
}

/** What `end()` accepts, for the values only known at the end. */
export interface EndOptions {
  output?: unknown;
  error?: unknown;
  endTime?: Date | number | string;
  metadata?: Record<string, unknown>;
  usage?: Record<string, number>;
  cost?: number;
  model?: string;
}

function normaliseScore(score: ScoreInput): FeedbackScoreIn | undefined {
  const name = clampText(score.name, MAX_SCORE_NAME_LENGTH);
  if (!name || !Number.isFinite(score.value)) return undefined;
  const out: FeedbackScoreIn = { name, value: score.value };
  const category = clampText(score.categoryName, MAX_SCORE_CATEGORY_LENGTH);
  if (category) out.category_name = category;
  const reason = clampText(score.reason, MAX_SCORE_REASON_LENGTH);
  if (reason) out.reason = reason;
  // Lower-cased: the store's provenance enum is lower case, and a value it does
  // not recognise costs the whole score request rather than the one score.
  const source = clampText(score.source, MAX_SCORE_SOURCE_LENGTH)?.toLowerCase();
  if (source) out.source = source;
  return out;
}

function validId(candidate: string | undefined): string {
  return candidate && isValidId(candidate) ? candidate : newId();
}

/** Shared state and behaviour of traces and spans. */
abstract class Unit {
  readonly id: string;
  name: string;
  readonly startTime: string;
  protected endTime: string | undefined;
  protected input: JsonObject | undefined;
  protected output: JsonObject | undefined;
  protected metadata: Record<string, unknown> = {};
  protected tags: string[] = [];
  protected errorInfo: ErrorInfoIn | undefined;
  protected scores: FeedbackScoreIn[] = [];
  protected agent: string | undefined;
  protected ended = false;

  constructor(protected readonly sink: TraceSink, options: CommonOptions) {
    this.id = validId(options.id);
    this.name = clampText(options.name, MAX_NAME_LENGTH) ?? 'unnamed';
    this.startTime = toIso(options.startTime) ?? nowIso();
    this.agent = clampText(options.agent, MAX_AGENT_LENGTH);
    if (options.input !== undefined) this.setInput(options.input);
    if (options.output !== undefined) this.setOutput(options.output);
    if (options.metadata) this.setMetadata(options.metadata);
    if (options.tags) this.addTags(...options.tags);
  }

  /** Whether this unit has already been closed. */
  get isEnded(): boolean {
    return this.ended;
  }

  /** Record what went in. Ignored when `captureInput` is off. */
  setInput(value: unknown): this {
    if (this.sink.captureInput()) this.input = toJsonObject(value);
    return this;
  }

  /** Record what came out. Ignored when `captureOutput` is off. */
  setOutput(value: unknown): this {
    if (this.sink.captureOutput()) this.output = toJsonObject(value);
    return this;
  }

  /** Merge keys into the metadata document. */
  setMetadata(values: Record<string, unknown>): this {
    Object.assign(this.metadata, values);
    return this;
  }

  /** Add tags, de-duplicated on conversion. */
  addTags(...tags: string[]): this {
    this.tags.push(...tags);
    return this;
  }

  /** Attach a feedback score that travels with this item. */
  score(score: ScoreInput): this {
    const normalised = normaliseScore(score);
    if (normalised && this.scores.length < MAX_SCORES_PER_ITEM) this.scores.push(normalised);
    return this;
  }

  /** Record a failure without closing the unit. */
  setError(thrown: unknown): this {
    this.errorInfo = toErrorInfo(thrown);
    return this;
  }

  /**
   * @internal Take on what `other` recorded, wherever this unit has nothing of
   * its own.
   *
   * For the trace the client opens around a span that had no run to belong to:
   * that span is the whole run, and the trace is the row the console lists, so
   * an input, output or failure set on the span only — `span.setInput(...)`
   * after it opened, `span.end({ output })` — has to show on the trace too.
   */
  adopt(other: Unit): this {
    this.input ??= other.input;
    this.output ??= other.output;
    this.errorInfo ??= other.errorInfo;
    return this;
  }

  /** Which agent this item belongs to. */
  setAgent(agent: string): this {
    this.agent = clampText(agent, MAX_AGENT_LENGTH);
    return this;
  }

  /** The agent to write on the wire: this item's, or the client's default. */
  protected resolvedAgent(): string | undefined {
    return clampText(this.agent ?? this.sink.defaultAgent(), MAX_AGENT_LENGTH);
  }

  protected applyEnd(options: EndOptions): void {
    if (options.output !== undefined) this.setOutput(options.output);
    if (options.error !== undefined) this.setError(options.error);
    if (options.metadata) this.setMetadata(options.metadata);
    this.endTime = toIso(options.endTime) ?? nowIso();
  }

  protected redactedInput(): JsonObject | undefined {
    return applyRedaction(this.input, this.sink.redactionRules(), 'input');
  }

  protected redactedOutput(): JsonObject | undefined {
    return applyRedaction(this.output, this.sink.redactionRules(), 'output');
  }

  protected redactedMetadata(): JsonObject | undefined {
    const normalised = normaliseMetadata(this.metadata);
    return applyRedaction(normalised, this.sink.redactionRules(), 'metadata');
  }
}

/** One unit of work inside a trace. */
export class Span extends Unit {
  readonly traceId: string;
  readonly parentSpanId: string | undefined;
  private type: SpanType;
  private model: string | undefined;
  private provider: string | undefined;
  private usage: Record<string, number> | undefined;
  private cost: number | undefined;
  private deferred = false;
  private readonly children: Span[] = [];

  constructor(
    sink: TraceSink,
    traceId: string,
    parentSpanId: string | undefined,
    options: SpanOptions,
    private readonly owner: Trace | undefined,
  ) {
    super(sink, options);
    this.traceId = traceId;
    this.parentSpanId = parentSpanId;
    this.type = options.type ?? 'general';
    this.model = clampText(options.model, MAX_MODEL_LENGTH);
    this.provider = clampText(options.provider, MAX_PROVIDER_LENGTH);
    this.usage = normaliseUsage(options.usage);
    if (options.cost !== undefined) this.setCost(options.cost);
  }

  /** Change what kind of work this span represents. */
  setType(type: SpanType): this {
    this.type = type;
    return this;
  }

  /** Record the model and provider, which drive the per-model cost breakdown. */
  setModel(model: string, provider?: string): this {
    this.model = clampText(model, MAX_MODEL_LENGTH);
    if (provider) this.provider = clampText(provider, MAX_PROVIDER_LENGTH);
    return this;
  }

  /** The trace this span belongs to, when it was opened through one. */
  get ownerTrace(): Trace | undefined {
    return this.owner;
  }

  /** What kind of work this span represents. */
  get spanType(): SpanType {
    return this.type;
  }

  /** The model recorded on this span so far, if any. */
  get modelName(): string | undefined {
    return this.model;
  }

  /** Record token counters. Provider-native key names are preserved. */
  setUsage(usage: Record<string, number>): this {
    this.usage = normaliseUsage({ ...(this.usage ?? {}), ...usage });
    return this;
  }

  /** Record what this span cost, in the workspace's currency. */
  setCost(cost: number): this {
    if (Number.isFinite(cost) && cost >= 0) this.cost = cost;
    return this;
  }

  /** Open a child span beneath this one. */
  startSpan(options: SpanOptions): Span {
    const child = new Span(this.sink, this.traceId, this.id, options, this.owner);
    this.children.push(child);
    this.owner?.register(child);
    return child;
  }

  /**
   * Say that this span will be closed by something that outlives the code
   * which opened it.
   *
   * A streamed model call is the case: the function that made the call returns
   * the stream and its trace closes, while the generation — and the tokens,
   * the text and the real duration — are still to come. A trace closes every
   * span left open, so without this the span is cut off at the hand-over and
   * everything learned afterwards is discarded. A deferred span is left
   * running instead, and the trace reports once it has ended.
   */
  defer(): this {
    if (!this.ended) this.deferred = true;
    return this;
  }

  /** Whether `defer()` was called on this span. */
  get isDeferred(): boolean {
    return this.deferred;
  }

  /** Close the span. A second call is a no-op. */
  end(options: EndOptions = {}): this {
    if (this.ended) return this;
    if (options.usage) this.setUsage(options.usage);
    if (options.cost !== undefined) this.setCost(options.cost);
    if (options.model) this.setModel(options.model);
    this.applyEnd(options);
    this.ended = true;
    // Sampling is decided once, on the trace, and a span goes where its trace
    // goes. Posting the spans of a run that was sampled out leaves orphans in
    // the store that no trace will ever claim, and each one is charged to the
    // ingest quota — so a 10% sampling rate would still cost 100% of the spans.
    if (this.sink.streamSpans() && this.owner?.sampled !== false) this.sink.submitSpan(this.toWire());
    if (this.deferred) this.owner?.deferredSpanEnded(this);
    return this;
  }

  /** The contract representation of this span. */
  toWire(): SpanIn {
    const wire: SpanIn = {
      id: this.id,
      trace_id: this.traceId,
      name: this.name,
      type: this.type,
      start_time: this.startTime,
    };
    if (this.parentSpanId) wire.parent_span_id = this.parentSpanId;
    if (this.endTime) wire.end_time = this.endTime;
    const input = this.redactedInput();
    if (input) wire.input = input;
    const output = this.redactedOutput();
    if (output) wire.output = output;
    if (this.usage) wire.usage = this.usage;
    if (this.model) wire.model = this.model;
    if (this.provider) wire.provider = this.provider;
    if (this.cost !== undefined) wire.total_estimated_cost = this.cost;
    if (this.errorInfo) wire.error_info = this.errorInfo;
    if (this.scores.length > 0) wire.feedback_scores = this.scores;
    const metadata = this.redactedMetadata();
    if (metadata) wire.metadata = metadata;
    const tags = normaliseTags(this.tags);
    if (tags) wire.tags = tags;
    const agent = this.resolvedAgent();
    if (agent) wire.agent = agent;
    return wire;
  }
}

/** One end-to-end agent invocation. */
export class Trace extends Unit {
  private conversation: string | undefined;
  private readonly spans: Span[] = [];
  /** Deferred spans a closed trace is still waiting on before it reports. */
  private readonly waitingOn = new Set<Span>();
  private holdTimer: ReturnType<typeof setTimeout> | undefined;
  private reported = false;
  /** False when sampling decided this trace is not reported. */
  readonly sampled: boolean;

  constructor(sink: TraceSink, options: TraceOptions) {
    super(sink, options);
    this.conversation = clampText(options.threadId, MAX_THREAD_ID_LENGTH);
    this.sampled = options.sampled ?? true;
  }

  /** Group this trace with others into one conversation. */
  setThreadId(threadId: string): this {
    this.conversation = clampText(threadId, MAX_THREAD_ID_LENGTH);
    return this;
  }

  /**
   * The conversation this trace is filed under, once it has been told one.
   *
   * Readable because the step that knows the conversation is not always the
   * one that opened the run: a `traced()` helper nested inside it names the
   * thread only for a run that does not have one yet, and has to be able to ask.
   */
  get threadId(): string | undefined {
    return this.conversation;
  }

  /** Every span opened under this trace, in creation order. */
  get spanCount(): number {
    return this.spans.length;
  }

  /** Called by `Span` so a trace collects its whole subtree, not just its children. */
  register(span: Span): void {
    if (this.spans.length < MAX_SPANS_PER_TRACE) this.spans.push(span);
  }

  /** Open a span directly beneath the trace. */
  startSpan(options: SpanOptions): Span {
    const parent = options.parent;
    if (parent instanceof Span) return parent.startSpan(options);
    const span = new Span(this.sink, this.id, undefined, options, this);
    this.register(span);
    return span;
  }

  /**
   * Close the trace and report it.
   *
   * Any span still open is closed first, with the trace's end time — an
   * unclosed span would otherwise arrive with a null `end_time` and show as a
   * zero-duration step in Replay Studio.
   *
   * The exception is a span that was `defer()`red: it is still doing the work
   * it describes (a stream being read after the function that opened it has
   * returned), so the trace holds its report until that span ends, or until
   * `DEFERRED_SPAN_GRACE_MS` has passed, whichever comes first.
   */
  end(options: EndOptions = {}): this {
    if (this.ended) return this;
    this.applyEnd(options);
    this.ended = true;
    for (const span of this.spans) {
      if (span.isEnded) continue;
      if (span.isDeferred && this.sampled) this.waitingOn.add(span);
      else span.end({ endTime: this.endTime });
    }
    if (this.waitingOn.size === 0) return this.report();

    this.holdTimer = setTimeout(() => this.release(), DEFERRED_SPAN_GRACE_MS);
    // Waiting for a stream must never be the reason a process stays alive; the
    // client's exit hook releases whatever is still held.
    unrefTimer(this.holdTimer);
    this.sink.holdTrace?.(this);
    return this;
  }

  /** @internal Called by a deferred span as it ends. */
  deferredSpanEnded(span: Span): void {
    if (!this.waitingOn.delete(span)) return;
    if (this.waitingOn.size === 0) this.release();
  }

  /**
   * Report a held trace now, closing whatever it was still waiting on.
   *
   * A no-op for a trace that is open, was never held, or has already reported.
   */
  release(): this {
    if (!this.ended || this.reported) return this;
    if (this.holdTimer !== undefined) clearTimeout(this.holdTimer);
    this.holdTimer = undefined;
    const waiting = Array.from(this.waitingOn);
    this.waitingOn.clear();
    for (const span of waiting) span.end();
    this.sink.releaseTrace?.(this);
    return this.report();
  }

  private report(): this {
    if (this.reported) return this;
    this.reported = true;
    if (this.sampled) this.sink.submitTrace(this.toWire());
    return this;
  }

  /** The contract representation of this trace, spans nested inside it. */
  toWire(): TraceIn {
    const wire: TraceIn = {
      id: this.id,
      name: this.name,
      start_time: this.startTime,
    };
    if (this.endTime) wire.end_time = this.endTime;
    const input = this.redactedInput();
    if (input) wire.input = input;
    const output = this.redactedOutput();
    if (output) wire.output = output;
    if (this.conversation) wire.thread_id = this.conversation;
    if (this.errorInfo) wire.error_info = this.errorInfo;
    if (this.scores.length > 0) wire.feedback_scores = this.scores;
    const metadata = this.redactedMetadata();
    if (metadata) wire.metadata = metadata;
    const tags = normaliseTags(this.tags);
    if (tags) wire.tags = tags;
    const agent = this.resolvedAgent();
    if (agent) wire.agent = agent;
    // With `streamSpans` the spans have already been posted on their own;
    // sending them again here would double-count them.
    if (!this.sink.streamSpans()) {
      if (this.spans.length > 0) wire.spans = this.spans.map((span) => span.toWire());
      return wire;
    }
    // The control plane reads a run's model off the spans nested in its trace,
    // and a streamed trace arrives with none — so Live Runs would show the
    // model the agent was registered with, or nothing, instead of the one that
    // ran. The trace says it itself, by the rule the control plane would have
    // applied, unless the caller's own metadata already names one.
    const model = this.modelThatRan();
    if (model && !wire.metadata?.model) wire.metadata = { ...(wire.metadata ?? {}), model };
    return wire;
  }

  /** The last `llm` span's model; failing that, the last model any span named. */
  private modelThatRan(): string | undefined {
    let fallback: string | undefined;
    for (let index = this.spans.length - 1; index >= 0; index -= 1) {
      const span = this.spans[index]!;
      if (!span.modelName) continue;
      if (span.spanType === 'llm') return span.modelName;
      fallback ??= span.modelName;
    }
    return fallback;
  }
}
