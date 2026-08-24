/**
 * Batching, retrying, dropping, and the guarantee that none of it reaches the
 * caller.
 *
 * These tests run against a real socket rather than a stubbed `fetch`, because
 * the behaviour under test *is* the HTTP behaviour — a status code the retry
 * loop branches on, a `Retry-After` header it must obey, a connection that goes
 * away. A mocked transport would let every one of those pass while broken.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { AuthenticationError, FulcrumOps, RateLimitError, backoffDelay } from '../src/index.js';
import type { FulcrumOpsError } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';

/** Collect everything the SDK routes to `onError` instead of throwing. */
function errorCollector(): { errors: Array<{ error: FulcrumOpsError; operation: string }>; onError: (error: FulcrumOpsError, context: { operation: string }) => void } {
  const errors: Array<{ error: FulcrumOpsError; operation: string }> = [];
  return {
    errors,
    onError: (error, context) => {
      errors.push({ error, operation: context.operation });
    },
  };
}

describe('backoffDelay', () => {
  it('doubles the base delay per attempt, with full jitter', () => {
    // A fixed "random" makes the schedule assertable; the jitter itself is the
    // point of the multiplication, not an accident.
    assert.equal(backoffDelay(0, 500, 30_000, undefined, () => 1), 500);
    assert.equal(backoffDelay(1, 500, 30_000, undefined, () => 1), 1_000);
    assert.equal(backoffDelay(2, 500, 30_000, undefined, () => 1), 2_000);
    assert.equal(backoffDelay(2, 500, 30_000, undefined, () => 0.5), 1_000);
    assert.equal(backoffDelay(0, 500, 30_000, undefined, () => 0), 0);
  });

  it('never exceeds the ceiling', () => {
    assert.equal(backoffDelay(30, 500, 1_000, undefined, () => 1), 1_000);
  });

  it('lets Retry-After win outright', () => {
    assert.equal(backoffDelay(0, 500, 30_000, 7, () => 1), 7_000);
    // Even a Retry-After is capped, so a hostile header cannot stall a process.
    assert.equal(backoffDelay(0, 500, 5_000, 600, () => 1), 5_000);
  });
});

describe('batching', () => {
  it('sends one request for many traces', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        batch: { maxItems: 100, flushIntervalMs: 60_000 },
      });
      for (let index = 0; index < 10; index += 1) client.trace(`trace-${index}`, () => index);
      assert.equal(client.getStats().pending, 10, 'nothing left early');

      await client.flush();
      assert.equal(stub.requestsFor('/ingest/traces').length, 1, 'ten traces, one request');
      assert.equal(stub.itemsFor('traces').length, 10);
      assert.equal(client.getStats().pending, 0);
      await client.close();
    });
  });

  it('flushes as soon as the item ceiling is reached', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        batch: { maxItems: 3, flushIntervalMs: 60_000 },
      });
      for (let index = 0; index < 6; index += 1) client.trace(`t${index}`, () => index);
      await client.flush();

      assert.equal(stub.requestsFor('/ingest/traces').length, 2, 'two full batches of three');
      await client.close();
    });
  });

  it('flushes on the interval without anyone asking', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        batch: { maxItems: 1_000, flushIntervalMs: 60 },
      });
      client.trace('timed', () => undefined);
      assert.equal(stub.requestsFor('/ingest/traces').length, 0);

      await new Promise((resolve) => setTimeout(resolve, 250));
      assert.equal(stub.requestsFor('/ingest/traces').length, 1, 'the timer flushed it');
      await client.close();
    });
  });

  it('keeps each kind in its own batch', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('a-trace', () => undefined);
      client.score({ id: '11111111-1111-4111-8111-111111111111', name: 'thumbs', value: 1 });
      client.policyViolation({ policy: 'no-pii', severity: 'High' });
      await client.close();

      assert.equal(stub.itemsFor('traces').length, 1);
      assert.equal(stub.itemsFor('scores').length, 1);
      assert.equal(stub.itemsFor('events').length, 1);
    });
  });

  it('drops the oldest items once the queue is at its ceiling', async () => {
    await withStub(async (_stub, baseUrl) => {
      const collector = errorCollector();
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collector.onError,
        batch: { maxItems: 1_000, maxQueueSize: 2, flushIntervalMs: 60_000 },
      });
      for (let index = 0; index < 5; index += 1) client.trace(`t${index}`, () => index);

      const stats = client.getStats();
      assert.equal(stats.pending, 2, 'the queue stayed bounded');
      assert.equal(stats.dropped, 3);
      assert.ok(collector.errors.some((entry) => entry.error.code === 'queue_overflow'));
      await client.close();
    });
  });
});

describe('retrying', () => {
  /**
   * A retry backoff must hold the event loop open.
   *
   * The queue's idle flush timer is deliberately unref'd so a scheduled flush
   * never keeps a short script alive. A retry backoff is the opposite case: it
   * runs inside a send someone is already awaiting, and an unref'd timer there
   * lets Node exit mid-backoff — `await flush()` never settles and the batch
   * disappears silently, in exactly the transient-failure case retries exist
   * for. Only a separate process can show this; inside the test runner the
   * runner's own handles hold the loop open and the bug is invisible.
   */
  it('keeps the process alive across a retry backoff, so flush() settles', () => {
    const child = join(__dirname, 'helpers', 'retry-exit-child.js');
    const result = spawnSync(process.execPath, [child], { encoding: 'utf8', timeout: 30_000 });

    assert.equal(result.status, 0, `child exited ${String(result.status)}: ${result.stderr}`);
    assert.match(result.stdout, /ATTEMPTS=2/, 'the send was retried once');
    assert.match(result.stdout, /SENT=1 FAILED=0/, 'the batch landed on the retry');
    assert.match(
      result.stdout,
      /FLUSH_SETTLED/,
      'the process exited during the backoff and flush() never returned',
    );
  });

  it('retries a 503 and succeeds on the next attempt', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('POST', '/api/v1/ingest/traces', { status: 503, times: 2 });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        retry: { maxAttempts: 3, backoffMs: 1, maxBackoffMs: 5 },
      });
      client.trace('eventually', () => undefined);
      await client.flush();

      assert.equal(stub.requestsFor('/ingest/traces').length, 3, 'two failures then a success');
      assert.equal(client.getStats().accepted, 1);
      assert.equal(client.getStats().failedBatches, 0);
      await client.close();
    });
  });

  it('obeys Retry-After on a 429', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('POST', '/api/v1/ingest/traces', { status: 429, headers: { 'retry-after': '0' } });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        retry: { maxAttempts: 2, backoffMs: 1, maxBackoffMs: 5 },
      });
      client.trace('rate-limited', () => undefined);
      await client.flush();

      assert.equal(stub.requestsFor('/ingest/traces').length, 2);
      assert.equal(client.getStats().accepted, 1);
      await client.close();
    });
  });

  it('does not retry a failure that repeating cannot fix', async () => {
    await withStub(async (stub, baseUrl) => {
      const collector = errorCollector();
      stub.inject('POST', '/api/v1/ingest/traces', {
        status: 400,
        times: 5,
        body: { error: { code: 'malformed_batch', message: 'The batch was malformed.', request_id: 'req-1' } },
      });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: collector.onError,
        retry: { maxAttempts: 3, backoffMs: 1 },
      });
      client.trace('doomed', () => undefined);
      await client.flush();

      assert.equal(stub.requestsFor('/ingest/traces').length, 1, 'a 400 is attempted exactly once');
      assert.equal(client.getStats().failedBatches, 1);

      const reported = collector.errors.find((entry) => entry.operation === 'flush:traces');
      assert.ok(reported, 'the loss was reported rather than hidden');
      await client.close();
    });
  });

  it('surfaces the server’s own error message and request id', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('GET', '/api/v1/ingest/config', {
        status: 401,
        body: {
          error: {
            code: 'unauthenticated',
            message: 'Valid credentials are required.',
            request_id: 'req-abc',
          },
        },
      });
      const client = new FulcrumOps({
        apiKey: 'revoked-key',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        retry: { maxAttempts: 0 },
      });
      await assert.rejects(client.config(), (error: unknown) => {
        assert.ok(error instanceof AuthenticationError);
        assert.equal(error.status, 401);
        assert.equal(error.code, 'unauthenticated');
        // The message a developer reads is the one the server wrote for a
        // person, not a generic "request failed".
        assert.equal(error.message, 'Valid credentials are required.');
        assert.equal(error.requestId, 'req-abc');
        assert.equal(error.retryable, false);
        return true;
      });
      await client.close();
    });
  });

  it('classifies 429 as a rate limit and carries the wait', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('GET', '/api/v1/ingest/config', {
        status: 429,
        times: 3,
        headers: { 'retry-after': '2' },
        body: { error: { code: 'rate_limited', message: 'Too many requests.' } },
      });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        retry: { maxAttempts: 0 },
      });
      await assert.rejects(client.config(), (error: unknown) => {
        assert.ok(error instanceof RateLimitError);
        assert.equal(error.retryable, true);
        assert.equal(error.retryAfterSeconds, 2);
        return true;
      });
      await client.close();
    });
  });
});

describe('never throwing into the caller’s path', () => {
  it('runs the body and returns its value when the control plane is unreachable', async () => {
    const collector = errorCollector();
    // Port 1 is reserved and refuses immediately, which is the fastest possible
    // stand-in for an unreachable control plane.
    const client = new FulcrumOps({
      apiKey: 'k',
      baseUrl: 'http://127.0.0.1:1/api/v1',
      bootstrap: false,
      setAsDefault: false,
      onError: collector.onError,
      retry: { maxAttempts: 0 },
      timeoutMs: 500,
    });

    const result = await client.trace('offline', async () => 'computed anyway');
    assert.equal(result, 'computed anyway');
    await client.flush();

    assert.ok(collector.errors.length > 0, 'the failure went to onError');
    assert.equal(client.getStats().failedBatches, 1);
    await client.close();
  });

  it('survives an onError handler that throws', async () => {
    const client = new FulcrumOps({
      apiKey: 'k',
      baseUrl: 'http://127.0.0.1:1/api/v1',
      bootstrap: false,
      setAsDefault: false,
      onError: () => {
        throw new Error('the caller’s handler is buggy');
      },
      retry: { maxAttempts: 0 },
      timeoutMs: 500,
    });
    assert.equal(await client.trace('t', async () => 'fine'), 'fine');
    await client.flush();
    await client.close();
  });

  it('serialises a value JSON.stringify alone would reject', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      const cyclic: Record<string, unknown> = { name: 'root' };
      cyclic.self = cyclic;

      client.trace({ name: 'exotic', input: cyclic }, () => new Map([['k', 1n]]));
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.deepEqual(trace.input, { name: 'root', self: '[circular]' });
      assert.deepEqual(trace.output, { k: '1' });
    });
  });
});

describe('lifecycle', () => {
  it('flush() then close() sends everything and stops the timers', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        batch: { flushIntervalMs: 60_000 },
      });
      client.trace('one', () => undefined);
      await client.flush();
      assert.equal(stub.itemsFor('traces').length, 1);

      client.trace('two', () => undefined);
      await client.close();
      assert.equal(stub.itemsFor('traces').length, 2, 'close() flushed the remainder');
    });
  });

  it('is inert after close(), rather than throwing', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await client.close();
      await client.close();

      const result = await client.trace('after-close', async () => 'still works');
      assert.equal(result, 'still works');
      client.score({ id: 'x', name: 'n', value: 1 });
      await client.flush();
      assert.equal(stub.itemsFor('traces').length, 0);
    });
  });

  it('registers a flush-on-exit hook and releases it on close', async () => {
    await withStub(async (_stub, baseUrl) => {
      const before = process.listenerCount('beforeExit');
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        flushOnExit: true,
      });
      assert.equal(process.listenerCount('beforeExit'), before + 1, 'the hook was installed');

      await client.close();
      assert.equal(process.listenerCount('beforeExit'), before, 'and removed again, so the process can exit');
    });
  });

  it('installs no exit hook when flushOnExit is off', async () => {
    await withStub(async (_stub, baseUrl) => {
      const before = process.listenerCount('beforeExit');
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        flushOnExit: false,
      });
      assert.equal(process.listenerCount('beforeExit'), before);
      await client.close();
    });
  });
});
