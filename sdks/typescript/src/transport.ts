/**
 * The HTTP layer: one place that knows about headers, timeouts, the error
 * envelope, and when it is worth trying again.
 *
 * Retry policy is *decorrelated exponential backoff with full jitter*. The
 * plain doubling schedule synchronises a fleet — every process that failed at
 * the same second retries at the same second, which is how a recovering service
 * gets knocked over a second time. Multiplying each delay by a fresh random
 * factor in `[0, 1)` spreads the herd out, and it is why `Math.random()` appears
 * in what is otherwise a deterministic function.
 */

import { errorFromResponse, toFulcrumError, FulcrumOpsError, TimeoutError } from './errors.js';
import { encodeBody } from './serialize.js';
import { unrefTimer } from './runtime.js';
import { SDK_NAME, SDK_VERSION, USER_AGENT } from './version.js';
import type { FetchLike, ResolvedOptions } from './options.js';

/** One HTTP call's worth of knobs. */
export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  path: string;
  query?: Record<string, string | number | boolean | undefined>;
  body?: unknown;
  headers?: Record<string, string>;
  timeoutMs?: number;
  /** Attempts after the first. Defaults to the client's retry settings. */
  maxAttempts?: number;
  /** Ask the host to finish this request even if the page is unloading. */
  keepalive?: boolean;
  /** Treat 304 as a result rather than an error, for the ETag'd config fetch. */
  allowNotModified?: boolean;
  signal?: AbortSignal;
}

/** A response the caller still needs the status and headers of. */
export interface RawResponse<T> {
  status: number;
  data: T | undefined;
  headers: Headers;
}

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

/**
 * Wait between retry attempts, holding the event loop open.
 *
 * Deliberately *not* unref'd, unlike the queue's idle flush timer. This sleep
 * only ever runs inside a send the caller is already waiting on, and an unref'd
 * timer here lets Node exit mid-backoff: a short-lived script that awaits
 * `flush()` would return from neither the flush nor the retry, and the batch
 * would be lost without a word — in precisely the transient-failure case the
 * retries exist to survive. `maxBackoffMs` bounds how long this can hold.
 */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Backoff for attempt `n`, in milliseconds.
 *
 * `Retry-After` wins outright when the server sent one — it is the server
 * saying how long it needs, and guessing over the top of that is rude and
 * usually wrong.
 */
export function backoffDelay(
  attempt: number,
  base: number,
  ceiling: number,
  retryAfterSeconds?: number,
  random: () => number = Math.random,
): number {
  if (retryAfterSeconds !== undefined && retryAfterSeconds >= 0) {
    return Math.min(retryAfterSeconds * 1000, ceiling);
  }
  const exponential = Math.min(base * 2 ** attempt, ceiling);
  // Full jitter: anywhere in [0, exponential). Keeps a restarted fleet from
  // retrying in lockstep.
  return Math.floor(random() * exponential);
}

/** Everything the SDK sends goes through one of these. */
export class Transport {
  private readonly fetchImpl: FetchLike;

  constructor(private readonly options: ResolvedOptions) {
    this.fetchImpl = options.fetch;
  }

  /** Headers common to every request, plus whatever the caller added. */
  private buildHeaders(extra?: Record<string, string>, hasBody = false): Record<string, string> {
    const headers: Record<string, string> = {
      accept: 'application/json',
      'x-fulcrum-sdk': SDK_NAME,
      'x-fulcrum-sdk-version': SDK_VERSION,
      ...this.options.headers,
      ...(extra ?? {}),
    };
    if (hasBody) headers['content-type'] = 'application/json';
    if (this.options.apiKey) headers.authorization = `Bearer ${this.options.apiKey}`;
    if (this.options.workspace) headers['x-fulcrum-workspace'] = this.options.workspace;
    // A browser refuses to let a page set User-Agent; setting it there is at
    // best ignored and at worst a console warning on every request.
    if (typeof document === 'undefined') headers['user-agent'] = USER_AGENT;
    return headers;
  }

  private buildUrl(path: string, query?: RequestOptions['query']): string {
    const suffix = path.startsWith('/') ? path : `/${path}`;
    const url = new URL(`${this.options.baseUrl}${suffix}`);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value === undefined) continue;
      url.searchParams.set(key, String(value));
    }
    return url.toString();
  }

  /**
   * Perform one attempt. Timeouts are enforced with an `AbortController` rather
   * than `AbortSignal.timeout`, which Node 18.0 does not have.
   */
  private async attempt<T>(request: RequestOptions): Promise<RawResponse<T>> {
    const controller = new AbortController();
    const timeoutMs = request.timeoutMs ?? this.options.timeoutMs;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    unrefTimer(timer);

    const forwardAbort = () => controller.abort();
    request.signal?.addEventListener('abort', forwardAbort);

    try {
      const hasBody = request.body !== undefined;
      const encoded = hasBody ? encodeBody(request.body) : undefined;
      const init: RequestInit = {
        method: request.method ?? (hasBody ? 'POST' : 'GET'),
        headers: this.buildHeaders(request.headers, hasBody),
        signal: controller.signal,
      };
      if (encoded) init.body = encoded.text;
      if (request.keepalive) (init as RequestInit & { keepalive?: boolean }).keepalive = true;

      const response = await this.fetchImpl(this.buildUrl(request.path, request.query), init);

      if (response.status === 304 && request.allowNotModified) {
        return { status: 304, data: undefined, headers: response.headers };
      }
      if (response.status === 204) {
        return { status: 204, data: undefined, headers: response.headers };
      }

      const text = await response.text();
      let parsed: unknown;
      if (text.length > 0) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      }

      if (!response.ok) throw errorFromResponse(response.status, parsed, response.headers);
      return { status: response.status, data: parsed as T, headers: response.headers };
    } catch (thrown) {
      if (timedOut) {
        throw new TimeoutError(`Request to ${request.path} timed out after ${timeoutMs}ms.`, {
          cause: thrown,
        });
      }
      throw toFulcrumError(thrown, `Request to ${request.path} failed.`);
    } finally {
      clearTimeout(timer);
      request.signal?.removeEventListener('abort', forwardAbort);
    }
  }

  /** Perform a request, retrying the transient failures. */
  async requestRaw<T>(request: RequestOptions): Promise<RawResponse<T>> {
    const maxAttempts = request.maxAttempts ?? this.options.retry.maxAttempts;
    let lastError: FulcrumOpsError | undefined;

    for (let attempt = 0; attempt <= maxAttempts; attempt += 1) {
      try {
        return await this.attempt<T>(request);
      } catch (thrown) {
        const error = toFulcrumError(thrown, 'Request failed.');
        lastError = error;

        const worthRetrying =
          error.retryable || (error.status !== undefined && RETRYABLE_STATUSES.has(error.status));
        if (!worthRetrying || attempt === maxAttempts) break;

        const delay = backoffDelay(
          attempt,
          this.options.retry.backoffMs,
          this.options.retry.maxBackoffMs,
          error.retryAfterSeconds,
        );
        this.debug(
          `attempt ${attempt + 1}/${maxAttempts + 1} for ${request.path} failed (${error.code}); retrying in ${delay}ms`,
        );
        await sleep(delay);
      }
    }

    throw lastError ?? new FulcrumOpsError(`Request to ${request.path} failed.`);
  }

  /** Perform a request and return only the decoded body. */
  async request<T>(request: RequestOptions): Promise<T> {
    const response = await this.requestRaw<T>(request);
    return response.data as T;
  }

  private debug(message: string): void {
    if (!this.options.debug) return;
    // eslint-disable-next-line no-console
    console.debug(`[fulcrum-ops] ${message}`);
  }
}
