/**
 * A short-lived script that awaits `flush()` across a retry, run as its own
 * process.
 *
 * The property under test is that Node stays alive while the SDK is waiting out
 * a retry backoff. That cannot be asserted from inside the test runner, whose
 * own process is kept alive by the runner itself — the timer could be unref'd
 * and every in-process test would still pass. So this runs standalone, with the
 * stub server `unref`'d so that during the backoff the *only* thing that can
 * hold the event loop open is the SDK's own retry timer.
 *
 * Prints `FLUSH_SETTLED` on success. An exit without that line is the failure:
 * the process died mid-backoff and the caller's `await flush()` never returned.
 */

import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';

import { FulcrumOps } from '../../src/index.js';

async function main(): Promise<void> {
  let seen = 0;

  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => {
      seen += 1;
      // Fail the first attempt with a retryable status, succeed on the retry.
      if (seen === 1) {
        response.writeHead(503, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: { code: 'unavailable', message: 'try again' } }));
        return;
      }
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ received: 1, accepted: 1, rejected: 0, blocked: 0 }));
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  // Without this the server alone would keep the process alive and the test
  // would pass whether or not the SDK's retry timer holds a reference.
  server.unref();
  const { port } = server.address() as AddressInfo;

  const client = new FulcrumOps({
    apiKey: 'k',
    baseUrl: `http://127.0.0.1:${port}`,
    bootstrap: false,
    setAsDefault: false,
    flushOnExit: false,
    // Long enough that an unref'd timer is certain to lose the race.
    retry: { maxAttempts: 3, backoffMs: 400, maxBackoffMs: 2_000 },
  });

  client.trace('needs-a-retry', () => 'done');
  await client.flush();

  const stats = client.getStats();
  process.stdout.write(`ATTEMPTS=${seen}\n`);
  process.stdout.write(`SENT=${stats.sent} FAILED=${stats.failedBatches}\n`);
  process.stdout.write('FLUSH_SETTLED\n');

  await client.close();
  server.close();
}

void main();
