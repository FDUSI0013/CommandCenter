/**
 * Client options: what a caller passes, what the environment supplies, and the
 * defaults that hold when neither does.
 *
 * Precedence is constructor argument, then environment variable, then default.
 * Nothing here reaches the network — `GET /ingest/config` may later narrow the
 * batching and sampling numbers, and when it does the *smaller* of the two
 * wins, because that document expresses a limit the deployment enforces rather
 * than a preference.
 */

import { ConfigurationError } from './errors.js';
import { getFetch, readEnv } from './runtime.js';
import type { FulcrumOpsError } from './errors.js';
import type { RedactionRule } from './types.js';

/** Signature of `globalThis.fetch`, so a caller can supply their own. */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Called whenever the SDK swallows a failure rather than throwing it at you. */
export type ErrorHandler = (error: FulcrumOpsError, context: { operation: string }) => void;

/** How the batching queue behaves. */
export interface BatchOptions {
  /** Flush once this many items are waiting. */
  maxItems?: number;
  /** Flush once the pending body would exceed this many bytes. */
  maxBytes?: number;
  /**
   * Spans one request may carry, summed across the traces in it. Default 1000,
   * which is the server's ceiling; `/ingest/config` may narrow it.
   */
  maxSpans?: number;
  /** Flush at least this often, in milliseconds. */
  flushIntervalMs?: number;
  /** Drop the oldest items past this depth rather than growing without bound. */
  maxQueueSize?: number;
}

/** How a failed send is retried. */
export interface RetryOptions {
  /** Attempts after the first. `0` disables retrying. */
  maxAttempts?: number;
  /** First backoff step, in milliseconds; doubles each attempt. */
  backoffMs?: number;
  /** Ceiling on any single backoff step, in milliseconds. */
  maxBackoffMs?: number;
}

/** Everything `new FulcrumOps(...)` accepts. */
export interface FulcrumOpsOptions {
  /** API key. Falls back to `FULCRUM_OPS_API_KEY`. */
  apiKey?: string;
  /** API root, e.g. `https://your-server.example.com/api/v1`. Falls back to `FULCRUM_OPS_BASE_URL`. */
  baseUrl?: string;
  /** Workspace slug, for keys that are not already scoped. Falls back to `FULCRUM_OPS_WORKSPACE`. */
  workspace?: string;
  /** Environment label recorded on every trace. Falls back to `FULCRUM_OPS_ENVIRONMENT`. */
  environment?: string;
  /** Default agent for traces that do not name their own. Falls back to `FULCRUM_OPS_AGENT`. */
  agent?: string;

  /**
   * Per-request timeout in milliseconds. Default 30000. Falls back to
   * `FULCRUM_OPS_TIMEOUT_MS`, then to `FULCRUM_OPS_TIMEOUT_SECONDS` — the name
   * and unit the Python SDK reads, so one fleet-wide setting covers both.
   */
  timeoutMs?: number;
  batch?: BatchOptions;
  retry?: RetryOptions;

  /** Fraction of traces to report, 0–1. Default 1. */
  samplingRate?: number;
  /** Record `input` on traces and spans. Default true. */
  captureInput?: boolean;
  /** Record `output` on traces and spans. Default true. */
  captureOutput?: boolean;
  /** Redaction rules applied on top of whatever `/ingest/config` sends. */
  redaction?: RedactionRule[];

  /**
   * Fetch `/ingest/config` on construction and adopt its batching, sampling and
   * redaction settings. Default true; the fetch is fire-and-forget, so start-up
   * never blocks on it.
   */
  bootstrap?: boolean;
  /** Flush what is queued when the process or page is going away. Default true. */
  flushOnExit?: boolean;
  /**
   * Post each span as it closes instead of nesting it in its trace. Use for
   * long-running traces whose spans should appear before the trace finishes.
   */
  streamSpans?: boolean;
  /**
   * Whether anything is reported. Defaults to true when an API key was found
   * and `FULCRUM_OPS_DISABLED` is not set; an explicit value here wins over both.
   */
  enabled?: boolean;
  /** Called for every failure the SDK absorbs. */
  onError?: ErrorHandler;
  /** Write internal diagnostics to the console. Falls back to `FULCRUM_OPS_DEBUG`. */
  debug?: boolean;
  /** Substitute for `globalThis.fetch`, for tests or a proxy-aware agent. */
  fetch?: FetchLike;
  /** Extra headers on every request. */
  headers?: Record<string, string>;
  /**
   * Use `AsyncLocalStorage` for trace nesting on Node. Default true. Set false
   * to force the explicit-parent model everywhere, which is what browsers get.
   */
  asyncContext?: boolean;
  /** Register this client as the one the module-level `traced()` helper uses. Default true. */
  setAsDefault?: boolean;
}

/** Options after defaults, environment variables and validation are applied. */
export interface ResolvedOptions {
  apiKey: string | undefined;
  baseUrl: string;
  workspace: string | undefined;
  environment: string | undefined;
  agent: string | undefined;
  timeoutMs: number;
  batch: Required<BatchOptions>;
  retry: Required<RetryOptions>;
  samplingRate: number;
  captureInput: boolean;
  captureOutput: boolean;
  redaction: RedactionRule[];
  bootstrap: boolean;
  flushOnExit: boolean;
  streamSpans: boolean;
  enabled: boolean;
  onError: ErrorHandler | undefined;
  debug: boolean;
  fetch: FetchLike;
  headers: Record<string, string>;
  asyncContext: boolean;
  setAsDefault: boolean;
}

export const DEFAULT_BASE_URL = 'http://127.0.0.1:8080/api/v1';

const DEFAULTS = {
  timeoutMs: 30_000,
  batch: { maxItems: 100, maxBytes: 4 * 1024 * 1024, maxSpans: 1_000, flushIntervalMs: 5_000, maxQueueSize: 10_000 },
  retry: { maxAttempts: 3, backoffMs: 500, maxBackoffMs: 30_000 },
  samplingRate: 1,
} as const;

function boolFromEnv(name: string): boolean | undefined {
  const raw = readEnv(name);
  if (raw === undefined) return undefined;
  const lowered = raw.toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(lowered)) return true;
  if (['0', 'false', 'no', 'off'].includes(lowered)) return false;
  return undefined;
}

function numberFromEnv(name: string): number | undefined {
  const raw = readEnv(name);
  if (raw === undefined) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/**
 * The request timeout the environment asks for, in milliseconds.
 *
 * Two spellings, because a compose file or a Kubernetes env block is shared by
 * a fleet and not by a language: the Python SDK reads
 * `FULCRUM_OPS_TIMEOUT_SECONDS`, and an operator who set that to keep telemetry
 * from stalling their agents meant the TypeScript ones too. The millisecond
 * name is this SDK's own and wins when both are set.
 */
function timeoutFromEnv(): number | undefined {
  const millis = numberFromEnv('FULCRUM_OPS_TIMEOUT_MS');
  if (millis !== undefined) return millis;
  const seconds = numberFromEnv('FULCRUM_OPS_TIMEOUT_SECONDS');
  return seconds === undefined ? undefined : seconds * 1000;
}

/**
 * Normalise the base URL.
 *
 * A trailing slash and a missing `/api/v1` are the two things people get wrong,
 * and both are silent failures later (404s on every ingest call), so both are
 * fixed here. An outright unparseable URL is a construction error — that one is
 * worth throwing for, because nothing the SDK does afterwards can work.
 */
export function normaliseBaseUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, '');
  if (trimmed.length === 0) throw new ConfigurationError('baseUrl must not be empty.');
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch (cause) {
    throw new ConfigurationError(
      `baseUrl is not a valid URL: ${raw}. Expected something like "https://your-server.example.com/api/v1".`,
      { cause },
    );
  }
  if (!/^https?:$/.test(parsed.protocol)) {
    throw new ConfigurationError(`baseUrl must be http or https; received "${parsed.protocol}".`);
  }
  // Point a bare host at the versioned API root rather than at the web app.
  if (parsed.pathname === '' || parsed.pathname === '/') return `${parsed.origin}/api/v1`;
  return trimmed;
}

/** Apply argument → environment → default precedence and validate the result. */
export function resolveOptions(options: FulcrumOpsOptions = {}): ResolvedOptions {
  const apiKey = options.apiKey?.trim() || readEnv('FULCRUM_OPS_API_KEY');
  const baseUrl = normaliseBaseUrl(
    options.baseUrl?.trim() || readEnv('FULCRUM_OPS_BASE_URL') || DEFAULT_BASE_URL,
  );

  const fetchImpl = (options.fetch ?? getFetch()) as FetchLike | undefined;
  if (!fetchImpl) {
    throw new ConfigurationError(
      'No global fetch was found. Use Node 18 or newer, or pass a `fetch` implementation in the options.',
    );
  }

  const samplingRate = clamp(options.samplingRate ?? numberFromEnv('FULCRUM_OPS_SAMPLING_RATE') ?? DEFAULTS.samplingRate, 0, 1);

  const batch: Required<BatchOptions> = {
    maxItems: Math.max(1, Math.trunc(options.batch?.maxItems ?? DEFAULTS.batch.maxItems)),
    maxBytes: Math.max(1_024, Math.trunc(options.batch?.maxBytes ?? DEFAULTS.batch.maxBytes)),
    maxSpans: Math.max(1, Math.trunc(options.batch?.maxSpans ?? DEFAULTS.batch.maxSpans)),
    flushIntervalMs: Math.max(50, Math.trunc(options.batch?.flushIntervalMs ?? DEFAULTS.batch.flushIntervalMs)),
    maxQueueSize: Math.max(1, Math.trunc(options.batch?.maxQueueSize ?? DEFAULTS.batch.maxQueueSize)),
  };

  const retry: Required<RetryOptions> = {
    maxAttempts: clamp(Math.trunc(options.retry?.maxAttempts ?? DEFAULTS.retry.maxAttempts), 0, 10),
    backoffMs: Math.max(1, Math.trunc(options.retry?.backoffMs ?? DEFAULTS.retry.backoffMs)),
    maxBackoffMs: Math.max(1, Math.trunc(options.retry?.maxBackoffMs ?? DEFAULTS.retry.maxBackoffMs)),
  };

  // A client with no key is not an error: it is a developer running the app
  // locally without credentials. Reporting turns itself off and everything else
  // keeps working.
  //
  // `FULCRUM_OPS_DISABLED` is the fleet-wide kill switch — the one line an
  // operator adds to silence telemetry in CI or in the middle of an incident,
  // without a deploy — and it means the same here as in the Python SDK. Only
  // an explicit `enabled` in code outranks it.
  const enabled = options.enabled ?? (Boolean(apiKey) && boolFromEnv('FULCRUM_OPS_DISABLED') !== true);

  return {
    apiKey,
    baseUrl,
    workspace: options.workspace?.trim() || readEnv('FULCRUM_OPS_WORKSPACE'),
    environment: options.environment?.trim() || readEnv('FULCRUM_OPS_ENVIRONMENT'),
    agent: options.agent?.trim() || readEnv('FULCRUM_OPS_AGENT'),
    timeoutMs: Math.max(1, Math.trunc(options.timeoutMs ?? timeoutFromEnv() ?? DEFAULTS.timeoutMs)),
    batch,
    retry,
    samplingRate,
    captureInput: options.captureInput ?? boolFromEnv('FULCRUM_OPS_CAPTURE_INPUT') ?? true,
    captureOutput: options.captureOutput ?? boolFromEnv('FULCRUM_OPS_CAPTURE_OUTPUT') ?? true,
    redaction: options.redaction ?? [],
    bootstrap: options.bootstrap ?? true,
    flushOnExit: options.flushOnExit ?? true,
    streamSpans: options.streamSpans ?? false,
    enabled,
    onError: options.onError,
    debug: options.debug ?? boolFromEnv('FULCRUM_OPS_DEBUG') ?? false,
    fetch: fetchImpl,
    headers: { ...(options.headers ?? {}) },
    asyncContext: options.asyncContext ?? true,
    setAsDefault: options.setAsDefault ?? true,
  };
}
