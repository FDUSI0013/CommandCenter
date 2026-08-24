/**
 * The ingest wire contract, typed.
 *
 * These interfaces mirror `POST /api/v1/ingest/{traces,spans,scores,events}`
 * and `GET /api/v1/ingest/config` exactly, field for field, in the snake_case
 * the API speaks. They are the boundary types: everything above them in the SDK
 * is camelCase and ergonomic, and the conversion happens in one place
 * (`trace.ts`) so a contract change lands in one file.
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** What a span was doing. Drives the tool chips and model breakdowns in the console. */
export type SpanType = 'general' | 'llm' | 'tool' | 'guardrail';

/** What a feedback score is attached to. */
export type ScoreTarget = 'trace' | 'span' | 'thread';

/** The governance events the SDK may report from inside a customer's process. */
export type IngestEventKind = 'guardrail.triggered' | 'policy.violation' | 'feedback.submitted';

/** What happened to one submitted row. */
export type ItemOutcome = 'accepted' | 'rejected' | 'blocked';

/** Machine-readable rejection reason on a per-item result. */
export type RejectionCode =
  | 'malformed'
  | 'unknown_agent'
  | 'agent_not_permitted'
  | 'agent_unprovisioned'
  | 'missing_trace_id'
  | 'invalid_id'
  | 'policy_blocked'
  | 'guardrail_blocked'
  | 'telemetry_rejected'
  | 'unsupported_event'
  | 'unknown_guardrail'
  | 'unknown_policy'
  | 'duplicate';

/** A failure captured by the SDK, carried verbatim onto the span. */
export interface ErrorInfoIn {
  exception_type: string;
  message?: string | null;
  traceback?: string | null;
}

/** One score attached to a trace, span or thread, folded into the item it scores. */
export interface FeedbackScoreIn {
  name: string;
  value: number;
  category_name?: string | null;
  reason?: string | null;
  source?: string;
}

/** One unit of work inside a trace. */
export interface SpanIn {
  id?: string | null;
  trace_id?: string | null;
  parent_span_id?: string | null;
  name: string;
  type?: SpanType;
  start_time: string;
  end_time?: string | null;
  input?: JsonObject | null;
  output?: JsonObject | null;
  usage?: Record<string, number> | null;
  model?: string | null;
  provider?: string | null;
  total_estimated_cost?: number | null;
  error_info?: ErrorInfoIn | null;
  feedback_scores?: FeedbackScoreIn[];
  metadata?: JsonObject;
  tags?: string[];
  agent?: string | null;
}

/** One end-to-end agent invocation, with its spans nested inside it. */
export interface TraceIn {
  id?: string | null;
  name: string;
  start_time: string;
  end_time?: string | null;
  input?: JsonObject | null;
  output?: JsonObject | null;
  thread_id?: string | null;
  error_info?: ErrorInfoIn | null;
  spans?: SpanIn[];
  feedback_scores?: FeedbackScoreIn[];
  metadata?: JsonObject;
  tags?: string[];
  agent?: string | null;
}

/** Body item of `POST /ingest/scores`: a score plus what it scores. */
export interface ScoreIn {
  name: string;
  value: number;
  category_name?: string | null;
  reason?: string | null;
  source?: string;
  target?: ScoreTarget;
  id: string;
  agent?: string | null;
}

/** A governance event the SDK observed in the customer's own process. */
export interface EventIn {
  kind: IngestEventKind;
  ref?: string | null;
  occurred_at?: string | null;
  agent?: string | null;
  trace_id?: string | null;
  span_id?: string | null;
  guardrail?: string | null;
  action_taken?: string | null;
  score?: number | null;
  matched?: JsonObject;
  sample?: string | null;
  policy?: string | null;
  severity?: string | null;
  detail?: JsonObject;
  rating?: number | null;
  sentiment?: string | null;
  body?: string | null;
  source?: string | null;
  submitted_by?: string | null;
}

/** Every ingest body carries the same three envelope fields. */
export interface BatchEnvelope {
  agent?: string | null;
  sdk?: string | null;
  sdk_version?: string | null;
}

export interface TraceBatchIn extends BatchEnvelope {
  traces: TraceIn[];
}
export interface SpanBatchIn extends BatchEnvelope {
  spans: SpanIn[];
}
export interface ScoreBatchIn extends BatchEnvelope {
  scores: ScoreIn[];
}
export interface EventBatchIn extends BatchEnvelope {
  events: EventIn[];
}

/** An agent the ingest path created because telemetry arrived for it. */
export interface AutoRegisteredAgent {
  id: string;
  name: string;
  slug: string;
  environment: string;
  status: string;
  engine_project_name?: string | null;
}

/** A quota this batch was measured against, after the batch was counted. */
export interface IngestQuotaState {
  id: string;
  name: string;
  resource: string;
  scope: string;
  scope_ref?: string | null;
  unit: string;
  limit_value: number;
  used_value: number;
  remaining: number;
  utilization_pct: number;
  enforcement: string;
  status: string;
  resets_at?: string | null;
}

/** What happened to one submitted row. */
export interface IngestItemResult {
  index: number;
  id?: string | null;
  outcome: ItemOutcome;
  code?: RejectionCode | null;
  reason?: string | null;
  agent_id?: string | null;
  spans?: number;
  policy_id?: string | null;
  guardrail_id?: string | null;
  masked?: boolean;
}

/**
 * The answer to every ingest POST.
 *
 * Always 200 when the request itself was well formed: the batch's fate is in
 * the counters and the per-item rows, not in the status code.
 */
export interface IngestBatchResult {
  received: number;
  accepted: number;
  rejected: number;
  blocked: number;
  spans_accepted?: number;
  scores_accepted?: number;
  events_recorded?: number;
  violations_recorded?: number;
  guardrails_evaluated?: boolean;
  agents?: string[];
  auto_registered?: AutoRegisteredAgent[];
  quotas?: IngestQuotaState[];
  results?: IngestItemResult[];
  duration_ms?: number;
}

/** A guardrail the SDK should know about, as the config endpoint reports it. */
export interface GuardrailDescriptor {
  id: string;
  name: string;
  type: string;
  action: string;
  threshold: number;
  scope: string;
  scope_ref?: string | null;
  status: string;
}

/** A rule the SDK applies locally, before content ever leaves the process. */
export interface RedactionRule {
  id: string;
  name: string;
  /** `'guardrail'` or `'workspace'`. */
  source: string;
  /** Named entity classes to remove, e.g. `'email'`. */
  entity_types?: string[];
  /** Regular expression matched against content. */
  pattern?: string | null;
  replacement?: string;
  /** Fields the rule covers: `input`, `output`, `metadata`. */
  applies_to?: string[];
}

/**
 * What an SDK fetches once at start-up and re-fetches when the ETag moves.
 *
 * Deliberately small and cacheable: it is the first call every agent process
 * makes, and a fleet restart must not turn into a stampede.
 */
export interface IngestConfig {
  workspace: string;
  environment?: string | null;
  agent_id?: string | null;
  agent_name?: string | null;
  /** True when the API key may only report for one agent. */
  agent_bound?: boolean;
  sampling_rate: number;
  batch_max_spans: number;
  batch_max_bytes: number;
  flush_interval_seconds: number;
  max_queue_size: number;
  retry_max_attempts: number;
  retry_backoff_seconds: number;
  capture_input?: boolean;
  capture_output?: boolean;
  endpoints?: Record<string, string>;
  guardrails?: GuardrailDescriptor[];
  redaction?: RedactionRule[];
  /** Opaque revision; changes when anything above changes. */
  revision: string;
  /** How long the SDK may cache this document. */
  refresh_after_seconds: number;
}

/** The error envelope every non-2xx response carries. */
export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    details?: Record<string, unknown>;
    request_id?: string;
  };
}

/** One page of a list endpoint. */
export interface Page<T> {
  items: T[];
  total?: number;
  page?: number;
  page_size?: number;
  pages?: number;
}

/** Lifecycle state of a prompt in the Prompt Manager. */
export type PromptStatus = 'Draft' | 'In Review' | 'Approved' | 'Blocked';

/** A prompt as `GET /api/v1/prompts/{id}` returns it. */
export interface PromptRead {
  id: string;
  name: string;
  description?: string | null;
  agent?: string | null;
  agent_id?: string | null;
  version?: string | null;
  commit?: string | null;
  status: PromptStatus;
  environment?: string | null;
  template?: string | null;
  estimated_tokens?: number;
  template_chars?: number;
  variables?: string[];
  tags?: string[];
  owner?: string | null;
  version_count?: number;
  created_at?: string | null;
  modified_at?: string | null;
}

/** One pinned commit of a prompt, template included. */
export interface PromptVersionDetail {
  commit: string;
  version?: string | null;
  status?: PromptStatus | null;
  change_note?: string | null;
  author?: string | null;
  created_at?: string | null;
  estimated_tokens?: number;
  is_head?: boolean;
  template: string;
  variables?: string[];
  metadata?: JsonObject;
}
