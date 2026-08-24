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
 * duration for a 12-second generation. The returned async iterable is wrapped
 * instead, and the span closes when the stream is exhausted, with the usage
 * chunk OpenAI sends at the end.
 */

import { isAsyncIterable, loadOptionalPackage, pick, proxyMethod } from './shared.js';
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
}

interface CallShape {
  model?: string;
  messages?: unknown;
  input?: unknown;
  stream?: boolean;
}

function usageOf(response: unknown): Record<string, number> | undefined {
  const usage = pick<Record<string, unknown>>(response, 'usage');
  if (!usage) return undefined;
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value === 'number') out[key] = value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Wrap the returned stream so the span closes when the caller finishes reading.
 *
 * Delegation is total: `tee`, `controller` and everything else OpenAI hangs off
 * its stream object stay reachable, because the wrapper is a proxy over the
 * original with only `Symbol.asyncIterator` replaced.
 */
function wrapStream(stream: AsyncIterable<unknown>, span: Span, captureOutput: boolean): AsyncIterable<unknown> {
  return new Proxy(stream as object, {
    get(target, property, receiver) {
      if (property !== Symbol.asyncIterator) return Reflect.get(target, property, receiver);
      return function iterate(): AsyncIterator<unknown> {
        const inner = (target as AsyncIterable<unknown>)[Symbol.asyncIterator]();
        const chunks: unknown[] = [];
        let closed = false;

        const finish = (error?: unknown) => {
          if (closed) return;
          closed = true;
          const usage = chunks.map((chunk) => usageOf(chunk)).filter(Boolean).pop();
          if (usage) span.setUsage(usage);
          span.end({
            output: captureOutput ? { chunks: chunks.length, text: collectText(chunks) } : undefined,
            error,
          });
        };

        return {
          async next(...args: [] | [undefined]) {
            try {
              const result = await inner.next(...args);
              if (result.done) finish();
              else chunks.push(result.value);
              return result;
            } catch (error) {
              finish(error);
              throw error;
            }
          },
          async return(value?: unknown) {
            finish();
            return inner.return ? inner.return(value) : { done: true, value };
          },
          async throw(error?: unknown) {
            finish(error);
            if (inner.throw) return inner.throw(error);
            throw error;
          },
          [Symbol.asyncIterator]() {
            return this;
          },
        } as AsyncIterator<unknown>;
      };
    },
  }) as AsyncIterable<unknown>;
}

/** Reassemble the text of a streamed completion, for the span's output. */
function collectText(chunks: readonly unknown[]): string {
  let text = '';
  for (const chunk of chunks) {
    const choices = pick<Array<Record<string, unknown>>>(chunk, 'choices');
    if (Array.isArray(choices)) {
      for (const choice of choices) {
        const delta = pick<Record<string, unknown>>(choice, 'delta', 'message');
        const content = delta && typeof delta.content === 'string' ? delta.content : undefined;
        if (content) text += content;
      }
      continue;
    }
    // The Responses API streams `response.output_text.delta` events.
    const delta = pick<string>(chunk, 'delta');
    if (typeof delta === 'string') text += delta;
  }
  return text;
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

        let result: unknown;
        try {
          result = original(...args);
        } catch (error) {
          // A synchronous throw is argument validation, not a model failure.
          span.end({ error });
          throw error;
        }

        if (!(result instanceof Promise)) {
          span.end({ output: options.captureOutput === false ? undefined : result });
          return result;
        }

        return result.then(
          (value) => {
            if (body.stream === true && isAsyncIterable(value)) {
              return wrapStream(value, span, options.captureOutput !== false);
            }
            const usage = usageOf(value);
            if (usage) span.setUsage(usage);
            const model = pick<string>(value, 'model');
            if (model) span.setModel(model, provider);
            span.end({ output: options.captureOutput === false ? undefined : value });
            return value;
          },
          (error: unknown) => {
            span.end({ error });
            throw error;
          },
        );
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
