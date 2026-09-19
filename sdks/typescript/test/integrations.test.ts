/**
 * The provider wrappers and the LangChain.js callback handler.
 *
 * None of these tests install `openai`, `@anthropic-ai/sdk` or
 * `@langchain/core`, which is the point: the wrappers take an instance the
 * caller already built and never import the package themselves, so a stand-in
 * with the same method shape exercises exactly the code path a real client
 * would take.
 *
 * The streaming assertions matter most. A streamed call resolves before the
 * first token, so a wrapper that closes its span on resolution reports a 40ms
 * duration for a 12-second generation and loses the usage numbers entirely.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps } from '../src/index.js';
import { wrapAnthropic } from '../src/integrations/anthropic.js';
import { wrapOpenAI } from '../src/integrations/openai.js';
import { FulcrumOpsCallbackHandler, langChainHandler } from '../src/integrations/langchain.js';
import { withStub } from './helpers/stub-server.js';
import type { StubServer } from './helpers/stub-server.js';

function spansOfSoleTrace(stub: StubServer): Record<string, unknown>[] {
  const traces = stub.itemsFor('traces');
  assert.equal(traces.length, 1, `expected one trace, saw ${traces.length}`);
  return (traces[0]!.spans as Record<string, unknown>[] | undefined) ?? [];
}

describe('wrapOpenAI', () => {
  it('opens an llm span around chat.completions.create and returns the response untouched', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const response = {
        id: 'chatcmpl-1',
        model: 'gpt-4o-mini-2024-07-18',
        usage: { prompt_tokens: 120, completion_tokens: 30 },
        choices: [{ message: { role: 'assistant', content: 'Hello.' } }],
      };
      const openai = {
        chat: {
          completions: {
            create: async (_body: { model: string; messages: unknown[] }) => response,
          },
        },
      };

      const wrapped = wrapOpenAI(openai, client);
      const returned = await client.trace('ask', async () =>
        wrapped.chat.completions.create({ model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'hi' }] }),
      );
      assert.equal(returned, response, 'the provider’s own object came back, not a copy');
      await client.close();

      const spans = spansOfSoleTrace(stub);
      assert.equal(spans.length, 1);
      const span = spans[0]!;
      assert.equal(span.name, 'openai.chat.completions.create');
      assert.equal(span.type, 'llm');
      assert.equal(span.provider, 'openai');
      // The model recorded is the one the response reported, not the one asked
      // for: a request for `gpt-4o-mini` is served by a dated snapshot, and the
      // cost breakdown wants the snapshot.
      assert.equal(span.model, 'gpt-4o-mini-2024-07-18');
      assert.deepEqual(span.usage, { prompt_tokens: 120, completion_tokens: 30, total_tokens: 150 });
    });
  });

  it('keeps the span open until a stream is exhausted', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });

      async function* chunks(): AsyncGenerator<unknown> {
        yield { choices: [{ delta: { content: 'Hel' } }] };
        await new Promise((resolve) => setTimeout(resolve, 30));
        yield { choices: [{ delta: { content: 'lo' } }], usage: { prompt_tokens: 4, completion_tokens: 2 } };
      }
      const openai = {
        chat: { completions: { create: async (_body: { model: string; stream: boolean }) => chunks() } },
      };

      const wrapped = wrapOpenAI(openai, client);
      const text: string[] = [];
      await client.trace('stream', async () => {
        const stream = await wrapped.chat.completions.create({ model: 'gpt-4o-mini', stream: true });
        for await (const chunk of stream as AsyncIterable<Record<string, unknown>>) {
          const choices = chunk.choices as Array<{ delta: { content?: string } }>;
          if (choices[0]?.delta.content) text.push(choices[0].delta.content);
        }
      });
      assert.equal(text.join(''), 'Hello', 'the caller read every chunk');
      await client.close();

      const span = spansOfSoleTrace(stub)[0]!;
      assert.deepEqual(span.usage, { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 });
      const elapsed = Date.parse(String(span.end_time)) - Date.parse(String(span.start_time));
      assert.ok(elapsed >= 25, `the span covered the generation, not the handover (${elapsed}ms)`);
      assert.deepEqual(span.output, { chunks: 2, text: 'Hello' });
    });
  });

  it('records a provider failure on the span and re-throws it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const openai = {
        chat: {
          completions: {
            create: async (_body: unknown): Promise<unknown> => {
              throw new Error('rate limited by the provider');
            },
          },
        },
      };
      const wrapped = wrapOpenAI(openai, client);

      await assert.rejects(
        client.trace('failing', async () => wrapped.chat.completions.create({})),
        /rate limited by the provider/,
      );
      await client.close();

      const span = spansOfSoleTrace(stub)[0]!;
      assert.equal((span.error_info as Record<string, unknown>).message, 'rate limited by the provider');
    });
  });

  it('leaves every other property of the client reachable', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const openai = {
        apiKey: 'sk-test',
        baseURL: 'https://example.invalid/v1',
        chat: { completions: { create: async () => ({}), stream: 'untouched' } },
        embeddings: { create: async (_body: unknown) => ({ data: [] }) },
      };
      const wrapped = wrapOpenAI(openai, client);

      assert.equal(wrapped.apiKey, 'sk-test');
      assert.equal(wrapped.baseURL, 'https://example.invalid/v1');
      assert.equal(wrapped.chat.completions.stream, 'untouched');
      await client.close();
    });
  });

  it('honours captureInput: false', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const openai = { chat: { completions: { create: async (_body: unknown) => ({ model: 'm' }) } } };
      const wrapped = wrapOpenAI(openai, client, { captureInput: false });

      await client.trace('quiet', async () =>
        wrapped.chat.completions.create({ messages: [{ role: 'user', content: 'a secret' }] }),
      );
      await client.close();

      assert.equal(spansOfSoleTrace(stub)[0]!.input, undefined);
    });
  });
});

describe('wrapAnthropic', () => {
  it('records Anthropic’s token counters under the names the console charts, and derives the total', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const anthropic = {
        messages: {
          create: async (_body: { model: string; messages: unknown[] }) => ({
            id: 'msg_1',
            model: 'claude-sonnet-4-5',
            usage: { input_tokens: 200, output_tokens: 50 },
            content: [{ type: 'text', text: 'Hello there.' }],
          }),
        },
      };

      const wrapped = wrapAnthropic(anthropic, client);
      await client.trace('ask', async () =>
        wrapped.messages.create({ model: 'claude-sonnet-4-5', messages: [{ role: 'user', content: 'hi' }] }),
      );
      await client.close();

      const span = spansOfSoleTrace(stub)[0]!;
      assert.equal(span.name, 'anthropic.messages.create');
      assert.equal(span.type, 'llm');
      assert.equal(span.provider, 'anthropic');
      assert.equal(span.model, 'claude-sonnet-4-5');
      assert.deepEqual(span.usage, { prompt_tokens: 200, completion_tokens: 50, total_tokens: 250 });
      assert.equal((span.output as Record<string, unknown>).text, 'Hello there.');
    });
  });

  it('accumulates usage split across message_start and message_delta', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });

      async function* events(): AsyncGenerator<unknown> {
        yield { type: 'message_start', message: { usage: { input_tokens: 90, output_tokens: 1 } } };
        yield { type: 'content_block_delta', delta: { text: 'Hi' } };
        yield { type: 'content_block_delta', delta: { text: ' there' } };
        // Anthropic reports output tokens cumulatively, so the later value
        // replaces the earlier one rather than adding to it.
        yield { type: 'message_delta', usage: { output_tokens: 12 } };
      }
      const anthropic = { messages: { stream: (_body: { model: string }) => events() } };

      const wrapped = wrapAnthropic(anthropic, client);
      await client.trace('streamed', async () => {
        const stream = wrapped.messages.stream({ model: 'claude-sonnet-4-5' });
        for await (const _event of stream as AsyncIterable<unknown>) {
          /* drain */
        }
      });
      await client.close();

      const span = spansOfSoleTrace(stub)[0]!;
      assert.deepEqual(span.usage, { prompt_tokens: 90, completion_tokens: 12, total_tokens: 102 });
      assert.deepEqual(span.output, { events: 4, text: 'Hi there' });
    });
  });
});

describe('the LangChain.js callback handler', () => {
  it('rebuilds LangChain’s run tree from runId and parentRunId', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handler = langChainHandler(client);

      // The event sequence LangChain emits for a retrieval chain. Note there is
      // no ambient async context here at all — by design, since LangChain runs
      // its callbacks from its own scheduler.
      handler.handleChainStart({ id: ['langchain', 'chains', 'RetrievalQA'] }, { query: 'why?' }, 'run-root');
      handler.handleRetrieverStart({ id: ['VectorStoreRetriever'] }, 'why?', 'run-retriever', 'run-root');
      handler.handleRetrieverEnd([{ pageContent: 'a' }, { pageContent: 'b' }], 'run-retriever');
      handler.handleChatModelStart(
        { id: ['ChatOpenAI'] },
        [[{ content: 'why?' }]],
        'run-model',
        'run-root',
        { invocation_params: { model: 'gpt-4o' } },
      );
      handler.handleLLMEnd({ llmOutput: { tokenUsage: { promptTokens: 55, completionTokens: 12 } } }, 'run-model');
      handler.handleChainEnd({ text: 'because' }, 'run-root');
      await client.close();

      const traces = stub.itemsFor('traces');
      assert.equal(traces.length, 1);
      const trace = traces[0]!;
      assert.equal(trace.name, 'RetrievalQA');

      const spans = trace.spans as Record<string, unknown>[];
      assert.equal(spans.length, 3);
      const root = spans.find((span) => span.name === 'RetrievalQA')!;
      const retriever = spans.find((span) => span.name === 'VectorStoreRetriever')!;
      const model = spans.find((span) => span.name === 'ChatOpenAI')!;

      assert.equal(root.parent_span_id, undefined);
      assert.equal(retriever.parent_span_id, root.id, 'the retriever hangs off the chain');
      assert.equal(model.parent_span_id, root.id, 'so does the model');
      assert.equal(retriever.type, 'tool');
      assert.equal((retriever.metadata as Record<string, unknown>).document_count, 2);
      assert.equal(model.type, 'llm');
      assert.equal(model.model, 'gpt-4o');
      // LangChain says `promptTokens`; the ingest contract says `prompt_tokens`.
      assert.deepEqual(model.usage, { prompt_tokens: 55, completion_tokens: 12, total_tokens: 67 });
    });
  });

  it('records a tool failure without throwing into LangChain’s scheduler', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handler = new FulcrumOpsCallbackHandler(client, { tags: ['agent-run'] });

      handler.handleChainStart({ name: 'AgentExecutor' }, { input: 'book a flight' }, 'r1');
      handler.handleToolStart({ name: 'search_flights' }, 'LHR->JFK', 'r2', 'r1');
      handler.handleToolError(new Error('the upstream API is down'), 'r2');
      handler.handleChainError(new Error('the tool failed'), 'r1');
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal((trace.error_info as Record<string, unknown>).message, 'the tool failed');
      const tool = (trace.spans as Record<string, unknown>[]).find((span) => span.name === 'search_flights')!;
      assert.equal((tool.error_info as Record<string, unknown>).message, 'the upstream API is down');
      assert.ok((tool.tags as string[]).includes('agent-run'));
    });
  });

  it('ignores an end callback for a run it never saw', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handler = langChainHandler(client);
      handler.handleChainEnd({}, 'never-started');
      handler.handleLLMError(new Error('x'), 'never-started');
      await client.close();
      assert.equal(stub.itemsFor('traces').length, 0);
    });
  });

  it('closes runs abandoned mid-flight', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handler = langChainHandler(client);
      handler.handleChainStart({ name: 'Abandoned' }, {}, 'r1');
      handler.flushOpenRuns();
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(trace.name, 'Abandoned');
      assert.ok(trace.end_time, 'the abandoned run was closed rather than left open forever');
    });
  });
});
