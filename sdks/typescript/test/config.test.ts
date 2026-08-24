/**
 * The `GET /ingest/config` bootstrap, and the local redaction it turns on.
 *
 * Redaction is the part worth being strict about: the whole reason the rules
 * are shipped to the SDK rather than applied server-side is that server-side is
 * already too late — by then the content has crossed the network and been
 * written to a request log. So the assertions check the payload the stub
 * actually received, not an intermediate object.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps, Transport, applyRedaction, compileRedactionRules, resolveOptions } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';

describe('bootstrap', () => {
  it('fetches the config on construction and adopts it', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });

      const config = client.getCachedConfig();
      assert.ok(config, 'the document was cached');
      assert.equal(config.workspace, 'acme');
      assert.equal(config.revision, 'rev-1');
      assert.equal(config.guardrails?.length, 1);
      assert.equal(stub.requestsFor('/ingest/config').length, 1);
      await client.close();
    });
  });

  it('serves the second read from cache rather than the network', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });
      await client.config();
      await client.config();
      assert.equal(stub.requestsFor('/ingest/config').length, 1, 'one fetch covered all three reads');
      await client.close();
    });
  });

  it('revalidates with If-None-Match and accepts a 304', async () => {
    await withStub(async (stub, baseUrl) => {
      // Driven at the transport level: the client's own cache would (correctly)
      // never re-ask within the document's lifetime, which is the behaviour the
      // test above covers.
      const transport = new Transport(resolveOptions({ apiKey: 'k', baseUrl }));
      const first = await transport.requestRaw<{ revision: string }>({
        method: 'GET',
        path: '/ingest/config',
        allowNotModified: true,
      });
      assert.equal(first.status, 200);
      const etag = first.headers.get('etag');
      assert.ok(etag, 'the config endpoint is ETagged');

      const second = await transport.requestRaw({
        method: 'GET',
        path: '/ingest/config',
        headers: { 'if-none-match': etag },
        allowNotModified: true,
      });
      assert.equal(second.status, 304, 'an unchanged revision costs no body');
      assert.equal(second.data, undefined);
      assert.equal(stub.requestsFor('/ingest/config').length, 2);
    });
  });

  it('takes the smaller sampling rate, the caller’s or the deployment’s', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.config = { ...stub.config, sampling_rate: 1 };
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        // The caller asked for none; the deployment allows all. The caller wins,
        // because their number is a preference the deployment has no reason to
        // widen. Zero rather than a fraction so this asserts the narrowing rule
        // and not the way a coin happened to land five times.
        samplingRate: 0,
      });
      assert.equal(client.getCachedConfig()?.sampling_rate, 1, 'the deployment’s own number is reported as it stands');

      for (let index = 0; index < 5; index += 1) client.trace(`dropped-${index}`, () => index);
      await client.flush();

      assert.equal(client.getStats().sampledOut, 5, 'the caller’s 0 beat the deployment’s 1');
      assert.equal(stub.requestsFor('/ingest/traces').length, 0, 'nothing was reported');
      await client.close();
    });
  });

  it('takes the smaller batch size, the caller’s or the deployment’s', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.config = { ...stub.config, sampling_rate: 1, batch_max_spans: 500, flush_interval_seconds: 2 };
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        batch: { maxItems: 5 },
      });

      for (let index = 0; index < 10; index += 1) client.trace(`t${index}`, () => index);
      await client.flush();

      // Batches of 5 (the caller's) rather than one of 10 (the deployment
      // would have allowed 500).
      assert.equal(stub.requestsFor('/ingest/traces').length, 2);
      assert.equal(stub.itemsFor('traces').length, 10);
      await client.close();
    });
  });

  it('lets the deployment turn capture off even when the caller left it on', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.config = { ...stub.config, capture_input: false, capture_output: false, redaction: [] };
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });

      client.trace({ name: 'quiet', input: { question: 'who is the customer?' } }, () => 'the answer');
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(trace.input, undefined, 'the deployment’s "no input" setting held');
      assert.equal(trace.output, undefined);
      assert.equal(trace.name, 'quiet', 'the shape of the run is still reported');
    });
  });

  it('keeps working when the bootstrap fetch fails', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('GET', '/api/v1/ingest/config', { status: 500, times: 5 });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        retry: { maxAttempts: 0 },
      });
      const result = await client.trace('undeterred', async () => 'ok');
      assert.equal(result, 'ok');
      await client.close();
      assert.equal(stub.itemsFor('traces').length, 1);
    });
  });
});

describe('redaction from the bootstrap document', () => {
  it('removes a configured entity class before the payload leaves the process', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });

      client.trace(
        { name: 'support', input: { question: 'my address is ada@example.com, can you help?' } },
        () => 'I have emailed ada@example.com.',
      );
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      const serialised = JSON.stringify(trace);
      assert.ok(!serialised.includes('ada@example.com'), 'the address never reached the wire');
      assert.ok(serialised.includes('[redacted by policy]'));
    });
  });

  it('applies a caller-supplied rule on top of the workspace’s', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        setAsDefault: false,
        redaction: [
          {
            id: 'local-1',
            name: 'Internal case numbers',
            source: 'workspace',
            pattern: 'CASE-\\d{6}',
            replacement: '[case]',
            applies_to: ['input', 'output', 'metadata'],
          },
        ],
      });

      client.trace(
        { name: 'ticket', input: { note: 'see CASE-123456' }, metadata: { ref: 'CASE-999999' } },
        () => 'resolved CASE-123456',
      );
      await client.close();

      const serialised = JSON.stringify(stub.itemsFor('traces')[0]!);
      assert.ok(!serialised.includes('CASE-123456'));
      assert.ok(!serialised.includes('CASE-999999'), 'metadata was covered too');
      assert.ok(serialised.includes('[case]'));
    });
  });

  it('redacts the sample on a guardrail event and keeps it inside the field limit', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, setAsDefault: false });
      client.guardrailTriggered({
        guardrail: 'PII masking',
        actionTaken: 'Mask',
        score: 0.9,
        sample: `${'x'.repeat(470)} ada@example.com`,
      });
      await client.close();

      const event = stub.itemsFor('events')[0]!;
      const sample = String(event.sample);
      assert.ok(!sample.includes('ada@example.com'));
      assert.ok(sample.length <= 500, `sample must fit the contract, saw ${sample.length}`);
    });
  });
});

describe('compileRedactionRules', () => {
  it('skips a rule whose expression this engine cannot parse, keeping the rest', () => {
    const compiled = compileRedactionRules([
      { id: 'bad', name: 'Broken', source: 'workspace', pattern: '(unclosed' },
      { id: 'good', name: 'Emails', source: 'workspace', entity_types: ['email'] },
    ]);
    assert.equal(compiled.length, 1, 'one operator typo does not disarm every other rule');
    assert.equal(compiled[0]!.id, 'good');
  });

  it('records entity classes it does not know how to match', () => {
    const compiled = compileRedactionRules([
      { id: 'r', name: 'Mixed', source: 'guardrail', entity_types: ['email', 'passport_number'] },
    ]);
    assert.deepEqual(compiled[0]!.unsupportedEntityTypes, ['passport_number']);
  });

  it('defaults to input and output when applies_to is absent', () => {
    const compiled = compileRedactionRules([{ id: 'r', name: 'Emails', source: 'workspace', entity_types: ['email'] }]);
    assert.equal(compiled[0]!.fields.has('input'), true);
    assert.equal(compiled[0]!.fields.has('output'), true);
    assert.equal(compiled[0]!.fields.has('metadata'), false);
  });

  it('only touches the fields a rule declares', () => {
    const rules = compileRedactionRules([
      { id: 'r', name: 'Emails', source: 'workspace', entity_types: ['email'], applies_to: ['input'] },
    ]);
    const payload = { note: 'ada@example.com' };
    assert.deepEqual(applyRedaction(payload, rules, 'input'), { note: '[redacted by policy]' });
    assert.deepEqual(applyRedaction(payload, rules, 'output'), payload, 'output was out of scope');
  });

  it('walks nested structures', () => {
    const rules = compileRedactionRules([{ id: 'r', name: 'Emails', source: 'workspace', entity_types: ['email'] }]);
    const redacted = applyRedaction(
      { messages: [{ role: 'user', content: 'reach me at ada@example.com' }] },
      rules,
      'input',
    );
    assert.equal(JSON.stringify(redacted).includes('ada@example.com'), false);
  });

  it('costs nothing when nothing is configured', () => {
    const payload = { note: 'ada@example.com' };
    assert.equal(applyRedaction(payload, [], 'input'), payload, 'the very object is returned');
  });
});
