/**
 * `@fulcrum-ops/sdk` — report agent telemetry and governance events to
 * FD AI Command Center.
 *
 * ```ts
 * import { FulcrumOps } from '@fulcrum-ops/sdk';
 *
 * const client = new FulcrumOps({ apiKey: process.env.FULCRUM_OPS_API_KEY });
 *
 * const answer = await client.trace({ name: 'support-question', agent: 'support-copilot' }, async () => {
 *   const context = await client.span({ name: 'retrieve', type: 'tool' }, () => search(question));
 *   return client.span({ name: 'answer', type: 'llm' }, () => model.complete(context));
 * });
 *
 * await client.close();
 * ```
 *
 * The integrations live behind subpath entry points so nothing provider-shaped
 * is loaded unless it is asked for:
 * `@fulcrum-ops/sdk/openai`, `/anthropic`, `/langchain`.
 */

export { FulcrumOps, getDefaultClient, setDefaultClient, traced } from './client.js';
export type {
  ClientStats,
  ConfigOptions,
  FeedbackEventOptions,
  FeedbackScoreOptions,
  GuardrailEventOptions,
  PolicyViolationOptions,
  SpanBody,
  TracedBody,
  TracedOptions,
} from './client.js';

export { Span, Trace } from './trace.js';
export type { EndOptions, ScoreInput, SpanOptions, TraceOptions, TraceSink } from './trace.js';

export { PromptClient, renderTemplate, templateVariables } from './prompts.js';
export type { FulcrumPrompt, GetPromptOptions } from './prompts.js';

export { DEFAULT_BASE_URL, normaliseBaseUrl, resolveOptions } from './options.js';
export type {
  BatchOptions,
  ErrorHandler,
  FetchLike,
  FulcrumOpsOptions,
  ResolvedOptions,
  RetryOptions,
} from './options.js';

export {
  ApiError,
  AuthenticationError,
  ConfigurationError,
  FulcrumOpsError,
  NetworkError,
  NotFoundError,
  PayloadTooLargeError,
  QuotaExceededError,
  RateLimitError,
  ServerError,
  TimeoutError,
} from './errors.js';

export { SUPPORTED_ENTITY_TYPES, applyRedaction, applyRedactionToText, compileRedactionRules } from './redaction.js';
export type { CompiledRedactionRule, RedactionField } from './redaction.js';

export { BatchQueue } from './queue.js';
export type { QueueKind, QueueStats } from './queue.js';

export { backoffDelay, Transport } from './transport.js';
export type { RawResponse, RequestOptions } from './transport.js';

export { ContextManager } from './context.js';
export type { ActiveContext, ContextBackend } from './context.js';

export { isValidId, newId } from './ids.js';
export { SDK_NAME, SDK_VERSION, USER_AGENT } from './version.js';

export type {
  ApiErrorEnvelope,
  AutoRegisteredAgent,
  ErrorInfoIn,
  EventIn,
  FeedbackScoreIn,
  GuardrailDescriptor,
  IngestBatchResult,
  IngestConfig,
  IngestEventKind,
  IngestItemResult,
  IngestQuotaState,
  ItemOutcome,
  JsonObject,
  JsonValue,
  Page,
  PromptRead,
  PromptStatus,
  PromptVersionDetail,
  RedactionRule,
  RejectionCode,
  ScoreIn,
  ScoreTarget,
  SpanIn,
  SpanType,
  TraceIn,
} from './types.js';
