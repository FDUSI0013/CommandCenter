/**
 * Reconciliation cover for the TypeScript SDK.
 *
 * `traced()` builds its wrapper once, so a `threadId` string on the options
 * files every call the wrapper ever makes under one conversation — which is
 * right for a worker that serves a single thread and wrong for every handler
 * that serves many. The resolver form is the only way to say "the conversation
 * is in the arguments", and it matches the Python SDK's `@trace(thread_id=…)`
 * callable so a team that reads both SDKs finds the same escape hatch.
 *
 * Worth pinning because it runs on the caller's hot path, before their own
 * function: the resolver sees their arguments and their `this`, and a resolver
 * that throws must cost the run its thread and nothing else.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';
import type { StubServer } from './helpers/stub-server.js';

/** The thread ids the stub was sent, in the order the traces arrived. */
function threadIds(stub: StubServer): (string | undefined)[] {
  return stub.itemsFor('traces').map((trace) => trace.thread_id as string | undefined);
}

function traceNamed(stub: StubServer, name: string): Record<string, unknown> {
  const found = stub.itemsFor('traces').find((trace) => trace.name === name);
  assert.ok(found, `expected a trace named "${name}"`);
  return found;
}

describe('traced() resolves a conversation per call', () => {
  it('reads the thread id out of each call’s own arguments', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const reply = client.traced(
        function reply(message: { conversationId: string; text: string }): string {
          return message.text.toUpperCase();
        },
        { threadId: (message) => message.conversationId },
      );

      assert.equal(reply({ conversationId: 'conversation-1', text: 'hi' }), 'HI');
      assert.equal(reply({ conversationId: 'conversation-2', text: 'yo' }), 'YO');
      await client.close();

      assert.deepEqual(threadIds(stub), ['conversation-1', 'conversation-2']);
    });
  });

  it('gives the resolver the receiver as well, so a decorated method can read its own state', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const session = {
        conversationId: 'conversation-from-this',
        answer(question: string): string {
          return `about ${question}`;
        },
      };
      session.answer = client.traced(session.answer, {
        name: 'answer',
        threadId: function (this: typeof session) {
          return this.conversationId;
        },
      });

      assert.equal(session.answer('billing'), 'about billing');
      await client.close();

      assert.deepEqual(threadIds(stub), ['conversation-from-this']);
    });
  });

  it('files a numeric conversation id as the string the store takes, and leaves a missing one unset', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const handle = client.traced(
        function handle(_conversation: number | null): string {
          return 'done';
        },
        { threadId: (conversation) => conversation as unknown as string | null },
      );

      handle(4210);
      handle(null);
      await client.close();

      assert.deepEqual(threadIds(stub), ['4210', undefined]);
    });
  });

  it('lets the call through when the resolver throws, and reports the failure', async () => {
    await withStub(async (stub, baseUrl) => {
      const failures: { operation: string; message: string }[] = [];
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: (error, context) => failures.push({ operation: context.operation, message: error.message }),
      });
      const summarise = client.traced(
        function summarise(payload: { conversation?: { id: string } }): string {
          return payload.conversation ? 'known' : 'anonymous';
        },
        // The field is not on this payload: the caller's own bug, on the
        // caller's hot path. It costs the run its thread, not the call.
        { threadId: (payload) => payload.conversation!.id },
      );

      assert.equal(summarise({}), 'anonymous');
      await client.close();

      assert.deepEqual(threadIds(stub), [undefined]);
      assert.equal(failures.length, 1);
      assert.equal(failures[0]!.operation, 'traced:threadId');
    });
  });

  it('names the conversation for the run above it when the resolver is on a nested step', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const load = client.traced(
        async function loadHistory(conversationId: string): Promise<number> {
          return conversationId.length;
        },
        { threadId: (conversationId) => conversationId },
      );
      const turn = client.traced(async function turn(): Promise<number> {
        return load('conversation-nested');
      });

      assert.equal(await turn(), 'conversation-nested'.length);
      await client.close();

      // One run, not two: the inner wrapper became a span of the outer trace,
      // and the thread it knew travelled up to the row the console lists.
      assert.equal(stub.itemsFor('traces').length, 1);
      assert.equal(traceNamed(stub, 'turn').thread_id, 'conversation-nested');
    });
  });
});
