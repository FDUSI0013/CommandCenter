/** Construction, environment fallbacks, and the disabled-without-a-key path. */

import assert from 'node:assert/strict';
import { after, describe, it } from 'node:test';

import { ConfigurationError, FulcrumOps, normaliseBaseUrl, resolveOptions } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';

function withEnv<T>(values: Record<string, string | undefined>, body: () => T): T {
  const previous: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(values)) {
    previous[key] = process.env[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try {
    return body();
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

describe('option resolution', () => {
  it('falls back to FULCRUM_OPS_API_KEY and FULCRUM_OPS_BASE_URL', () => {
    withEnv(
      { FULCRUM_OPS_API_KEY: 'fo_env_key', FULCRUM_OPS_BASE_URL: 'https://ops.example.com/api/v1' },
      () => {
        const options = resolveOptions();
        assert.equal(options.apiKey, 'fo_env_key');
        assert.equal(options.baseUrl, 'https://ops.example.com/api/v1');
        assert.equal(options.enabled, true);
      },
    );
  });

  it('prefers an explicit argument over the environment', () => {
    withEnv({ FULCRUM_OPS_API_KEY: 'from_env' }, () => {
      assert.equal(resolveOptions({ apiKey: 'explicit' }).apiKey, 'explicit');
    });
  });

  it('reads the workspace, environment and agent from the environment too', () => {
    withEnv(
      {
        FULCRUM_OPS_API_KEY: 'k',
        FULCRUM_OPS_WORKSPACE: 'acme',
        FULCRUM_OPS_ENVIRONMENT: 'Staging',
        FULCRUM_OPS_AGENT: 'support-copilot',
      },
      () => {
        const options = resolveOptions();
        assert.equal(options.workspace, 'acme');
        assert.equal(options.environment, 'Staging');
        assert.equal(options.agent, 'support-copilot');
      },
    );
  });

  it('disables reporting when no key is found anywhere', () => {
    withEnv({ FULCRUM_OPS_API_KEY: undefined }, () => {
      assert.equal(resolveOptions().enabled, false);
    });
  });

  it('clamps sampling into 0..1 and keeps batching sane', () => {
    const options = resolveOptions({ apiKey: 'k', samplingRate: 4, batch: { maxItems: 0 }, retry: { maxAttempts: 99 } });
    assert.equal(options.samplingRate, 1);
    assert.equal(options.batch.maxItems, 1);
    assert.equal(options.retry.maxAttempts, 10);
  });
});

describe('base URL normalisation', () => {
  it('strips trailing slashes', () => {
    assert.equal(normaliseBaseUrl('https://ops.example.com/api/v1/'), 'https://ops.example.com/api/v1');
  });

  it('points a bare host at the versioned API root', () => {
    assert.equal(normaliseBaseUrl('https://ops.example.com'), 'https://ops.example.com/api/v1');
    assert.equal(normaliseBaseUrl('https://ops.example.com/'), 'https://ops.example.com/api/v1');
  });

  it('rejects a URL it cannot use', () => {
    assert.throws(() => normaliseBaseUrl('not a url'), ConfigurationError);
    assert.throws(() => normaliseBaseUrl('ftp://ops.example.com'), ConfigurationError);
  });
});

describe('a client with no API key', () => {
  const client = new FulcrumOps({ apiKey: '', baseUrl: 'https://ops.invalid/api/v1', bootstrap: false, setAsDefault: false });
  after(() => client.close());

  it('still runs the traced body and returns its value', async () => {
    const result = await client.trace('offline', async () => 41 + 1);
    assert.equal(result, 42);
  });

  it('queues nothing', async () => {
    client.trace('offline', () => undefined);
    client.score({ id: 'x', name: 'thumbs', value: 1 });
    await client.flush();
    assert.equal(client.getStats().pending, 0);
    assert.equal(client.getStats().sent, 0);
  });
});

describe('a client against the stub', () => {
  it('sends the API key as a bearer token and identifies the SDK', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'fo_test_key', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('hello', () => 'world');
      await client.flush();
      await client.close();

      const [request] = stub.requestsFor('/ingest/traces');
      assert.ok(request, 'a traces request was made');
      assert.equal(request.headers.authorization, 'Bearer fo_test_key');
      assert.equal(request.headers['x-fulcrum-sdk'], 'typescript');
      assert.equal(request.body?.sdk, 'typescript');
      assert.equal(typeof request.body?.sdk_version, 'string');
    });
  });

  it('sends the workspace header when one is configured', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        workspace: 'acme',
        bootstrap: false,
        setAsDefault: false,
      });
      client.trace('hello', () => undefined);
      await client.close();
      assert.equal(stub.requestsFor('/ingest/traces')[0]?.headers['x-fulcrum-workspace'], 'acme');
    });
  });

  it('reports what the queue did', async () => {
    await withStub(async (_stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('one', () => undefined);
      client.trace('two', () => undefined);
      await client.flush();
      const stats = client.getStats();
      assert.equal(stats.sent, 2);
      assert.equal(stats.accepted, 2);
      assert.equal(stats.pending, 0);
      await client.close();
    });
  });

  /**
   * `flush()` and `close()` are what a `finally` block and a shutdown hook
   * call. If either rejected when the control plane were unreachable, adding
   * telemetry to a request handler would turn an outage in the control plane
   * into failed requests in the customer's own product — the exact coupling
   * this SDK exists to avoid. The failure still has to reach `onError`.
   */
  it('never rejects from flush() or close(), even with nowhere to send', async () => {
    const errors: string[] = [];
    const client = new FulcrumOps({
      apiKey: 'k',
      // A port nothing is listening on: every attempt fails at connect.
      baseUrl: 'http://127.0.0.1:9/api/v1',
      bootstrap: false,
      setAsDefault: false,
      flushOnExit: false,
      retry: { maxAttempts: 0 },
      timeoutMs: 500,
      onError: (error, context) => errors.push(`${context.operation}:${error.code}`),
    });

    client.trace('doomed', () => 'the body still ran');
    await assert.doesNotReject(() => client.flush());
    await assert.doesNotReject(() => client.close());

    assert.ok(errors.some((entry) => entry.startsWith('flush:traces')), `expected a flush failure, saw ${errors.join(', ')}`);
    assert.equal(client.getStats().failedBatches, 1);
  });
});
