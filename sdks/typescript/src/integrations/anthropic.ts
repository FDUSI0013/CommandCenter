/**
 * Anthropic Node SDK wrapper.
 *
 * Same shape as the OpenAI wrapper and for the same reasons — a proxy over a
 * client the caller built, no import of `@anthropic-ai/sdk`, streams closed
 * when they are exhausted rather than when they are handed over.
 *
 * The differences that matter are in the payload. Anthropic reports
 * `input_tokens` / `output_tokens` where OpenAI reports `prompt_tokens` /
 * `completion_tokens`; they are the same two numbers, so they are recorded
 * under the second pair of names (see `canonicalUsage`) and `total_tokens` is
 * derived, which is what makes the console's per-model breakdown add up
 * across providers. Usage also arrives split across two stream events
 * (`message_start` carries the input count, `message_delta` the output count),
 * so it is accumulated rather than taken from the last chunk.
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
  const usage = canonicalUsage(pick(source, 'usage'));
  if (!usage) return;
  for (const [key, value] of Object.entries(usage)) {
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

/** The slice of Anthropic's `MessageStream` the wrapper listens through. */
interface MessageStreamLike {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  finalMessage(): unknown;
  errored?: boolean;
  aborted?: boolean;
}

function isMessageStream(value: unknown): value is MessageStreamLike {
  const candidate = value as MessageStreamLike | null;
  return (
    candidate !== null &&
    typeof candidate === 'object' &&
    typeof candidate.on === 'function' &&
    typeof candidate.finalMessage === 'function'
  );
}

/**
 * Watch the returned stream so the span closes when the generation is over.
 *
 * The caller gets Anthropic's own object back, never a stand-in: the helper
 * keeps its listeners and its snapshot in `#private` fields, and
 * `stream.on('text', ...)` through a proxy throws inside the caller's code.
 *
 * `messages.stream()` returns a `MessageStream`, which runs on its own whether
 * or not anybody iterates it — the documented use is `.on('text')` and
 * `await stream.finalMessage()`, with no `for await` at all. So that one is
 * followed through its own events. Only `end` is listened to for the outcome,
 * deliberately not `error` or `abort`: the helper raises an unhandled rejection
 * when a stream fails and nobody registered for those, and a telemetry listener
 * must not be what silences it.
 *
 * `messages.create({ stream: true })` returns a bare `Stream` with no events,
 * which is followed through its reads instead.
 *
 * Either way the span is deferred first: the usual caller returns the stream
 * from the function that asked for it, so the trace around the call closes
 * before the first token, and would otherwise take this span down with it.
 */
function watchStream<T extends AsyncIterable<unknown>>(stream: T, span: Span, captureOutput: boolean): T {
  span.defer();
  const usage: Record<string, number> = {};
  let text = '';
  let events = 0;
  let closed = false;

  const onEvent = (event: unknown) => {
    if (closed) return;
    events += 1;
    mergeUsage(usage, event);
    mergeUsage(usage, pick(event, 'message'));
    const delta = pick<Record<string, unknown>>(event, 'delta');
    if (delta && typeof delta.text === 'string') text += delta.text;
  };

  const finish = (error?: unknown) => {
    if (closed) return;
    closed = true;
    if (Object.keys(usage).length > 0) span.setUsage(usage);
    span.end({ output: captureOutput ? { events, text } : undefined, error });
  };

  if (isMessageStream(stream)) {
    try {
      let completed = false;
      stream.on('streamEvent', onEvent);
      stream.on('finalMessage', (message) => {
        completed = true;
        mergeUsage(usage, message);
        const model = pick<string>(message, 'model');
        if (model) span.setModel(model);
      });
      stream.on('end', () => {
        if (completed) return finish();
        // The helper says that it failed but not, without an `error` listener,
        // why. The reason is on the trace: `finalMessage()` rejected with it.
        if (stream.aborted) return finish(new Error('The stream was aborted before the message was complete.'));
        if (stream.errored) return finish(new Error('The stream failed before the message was complete.'));
        return finish();
      });
      return stream;
    } catch {
      /* not the emitter it looked like; follow the reads instead */
    }
  }

  return observeIteration(stream, { onItem: onEvent, onEnd: finish });
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
          return watchStream(result, span, options.captureOutput !== false);
        }

        if (!isThenable(result)) {
          span.end({ output: options.captureOutput === false ? undefined : result });
          return result;
        }

        // What goes back is Anthropic's own `APIPromise`, so `.withResponse()`
        // and `.asResponse()` are still there. The caller may read it more than
        // once (`await` it, then `.finally()` it), so the first look decides.
        let seen = false;
        let handedBack: unknown;
        return observePromise(result, {
          onValue(value) {
            if (seen) return handedBack;
            seen = true;
            handedBack = value;
            if ((body.stream === true || alwaysStreams) && isAsyncIterable(value)) {
              handedBack = watchStream(value, span, options.captureOutput !== false);
              return handedBack;
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
