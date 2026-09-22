/**
 * Local redaction — the rules from `GET /ingest/config`, applied at source.
 *
 * The server can mask content after it arrives, but by then the content
 * has already crossed the network and been written to a request log. So every
 * guardrail set to *Mask*, plus any rule an operator wrote into the workspace
 * settings, is shipped down to the SDK and applied here, before the payload
 * leaves the customer's process. That is the whole point of the `redaction`
 * array in the bootstrap document.
 *
 * Two kinds of rule arrive: a `pattern` (a regular expression the server wrote)
 * and `entity_types` (named classes like `email`, which the SDK is expected to
 * recognise). The named classes are implemented below so the server does not
 * have to ship a regex for things every SDK can match on its own.
 */

import type { JsonObject, JsonValue, RedactionRule } from './types.js';

/** Where a rule applies. The contract's `applies_to` values. */
export type RedactionField = 'input' | 'output' | 'metadata';

const DEFAULT_REPLACEMENT = '[redacted by policy]';
const DEFAULT_FIELDS: RedactionField[] = ['input', 'output'];

/**
 * The named entity classes an SDK is expected to recognise.
 *
 * Deliberately conservative: a pattern that over-matches silently destroys
 * telemetry, and a missed match is visible in the console where an operator can
 * write a workspace rule for it. Each one is anchored on structure rather than
 * on a loose character class for that reason.
 */
const ENTITY_PATTERNS: Record<string, RegExp> = {
  email: /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g,
  phone: /(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]\d{3,4}(?:[ .-]\d{2,4})?/g,
  ssn: /\b\d{3}-\d{2}-\d{4}\b/g,
  credit_card: /\b(?:\d[ -]?){13,19}\b/g,
  ip: /\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b/g,
  ipv4: /\b(?:\d{1,3}\.){3}\d{1,3}\b/g,
  ipv6: /\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b/g,
  url: /\bhttps?:\/\/[^\s"'<>]+/g,
  iban: /\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b/g,
  api_key: /\b(?:sk|pk|rk|api|key|token)[-_][A-Za-z0-9_-]{16,}\b/gi,
  aws_access_key: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g,
  jwt: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
};

/** The entity class names this SDK can match without a server-supplied pattern. */
export const SUPPORTED_ENTITY_TYPES: readonly string[] = Object.freeze(Object.keys(ENTITY_PATTERNS));

/** A rule with its expressions compiled once, rather than per payload. */
export interface CompiledRedactionRule {
  id: string;
  name: string;
  replacement: string;
  fields: Set<RedactionField>;
  patterns: RegExp[];
  /** Entity classes named by the rule that this SDK version cannot match. */
  unsupportedEntityTypes: string[];
}

function toField(value: string): RedactionField | undefined {
  const lowered = value.trim().toLowerCase();
  return lowered === 'input' || lowered === 'output' || lowered === 'metadata' ? lowered : undefined;
}

/**
 * Compile the rules the config endpoint sent.
 *
 * A rule with an expression the local engine cannot parse is dropped rather
 * than thrown: an operator's typo in one rule must not stop the other rules
 * from protecting anything.
 */
export function compileRedactionRules(rules: readonly RedactionRule[] | undefined): CompiledRedactionRule[] {
  if (!rules || rules.length === 0) return [];
  const compiled: CompiledRedactionRule[] = [];

  for (const rule of rules) {
    const patterns: RegExp[] = [];
    const unsupported: string[] = [];

    if (rule.pattern) {
      try {
        patterns.push(new RegExp(rule.pattern, 'g'));
      } catch {
        // An expression this engine cannot parse — the server's flavour and
        // JavaScript's do not agree on everything. Skip it, keep the rest.
      }
    }

    for (const entity of rule.entity_types ?? []) {
      const key = String(entity).trim().toLowerCase().replace(/[\s-]+/g, '_');
      const pattern = ENTITY_PATTERNS[key];
      if (pattern) patterns.push(new RegExp(pattern.source, pattern.flags));
      else unsupported.push(String(entity));
    }

    if (patterns.length === 0) continue;

    const fields = new Set<RedactionField>();
    for (const raw of rule.applies_to ?? DEFAULT_FIELDS) {
      const field = toField(String(raw));
      if (field) fields.add(field);
    }
    if (fields.size === 0) for (const field of DEFAULT_FIELDS) fields.add(field);

    compiled.push({
      id: rule.id,
      name: rule.name,
      replacement: rule.replacement || DEFAULT_REPLACEMENT,
      fields,
      patterns,
      unsupportedEntityTypes: unsupported,
    });
  }

  return compiled;
}

function redactString(value: string, rules: readonly CompiledRedactionRule[]): string {
  let out = value;
  for (const rule of rules) {
    for (const pattern of rule.patterns) {
      pattern.lastIndex = 0;
      out = out.replace(pattern, rule.replacement);
    }
  }
  return out;
}

function walk(value: JsonValue, rules: readonly CompiledRedactionRule[]): JsonValue {
  if (typeof value === 'string') return redactString(value, rules);
  if (Array.isArray(value)) return value.map((item) => walk(item, rules));
  if (value !== null && typeof value === 'object') {
    const out: JsonObject = {};
    for (const [key, item] of Object.entries(value)) out[key] = walk(item, rules);
    return out;
  }
  return value;
}

/**
 * Apply every rule that covers `field` to an already-serialised payload.
 *
 * Returns the input untouched when no rule applies, so the common case — no
 * redaction configured — costs one array check.
 */
export function applyRedaction<T extends JsonObject | undefined>(
  payload: T,
  rules: readonly CompiledRedactionRule[],
  field: RedactionField,
): T {
  if (!payload || rules.length === 0) return payload;
  const applicable = rules.filter((rule) => rule.fields.has(field));
  if (applicable.length === 0) return payload;
  return walk(payload, applicable) as T;
}

/** Redact a bare string, for the `sample` field on a guardrail event. */
export function applyRedactionToText(
  text: string,
  rules: readonly CompiledRedactionRule[],
  field: RedactionField = 'input',
): string {
  if (rules.length === 0) return text;
  const applicable = rules.filter((rule) => rule.fields.has(field));
  return applicable.length === 0 ? text : redactString(text, applicable);
}
