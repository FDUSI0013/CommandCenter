/**
 * The contract's field limits, mirrored so the SDK trims instead of the server
 * rejecting.
 *
 * A row that violates one of these comes back as a `malformed` per-item result,
 * which is a silent data loss the caller never sees — telemetry does not throw.
 * Clamping here turns "the trace vanished" into "the trace arrived with a
 * truncated tag", which is the better failure.
 */

export const MAX_TRACES_PER_BATCH = 1_000;
export const MAX_SPANS_PER_BATCH = 1_000;
export const MAX_SPANS_PER_TRACE = 1_000;
export const MAX_SCORES_PER_BATCH = 1_000;
export const MAX_EVENTS_PER_BATCH = 500;

export const MAX_TAGS = 32;
export const MAX_TAG_LENGTH = 64;
export const MAX_METADATA_KEYS = 64;
export const MAX_USAGE_KEYS = 24;
export const MAX_SCORES_PER_ITEM = 25;
export const MAX_NAME_LENGTH = 200;
export const MAX_SAMPLE_LENGTH = 500;

export const MAX_EXCEPTION_TYPE_LENGTH = 200;
export const MAX_EXCEPTION_MESSAGE_LENGTH = 4_000;
export const MAX_TRACEBACK_LENGTH = 16_000;

export const MAX_SCORE_NAME_LENGTH = 80;
export const MAX_SCORE_CATEGORY_LENGTH = 80;
export const MAX_SCORE_REASON_LENGTH = 1_000;
export const MAX_SCORE_SOURCE_LENGTH = 32;

export const MAX_AGENT_LENGTH = 160;
export const MAX_ID_LENGTH = 120;
export const MAX_REF_LENGTH = 64;
export const MAX_THREAD_ID_LENGTH = 120;

export const MAX_MODEL_LENGTH = 120;
export const MAX_PROVIDER_LENGTH = 80;

/** `EventIn` field ceilings, which are tighter than they look. */
export const MAX_GUARDRAIL_LENGTH = 160;
export const MAX_POLICY_LENGTH = 160;
/** 24 characters — "Blocked", "Masked", "Warned" fit; a sentence does not. */
export const MAX_ACTION_TAKEN_LENGTH = 24;
export const MAX_SEVERITY_LENGTH = 16;
export const MAX_SENTIMENT_LENGTH = 16;
export const MAX_EVENT_BODY_LENGTH = 4_000;
export const MAX_EVENT_SOURCE_LENGTH = 40;
export const MAX_SUBMITTED_BY_LENGTH = 160;

/** `rating` is an integer star count; anything outside the range is rejected. */
export const MIN_RATING = 1;
export const MAX_RATING = 5;

/** How much of `batch_max_bytes` a batch may fill before it flushes early. */
export const BYTE_BUDGET_RATIO = 0.85;

/** Trim a string to a byte-safe character ceiling, or drop it if it is empty. */
export function clampText(value: string | null | undefined, max: number): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (trimmed.length === 0) return undefined;
  return trimmed.length > max ? trimmed.slice(0, max) : trimmed;
}
