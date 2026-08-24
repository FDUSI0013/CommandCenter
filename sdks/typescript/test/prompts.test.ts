/**
 * Prompt fetching, pinning and caching.
 *
 * Unlike telemetry, a prompt lookup sits on the caller's critical path: it is
 * the thing that used to be a string literal. So the two behaviours asserted
 * hardest are that a miss *throws* rather than quietly returning nothing, and
 * that a hit costs no network round trip the second time.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps, NotFoundError, renderTemplate, templateVariables } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';

describe('template rendering', () => {
  it('lists the placeholders a template declares, in first-seen order', () => {
    assert.deepEqual(
      templateVariables('Hello {{ customer_name }}, about {{topic}} — {{customer_name}} again.'),
      ['customer_name', 'topic'],
    );
  });

  it('substitutes what it was given', () => {
    assert.equal(
      renderTemplate('Hello {{name}}, you have {{count}} messages.', { name: 'Ada', count: 3 }),
      'Hello Ada, you have 3 messages.',
    );
  });

  it('leaves an unsupplied placeholder visible rather than blanking it', () => {
    // A visible `{{topic}}` in a model's context is a bug someone notices; a
    // silently empty one is a bug nobody does.
    assert.equal(renderTemplate('Help {{name}} with {{topic}}.', { name: 'Ada' }), 'Help Ada with {{topic}}.');
    assert.equal(renderTemplate('{{a}}', { a: null }), '{{a}}');
  });
});

describe('prompts.get()', () => {
  it('resolves a prompt by name and formats it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const prompt = await client.prompts.get('support-system');

      assert.equal(prompt.id, 'prompt-1');
      assert.equal(prompt.name, 'support-system');
      assert.equal(prompt.status, 'Approved');
      assert.equal(prompt.version, 'v3');
      assert.deepEqual(prompt.variables, ['customer_name', 'topic']);
      assert.equal(
        prompt.format({ customer_name: 'Ada', topic: 'billing' }),
        'You are helping Ada with billing.',
      );
      assert.ok(stub.requestsFor('/prompts').length > 0);
      await client.close();
    });
  });

  it('resolves by id without searching', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const prompt = await client.prompts.get('prompt-1');
      assert.equal(prompt.name, 'support-system');
      assert.equal(
        stub.requests.filter((request) => request.path === '/api/v1/prompts').length,
        0,
        'a direct hit does not fall through to the search',
      );
      await client.close();
    });
  });

  it('serves the second lookup from cache', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await client.prompts.get('prompt-1');
      const before = stub.requests.length;
      await client.prompts.get('prompt-1');
      assert.equal(stub.requests.length, before, 'no second round trip');
      assert.equal(client.prompts.cacheSize, 1);
      await client.close();
    });
  });

  it('refreshes on demand and after clearCache()', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await client.prompts.get('prompt-1');
      await client.prompts.get('prompt-1', { refresh: true });
      assert.equal(stub.requestsFor('/prompts/prompt-1').length, 2);

      client.prompts.clearCache();
      assert.equal(client.prompts.cacheSize, 0);
      await client.prompts.get('prompt-1');
      assert.equal(stub.requestsFor('/prompts/prompt-1').length, 3);
      await client.close();
    });
  });

  it('pins to an immutable commit, and caches it forever', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const pinned = await client.prompts.get('prompt-1', { commit: 'deadbee' });

      assert.equal(pinned.commit, 'deadbee');
      assert.equal(pinned.version, 'v1');
      assert.equal(pinned.format({ customer_name: 'Ada' }), 'Old template for Ada.');

      const before = stub.requests.length;
      await client.prompts.get('prompt-1', { commit: 'deadbee' });
      assert.equal(stub.requests.length, before, 'a commit cannot change, so it is never revalidated');

      // The head and the pin are separate cache entries.
      const head = await client.prompts.get('prompt-1');
      assert.equal(head.commit, 'c0ffee');
      await client.close();
    });
  });

  it('throws rather than handing a model an empty system prompt', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await assert.rejects(client.prompts.get('no-such-prompt'), NotFoundError);
      await assert.rejects(client.prompts.get('prompt-1', { commit: 'nope' }), NotFoundError);
      await client.close();
    });
  });

  it('lists prompts for tooling that enumerates rather than resolves', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const page = await client.prompts.list({ q: 'support', pageSize: 10 });
      assert.equal(page.items.length, 1);

      const request = stub.requests.find((entry) => entry.path === '/api/v1/prompts');
      assert.equal(request?.query.get('q'), 'support');
      assert.equal(request?.query.get('page_size'), '10');
      await client.close();
    });
  });
});
