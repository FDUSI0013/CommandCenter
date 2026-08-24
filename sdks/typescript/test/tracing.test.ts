/**
 * Traces, spans, nesting, and the promise that telemetry never changes what the
 * traced code does.
 *
 * The nesting assertions are the ones worth having: `parent_span_id` is the
 * only thing that turns a flat list of spans into the tree Replay Studio draws,
 * and it is produced by machinery (`AsyncLocalStorage`, a fallback stack, an
 * explicit parent) that has three separate implementations to get wrong.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps, setDefaultClient, traced } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';
import type { StubServer } from './helpers/stub-server.js';

/** The one trace the stub received, asserted to exist. */
function soleTrace(stub: StubServer): Record<string, unknown> {
  const traces = stub.itemsFor('traces');
  assert.equal(traces.length, 1, `expected exactly one trace, saw ${traces.length}`);
  return traces[0]!;
}

function spansOf(trace: Record<string, unknown>): Record<string, unknown>[] {
  return (trace.spans as Record<string, unknown>[] | undefined) ?? [];
}

function spanNamed(trace: Record<string, unknown>, name: string): Record<string, unknown> {
  const found = spansOf(trace).find((span) => span.name === name);
  assert.ok(found, `expected a span named "${name}", saw ${spansOf(trace).map((s) => s.name).join(', ')}`);
  return found;
}

describe('trace()', () => {
  it('returns what a synchronous body returned', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const result = client.trace('sync', () => 'the value');
      assert.equal(result, 'the value');
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(trace.name, 'sync');
      assert.deepEqual(trace.output, { value: 'the value' });
      assert.ok(typeof trace.start_time === 'string' && typeof trace.end_time === 'string');
    });
  });

  it('returns what an async body resolved to, and closes when it settles', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const result = await client.trace('async', async () => {
        await new Promise((resolve) => setTimeout(resolve, 20));
        return { answer: 42 };
      });
      assert.deepEqual(result, { answer: 42 });
      await client.close();

      const trace = soleTrace(stub);
      assert.deepEqual(trace.output, { answer: 42 });
      // The end time must reflect the awaited work, not the synchronous return.
      const elapsed = Date.parse(String(trace.end_time)) - Date.parse(String(trace.start_time));
      assert.ok(elapsed >= 15, `expected the trace to span the await, saw ${elapsed}ms`);
    });
  });

  it('re-throws the body’s error untouched and records it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const thrown = new TypeError('the caller’s own failure');

      assert.throws(
        () =>
          client.trace('boom', () => {
            throw thrown;
          }),
        (error: unknown) => error === thrown,
      );
      await client.close();

      const trace = soleTrace(stub);
      const errorInfo = trace.error_info as Record<string, unknown>;
      assert.equal(errorInfo.exception_type, 'TypeError');
      assert.equal(errorInfo.message, 'the caller’s own failure');
      assert.ok(typeof errorInfo.traceback === 'string');
    });
  });

  it('re-throws an async body’s rejection and records it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await assert.rejects(
        client.trace('async-boom', async () => {
          throw new RangeError('later');
        }),
        RangeError,
      );
      await client.close();
      assert.equal((soleTrace(stub).error_info as Record<string, unknown>).exception_type, 'RangeError');
    });
  });

  it('records the environment on the trace’s metadata', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        environment: 'Staging',
        bootstrap: false,
        setAsDefault: false,
      });
      client.trace('with-env', () => undefined);
      await client.close();
      assert.equal((soleTrace(stub).metadata as Record<string, unknown>).environment, 'Staging');
    });
  });

  it('carries the thread id that groups a conversation', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace({ name: 'turn-2', threadId: 'conversation-9' }, () => undefined);
      await client.close();
      assert.equal(soleTrace(stub).thread_id, 'conversation-9');
    });
  });
});

describe('span nesting on Node', () => {
  it('uses AsyncLocalStorage and builds the tree from ambient context', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      assert.equal(client.contextBackend, 'async-local-storage');

      await client.trace('support-question', async () => {
        await client.span('retrieve', async () => {
          await client.span({ name: 'embed', type: 'llm', model: 'text-embedding-3-small' }, async () => 'vector');
        });
        await client.span({ name: 'answer', type: 'llm' }, async () => 'the answer');
      });
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(spansOf(trace).length, 3);

      const retrieve = spanNamed(trace, 'retrieve');
      const embed = spanNamed(trace, 'embed');
      const answer = spanNamed(trace, 'answer');

      assert.equal(retrieve.trace_id, trace.id);
      assert.equal(retrieve.parent_span_id, undefined, 'a top-level span hangs off the trace');
      assert.equal(embed.parent_span_id, retrieve.id, 'the inner span nests under the outer one');
      assert.equal(answer.parent_span_id, undefined, 'the sibling is not captured by the closed span');
      assert.equal(embed.type, 'llm');
      assert.equal(embed.model, 'text-embedding-3-small');
    });
  });

  it('keeps two concurrent traces apart', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });

      const one = client.trace('first', async () => {
        await new Promise((resolve) => setTimeout(resolve, 25));
        return client.span('first-child', async () => 'a');
      });
      const two = client.trace('second', async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        return client.span('second-child', async () => 'b');
      });
      await Promise.all([one, two]);
      await client.close();

      const traces = stub.itemsFor('traces');
      assert.equal(traces.length, 2);
      for (const trace of traces) {
        const spans = spansOf(trace);
        assert.equal(spans.length, 1, `${String(trace.name)} kept exactly its own span`);
        assert.equal(spans[0]!.trace_id, trace.id);
        assert.ok(String(spans[0]!.name).startsWith(String(trace.name)));
      }
    });
  });
});

describe('span nesting without async context', () => {
  it('nests sequential work correctly on the fallback stack', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        asyncContext: false,
      });
      assert.equal(client.contextBackend, 'stack');

      await client.trace('browser-shaped', async () => {
        await client.span('outer', async () => {
          await client.span('inner', async () => 'done');
        });
      });
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(spanNamed(trace, 'inner').parent_span_id, spanNamed(trace, 'outer').id);
    });
  });

  /**
   * Node 18 has no `process.getBuiltinModule`, so `AsyncLocalStorage` can only
   * be had from an `await import()`. A client built there starts on the stack
   * backend and switches to ALS a few ticks later — possibly in the middle of a
   * trace. Everything opened before the switch lives on the stack, and the ALS
   * store is empty, so a naive lookup finds no active trace and orphans the
   * next span into a trace of its own. Hiding the synchronous accessor puts the
   * SDK on that path deliberately.
   */
  it('keeps nesting when the async-context backend arrives mid-trace', async () => {
    const proc = process as unknown as { getBuiltinModule?: (name: string) => unknown };
    const original = proc.getBuiltinModule;
    delete proc.getBuiltinModule;
    try {
      await withStub(async (stub, baseUrl) => {
        const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
        assert.equal(client.contextBackend, 'stack', 'starts on the fallback, as Node 18 does');

        await client.trace('long-running', async () => {
          await client.span('step-one', async () => 1);
          // By now the import has landed and the backend has flipped.
          await client.span('step-two', async () => 2);
        });
        assert.equal(client.contextBackend, 'async-local-storage', 'the backend switched mid-trace');
        await client.close();

        const trace = soleTrace(stub);
        assert.deepEqual(
          spansOf(trace).map((span) => span.name).sort(),
          ['step-one', 'step-two'],
          'both spans stayed with the trace that was open',
        );
      });
    } finally {
      if (original) proc.getBuiltinModule = original;
    }
  });

  it('honours an explicit parent, which is the browser escape hatch', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        asyncContext: false,
      });

      const trace = client.startTrace('manual');
      const parent = trace.startSpan({ name: 'parent' });
      // Interleaved work that would confuse a stack: the child names its parent.
      const child = client.startSpan({ name: 'child', parent });
      child.end({ output: 'x' });
      parent.end({});
      trace.end({});
      await client.close();

      const wire = soleTrace(stub);
      assert.equal(spanNamed(wire, 'child').parent_span_id, spanNamed(wire, 'parent').id);
    });
  });
});

describe('manual spans', () => {
  it('wraps a stray span in an implicit trace so it is not dropped', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const span = client.startSpan({ name: 'orphan', type: 'tool' });
      span.end({ output: 'ok' });
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(trace.name, 'orphan');
      assert.equal(spansOf(trace).length, 1);
      assert.equal(spansOf(trace)[0]!.type, 'tool');
    });
  });

  it('closes a span still open when its trace ends', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const trace = client.startTrace('leaky');
      trace.startSpan({ name: 'never-closed' });
      trace.end({});
      await client.close();

      const span = spanNamed(soleTrace(stub), 'never-closed');
      assert.ok(span.end_time, 'the abandoned span was closed with the trace');
    });
  });

  it('records usage, model, provider and cost on an llm span', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('priced', (trace) => {
        const span = trace.startSpan({ name: 'generate', type: 'llm' });
        span.setModel('gpt-4o-mini', 'openai');
        span.setUsage({ prompt_tokens: 900, completion_tokens: 100 });
        span.setCost(0.0123);
        span.end({ output: 'text' });
      });
      await client.close();

      const span = spanNamed(soleTrace(stub), 'generate');
      assert.equal(span.model, 'gpt-4o-mini');
      assert.equal(span.provider, 'openai');
      assert.equal(span.total_estimated_cost, 0.0123);
      // `total_tokens` is derived so the console's per-model breakdown adds up.
      assert.deepEqual(span.usage, { prompt_tokens: 900, completion_tokens: 100, total_tokens: 1000 });
    });
  });

  it('posts each span on its own when streamSpans is on', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        streamSpans: true,
      });
      await client.trace('long-running', async () => {
        await client.span('step-one', async () => 1);
        await client.span('step-two', async () => 2);
      });
      await client.close();

      const spans = stub.itemsFor('spans');
      assert.equal(spans.length, 2);
      assert.deepEqual(spans.map((span) => span.name).sort(), ['step-one', 'step-two']);
      // The trace still arrives, but without its spans duplicated inside it.
      assert.equal(spansOf(soleTrace(stub)).length, 0);
    });
  });
});

describe('sampling', () => {
  it('drops a trace that sampling excluded, without running less code', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        samplingRate: 0,
      });
      const result = await client.trace('unsampled', async () => 'still ran');
      assert.equal(result, 'still ran');
      await client.close();

      assert.equal(stub.itemsFor('traces').length, 0);
      assert.equal(client.getStats().sampledOut, 1);
    });
  });
});

describe('traced()', () => {
  it('wraps a function, keeping its name and arity', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      function summarise(text: string, limit: number): string {
        return text.slice(0, limit);
      }
      const wrapped = client.traced(summarise);
      assert.equal(wrapped.name, 'summarise');
      assert.equal(wrapped.length, 2);
      assert.equal(wrapped('hello world', 5), 'hello');
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(trace.name, 'summarise');
      assert.deepEqual(trace.input, { args: ['hello world', 5] });
      assert.deepEqual(trace.output, { value: 'hello' });
    });
  });

  it('folds a nested traced call into the trace above it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const inner = client.traced(async function fetchDocs(): Promise<string[]> {
        return ['a', 'b'];
      });
      const outer = client.traced(async function answer(): Promise<number> {
        return (await inner()).length;
      });

      assert.equal(await outer(), 2);
      await client.close();

      const trace = soleTrace(stub);
      assert.equal(trace.name, 'answer');
      assert.equal(spanNamed(trace, 'fetchDocs').trace_id, trace.id);
    });
  });

  it('is a no-op passthrough when no default client is configured', async () => {
    setDefaultClient(undefined);
    const wrapped = traced((n: number) => n * 2);
    assert.equal(wrapped(21), 42);
  });

  it('reports through the module-level default client once one is set', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      setDefaultClient(client);
      try {
        const wrapped = traced(function classify(text: string): string {
          return text.toUpperCase();
        });
        assert.equal(wrapped('hi'), 'HI');
        await client.flush();
        assert.equal(soleTrace(stub).name, 'classify');
      } finally {
        setDefaultClient(undefined);
        await client.close();
      }
    });
  });

  // The module-level twin has to keep the same identity contract as
  // `client.traced()`. It is the easier one to get wrong, because it resolves
  // its client at call time and so cannot simply return the client's wrapper.
  it('keeps the function name and arity with no client configured', () => {
    setDefaultClient(undefined);
    const wrapped = traced(function summarise(text: string, limit: number): string {
      return text.slice(0, limit);
    });
    assert.equal(wrapped.name, 'summarise');
    assert.equal(wrapped.length, 2);
    assert.equal(wrapped('hello world', 5), 'hello');
  });

  it('keeps the function name and arity once a client is configured', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      setDefaultClient(client);
      try {
        const wrapped = traced(function summarise(text: string, limit: number): string {
          return text.slice(0, limit);
        });
        assert.equal(wrapped.name, 'summarise');
        assert.equal(wrapped.length, 2);
        assert.equal(wrapped('hello world', 5), 'hello');
        await client.flush();
        assert.equal(soleTrace(stub).name, 'summarise');
      } finally {
        setDefaultClient(undefined);
        await client.close();
      }
    });
  });

  it('takes an explicit name and an explicit client', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      setDefaultClient(undefined);
      try {
        const wrapped = traced(async (id: string) => `done ${id}`, { name: 'reconcile', client });
        assert.equal(wrapped.name, 'reconcile');
        assert.equal(wrapped.length, 1);
        assert.equal(await wrapped('INV-1042'), 'done INV-1042');
        await client.flush();

        const trace = soleTrace(stub);
        assert.equal(trace.name, 'reconcile');
        // `client` is a wrapper option, not a trace field, so it must not reach
        // the wire — it is not in the contract and would be rejected.
        assert.equal((trace.metadata as Record<string, unknown> | undefined)?.client, undefined);
      } finally {
        await client.close();
      }
    });
  });

  it('reuses one wrapper per client rather than rebuilding it per call', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      setDefaultClient(client);
      try {
        let calls = 0;
        const wrapped = traced(() => {
          calls += 1;
          return calls;
        });
        assert.equal(wrapped(), 1);
        assert.equal(wrapped(), 2);
        assert.equal(client.getStats().sent + client.getStats().pending, 2);
      } finally {
        setDefaultClient(undefined);
        await client.close();
      }
    });
  });
});
