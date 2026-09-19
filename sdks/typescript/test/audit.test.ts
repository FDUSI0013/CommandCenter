/**
 * Regressions from the 2026-09-18 audit.
 *
 * Each block names the defect it pins. Like the rest of the suite these run
 * against the stub on a real socket, and the stub enforces the same
 * whole-request ceilings the real ingest path does — a span total and a body
 * size — because the first defect below was invisible to a stand-in that
 * accepted anything.
 */

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { FulcrumOps, isValidId, newId, resolveOptions, setDefaultClient, traced } from '../src/index.js';
import type { FulcrumOpsError } from '../src/index.js';
import { wrapAnthropic } from '../src/integrations/anthropic.js';
import { langChainHandler } from '../src/integrations/langchain.js';
import { wrapOpenAI } from '../src/integrations/openai.js';
import { FakeAPIPromise, FakeMessageStream, FakeStream, fakeHttpResponse } from './helpers/provider-doubles.js';
import { withStub } from './helpers/stub-server.js';
import type { StubServer } from './helpers/stub-server.js';

function errorCollector(): {
  errors: Array<{ error: FulcrumOpsError; operation: string }>;
  onError: (error: FulcrumOpsError, context: { operation: string }) => void;
} {
  const errors: Array<{ error: FulcrumOpsError; operation: string }> = [];
  return {
    errors,
    onError: (error, context) => {
      errors.push({ error, operation: context.operation });
    },
  };
}

/** Spans carried by each traces request the stub accepted or refused, in order. */
function spanTotals(stub: StubServer): number[] {
  return stub.requestsFor('/ingest/traces').map((request) => {
    const traces = (request.body?.traces as Array<{ spans?: unknown[] }> | undefined) ?? [];
    return traces.reduce((total, trace) => total + (trace.spans?.length ?? 0), 0);
  });
}

describe('audit #74: a batch is cut by spans and bytes, not only by item count', () => {
  it('never sends more spans in one request than the server accepts', async () => {
    await withStub(async (stub, baseUrl) => {
      const collector = errorCollector();
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collector.onError,
        batch: { maxItems: 100, flushIntervalMs: 60_000 },
      });

      // Sixty runs of a tool-using agent, twenty-five steps each: well inside
      // "100 items", half as much again as the 1,000 spans a request may carry.
      for (let run = 0; run < 60; run += 1) {
        client.trace(`run-${run}`, (trace) => {
          for (let step = 0; step < 25; step += 1) trace.startSpan({ name: `step-${step}`, type: 'tool' }).end();
        });
      }
      await client.close();

      assert.equal(stub.itemsFor('traces').length, 60, 'every run was offered');
      assert.ok(
        spanTotals(stub).every((total) => total <= 1_000),
        `no request was over the span ceiling (saw ${spanTotals(stub).join(', ')})`,
      );
      const stats = client.getStats();
      assert.equal(stats.dropped, 0, 'nothing was lost to a 413');
      assert.equal(stats.accepted, 60);
      assert.deepEqual(collector.errors, []);
    });
  });

  it('adopts the deployment’s span budget as a span budget, not as a trace count', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.config = { ...stub.config, batch_max_spans: 40 };
      stub.maxBatchSpans = 40;
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        batch: { maxItems: 100, flushIntervalMs: 60_000 },
      });

      for (let run = 0; run < 12; run += 1) {
        client.trace(`run-${run}`, (trace) => {
          for (let step = 0; step < 10; step += 1) trace.startSpan({ name: `step-${step}` }).end();
        });
      }
      await client.close();

      assert.deepEqual(spanTotals(stub), [40, 40, 40], 'four ten-span runs per request');
      assert.equal(client.getStats().dropped, 0);
    });
  });

  it('cuts a request where the byte budget runs out', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.maxBodyBytes = 64 * 1024;
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        batch: { maxItems: 100, maxBytes: 64 * 1024, flushIntervalMs: 60_000 },
      });

      // Twenty runs with ~10 KB of retrieved context each: 200 KB in all.
      const context = 'x'.repeat(10_000);
      for (let run = 0; run < 20; run += 1) client.trace({ name: `rag-${run}`, input: { context } }, () => 'ok');
      await client.close();

      const requests = stub.requestsFor('/ingest/traces');
      assert.ok(requests.length >= 4, `the backlog went out in several requests (saw ${requests.length})`);
      assert.equal(client.getStats().accepted, 20);
      assert.equal(client.getStats().dropped, 0, 'no request was refused as too large');
    });
  });

  it('halves and resends a batch the server refuses as too large', async () => {
    await withStub(async (stub, baseUrl) => {
      // The deployment's ceiling is tighter than the SDK's default and the
      // bootstrap document was never read, so the first request overshoots.
      stub.maxBatchSpans = 30;
      const collector = errorCollector();
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collector.onError,
        batch: { maxItems: 100, flushIntervalMs: 60_000 },
      });

      for (let run = 0; run < 8; run += 1) {
        client.trace(`run-${run}`, (trace) => {
          for (let step = 0; step < 10; step += 1) trace.startSpan({ name: `step-${step}` }).end();
        });
      }
      await client.close();

      const stats = client.getStats();
      assert.equal(stats.accepted, 8, 'every run arrived once the batch was split');
      assert.equal(stats.dropped, 0);
      assert.equal(stats.failedBatches, 0);
      assert.deepEqual(collector.errors, []);
    });
  });

  it('still drops, and reports, a single item that is too large on its own', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.maxBatchSpans = 5;
      const collector = errorCollector();
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collector.onError,
      });

      client.trace('small', (trace) => void trace.startSpan({ name: 'only' }).end());
      client.trace('huge', (trace) => {
        for (let step = 0; step < 10; step += 1) trace.startSpan({ name: `step-${step}` }).end();
      });
      await client.close();

      const stats = client.getStats();
      assert.equal(stats.accepted, 1, 'the run that fits was not taken down with the one that does not');
      assert.equal(stats.dropped, 1);
      assert.ok(collector.errors.some((entry) => entry.error.status === 413));
    });
  });
});

function soleSpan(stub: StubServer): Record<string, unknown> {
  const traces = stub.itemsFor('traces');
  assert.equal(traces.length, 1, `expected one trace, saw ${traces.length}`);
  const spans = (traces[0]!.spans as Record<string, unknown>[] | undefined) ?? [];
  assert.equal(spans.length, 1, `expected one span, saw ${spans.length}`);
  return spans[0]!;
}

describe('audit #75: the wrappers hand back the provider’s own objects', () => {
  const completion = {
    id: 'chatcmpl-1',
    model: 'gpt-4o-mini-2024-07-18',
    usage: { prompt_tokens: 12, completion_tokens: 3 },
    choices: [{ message: { role: 'assistant', content: 'Hi.' } }],
  };

  /** An OpenAI-shaped client whose `create` returns whatever `make` builds. */
  function openaiReturning<R>(make: () => R): { chat: { completions: { create: (body: Record<string, unknown>) => R } } } {
    return { chat: { completions: { create: (_body: Record<string, unknown>) => make() } } };
  }

  it('keeps withResponse() on the promise, and still records the call', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const original = new FakeAPIPromise<typeof completion>(
        Promise.resolve(fakeHttpResponse(completion, { 'x-ratelimit-remaining': '41' })),
      );
      const wrapped = wrapOpenAI(openaiReturning(() => original), client);

      await client.trace('ask', async () => {
        const returned = wrapped.chat.completions.create({ model: 'gpt-4o-mini' });
        assert.equal(returned, original, 'the provider’s own promise came back, not one derived from it');
        const { data, response } = await returned.withResponse();
        assert.equal(data, completion);
        assert.equal(response.headers.get('x-ratelimit-remaining'), '41');
      });
      await client.close();

      const span = soleSpan(stub);
      assert.equal(span.model, 'gpt-4o-mini-2024-07-18');
      assert.deepEqual(span.usage, { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 });
      assert.deepEqual((span.output as Record<string, unknown>).id, 'chatcmpl-1');
    });
  });

  it('leaves the body unread for a caller who asked for the raw response', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const wrapped = wrapOpenAI(
        openaiReturning(() => new FakeAPIPromise<typeof completion>(Promise.resolve(fakeHttpResponse(completion)))),
        client,
      );

      await client.trace('raw-response', async () => {
        const raw = await wrapped.chat.completions.create({ model: 'gpt-4o-mini' }).asResponse();
        assert.equal(raw.bodyUsed, false, 'telemetry did not parse a body the caller said they would read');
        assert.equal(await raw.json(), completion);
        await new Promise((resolve) => setTimeout(resolve, 40));
      });
      await client.close();

      const span = soleSpan(stub);
      assert.equal(span.output, undefined, 'the body was the caller’s, so none of it is recorded');
      assert.equal(span.error_info, undefined);
      const trace = stub.itemsFor('traces')[0]!;
      const lead = Date.parse(String(trace.end_time)) - Date.parse(String(span.end_time));
      assert.ok(lead >= 30, `the span closed when the response arrived, not when the trace did (${lead}ms)`);
    });
  });

  it('sees the outcome through catch() and finally(), not only through await', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const failing = wrapOpenAI(
        openaiReturning(
          () => new FakeAPIPromise<string>(Promise.reject(new Error('rate limited by the provider'))),
        ),
        client,
      );

      const fallback = await client.trace('handled', async () =>
        failing.chat.completions.create({ model: 'gpt-4o-mini' }).catch(() => 'fallback answer'),
      );
      assert.equal(fallback, 'fallback answer');
      await client.close();

      const span = soleSpan(stub);
      assert.equal((span.error_info as Record<string, unknown>).message, 'rate limited by the provider');
    });

    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const wrapped = wrapOpenAI(
        openaiReturning(() => new FakeAPIPromise<typeof completion>(Promise.resolve(fakeHttpResponse(completion)))),
        client,
      );
      let cleanedUp = false;
      const value = await client.trace('finally', async () =>
        wrapped.chat.completions.create({ model: 'gpt-4o-mini' }).finally(() => {
          cleanedUp = true;
        }),
      );
      assert.equal(value, completion);
      assert.ok(cleanedUp);
      await client.close();
      assert.deepEqual(soleSpan(stub).usage, { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 });
    });
  });

  it('returns the provider’s own stream, private state and tee() intact', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      async function* chunks(): AsyncGenerator<Record<string, unknown>> {
        yield { choices: [{ delta: { content: 'Hel' } }] };
        yield { choices: [{ delta: { content: 'lo' } }], usage: { prompt_tokens: 4, completion_tokens: 2 } };
      }
      const original = new FakeStream(() => chunks());
      const wrapped = wrapOpenAI(openaiReturning(() => Promise.resolve(original)), client);

      const text: string[] = [];
      await client.trace('teed', async () => {
        const stream = await wrapped.chat.completions.create({ model: 'gpt-4o-mini', stream: true });
        assert.equal(stream, original, 'the provider’s own stream came back, not a stand-in');
        const [forTheUser, forTheLog] = stream.tee();
        for await (const chunk of forTheUser) {
          const choices = chunk.choices as Array<{ delta: { content?: string } }>;
          text.push(choices[0]?.delta.content ?? '');
        }
        for await (const _chunk of forTheLog) {
          /* drain */
        }
      });
      assert.equal(text.join(''), 'Hello');
      await client.close();

      const span = soleSpan(stub);
      assert.deepEqual(span.usage, { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 });
      assert.deepEqual(span.output, { chunks: 2, text: 'Hello' });
    });
  });

  it('follows an Anthropic MessageStream through its events, with no for-await anywhere', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const original = new FakeMessageStream(
        [
          { type: 'message_start', message: { usage: { input_tokens: 90, output_tokens: 1 } } },
          { type: 'content_block_delta', delta: { text: 'Hi' } },
          { type: 'content_block_delta', delta: { text: ' there' } },
          { type: 'message_delta', usage: { output_tokens: 12 } },
        ],
        { model: 'claude-sonnet-4-5-20250929', usage: { input_tokens: 90, output_tokens: 12 }, content: [] },
      );
      const anthropic = { messages: { stream: (_body: { model: string }) => original } };
      const wrapped = wrapAnthropic(anthropic, client);

      const heard: string[] = [];
      await client.trace('documented-usage', async () => {
        const stream = wrapped.messages.stream({ model: 'claude-sonnet-4-5' });
        assert.equal(stream, original, 'the provider’s own stream came back, not a stand-in');
        stream.on('text', (delta) => heard.push(String(delta)));
        await stream.finalMessage();
      });
      assert.equal(heard.join(''), 'Hi there');
      await client.close();

      const span = soleSpan(stub);
      assert.equal(span.model, 'claude-sonnet-4-5-20250929');
      assert.equal((span.usage as Record<string, number>).total_tokens, 102);
      assert.deepEqual(span.output, { events: 4, text: 'Hi there' });
      const elapsed = Date.parse(String(span.end_time)) - Date.parse(String(span.start_time));
      assert.ok(elapsed >= 15, `the span covered the generation, not the handover (${elapsed}ms)`);
    });
  });

  it('marks the span failed when a MessageStream ends without its message', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const anthropic = {
        messages: {
          stream: (_body: { model: string }) =>
            new FakeMessageStream(
              [{ type: 'message_start', message: { usage: { input_tokens: 9 } } }],
              {},
              new Error('overloaded'),
            ),
        },
      };
      const wrapped = wrapAnthropic(anthropic, client);

      await assert.rejects(
        client.trace('fails', async () => wrapped.messages.stream({ model: 'claude-sonnet-4-5' }).finalMessage()),
        /overloaded/,
      );
      await client.close();

      const span = soleSpan(stub);
      assert.ok(span.error_info, 'the llm span is marked failed');
      assert.equal((stub.itemsFor('traces')[0]!.error_info as Record<string, unknown>).message, 'overloaded');
    });
  });

  it('runs the client’s own methods against the real client, not the stand-in', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      class FakeClient {
        #encoder = 'json';
        chat = { completions: { create: async (_body: unknown) => ({}) } };
        describe(): string {
          return `encodes as ${this.#encoder}`;
        }
        get encoding(): string {
          return this.#encoder;
        }
      }
      const wrapped = wrapOpenAI(new FakeClient(), client);

      assert.equal(wrapped.describe(), 'encodes as json');
      assert.equal(wrapped.encoding, 'json');
      assert.equal(wrapped.describe, wrapped.describe, 'a forwarded method keeps one identity');
      assert.ok(wrapped instanceof FakeClient);
      await client.close();
    });
  });
});

describe('audit #190: a streamed call keeps its usage, model and output', () => {
  it('reads usage and model off the Responses API’s terminal event, under the charted names', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      async function* events(): AsyncGenerator<Record<string, unknown>> {
        yield { type: 'response.created', response: { model: 'gpt-4.1-2025-04-14', usage: null } };
        yield { type: 'response.function_call_arguments.delta', delta: '{"city":' };
        yield { type: 'response.output_text.delta', delta: 'Sunny' };
        yield { type: 'response.output_text.delta', delta: ' today.' };
        yield {
          type: 'response.completed',
          response: {
            model: 'gpt-4.1-2025-04-14',
            usage: { input_tokens: 31, output_tokens: 7, total_tokens: 38, output_tokens_details: { reasoning_tokens: 0 } },
          },
        };
      }
      const openai = { responses: { create: async (_body: Record<string, unknown>) => events() } };
      const wrapped = wrapOpenAI(openai, client);

      await client.trace('responses-stream', async () => {
        const stream = await wrapped.responses.create({ model: 'gpt-4.1', input: 'weather?', stream: true });
        for await (const _event of stream) {
          /* drain */
        }
      });
      await client.close();

      const span = soleSpan(stub);
      assert.deepEqual(span.usage, { prompt_tokens: 31, completion_tokens: 7, total_tokens: 38 });
      assert.equal(span.model, 'gpt-4.1-2025-04-14', 'the snapshot that served the call, not the alias asked for');
      assert.deepEqual(span.output, { chunks: 5, text: 'Sunny today.' });
    });
  });

  it('waits for a stream that outlives the trace it was opened in', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      async function* chunks(): AsyncGenerator<Record<string, unknown>> {
        yield { model: 'gpt-4o-mini-2024-07-18', choices: [{ delta: { content: 'Hel' } }] };
        await new Promise((resolve) => setTimeout(resolve, 40));
        yield { model: 'gpt-4o-mini-2024-07-18', choices: [{ delta: { content: 'lo' } }] };
        yield { model: 'gpt-4o-mini-2024-07-18', choices: [], usage: { prompt_tokens: 4, completion_tokens: 2 } };
      }
      const openai = { chat: { completions: { create: async (_body: Record<string, unknown>) => chunks() } } };
      const wrapped = wrapOpenAI(openai, client);

      // The shape of a chat route: the handler's trace closes on the hand-over
      // and the stream is read afterwards, by whoever is piping it out.
      const stream = await client.trace('chat-route', async () =>
        wrapped.chat.completions.create({ model: 'gpt-4o-mini', stream: true }),
      );
      await client.flush();
      assert.equal(stub.itemsFor('traces').length, 0, 'the run is not reported while its stream is still being read');

      for await (const _chunk of stream) {
        /* the response body being piped to the browser */
      }
      await client.close();

      const span = soleSpan(stub);
      assert.deepEqual(span.usage, { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 });
      assert.deepEqual(span.output, { chunks: 3, text: 'Hello' });
      assert.equal(span.model, 'gpt-4o-mini-2024-07-18');
      const elapsed = Date.parse(String(span.end_time)) - Date.parse(String(span.start_time));
      assert.ok(elapsed >= 30, `the span covered the generation, not the hand-over (${elapsed}ms)`);
    });
  });

  it('reports a held run on close() even if nobody ever reads the stream', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      async function* chunks(): AsyncGenerator<Record<string, unknown>> {
        yield { choices: [{ delta: { content: 'never read' } }] };
      }
      const openai = { chat: { completions: { create: async (_body: Record<string, unknown>) => chunks() } } };
      const wrapped = wrapOpenAI(openai, client);

      await client.trace('abandoned', async () => wrapped.chat.completions.create({ model: 'm', stream: true }));
      await client.close();

      const span = soleSpan(stub);
      assert.ok(span.end_time, 'the span was closed on the way out rather than sent open');
    });
  });

  it('asks for stream usage only when told to, and never over the caller’s own choice', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const received: Array<Record<string, unknown>> = [];
      const openai = {
        chat: {
          completions: {
            create: async (body: Record<string, unknown>) => {
              received.push(body);
              return {};
            },
          },
        },
      };

      const request = { model: 'm', stream: true };
      await wrapOpenAI(openai, client).chat.completions.create(request);
      assert.equal(received[0], request, 'by default the provider gets the caller’s own request object');

      const asking = wrapOpenAI(openai, client, { streamUsage: true });
      await asking.chat.completions.create(request);
      assert.deepEqual(received[1], { model: 'm', stream: true, stream_options: { include_usage: true } });
      assert.deepEqual(request, { model: 'm', stream: true }, 'the caller’s object was not edited');

      await asking.chat.completions.create({ model: 'm', stream: true, stream_options: { include_usage: false } });
      assert.deepEqual(received[2]!.stream_options, { include_usage: false });

      await asking.chat.completions.create({ model: 'm' });
      assert.equal(received[3]!.stream_options, undefined, 'a call that does not stream is left alone');
      await client.close();
    });
  });

  it('gives an Anthropic model behind LangChain the same counter names', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handler = langChainHandler(client);
      handler.handleChainStart({ name: 'chain' }, { q: 'why?' }, 'r1');
      handler.handleChatModelStart({ id: ['ChatAnthropic'] }, [[{ content: 'why?' }]], 'r2', 'r1', {});
      handler.handleLLMEnd({ llmOutput: { usage: { input_tokens: 40, output_tokens: 9 } } }, 'r2');
      handler.handleChainEnd({ text: 'because' }, 'r1');
      await client.close();

      const spans = stub.itemsFor('traces')[0]!.spans as Record<string, unknown>[];
      const model = spans.find((span) => span.name === 'ChatAnthropic')!;
      assert.deepEqual(model.usage, { prompt_tokens: 40, completion_tokens: 9, total_tokens: 49 });
    });
  });
});

describe('audit #191: nothing the SDK sends makes the store refuse the whole request', () => {
  it('replaces a caller’s id that is a UUID but not a version 7 one', async () => {
    await withStub(async (stub, baseUrl) => {
      const collected = errorCollector();
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collected.onError,
      });

      // `crypto.randomUUID()` is what "supply a UUID" suggests, and it is v4.
      const v4 = '2f1d6c0e-8a4b-4c7e-9b1a-3d5e7f9a1c2b';
      const v7 = newId();
      assert.equal(isValidId(v4), false, 'a v4 UUID is not an id the store takes');
      assert.equal(isValidId(v7), true);

      client.trace('neighbour', () => undefined);
      const run = client.startTrace({ name: 'retried', id: v4 });
      const step = run.startSpan({ name: 'step', id: v4 });
      step.end();
      run.end();
      const kept = client.startTrace({ name: 'idempotent', id: v7 });
      kept.end();
      await client.close();

      assert.notEqual(run.id, v4);
      assert.ok(isValidId(run.id) && isValidId(step.id), 'the ids in use are ones the store takes');
      assert.equal(kept.id, v7, 'a version 7 id is kept, which is what makes a retry idempotent');

      const traces = stub.itemsFor('traces');
      assert.equal(traces.length, 3);
      assert.equal(client.getStats().rejected, 0, 'the neighbouring runs were not refused along with it');
      assert.deepEqual(collected.errors, []);
    });
  });

  it('always sends a traceback, even for a thrown value that has no stack', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });

      client.trace('neighbour', () => undefined);
      await assert.rejects(
        client.trace('string-thrower', async () => {
          // eslint-disable-next-line @typescript-eslint/only-throw-error
          throw 'timeout';
        }),
      );
      await client
        .trace('object-rejecter', () => Promise.reject(Object.assign(Object.create(null) as object, { code: 'E_BUSY' })))
        .catch(() => undefined);
      const stackless = new RangeError('no frames');
      stackless.stack = '';
      client.startTrace('stackless').end({ error: stackless });
      await client.close();

      const byName = new Map(stub.itemsFor('traces').map((trace) => [trace.name, trace]));
      assert.deepEqual(byName.get('string-thrower')!.error_info, {
        exception_type: 'Error',
        message: 'timeout',
        traceback: 'Error: timeout',
      });
      const rejected = byName.get('object-rejecter')!.error_info as Record<string, unknown>;
      assert.ok(typeof rejected.traceback === 'string' && rejected.traceback.length > 0);
      assert.equal((byName.get('stackless')!.error_info as Record<string, unknown>).traceback, 'RangeError: no frames');
      assert.equal(client.getStats().rejected, 0);
    });
  });

  it('sends a score’s source in the lower case the store’s enum is written in', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const run = client.startTrace('scored');
      run.score({ name: 'helpful', value: 1, source: 'UI' });
      run.end();
      client.score({ id: run.id, name: 'thumbs', value: 1, source: 'Online_Scoring' });
      await client.close();

      const attached = stub.itemsFor('traces')[0]!.feedback_scores as Array<Record<string, unknown>>;
      assert.equal(attached[0]!.source, 'ui');
      assert.equal(stub.itemsFor('scores')[0]!.source, 'online_scoring');
    });
  });
});

describe('audit #192: streamed spans follow their trace', () => {
  it('posts no spans for a run that sampling excluded', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        streamSpans: true,
        samplingRate: 0,
      });
      await client.trace('unsampled', async (trace) => {
        await client.span({ name: 'plan', type: 'llm', model: 'gpt-4o-mini' }, async (span) => {
          span.startSpan({ name: 'lookup', type: 'tool' }).end();
        });
        trace.startSpan({ name: 'by-hand' }).end();
      });
      // A span opened with nothing in scope brings its own trace, which
      // sampling excludes just the same.
      await client.span({ name: 'stray', type: 'llm' }, async () => 'ok');
      await client.close();

      assert.equal(stub.itemsFor('traces').length, 0);
      assert.equal(stub.itemsFor('spans').length, 0, 'spans of an excluded run are orphans, and each is billed');
      assert.equal(stub.requestsFor('/ingest/spans').length, 0);
    });
  });

  it('still streams the spans of a run that is reported', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        streamSpans: true,
      });
      const kept = client.startTrace({ name: 'kept', sampled: true });
      kept.startSpan({ name: 'a' }).end();
      const dropped = client.startTrace({ name: 'dropped', sampled: false });
      dropped.startSpan({ name: 'b' }).end();
      kept.end();
      dropped.end();
      await client.close();

      assert.deepEqual(stub.itemsFor('spans').map((span) => span.name), ['a']);
      assert.deepEqual(stub.itemsFor('traces').map((trace) => trace.name), ['kept']);
    });
  });

  it('names the model that ran on a streamed trace, which carries no spans to read it from', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        streamSpans: true,
      });
      await client.trace('two-models', async (trace) => {
        trace.startSpan({ name: 'draft', type: 'llm', model: 'gpt-4o-mini' }).end();
        trace.startSpan({ name: 'embed', type: 'tool', model: 'text-embedding-3-small' }).end();
        const final = trace.startSpan({ name: 'final', type: 'llm' });
        final.end({ model: 'gpt-4o-2024-08-06' });
        trace.startSpan({ name: 'format', type: 'general' }).end();
      });
      client.trace({ name: 'stated', metadata: { model: 'the-callers-own' } }, (trace) => {
        trace.startSpan({ name: 'llm', type: 'llm', model: 'gpt-4o-mini' }).end();
      });
      client.trace('no-model', (trace) => {
        trace.startSpan({ name: 'tool', type: 'tool' }).end();
      });
      await client.close();

      const byName = new Map(stub.itemsFor('traces').map((trace) => [trace.name, trace]));
      assert.equal((byName.get('two-models')!.metadata as Record<string, unknown>).model, 'gpt-4o-2024-08-06');
      assert.equal((byName.get('stated')!.metadata as Record<string, unknown>).model, 'the-callers-own');
      assert.equal(byName.get('no-model')!.metadata, undefined, 'a model nobody measured is not made up');
      assert.equal(byName.get('two-models')!.spans, undefined);
    });
  });

  it('leaves a nested trace’s metadata alone: the control plane reads the spans it was sent', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('nested', (trace) => {
        trace.startSpan({ name: 'llm', type: 'llm', model: 'gpt-4o-mini' }).end();
      });
      await client.close();
      assert.equal(stub.itemsFor('traces')[0]!.metadata, undefined);
    });
  });
});

describe('audit #193: a span with no trace above it still makes a whole run', () => {
  it('puts what the caller said about the work on the run, not only on the step inside it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        environment: 'staging',
      });
      const started = new Date(Date.now() - 1_500);
      const span = client.startSpan({
        name: 'answer',
        type: 'llm',
        model: 'gpt-4o-mini',
        input: { question: 'why?' },
        metadata: { tenant: 'acme' },
        tags: ['support'],
        startTime: started,
      });
      span.setOutput({ text: 'because' });
      span.end();
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.deepEqual(trace.input, { question: 'why?' });
      assert.deepEqual(trace.output, { text: 'because' }, 'an output set before end() reaches the run too');
      assert.deepEqual(trace.tags, ['support']);
      assert.deepEqual(trace.metadata, { environment: 'staging', tenant: 'acme' });
      assert.equal(trace.start_time, started.toISOString());
      const spans = trace.spans as Array<Record<string, unknown>>;
      assert.equal(spans.length, 1);
      assert.equal(spans[0]!.type, 'llm');
      assert.deepEqual(spans[0]!.input, { question: 'why?' });
    });
  });

  it('gives a drop-in wrapped call a run that shows its prompt and its answer', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const openai = {
        chat: {
          completions: {
            create: async (_body: Record<string, unknown>) => ({
              model: 'gpt-4o-mini-2024-07-18',
              choices: [{ message: { role: 'assistant', content: 'Hello.' } }],
              usage: { prompt_tokens: 5, completion_tokens: 2 },
            }),
          },
        },
      };
      // No `client.trace()` anywhere: the wrapper is the whole integration.
      await wrapOpenAI(openai, client).chat.completions.create({
        model: 'gpt-4o-mini',
        messages: [{ role: 'user', content: 'Say hello.' }],
      });
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(trace.name, 'openai.chat.completions.create');
      assert.deepEqual((trace.input as Record<string, unknown>).messages, [{ role: 'user', content: 'Say hello.' }]);
      assert.equal(((trace.output as Record<string, unknown>).choices as unknown[]).length, 1);
    });
  });

  it('records a failure raised inside a root span on the run as well', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await assert.rejects(
        client.span({ name: 'flaky', type: 'tool' }, async () => {
          throw new TypeError('socket closed');
        }),
      );
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal((trace.error_info as Record<string, unknown>).exception_type, 'TypeError');
      const spans = trace.spans as Array<Record<string, unknown>>;
      assert.equal((spans[0]!.error_info as Record<string, unknown>).message, 'socket closed');
    });
  });

  it('has the run in scope inside a root span, so traced() helpers nest instead of splitting off', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const fetchDocs = client.traced(async function fetchDocs(): Promise<string[]> {
        return ['a', 'b'];
      });
      const rank = client.traced(async function rank(docs: string[]): Promise<string> {
        return docs[0]!;
      });
      const answer = client.traced(
        async function answer(): Promise<string> {
          assert.ok(client.currentTrace(), 'the run is in scope inside its root span');
          assert.equal(client.currentTrace()!.id, client.currentSpan()!.traceId);
          return rank(await fetchDocs());
        },
        { asSpan: true },
      );

      assert.equal(await answer(), 'a');
      await client.close();

      const traces = stub.itemsFor('traces');
      assert.equal(traces.length, 1, 'one call graph is one run, not three');
      const spans = traces[0]!.spans as Array<Record<string, unknown>>;
      assert.deepEqual(spans.map((span) => span.name).sort(), ['answer', 'fetchDocs', 'rank']);
      const root = spans.find((span) => span.name === 'answer')!;
      for (const name of ['fetchDocs', 'rank']) {
        assert.equal(spans.find((span) => span.name === name)!.parent_span_id, root.id);
      }
    });
  });

  it('opens a typed root traced() call as a run with its typed span, not a run with no steps', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const callModel = client.traced(
        async function callModel(prompt: string): Promise<string> {
          client.currentSpan()!.setModel('gpt-4o-2024-08-06').setUsage({ prompt_tokens: 11, completion_tokens: 4 });
          return prompt.toUpperCase();
        },
        { type: 'llm', model: 'gpt-4o', provider: 'openai', threadId: 'conversation-7', tags: ['chat'] },
      );

      assert.equal(await callModel('hi'), 'HI');
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(trace.name, 'callModel');
      assert.equal(trace.thread_id, 'conversation-7');
      assert.deepEqual(trace.tags, ['chat']);
      assert.deepEqual(trace.input, { value: 'hi' });
      assert.deepEqual(trace.output, { value: 'HI' });
      const spans = (trace.spans as Array<Record<string, unknown>> | undefined) ?? [];
      assert.equal(spans.length, 1, 'the run has the step that is its whole body');
      assert.equal(spans[0]!.type, 'llm');
      assert.equal(spans[0]!.model, 'gpt-4o-2024-08-06');
      assert.equal(spans[0]!.provider, 'openai');
      assert.deepEqual(spans[0]!.usage, { prompt_tokens: 11, completion_tokens: 4, total_tokens: 15 });
    });
  });

  it('does the same for trace() when span-only options reach it unchecked, as they do from JavaScript', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const id = newId();
      // What a JavaScript caller, or a spread options object, can pass.
      const options = { name: 'complete', id, type: 'llm', model: 'gpt-4o-mini' } as Record<string, unknown>;
      const result = await client.trace(options, async (trace) => {
        assert.equal(trace.id, id, 'the body still gets the trace, under the id the caller gave it');
        assert.equal(client.currentSpan()?.traceId, id);
        trace.addTags('from-the-body');
        return 'text';
      });
      assert.equal(result, 'text');
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(trace.id, id);
      assert.deepEqual(trace.tags, ['from-the-body']);
      assert.deepEqual(trace.output, { value: 'text' });
      const spans = trace.spans as Array<Record<string, unknown>>;
      assert.equal(spans.length, 1);
      assert.equal(spans[0]!.type, 'llm');
      assert.equal(spans[0]!.model, 'gpt-4o-mini');
      assert.notEqual(spans[0]!.id, id, 'one id is not used twice');
    });
  });

  it('keeps a typed traced() call a run of its own when told to, even inside another', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      setDefaultClient(client);
      try {
        const detached = traced(async () => 'ok', { name: 'detached', type: 'tool', asSpan: false });
        await client.trace('outer', async () => detached());
      } finally {
        setDefaultClient(undefined);
      }
      await client.close();

      const byName = new Map(stub.itemsFor('traces').map((trace) => [trace.name, trace]));
      assert.equal(byName.size, 2);
      assert.equal(byName.get('outer')!.spans, undefined);
      const spans = byName.get('detached')!.spans as Array<Record<string, unknown>>;
      assert.equal(spans[0]!.type, 'tool');
      assert.equal(spans[0]!.trace_id, byName.get('detached')!.id);
    });
  });
});

/** Run `body` with these environment variables, then put the old ones back. */
async function withEnv<T>(values: Record<string, string | undefined>, body: () => T | Promise<T>): Promise<T> {
  const previous: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(values)) {
    previous[key] = process.env[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try {
    return await body();
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

describe('audit #227: one fleet-wide environment block means the same to both SDKs', () => {
  const clean = {
    FULCRUM_OPS_DISABLED: undefined,
    FULCRUM_OPS_TIMEOUT_MS: undefined,
    FULCRUM_OPS_TIMEOUT_SECONDS: undefined,
  };

  it('honours the FULCRUM_OPS_DISABLED kill switch, and sends nothing', async () => {
    await withStub(async (stub, baseUrl) => {
      await withEnv({ ...clean, FULCRUM_OPS_DISABLED: '1' }, async () => {
        assert.equal(resolveOptions({ apiKey: 'k' }).enabled, false);
        assert.equal(resolveOptions({ apiKey: 'k', enabled: true }).enabled, true, 'an explicit option still wins');

        const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });
        assert.equal(await client.trace('silenced', async () => 'still ran'), 'still ran');
        client.score({ id: newId(), name: 'thumbs', value: 1 });
        await client.close();
      });
      assert.equal(stub.requests.length, 0, 'not the bootstrap fetch, not a batch');
    });
  });

  it('does not read a falsy or unparseable FULCRUM_OPS_DISABLED as "off"', async () => {
    for (const value of ['0', 'false', 'no', 'maybe']) {
      await withEnv({ ...clean, FULCRUM_OPS_DISABLED: value }, () => {
        assert.equal(resolveOptions({ apiKey: 'k' }).enabled, true, `FULCRUM_OPS_DISABLED=${value}`);
      });
    }
    await withEnv({ ...clean, FULCRUM_OPS_DISABLED: 'TRUE' }, () => {
      assert.equal(resolveOptions({ apiKey: 'k' }).enabled, false);
    });
  });

  it('reads the timeout under the Python SDK’s name and unit as well as its own', async () => {
    await withEnv({ ...clean, FULCRUM_OPS_TIMEOUT_SECONDS: '5' }, () => {
      assert.equal(resolveOptions({ apiKey: 'k' }).timeoutMs, 5_000);
    });
    await withEnv({ ...clean, FULCRUM_OPS_TIMEOUT_SECONDS: '0.25' }, () => {
      assert.equal(resolveOptions({ apiKey: 'k' }).timeoutMs, 250);
    });
    await withEnv({ ...clean, FULCRUM_OPS_TIMEOUT_SECONDS: '5', FULCRUM_OPS_TIMEOUT_MS: '1500' }, () => {
      assert.equal(resolveOptions({ apiKey: 'k' }).timeoutMs, 1_500, 'this SDK’s own name wins when both are set');
      assert.equal(resolveOptions({ apiKey: 'k', timeoutMs: 900 }).timeoutMs, 900, 'and the option wins over both');
    });
    await withEnv({ ...clean, FULCRUM_OPS_TIMEOUT_SECONDS: 'soon' }, () => {
      assert.equal(resolveOptions({ apiKey: 'k' }).timeoutMs, 30_000);
    });
  });
});

/** The compiled SDK, as a child process started from this test would load it. */
const SDK_ENTRY = join(__dirname, '..', 'src', 'index.js');

/**
 * Run a script in a process of its own and report how it ended.
 *
 * What a process does as it exits cannot be observed from inside it. The child
 * is given `deadlineMs` to end by itself; one that is still running then is
 * killed, and says so.
 */
function runToExit(script: string, deadlineMs: number): Promise<{ code: number | null; timedOut: boolean; elapsedMs: number }> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const child = spawn(process.execPath, ['-e', script], { stdio: 'ignore' });
    let timedOut = false;
    const deadline = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, deadlineMs);
    child.on('error', reject);
    child.on('exit', (code) => {
      clearTimeout(deadline);
      resolve({ code, timedOut, elapsedMs: Date.now() - started });
    });
  });
}

describe('audit #226: the SDK neither outstays a finished process nor misses a stopped one', () => {
  it('does not keep a finished process alive retrying a bootstrap fetch nobody asked for', async () => {
    // A control plane that accepts the connection and never answers.
    let requests = 0;
    const hung = createServer(() => {
      requests += 1;
    });
    await new Promise<void>((resolve) => hung.listen(0, '127.0.0.1', resolve));
    const baseUrl = `http://127.0.0.1:${(hung.address() as AddressInfo).port}/api/v1`;
    try {
      const script = `
        const { FulcrumOps } = require(${JSON.stringify(SDK_ENTRY)});
        new FulcrumOps({
          apiKey: 'k',
          baseUrl: ${JSON.stringify(baseUrl)},
          timeoutMs: 300,
          retry: { maxAttempts: 3, backoffMs: 200, maxBackoffMs: 400 },
        });
        // ...and that is the whole job: the script has nothing left to do.
      `;
      const outcome = await runToExit(script, 15_000);
      assert.equal(outcome.timedOut, false, 'the process ended by itself');
      assert.equal(outcome.code, 0);
      assert.equal(requests, 1, 'one short attempt, and the waits between retries do not hold the process');
    } finally {
      (hung as unknown as { closeAllConnections?: () => void }).closeAllConnections?.();
      await new Promise<void>((resolve) => hung.close(() => resolve()));
    }
  });

  it('still retries the bootstrap fetch in a process that stays up', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('GET', '/api/v1/ingest/config', { status: 503, times: 2 });
      const collected = errorCollector();
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        retry: { maxAttempts: 3, backoffMs: 5, maxBackoffMs: 10 },
        onError: collected.onError,
      });
      const deadline = Date.now() + 5_000;
      while (!client.getCachedConfig() && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.equal(client.getCachedConfig()?.revision, 'rev-1', 'the workspace’s settings were adopted on the third try');
      assert.equal(stub.requestsFor('/ingest/config').length, 3);
      assert.deepEqual(collected.errors, [], 'a retry that worked is not an error');
      await client.close();
    });
  });

  it('gives a caller who awaits config() the full retry policy, not the background attempt’s one try', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('GET', '/api/v1/ingest/config', { status: 503, times: 1 });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        retry: { maxAttempts: 2, backoffMs: 1, maxBackoffMs: 2 },
      });
      const config = await client.config();
      assert.equal(config?.revision, 'rev-1');
      await client.close();
    });
  });

  it('flushes what is queued when the process is told to terminate, then lets the signal do its work', async () => {
    await withStub(async (stub, baseUrl) => {
      const script = `
        const { FulcrumOps } = require(${JSON.stringify(SDK_ENTRY)});
        const client = new FulcrumOps({
          apiKey: 'k',
          baseUrl: ${JSON.stringify(baseUrl)},
          bootstrap: false,
          batch: { flushIntervalMs: 60000 },
        });
        client.trace('last-run-before-the-deploy', () => 'done');
        setInterval(() => undefined, 1000); // a server: up until told otherwise
        setTimeout(() => process.emit('SIGTERM', 'SIGTERM'), 50);
      `;
      const outcome = await runToExit(script, 15_000);
      assert.equal(outcome.timedOut, false, 'the signal still ended the process');
      assert.notEqual(outcome.code, 0, 'and it ended as a terminated process, not as a clean exit');
      assert.deepEqual(stub.itemsFor('traces').map((trace) => trace.name), ['last-run-before-the-deploy']);
    });
  });

  it('leaves the shutdown to an application that handles the signal itself', async () => {
    await withStub(async (stub, baseUrl) => {
      const script = `
        const { FulcrumOps } = require(${JSON.stringify(SDK_ENTRY)});
        const client = new FulcrumOps({
          apiKey: 'k',
          baseUrl: ${JSON.stringify(baseUrl)},
          bootstrap: false,
          batch: { flushIntervalMs: 60000 },
        });
        let handled = 0;
        process.on('SIGTERM', () => {
          handled += 1;
          // The application's own graceful shutdown, which takes a moment.
          setTimeout(() => process.exit(handled === 1 ? 7 : 8), 400);
        });
        client.trace('drained-by-the-app', () => 'done');
        setInterval(() => undefined, 1000);
        setTimeout(() => process.emit('SIGTERM', 'SIGTERM'), 50);
      `;
      const outcome = await runToExit(script, 15_000);
      assert.equal(outcome.code, 7, 'their handler ran once and chose the exit code; nothing was re-raised over it');
      assert.ok(outcome.elapsedMs >= 400, 'and their shutdown was not cut short');
      assert.deepEqual(stub.itemsFor('traces').map((trace) => trace.name), ['drained-by-the-app']);
    });
  });

  it('takes its signal listeners away with the last client, so Ctrl+C is not swallowed', async () => {
    // In a process of its own: the count is only meaningful where no other
    // test has a client open.
    const script = `
      const { FulcrumOps } = require(${JSON.stringify(SDK_ENTRY)});
      const counts = () => [process.listenerCount('SIGTERM'), process.listenerCount('SIGINT')].join();
      const expect = (label, actual, wanted) => {
        if (actual !== wanted) { console.error(label, actual, wanted); process.exit(3); }
      };
      (async () => {
        expect('before', counts(), '0,0');
        const optedOut = new FulcrumOps({ apiKey: 'k', bootstrap: false, flushOnExit: false });
        expect('flushOnExit: false installs nothing', counts(), '0,0');
        const first = new FulcrumOps({ apiKey: 'k', bootstrap: false });
        const second = new FulcrumOps({ apiKey: 'k', bootstrap: false });
        expect('one listener however many clients', counts(), '1,1');
        await first.close();
        expect('still one while a client is open', counts(), '1,1');
        await second.close();
        expect('gone with the last client', counts(), '0,0');
        await optedOut.close();
      })();
    `;
    const outcome = await runToExit(script, 15_000);
    assert.equal(outcome.timedOut, false);
    assert.equal(outcome.code, 0);
  });
});
