/**
 * A stand-in for the control plane, on a real socket.
 *
 * The tests drive the SDK against HTTP rather than against a mocked `fetch`,
 * because most of what is worth testing here *is* the HTTP behaviour: status
 * handling, the retry loop, `Retry-After`, ETag revalidation, keepalive. A
 * stubbed fetch would let all four of those pass while broken.
 *
 * The stub implements the ingest and prompt contracts faithfully enough to
 * catch a shape error, and adds failure injection so a test can ask for "429
 * twice, then 200" without waiting for a real outage.
 */

import { createServer } from 'node:http';
import type { IncomingMessage, Server, ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

/** One request the stub saw, kept for assertions. */
export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string | string[] | undefined>;
  body: Record<string, unknown> | undefined;
}

/** A canned response to serve instead of the normal handler. */
export interface InjectedResponse {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Serve this response for the next N matching requests. Default 1. */
  times?: number;
}

const DEFAULT_CONFIG = {
  workspace: 'acme',
  environment: 'Production',
  agent_id: 'agent-1',
  agent_name: 'support-copilot',
  agent_bound: true,
  sampling_rate: 1,
  batch_max_spans: 500,
  batch_max_bytes: 2_000_000,
  flush_interval_seconds: 2,
  max_queue_size: 5_000,
  retry_max_attempts: 3,
  retry_backoff_seconds: 0.5,
  capture_input: true,
  capture_output: true,
  endpoints: {
    traces: '/api/v1/ingest/traces',
    spans: '/api/v1/ingest/spans',
    scores: '/api/v1/ingest/scores',
    events: '/api/v1/ingest/events',
    config: '/api/v1/ingest/config',
    otlp_traces: '/v1/traces',
  },
  guardrails: [
    {
      id: 'gr-pii',
      name: 'PII masking',
      type: 'pii',
      action: 'Mask',
      threshold: 0.5,
      scope: 'global',
      scope_ref: null,
      status: 'Active',
    },
  ],
  redaction: [
    {
      id: 'gr-pii',
      name: 'PII masking',
      source: 'guardrail',
      entity_types: ['email'],
      pattern: null,
      replacement: '[redacted by policy]',
      applies_to: ['input', 'output'],
    },
  ],
  revision: 'rev-1',
  refresh_after_seconds: 300,
};

export class StubServer {
  private server: Server | undefined;
  private port = 0;

  /** Every request the stub has served, in order. */
  readonly requests: RecordedRequest[] = [];

  /** Canned responses keyed by `METHOD /path`, consumed in order. */
  private readonly injected = new Map<string, InjectedResponse[]>();

  /** The document `GET /ingest/config` returns; mutable so a test can move it. */
  config: Record<string, unknown> = { ...DEFAULT_CONFIG };

  /** Prompts the stub knows about, by id. */
  prompts = new Map<string, Record<string, unknown>>([
    [
      'prompt-1',
      {
        id: 'prompt-1',
        name: 'support-system',
        status: 'Approved',
        version: 'v3',
        commit: 'c0ffee',
        template: 'You are helping {{customer_name}} with {{topic}}.',
        variables: ['customer_name', 'topic'],
      },
    ],
  ]);

  /** Versions keyed by `promptId/commit`. */
  promptVersions = new Map<string, Record<string, unknown>>([
    [
      'prompt-1/deadbee',
      {
        commit: 'deadbee',
        version: 'v1',
        status: 'Approved',
        template: 'Old template for {{customer_name}}.',
        variables: ['customer_name'],
        is_head: false,
      },
    ],
  ]);

  /** Require an `Authorization` header. */
  requireAuth = true;

  async start(): Promise<string> {
    this.server = createServer((request, response) => {
      void this.handle(request, response);
    });
    await new Promise<void>((resolve) => this.server!.listen(0, '127.0.0.1', resolve));
    this.port = (this.server!.address() as AddressInfo).port;
    return this.baseUrl;
  }

  get baseUrl(): string {
    return `http://127.0.0.1:${this.port}/api/v1`;
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    await new Promise<void>((resolve) => this.server!.close(() => resolve()));
    this.server = undefined;
  }

  /** Serve `response` for the next matching request(s). */
  inject(method: string, path: string, response: InjectedResponse): void {
    const key = `${method.toUpperCase()} ${path}`;
    const queue = this.injected.get(key) ?? [];
    for (let index = 0; index < (response.times ?? 1); index += 1) queue.push(response);
    this.injected.set(key, queue);
  }

  /** Requests whose path ends with `suffix`. */
  requestsFor(suffix: string): RecordedRequest[] {
    return this.requests.filter((request) => request.path.endsWith(suffix));
  }

  /** Every item posted to one ingest endpoint, flattened across batches. */
  itemsFor(kind: 'traces' | 'spans' | 'scores' | 'events'): Record<string, unknown>[] {
    const out: Record<string, unknown>[] = [];
    for (const request of this.requestsFor(`/ingest/${kind}`)) {
      const items = request.body?.[kind];
      if (Array.isArray(items)) out.push(...(items as Record<string, unknown>[]));
    }
    return out;
  }

  reset(): void {
    this.requests.length = 0;
    this.injected.clear();
    this.config = { ...DEFAULT_CONFIG };
  }

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const url = new URL(request.url ?? '/', `http://127.0.0.1:${this.port}`);
    const raw = await readBody(request);
    let parsed: Record<string, unknown> | undefined;
    if (raw.length > 0) {
      try {
        parsed = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        parsed = undefined;
      }
    }

    this.requests.push({
      method: request.method ?? 'GET',
      path: url.pathname,
      query: url.searchParams,
      headers: request.headers,
      body: parsed,
    });

    const key = `${request.method?.toUpperCase() ?? 'GET'} ${url.pathname}`;
    const queued = this.injected.get(key);
    if (queued && queued.length > 0) {
      const canned = queued.shift()!;
      send(response, canned.status, canned.body ?? { error: { code: 'injected', message: 'Injected failure.' } }, canned.headers);
      return;
    }

    if (this.requireAuth && !request.headers.authorization && !request.headers['x-fulcrum-api-key']) {
      send(response, 401, { error: { code: 'unauthenticated', message: 'Valid credentials are required.' } });
      return;
    }

    const path = url.pathname.replace(/^\/api\/v1/, '');

    if (path === '/ingest/config' && request.method === 'GET') {
      const etag = `W/"${String(this.config.revision)}"`;
      if (request.headers['if-none-match'] === etag) {
        response.writeHead(304, { ETag: etag, 'Cache-Control': 'private, max-age=300' });
        response.end();
        return;
      }
      send(response, 200, this.config, { ETag: etag, 'Cache-Control': 'private, max-age=300' });
      return;
    }

    const ingestMatch = /^\/ingest\/(traces|spans|scores|events)$/.exec(path);
    if (ingestMatch && request.method === 'POST') {
      const kind = ingestMatch[1]!;
      const items = Array.isArray(parsed?.[kind]) ? (parsed[kind] as unknown[]) : [];
      send(response, 200, {
        received: items.length,
        accepted: items.length,
        rejected: 0,
        blocked: 0,
        spans_accepted: kind === 'spans' ? items.length : 0,
        scores_accepted: kind === 'scores' ? items.length : 0,
        events_recorded: kind === 'events' ? items.length : 0,
        guardrails_evaluated: true,
        agents: ['agent-1'],
        results: items.map((_item, index) => ({ index, outcome: 'accepted', id: `id-${index}` })),
        duration_ms: 1,
      });
      return;
    }

    const versionMatch = /^\/prompts\/([^/]+)\/versions\/([^/]+)$/.exec(path);
    if (versionMatch && request.method === 'GET') {
      const version = this.promptVersions.get(`${versionMatch[1]}/${versionMatch[2]}`);
      if (!version) {
        send(response, 404, { error: { code: 'not_found', message: 'No such version.' } });
        return;
      }
      send(response, 200, version);
      return;
    }

    const promptMatch = /^\/prompts\/([^/]+)$/.exec(path);
    if (promptMatch && request.method === 'GET') {
      const prompt = this.prompts.get(promptMatch[1]!);
      if (!prompt) {
        send(response, 404, { error: { code: 'not_found', message: 'The requested resource does not exist.' } });
        return;
      }
      send(response, 200, prompt);
      return;
    }

    if (path === '/prompts' && request.method === 'GET') {
      const q = (url.searchParams.get('q') ?? '').toLowerCase();
      const items = Array.from(this.prompts.values()).filter(
        (prompt) => q.length === 0 || String(prompt.name).toLowerCase().includes(q),
      );
      send(response, 200, { items, total: items.length, page: 1, page_size: 25, pages: 1 });
      return;
    }

    send(response, 404, { error: { code: 'not_found', message: `No route for ${path}.` } });
  }
}

function send(response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const payload = JSON.stringify(body);
  response.writeHead(status, { 'content-type': 'application/json', ...headers });
  response.end(payload);
}

function readBody(request: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

/** Start a stub, run `body` against it, and always shut it down. */
export async function withStub<T>(body: (stub: StubServer, baseUrl: string) => Promise<T>): Promise<T> {
  const stub = new StubServer();
  const baseUrl = await stub.start();
  try {
    return await body(stub, baseUrl);
  } finally {
    await stub.stop();
  }
}
