/**
 * Errors the SDK raises, and the translation from the API's error envelope.
 *
 * Two rules govern everything here:
 *
 * 1. **Telemetry never throws into the caller's path.** These errors surface on
 *    `onError` and from the explicitly awaited calls (`flush`, `config`,
 *    `prompts.get`). A `trace()` body that ran fine returns its value even if
 *    reporting it failed.
 * 2. **Every error says whether retrying could help.** `retryable` is what the
 *    queue's backoff loop branches on, so the decision lives with the error
 *    rather than being re-derived at each call site.
 */

import type { ApiErrorEnvelope } from './types.js';

/** Base class for everything the SDK throws. */
export class FulcrumOpsError extends Error {
  /** Stable machine-readable code, from the API envelope where there is one. */
  readonly code: string;
  /** HTTP status, when the failure came from a response. */
  readonly status: number | undefined;
  /** Server-side correlation id, echoed in the API's error envelope. */
  readonly requestId: string | undefined;
  /** Structured extras the API attached to the failure. */
  readonly details: Record<string, unknown> | undefined;
  /** Whether repeating the request unchanged could plausibly succeed. */
  readonly retryable: boolean;
  /** Server-requested wait before the next attempt, in seconds. */
  readonly retryAfterSeconds: number | undefined;
  /** The underlying failure, when this error wraps one. */
  override readonly cause: unknown;

  constructor(
    message: string,
    options: {
      code?: string;
      status?: number;
      requestId?: string;
      details?: Record<string, unknown>;
      retryable?: boolean;
      retryAfterSeconds?: number;
      cause?: unknown;
    } = {},
  ) {
    super(message);
    this.name = new.target.name;
    this.code = options.code ?? 'sdk_error';
    this.status = options.status;
    this.requestId = options.requestId;
    this.details = options.details;
    this.retryable = options.retryable ?? false;
    this.retryAfterSeconds = options.retryAfterSeconds;
    this.cause = options.cause;
    // Keeps `instanceof` working when the package is compiled down to ES5 by a
    // consumer's bundler.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** The SDK was constructed or called with settings it cannot work with. */
export class ConfigurationError extends FulcrumOpsError {
  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message, { code: 'configuration_error', retryable: false, cause: options.cause });
  }
}

/** A non-2xx response from the API. */
export class ApiError extends FulcrumOpsError {}

/** 401/403 — the API key is missing, wrong, revoked, or lacks a scope. */
export class AuthenticationError extends ApiError {}

/** 404 — the addressed resource does not exist. */
export class NotFoundError extends ApiError {}

/** 413 — the batch body exceeded what the endpoint accepts. */
export class PayloadTooLargeError extends ApiError {}

/** 429 — slow down; `retryAfterSeconds` says by how much. */
export class RateLimitError extends ApiError {}

/** 402 — the workspace has exhausted its entitlement for this resource. */
export class QuotaExceededError extends ApiError {}

/** 5xx, including the server's own `telemetry_unavailable`. */
export class ServerError extends ApiError {}

/** The request never completed: DNS, TLS, connection reset, offline browser. */
export class NetworkError extends FulcrumOpsError {
  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message, { code: 'network_error', retryable: true, cause: options.cause });
  }
}

/** The request was still running when the configured timeout elapsed. */
export class TimeoutError extends FulcrumOpsError {
  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message, { code: 'timeout', retryable: true, cause: options.cause });
  }
}

/** Statuses worth another attempt: transient by definition. */
const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

function parseRetryAfter(headerValue: string | null | undefined): number | undefined {
  if (!headerValue) return undefined;
  const seconds = Number(headerValue);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  // The header may also be an HTTP-date.
  const when = Date.parse(headerValue);
  if (Number.isNaN(when)) return undefined;
  return Math.max(0, (when - Date.now()) / 1000);
}

function errorClassFor(status: number): typeof ApiError {
  if (status === 401 || status === 403) return AuthenticationError;
  if (status === 402) return QuotaExceededError;
  if (status === 404) return NotFoundError;
  if (status === 413) return PayloadTooLargeError;
  if (status === 429) return RateLimitError;
  if (status >= 500) return ServerError;
  return ApiError;
}

/**
 * Build the right error from a failed response.
 *
 * The API returns one envelope shape for every deliberate failure
 * (`{"error": {"code", "message", "details", "request_id"}}`), so the message a
 * developer sees is the one the server wrote for a person, not a generic
 * "request failed".
 */
export function errorFromResponse(
  status: number,
  body: unknown,
  headers?: { get(name: string): string | null },
): ApiError {
  const envelope = (body ?? {}) as ApiErrorEnvelope;
  const detail = envelope.error;
  const ErrorClass = errorClassFor(status);
  const message =
    detail?.message ??
    (typeof body === 'string' && body.trim().length > 0
      ? body.trim().slice(0, 500)
      : `The API returned HTTP ${status}.`);

  return new ErrorClass(message, {
    code: detail?.code ?? `http_${status}`,
    status,
    requestId: detail?.request_id,
    details: detail?.details,
    retryable: RETRYABLE_STATUSES.has(status),
    retryAfterSeconds: parseRetryAfter(headers?.get('retry-after')),
  });
}

/** Normalise anything thrown into a `FulcrumOpsError` without losing the original. */
export function toFulcrumError(thrown: unknown, fallbackMessage: string): FulcrumOpsError {
  if (thrown instanceof FulcrumOpsError) return thrown;
  if (thrown instanceof Error) {
    const isAbort = thrown.name === 'AbortError' || thrown.name === 'TimeoutError';
    if (isAbort) return new TimeoutError(thrown.message || fallbackMessage, { cause: thrown });
    return new NetworkError(thrown.message || fallbackMessage, { cause: thrown });
  }
  return new FulcrumOpsError(fallbackMessage, { cause: thrown });
}
