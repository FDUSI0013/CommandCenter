/**
 * Anthropic Node SDK wrapper.
 *
 * Same shape as the OpenAI wrapper and for the same reasons — a proxy over a
 * client the caller built, no import of `@anthropic-ai/sdk`, streams closed
 * when they are exhausted rather than when they are handed over.
 *
 * The differences that matter are in the payload. Anthropic reports
 * `input_tokens` / `output_tokens` where OpenAI reports `prompt_tokens` /
 * `completion_tokens`; both are carried through under their own names and
 * `total_tokens` is derived, so the console's per-model breakdown adds up
 * across providers. Usage also arrives split across two stream events
 * (`message_start` carries the input count, `message_delta` the output count),
 * so it is accumulated rather than taken from the last chunk.
 */

import { isAsyncIterable, loadOptionalPackage, pick, proxyMethod } from './shared.js';
import type { FulcrumOps } from '../client.js';
import type { Span } from '../trace.js';

/** The little that this wrapper needs to be true of an Anthropic client. */
export interface AnthropicLike {
  messages?: { create?: unknown; stream?: unknown };
  completions?: { create?: unknown };
}

export interface WrapAnthropicOptions {
  spanName?: string;
  /** Provider label recorded on the span. Defaults to `anthropic`. */
  provider?: string;
  captureInput?: boolean;
  captureOutput?: boolean;
}

interface CallShape {
  model?: string;
  messages?: unknown;
  system?: unknown;
  stream?: boolean;
}

function mergeUsage(into: Record<string, number>, source: unknown): void {
  const usage = pick<Record<string, unknown>>(source, 'usage');
  if (!usage) return;
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value !== 'number') continue;
    // Output tokens are reported cumulatively on `message_delta`, so the later
    // value replaces the earlier one rather than adding to it.
    into[key] = Math.max(into[key] ?? 0, value);
  }
}

/** Text of a non-streamed message response, for the span's output. */
function collectContentText(response: unknown): string | undefined {
  const content = pick<Array<Record<string, unknown>>>(response, 'content');
  if (!Array.isArray(content)) return undefined;
  let text = '';
  for (const block of content) {
    if (block && block.type === 'text' && typeof block.text === 'string') text += block.text;
  }
  return text.length > 0 ? text : undefined;
}

function wrapStream(stream: AsyncIterable<unknown>, span: Span, captureOutput: boolean): AsyncIterable<unknown> {
  return new Proxy(stream as object, {
    get(target, property, receiver) {
      if (property !== Symbol.asyncIterator) return Reflect.get(target, property, receiver);
      return function iterate(): AsyncIterator<unknown> {
        const inner = (target as AsyncIterable<unknown>)[Symbol.asyncIterator]();
        const usage: Record<string, number> = {};
        let text = '';
        let events = 0;
        let closed = false;

        const finish = (error?: unknown) => {
          if (closed) return;
          closed = true;
          if (Object.keys(usage).length > 0) span.setUsage(usage);
          span.end({ output: captureOutput ? { events, text } : undefined, error });
        };

        return {
          async next(...args: [] | [undefined]) {
            try {
              const result = await inner.next(...args);
              if (result.done) {
                finish();
                return result;
              }
              events += 1;
              const event = result.value;
              mergeUsage(usage, event);
              mergeUsage(usage, pick(event, 'message'));
              const delta = pick<Record<string, unknown>>(event, 'delta');
              if (delta && typeof delta.text === 'string') text += delta.text;
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

/** Instrument an existing Anthropic client. */
export function wrapAnthropic<T extends AnthropicLike>(
  anthropic: T,
  client: FulcrumOps,
  options: WrapAnthropicOptions = {},
): T {
  const provider = options.provider ?? 'anthropic';

  const instrument = (methodName: string, alwaysStreams = false) =>
    (original: (...args: never[]) => unknown) =>
      function (this: unknown, ...args: never[]): unknown {
        const body = (args[0] ?? {}) as CallShape;
        const span = client.startSpan({
          name: options.spanName ?? `anthropic.${methodName}`,
          type: 'llm',
          model: typeof body.model === 'string' ? body.model : undefined,
          provider,
          ...(options.captureInput === false ? {} : { input: body }),
        });

        let result: unknown;
        try {
          result = original(...args);
        } catch (error) {
          span.end({ error });
          throw error;
        }

        // `messages.stream()` returns a stream object directly rather than a
        // promise of one.
        if (alwaysStreams && isAsyncIterable(result)) {
          return wrapStream(result, span, options.captureOutput !== false);
        }

        if (!(result instanceof Promise)) {
          span.end({ output: options.captureOutput === false ? undefined : result });
          return result;
        }

        return result.then(
          (value) => {
            if ((body.stream === true || alwaysStreams) && isAsyncIterable(value)) {
              return wrapStream(value, span, options.captureOutput !== false);
            }
            const usage: Record<string, number> = {};
            mergeUsage(usage, value);
            if (Object.keys(usage).length > 0) span.setUsage(usage);
            const model = pick<string>(value, 'model');
            if (model) span.setModel(model, provider);
            if (options.captureOutput !== false) {
              const text = collectContentText(value);
              span.end({ output: text !== undefined ? { text, response: value } : value });
            } else {
              span.end({});
            }
            return value;
          },
          (error: unknown) => {
            span.end({ error });
            throw error;
          },
        );
      };

  let wrapped = anthropic as AnthropicLike;
  if (anthropic.messages?.create) {
    wrapped = proxyMethod(wrapped, ['messages', 'create'], instrument('messages.create'));
  }
  if (anthropic.messages?.stream) {
    wrapped = proxyMethod(wrapped, ['messages', 'stream'], instrument('messages.stream', true));
  }
  if (anthropic.completions?.create) {
    wrapped = proxyMethod(wrapped, ['completions', 'create'], instrument('completions.create'));
  }
  return wrapped as T;
}

/** Build an instrumented Anthropic client, importing `@anthropic-ai/sdk` lazily. */
export async function createAnthropic<T extends AnthropicLike>(
  client: FulcrumOps,
  anthropicOptions: Record<string, unknown> = {},
  wrapOptions: WrapAnthropicOptions = {},
): Promise<T> {
  const module = await loadOptionalPackage<{
    default?: new (o: unknown) => T;
    Anthropic?: new (o: unknown) => T;
  }>('@anthropic-ai/sdk', 'npm install @anthropic-ai/sdk');
  const AnthropicCtor = module.Anthropic ?? module.default;
  if (!AnthropicCtor) throw new Error('The "@anthropic-ai/sdk" package did not export a constructor.');
  return wrapAnthropic(new AnthropicCtor(anthropicOptions), client, wrapOptions);
}
