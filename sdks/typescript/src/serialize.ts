/**
 * Turning arbitrary JavaScript into something the API will accept.
 *
 * Callers hand the SDK whatever their code already has: class instances, Maps,
 * `Date`s, an OpenAI response object, occasionally a structure with a cycle in
 * it. None of that survives `JSON.stringify` unassisted, and a serialisation
 * throw inside a `trace()` body would be the SDK breaking the caller's code —
 * the one thing it must never do. So conversion is total: every input produces
 * *some* JSON value, and the lossy cases are labelled rather than dropped.
 */

import {
  MAX_METADATA_KEYS,
  MAX_TAGS,
  MAX_TAG_LENGTH,
  MAX_USAGE_KEYS,
  MAX_EXCEPTION_MESSAGE_LENGTH,
  MAX_EXCEPTION_TYPE_LENGTH,
  MAX_TRACEBACK_LENGTH,
  clampText,
} from './limits.js';
import { byteLength } from './runtime.js';
import type { ErrorInfoIn, JsonObject, JsonValue } from './types.js';

const MAX_DEPTH = 12;
const MAX_ARRAY_ITEMS = 1_000;
const MAX_STRING_LENGTH = 200_000;

/**
 * Convert any value into JSON, replacing what cannot be represented.
 *
 * `undefined` comes back as `undefined` so object keys holding it can be
 * omitted rather than written as `null`.
 */
export function toJsonValue(value: unknown, depth = 0, seen = new WeakSet<object>()): JsonValue | undefined {
  if (value === null) return null;

  switch (typeof value) {
    case 'undefined':
      return undefined;
    case 'string':
      return value.length > MAX_STRING_LENGTH ? `${value.slice(0, MAX_STRING_LENGTH)}…[truncated]` : value;
    case 'number':
      return Number.isFinite(value) ? value : String(value);
    case 'boolean':
      return value;
    case 'bigint':
      return value.toString();
    case 'function':
      return `[function ${value.name || 'anonymous'}]`;
    case 'symbol':
      return value.toString();
    default:
      break;
  }

  if (depth >= MAX_DEPTH) return '[max depth]';

  const object = value as object;
  if (seen.has(object)) return '[circular]';

  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.toISOString();
  if (value instanceof Error) return errorToJson(value);
  if (value instanceof RegExp) return value.toString();
  if (typeof URL !== 'undefined' && value instanceof URL) return value.toString();

  seen.add(object);
  try {
    if (Array.isArray(value)) {
      const items = value.slice(0, MAX_ARRAY_ITEMS);
      const out: JsonValue[] = [];
      for (const item of items) out.push(toJsonValue(item, depth + 1, seen) ?? null);
      if (value.length > MAX_ARRAY_ITEMS) out.push(`[+${value.length - MAX_ARRAY_ITEMS} more]`);
      return out;
    }

    if (value instanceof Map) {
      const out: JsonObject = {};
      for (const [key, item] of value.entries()) {
        const converted = toJsonValue(item, depth + 1, seen);
        if (converted !== undefined) out[String(key)] = converted;
      }
      return out;
    }

    if (value instanceof Set) {
      const out: JsonValue[] = [];
      for (const item of value.values()) out.push(toJsonValue(item, depth + 1, seen) ?? null);
      return out;
    }

    // Respect an explicit `toJSON`, which is how most SDK response objects
    // (and Temporal, and Decimal libraries) say how they want to be written.
    const withToJson = value as { toJSON?: () => unknown };
    const toJson = typeof withToJson.toJSON === 'function' ? withToJson.toJSON : undefined;
    if (toJson) {
      try {
        return toJsonValue(toJson.call(value), depth + 1, seen) ?? null;
      } catch {
        /* fall through to plain enumeration */
      }
    }

    const out: JsonObject = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      // A `toJSON` that threw is a broken serialiser, not telemetry. Writing it
      // out as "[function toJSON]" would put noise in every payload from an
      // object whose own hook is what failed.
      if (toJson && key === 'toJSON') continue;
      const converted = toJsonValue(item, depth + 1, seen);
      if (converted !== undefined) out[key] = converted;
    }
    return out;
  } catch {
    return '[unserialisable]';
  } finally {
    seen.delete(object);
  }
}

function errorToJson(error: Error): JsonObject {
  const out: JsonObject = { name: error.name, message: error.message };
  if (typeof error.stack === 'string') out.stack = error.stack.slice(0, MAX_TRACEBACK_LENGTH);
  return out;
}

/**
 * Coerce a value into the JSON *object* the `input`/`output` fields require.
 *
 * A caller who traces a function taking a single string should not have to wrap
 * it, so a bare value is boxed under `value` rather than refused.
 */
export function toJsonObject(value: unknown): JsonObject | undefined {
  if (value === undefined) return undefined;
  const converted = toJsonValue(value);
  if (converted === undefined) return undefined;
  if (converted !== null && typeof converted === 'object' && !Array.isArray(converted)) {
    return converted;
  }
  return { value: converted };
}

/** Positional arguments, named where the function declared parameter names. */
export function argsToJsonObject(args: readonly unknown[], names: readonly string[] = []): JsonObject | undefined {
  if (args.length === 0) return undefined;
  const out: JsonObject = {};
  args.forEach((argument, index) => {
    const key = names[index] ?? `arg${index}`;
    const converted = toJsonValue(argument);
    if (converted !== undefined) out[key] = converted;
  });
  return Object.keys(out).length > 0 ? out : undefined;
}

/** Metadata trimmed to the key ceiling the contract enforces. */
export function normaliseMetadata(value: unknown): JsonObject | undefined {
  const object = toJsonObject(value);
  if (!object) return undefined;
  const keys = Object.keys(object);
  if (keys.length === 0) return undefined;
  if (keys.length <= MAX_METADATA_KEYS) return object;
  const out: JsonObject = {};
  for (const key of keys.slice(0, MAX_METADATA_KEYS - 1)) out[key] = object[key]!;
  out.truncated_keys = keys.length - (MAX_METADATA_KEYS - 1);
  return out;
}

/** Tags de-duplicated, trimmed and capped. */
export function normaliseTags(tags: readonly string[] | undefined): string[] | undefined {
  if (!tags || tags.length === 0) return undefined;
  const seen = new Set<string>();
  for (const tag of tags) {
    const clean = clampText(String(tag), MAX_TAG_LENGTH);
    if (clean) seen.add(clean);
    if (seen.size >= MAX_TAGS) break;
  }
  return seen.size > 0 ? Array.from(seen) : undefined;
}

/**
 * Token counters, coerced to the whole non-negative numbers the API accepts.
 *
 * Provider SDKs disagree on shape — `prompt_tokens`/`completion_tokens` from
 * OpenAI, `input_tokens`/`output_tokens` from Anthropic — so both are carried
 * through unchanged and `total_tokens` is filled in when it can be derived.
 */
export function normaliseUsage(usage: unknown): Record<string, number> | undefined {
  if (!usage || typeof usage !== 'object') return undefined;
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(usage as Record<string, unknown>)) {
    if (Object.keys(out).length >= MAX_USAGE_KEYS) break;
    const numeric = typeof raw === 'number' ? raw : Number(raw);
    if (!Number.isFinite(numeric) || numeric < 0) continue;
    out[key] = Math.round(numeric);
  }
  if (Object.keys(out).length === 0) return undefined;

  if (out.total_tokens === undefined) {
    const input = out.prompt_tokens ?? out.input_tokens;
    const output = out.completion_tokens ?? out.output_tokens;
    if (input !== undefined || output !== undefined) {
      out.total_tokens = (input ?? 0) + (output ?? 0);
    }
  }
  return out;
}

/** A thrown value rendered as the contract's `error_info`. */
export function toErrorInfo(thrown: unknown): ErrorInfoIn {
  if (thrown instanceof Error) {
    return {
      exception_type: clampText(thrown.name, MAX_EXCEPTION_TYPE_LENGTH) ?? 'Error',
      message: clampText(thrown.message, MAX_EXCEPTION_MESSAGE_LENGTH) ?? null,
      traceback: clampText(thrown.stack, MAX_TRACEBACK_LENGTH) ?? null,
    };
  }
  return {
    exception_type: 'Error',
    message: clampText(String(thrown), MAX_EXCEPTION_MESSAGE_LENGTH) ?? null,
    traceback: null,
  };
}

/**
 * Serialise a body, and say how large it is.
 *
 * The queue needs the byte count to decide when to flush early, and computing
 * it from the string it is about to send is both exact and free.
 */
export function encodeBody(body: unknown): { text: string; bytes: number } {
  let text: string;
  try {
    text = JSON.stringify(body);
  } catch {
    text = JSON.stringify(toJsonValue(body) ?? null);
  }
  if (typeof text !== 'string') text = 'null';
  return { text, bytes: byteLength(text) };
}

/** Approximate serialised size of one queued item, for the byte budget. */
export function approximateBytes(value: unknown): number {
  try {
    const text = JSON.stringify(value);
    return typeof text === 'string' ? byteLength(text) : 0;
  } catch {
    return 0;
  }
}
