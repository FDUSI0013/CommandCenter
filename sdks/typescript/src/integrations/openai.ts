/**
 * OpenAI Node SDK wrapper.
 *
 * `wrapOpenAI(client)` returns a stand-in for the client that opens an `llm`
 * span around `chat.completions.create`, `responses.create` and
 * `embeddings.create`, records the model, token usage and output, and returns
 * the provider's own value untouched. It never imports `openai` — the caller
 * already has an instance, which is also what lets this work with Azure OpenAI,
 * a proxied base URL, or a custom fetch.
 *
 * Streaming is handled properly rather than skipped: a streamed call resolves
 * before the first token, so closing the span there would report a 40ms
 * duration for a 12-second generation. The returned stream is watched instead,
 * and the span closes when it is exhausted, with the usage chunk OpenAI sends
 * at the end.
 *
 * "Untouched" is meant literally. The promise that comes back is OpenAI's own
 * `APIPromise` and the stream is OpenAI's own `Stream`, not stand-ins, so
 * `.withResponse()`, `.asResponse()`, `.tee()` and `.toReadableStream()` keep
 * working exactly as they do on an uninstrumented client.
 */

import {
  canonicalUsage,
  isAsyncIterable,
  isThenable,
  loadOptionalPackage,
  observeIteration,
  observePromise,
  pick,
  proxyMethod,
} from './shared.js';
import type { FulcrumOps } from '../client.js';
import type { Span } from '../trace.js';

/** The little that this wrapper needs to be true of an OpenAI client. */
export interface OpenAILike {
  chat?: { completions?: { create?: unknown } };
  responses?: { create?: unknown };
  embeddings?: { create?: unknown };
}

export interface WrapOpenAIOptions {
  /** Name for the spans. Defaults to the method being called. */
  spanName?: string;
  /** Provider label recorded on the span. Defaults to `openai`. */
  provider?: string;
  /** Record the request body as span input. Defaults to the client's setting. */
  captureInput?: boolean;
  /** Record the response as span output. Defaults to the client's setting. */
  captureOutput?: boolean;
  /**
   * Ask for token usage on streamed Chat Completions, by adding
   * `stream_options: { include_usage: true }` to calls that did not set it.
   *
   * OpenAI reports no usage on a streamed completion unless asked, so without
   * this a streamed call's tokens and cost are blank. It is off by default
   * because it is not free of side effects: the stream gains one final chunk
   * whose `choices` is empty (code indexing `chunk.choices[0].delta` unguarded
   * will trip on it), and an OpenAI-compatible endpoint that predates the
   * option may refuse the request. Turn it on where neither applies. The
   * Responses API always reports usage and needs nothing.
   */
  streamUsage?: boolean;
}

interface CallShape {
  model?: string;
  messages?: unknown;
  input?: unknown;
  stream?: boolean;
  stream_options?: Record<string, unknown> | null;
}

function usageOf(response: unknown): Record<string, number> | undefined {
  return canonicalUsage(pick(response, 'usage'));
}

/**
 * Watch the returned stream so the span closes when the caller finishes reading.
 *
 * The caller gets OpenAI's own `Stream` back — `tee`, `controller`,
 * `toReadableStream` and its private state all intact — with the reads observed
 * from the side. See `observeIteration` for why it is not a proxy.
 *
 * The span is deferred first: the usual caller returns the stream from the
 * function that asked for it, so the trace around the call closes before the
 * first token is read, and would otherwise take this span down with it.
 */
function watchStream<T extends AsyncIterable<unknown>>(stream: T, span: Span, captureOutput: boolean): T {
  span.defer();
  let chunks = 0;
  let text = '';
  let usage: Record<string, number> | undefined;
  let model: string | undefined;
  let closed = false;

  return observeIteration(stream, {
    onItem(chunk) {
      if (closed) return;
      chunks += 1;
      if (captureOutput) text += textOf(chunk);
      // Chat Completions puts usage on its last chunk and the model on every
      // one. The Responses API puts both on the `response` its terminal event
      // (`response.completed`) carries, and nowhere on the event itself.
      const response = pick<unknown>(chunk, 'response');
      usage = usageOf(chunk) ?? usageOf(response) ?? usage;
      model = pick<string>(chunk, 'model') ?? pick<string>(response, 'model') ?? model;
    },
    onEnd(error) {
      if (closed) return;
      closed = true;
      if (usage) span.setUsage(usage);
      if (typeof model === 'string' && model) span.setModel(model);
      span.end({ output: captureOutput ? { chunks, text } : undefined, error });
    },
  });
}

/** The text one streamed chunk adds to the completion, for the span's output. */
function textOf(chunk: unknown): string {
  const choices = pick<Array<Record<string, unknown>>>(chunk, 'choices');
  if (Array.isArray(choices)) {
    let text = '';
    for (const choice of choices) {
      const delta = pick<Record<string, unknown>>(choice, 'delta', 'message');
      const content = delta && typeof delta.content === 'string' ? delta.content : undefined;
      if (content) text += content;
    }
    return text;
  }
  // The Responses API streams typed events. Only `response.output_text.delta`
  // is the answer; tool-call arguments and reasoning summaries arrive as
  // string deltas too and are not.
  const type = pick<string>(chunk, 'type');
  const delta = pick<string>(chunk, 'delta');
  if (typeof delta !== 'string') return '';
  return type === undefined || type === 'response.output_text.delta' ? delta : '';
}

/**
 * Instrument an existing OpenAI client.
 *
 * The returned object behaves identically to the one passed in: same methods,
 * same return values, same errors. Instrumenting a client the caller built
 * means every configuration they applied — Azure endpoints, custom headers,
 * retries — still holds.
 */
export function wrapOpenAI<T extends OpenAILike>(
  openai: T,
  client: FulcrumOps,
  options: WrapOpenAIOptions = {},
): T {
  const provider = options.provider ?? 'openai';

  const instrument = (methodName: string) =>
    (original: (...args: never[]) => unknown) =>
      function (this: unknown, ...args: never[]): unknown {
        const body = (args[0] ?? {}) as CallShape;
        const span = client.startSpan({
          name: options.spanName ?? `openai.${methodName}`,
          type: 'llm',
          model: typeof body.model === 'string' ? body.model : undefined,
          provider,
          ...(options.captureInput === false ? {} : { input: body }),
        });

        // Opt-in only, and never over the caller's own choice: see `streamUsage`.
        let callArgs = args;
        if (
          options.streamUsage === true &&
          methodName === 'chat.completions.create' &&
          body.stream === true &&
          body.stream_options?.include_usage === undefined
        ) {
          const withUsage = { ...body, stream_options: { ...(body.stream_options ?? {}), include_usage: true } };
          callArgs = [withUsage, ...args.slice(1)] as never[];
        }

        let result: unknown;
        try {
          result = original(...callArgs);
        } catch (error) {
          // A synchronous throw is argument validation, not a model failure.
          span.end({ error });
          throw error;
        }

        if (!isThenable(result)) {
          span.end({ output: options.captureOutput === false ? undefined : result });
          return result;
        }

        // What goes back is OpenAI's own `APIPromise`, so `.withResponse()` and
        // `.asResponse()` are still there. The caller may read it more than
        // once (`await` it, then `.finally()` it), so the first look decides.
        let seen = false;
        let handedBack: unknown;
        return observePromise(result, {
          onValue(value) {
            if (seen) return handedBack;
            seen = true;
            handedBack = value;
            if (body.stream === true && isAsyncIterable(value)) {
              handedBack = watchStream(value, span, options.captureOutput !== false);
              return handedBack;
            }
            const usage = usageOf(value);
            if (usage) span.setUsage(usage);
            const model = pick<string>(value, 'model');
            if (model) span.setModel(model, provider);
            span.end({ output: options.captureOutput === false ? undefined : value });
            return value;
          },
          onError(error) {
            span.end({ error });
          },
          onRawResponse() {
            // The body is the caller's to read, so there is no output or usage
            // to record — only that the call came back, and when.
            span.end({});
          },
        });
      };

  let wrapped = openai as OpenAILike;
  if (openai.chat?.completions?.create) {
    wrapped = proxyMethod(wrapped, ['chat', 'completions', 'create'], instrument('chat.completions.create'));
  }
  if (openai.responses?.create) {
    wrapped = proxyMethod(wrapped, ['responses', 'create'], instrument('responses.create'));
  }
  if (openai.embeddings?.create) {
    wrapped = proxyMethod(wrapped, ['embeddings', 'create'], instrument('embeddings.create'));
  }
  return wrapped as T;
}

/**
 * Build an instrumented OpenAI client, importing `openai` lazily.
 *
 * For callers who would rather not construct one themselves. The import happens
 * on the first call, so a project that never uses OpenAI never loads it.
 */
export async function createOpenAI<T extends OpenAILike>(
  client: FulcrumOps,
  openaiOptions: Record<string, unknown> = {},
  wrapOptions: WrapOpenAIOptions = {},
): Promise<T> {
  const module = await loadOptionalPackage<{ default?: new (o: unknown) => T; OpenAI?: new (o: unknown) => T }>(
    'openai',
    'npm install openai',
  );
  const OpenAICtor = module.OpenAI ?? module.default;
  if (!OpenAICtor) throw new Error('The "openai" package did not export a constructor.');
  return wrapOpenAI(new OpenAICtor(openaiOptions), client, wrapOptions);
}
