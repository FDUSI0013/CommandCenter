/**
 * LangChain.js callback handler.
 *
 * LangChain reports its own execution as a stream of paired start/end events,
 * each carrying a `runId` and the `parentRunId` it hangs beneath. That is
 * already a tree, so this handler does not use the SDK's ambient context at
 * all: it keeps its own map from `runId` to the span it opened, and resolves a
 * parent by looking `parentRunId` up in that map. Relying on
 * `AsyncLocalStorage` here would be wrong — LangChain runs callbacks from its
 * own scheduler, and by the time `handleLLMEnd` fires the async context that
 * started the call is long gone.
 *
 * `@langchain/core` is never imported at module load. A plain object with the
 * handler methods is a valid `callbacks` entry, so the common case costs
 * nothing. `createLangChainHandler()` additionally awaits a lazy import to
 * return a real `BaseCallbackHandler` subclass for the code paths that
 * `instanceof`-check it.
 */

import { loadOptionalPackage } from './shared.js';
import type { FulcrumOps } from '../client.js';
import type { Span, Trace } from '../trace.js';
import type { SpanType } from '../types.js';

/** LangChain's serialised component descriptor. */
interface Serialized {
  id?: string[];
  name?: string;
  kwargs?: Record<string, unknown>;
}

export interface LangChainHandlerOptions {
  /** Name for the trace opened by a top-level run. Defaults to the chain's name. */
  traceName?: string;
  /** Agent to attribute the run to. Defaults to the client's. */
  agent?: string;
  /** Tags copied onto every span. */
  tags?: string[];
  captureInput?: boolean;
  captureOutput?: boolean;
}

interface OpenRun {
  span: Span;
  /** The trace this run opened, when it was the root. */
  trace?: Trace;
}

function componentName(serialized: Serialized | undefined, fallback: string): string {
  if (!serialized) return fallback;
  if (typeof serialized.name === 'string' && serialized.name) return serialized.name;
  const id = serialized.id;
  if (Array.isArray(id) && id.length > 0) return String(id[id.length - 1]);
  return fallback;
}

function usageFrom(output: unknown): Record<string, number> | undefined {
  if (!output || typeof output !== 'object') return undefined;
  const record = output as Record<string, unknown>;
  const llmOutput = record.llmOutput as Record<string, unknown> | undefined;
  const candidates = [
    llmOutput?.tokenUsage,
    llmOutput?.usage,
    llmOutput?.estimatedTokenUsage,
    record.tokenUsage,
  ];
  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== 'object') continue;
    const out: Record<string, number> = {};
    for (const [key, value] of Object.entries(candidate as Record<string, unknown>)) {
      if (typeof value === 'number') out[toSnake(key)] = value;
    }
    if (Object.keys(out).length > 0) return out;
  }
  return undefined;
}

/** LangChain reports `promptTokens`; the ingest contract wants `prompt_tokens`. */
function toSnake(key: string): string {
  return key.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
}

function modelFrom(serialized: Serialized | undefined, extra: Record<string, unknown> | undefined): string | undefined {
  const fromExtra =
    (extra?.invocation_params as Record<string, unknown> | undefined)?.model ??
    (extra?.invocation_params as Record<string, unknown> | undefined)?.model_name;
  if (typeof fromExtra === 'string') return fromExtra;
  const kwargs = serialized?.kwargs;
  const fromKwargs = kwargs?.model ?? kwargs?.modelName ?? kwargs?.model_name;
  return typeof fromKwargs === 'string' ? fromKwargs : undefined;
}

/**
 * The handler itself, as a plain class with no LangChain base.
 *
 * Usable directly as a `callbacks` entry: LangChain accepts any object carrying
 * these method names.
 */
export class FulcrumOpsCallbackHandler {
  /** LangChain identifies handlers by this property. */
  readonly name = 'fulcrum_ops_callback_handler';
  readonly awaitHandlers = true;

  private readonly runs = new Map<string, OpenRun>();

  constructor(
    private readonly client: FulcrumOps,
    private readonly options: LangChainHandlerOptions = {},
  ) {}

  /**
   * Open a span for a run, rooting a trace when it has no parent.
   *
   * A LangChain invocation's root callback is the whole run as far as the
   * console is concerned, so it becomes the trace rather than a span inside one.
   */
  private open(
    runId: string,
    parentRunId: string | undefined,
    name: string,
    type: SpanType,
    input: unknown,
    tags?: string[],
  ): Span | undefined {
    try {
      const parent = parentRunId ? this.runs.get(parentRunId) : undefined;
      const spanOptions = {
        name,
        type,
        ...(this.options.captureInput === false ? {} : { input }),
        ...(this.options.agent ? { agent: this.options.agent } : {}),
        tags: [...(this.options.tags ?? []), ...(tags ?? [])],
      };

      if (parent) {
        const span = parent.span.startSpan(spanOptions);
        this.runs.set(runId, { span });
        return span;
      }

      const trace = this.client.startTrace({
        name: this.options.traceName ?? name,
        ...(this.options.captureInput === false ? {} : { input }),
        ...(this.options.agent ? { agent: this.options.agent } : {}),
        tags: [...(this.options.tags ?? []), ...(tags ?? [])],
      });
      const span = trace.startSpan(spanOptions);
      this.runs.set(runId, { span, trace });
      return span;
    } catch {
      // A callback that throws would surface inside LangChain's own error
      // handling and look like a chain failure. Telemetry does not get to do
      // that.
      return undefined;
    }
  }

  /** Close a run's span, and its trace when the run was the root. */
  private close(runId: string, output: unknown, error?: unknown): void {
    try {
      const run = this.runs.get(runId);
      if (!run) return;
      this.runs.delete(runId);
      const payload = this.options.captureOutput === false ? undefined : output;
      run.span.end({ output: payload, error });
      run.trace?.end({ output: payload, error });
    } catch {
      /* never throw into LangChain's scheduler */
    }
  }

  private spanFor(runId: string): Span | undefined {
    return this.runs.get(runId)?.span;
  }

  // --- chains -------------------------------------------------------------

  handleChainStart(serialized: Serialized, inputs: unknown, runId: string, parentRunId?: string, tags?: string[]): void {
    this.open(runId, parentRunId, componentName(serialized, 'chain'), 'general', inputs, tags);
  }

  handleChainEnd(outputs: unknown, runId: string): void {
    this.close(runId, outputs);
  }

  handleChainError(error: unknown, runId: string): void {
    this.close(runId, undefined, error);
  }

  // --- models -------------------------------------------------------------

  handleLLMStart(
    serialized: Serialized,
    prompts: string[],
    runId: string,
    parentRunId?: string,
    extraParams?: Record<string, unknown>,
    tags?: string[],
  ): void {
    const span = this.open(runId, parentRunId, componentName(serialized, 'llm'), 'llm', { prompts }, tags);
    const model = modelFrom(serialized, extraParams);
    if (span && model) span.setModel(model);
  }

  handleChatModelStart(
    serialized: Serialized,
    messages: unknown,
    runId: string,
    parentRunId?: string,
    extraParams?: Record<string, unknown>,
    tags?: string[],
  ): void {
    const span = this.open(runId, parentRunId, componentName(serialized, 'chat_model'), 'llm', { messages }, tags);
    const model = modelFrom(serialized, extraParams);
    if (span && model) span.setModel(model);
  }

  handleLLMEnd(output: unknown, runId: string): void {
    const span = this.spanFor(runId);
    const usage = usageFrom(output);
    if (span && usage) span.setUsage(usage);
    this.close(runId, output);
  }

  handleLLMError(error: unknown, runId: string): void {
    this.close(runId, undefined, error);
  }

  // --- tools --------------------------------------------------------------

  handleToolStart(serialized: Serialized, input: unknown, runId: string, parentRunId?: string, tags?: string[]): void {
    this.open(runId, parentRunId, componentName(serialized, 'tool'), 'tool', { input }, tags);
  }

  handleToolEnd(output: unknown, runId: string): void {
    this.close(runId, output);
  }

  handleToolError(error: unknown, runId: string): void {
    this.close(runId, undefined, error);
  }

  // --- retrievers ---------------------------------------------------------

  handleRetrieverStart(serialized: Serialized, query: string, runId: string, parentRunId?: string, tags?: string[]): void {
    this.open(runId, parentRunId, componentName(serialized, 'retriever'), 'tool', { query }, tags);
  }

  handleRetrieverEnd(documents: unknown, runId: string): void {
    const span = this.spanFor(runId);
    if (span && Array.isArray(documents)) span.setMetadata({ document_count: documents.length });
    this.close(runId, documents);
  }

  handleRetrieverError(error: unknown, runId: string): void {
    this.close(runId, undefined, error);
  }

  // --- agents -------------------------------------------------------------

  handleAgentAction(action: unknown, runId: string): void {
    const span = this.spanFor(runId);
    if (!span) return;
    const tool = (action as { tool?: unknown } | undefined)?.tool;
    span.setMetadata({ agent_action: typeof tool === 'string' ? tool : 'action' });
  }

  handleAgentEnd(action: unknown, runId: string): void {
    this.close(runId, action);
  }

  /** Close anything still open, e.g. after a run was abandoned. */
  flushOpenRuns(): void {
    for (const runId of Array.from(this.runs.keys())) this.close(runId, undefined);
  }
}

/**
 * Build a handler, upgrading to a real `BaseCallbackHandler` when available.
 *
 * Some LangChain code paths `instanceof`-check the base class rather than
 * duck-typing, so this awaits a lazy import of `@langchain/core` and returns a
 * subclass instance when it is installed. When it is not, the plain handler
 * above is returned, which every documented `callbacks` array accepts.
 */
export async function createLangChainHandler(
  client: FulcrumOps,
  options: LangChainHandlerOptions = {},
): Promise<FulcrumOpsCallbackHandler> {
  try {
    const module = await loadOptionalPackage<{
      BaseCallbackHandler?: new (...args: unknown[]) => object;
    }>('@langchain/core/callbacks/base', 'npm install @langchain/core');
    const Base = module.BaseCallbackHandler;
    if (!Base) return new FulcrumOpsCallbackHandler(client, options);

    const handler = new FulcrumOpsCallbackHandler(client, options);
    // Splice the base class in beneath the handler's own prototype so both the
    // `instanceof` check and every method above hold.
    const proto = Object.getPrototypeOf(handler) as object;
    if (Object.getPrototypeOf(proto) === Object.prototype) {
      Object.setPrototypeOf(proto, Base.prototype);
    }
    return handler;
  } catch {
    return new FulcrumOpsCallbackHandler(client, options);
  }
}

/** Synchronous alias, for callers who do not need the `instanceof` guarantee. */
export function langChainHandler(
  client: FulcrumOps,
  options: LangChainHandlerOptions = {},
): FulcrumOpsCallbackHandler {
  return new FulcrumOpsCallbackHandler(client, options);
}
