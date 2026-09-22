/**
 * Fetching prompts from the Prompt Manager, with a cache.
 *
 * An agent that pulls its system prompt from the server gets versioning,
 * review and rollback for free — but it also gets a network round trip on a
 * path that used to be a string literal. So every lookup is cached, and the
 * cache is the point rather than an optimisation.
 *
 * Two lookups are supported and they behave differently on purpose:
 *
 * * **By name or id, unpinned** — resolves to the prompt's current head. Cached
 *   for a TTL, because the whole reason to fetch it is that someone may change
 *   it without redeploying.
 * * **By commit** — a pinned, immutable version. Cached forever, because a
 *   commit cannot change.
 */

import { NotFoundError } from './errors.js';
import type { Transport } from './transport.js';
import type { Page, PromptRead, PromptStatus, PromptVersionDetail } from './types.js';

/** Mustache-style placeholder, which is what the server's templates use. */
const VARIABLE_PATTERN = /\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}/g;

/** A prompt, resolved and ready to render. */
export interface FulcrumPrompt {
  id: string;
  name: string;
  /** The template text, `{{placeholders}}` intact. */
  template: string;
  /** Placeholders the template declares, in first-seen order. */
  variables: string[];
  /** Human version label, when the prompt has one. */
  version: string | undefined;
  /** Immutable commit this text came from. */
  commit: string | undefined;
  status: PromptStatus | undefined;
  /**
   * Substitute the placeholders.
   *
   * A placeholder with no value is left as-is rather than replaced with an
   * empty string: a visible `{{customer_name}}` in a model's context is a bug
   * someone notices, and a silently missing one is a bug nobody does.
   */
  format(variables?: Record<string, unknown>): string;
}

/** Options for one lookup. */
export interface GetPromptOptions {
  /** Pin to an immutable commit instead of taking the head. */
  commit?: string;
  /** Override the cache lifetime for this lookup, in milliseconds. */
  cacheTtlMs?: number;
  /** Bypass the cache and refresh the entry. */
  refresh?: boolean;
}

interface CacheEntry {
  prompt: FulcrumPrompt;
  /** `Infinity` for pinned commits, which cannot change. */
  expiresAt: number;
}

/** Placeholders a template declares, in first-seen order. */
export function templateVariables(template: string): string[] {
  const seen = new Set<string>();
  for (const match of template.matchAll(VARIABLE_PATTERN)) {
    if (match[1]) seen.add(match[1]);
  }
  return Array.from(seen);
}

/** Substitute every placeholder present in `variables`; leave the rest alone. */
export function renderTemplate(template: string, variables: Record<string, unknown> = {}): string {
  return template.replace(VARIABLE_PATTERN, (whole, name: string) => {
    if (!(name in variables)) return whole;
    const value = variables[name];
    if (value === null || value === undefined) return whole;
    return typeof value === 'string' ? value : String(value);
  });
}

function buildPrompt(
  id: string,
  name: string,
  template: string,
  extras: { version?: string | null; commit?: string | null; status?: PromptStatus | null; variables?: string[] },
): FulcrumPrompt {
  const variables = extras.variables?.length ? extras.variables : templateVariables(template);
  return {
    id,
    name,
    template,
    variables,
    version: extras.version ?? undefined,
    commit: extras.commit ?? undefined,
    status: extras.status ?? undefined,
    format: (values?: Record<string, unknown>) => renderTemplate(template, values ?? {}),
  };
}

const DEFAULT_TTL_MS = 5 * 60_000;

/** `client.prompts` — the Prompt Manager, cached. */
export class PromptClient {
  private readonly cache = new Map<string, CacheEntry>();

  constructor(
    private readonly transport: Transport,
    private readonly defaultTtlMs: number = DEFAULT_TTL_MS,
  ) {}

  /**
   * Fetch a prompt by id or by name.
   *
   * The id path is tried first because it is the exact one; a name falls
   * through to a search, which is how a caller who only knows
   * `"support-copilot-system"` gets an answer without hard-coding a UUID.
   *
   * Throws `NotFoundError` when nothing matches — unlike telemetry, a prompt
   * lookup is on the caller's critical path, and silently returning nothing
   * would hand a model an empty system prompt.
   */
  async get(nameOrId: string, options: GetPromptOptions = {}): Promise<FulcrumPrompt> {
    const key = `${nameOrId}::${options.commit ?? 'head'}`;
    if (!options.refresh) {
      const hit = this.cache.get(key);
      if (hit && hit.expiresAt > Date.now()) return hit.prompt;
    }

    const prompt = options.commit
      ? await this.fetchCommit(nameOrId, options.commit)
      : await this.fetchHead(nameOrId);

    this.cache.set(key, {
      prompt,
      // A pinned commit is immutable, so it never needs revalidating.
      expiresAt: options.commit ? Number.POSITIVE_INFINITY : Date.now() + (options.cacheTtlMs ?? this.defaultTtlMs),
    });
    return prompt;
  }

  private async fetchHead(nameOrId: string): Promise<FulcrumPrompt> {
    const direct = await this.tryGetById(nameOrId);
    const record = direct ?? (await this.findByName(nameOrId));
    if (!record) {
      throw new NotFoundError(`No prompt named or identified by "${nameOrId}" exists in this workspace.`, {
        code: 'not_found',
        status: 404,
      });
    }
    if (typeof record.template !== 'string' || record.template.length === 0) {
      throw new NotFoundError(`Prompt "${record.name}" has no template text at its current version.`, {
        code: 'not_found',
        status: 404,
      });
    }
    return buildPrompt(record.id, record.name, record.template, {
      version: record.version,
      commit: record.commit,
      status: record.status,
      variables: record.variables ?? [],
    });
  }

  private async fetchCommit(nameOrId: string, commit: string): Promise<FulcrumPrompt> {
    let promptId = nameOrId;
    let name = nameOrId;

    const direct = await this.tryGetById(nameOrId);
    if (direct) {
      promptId = direct.id;
      name = direct.name;
    } else {
      const found = await this.findByName(nameOrId);
      if (!found) {
        throw new NotFoundError(`No prompt named or identified by "${nameOrId}" exists in this workspace.`, {
          code: 'not_found',
          status: 404,
        });
      }
      promptId = found.id;
      name = found.name;
    }

    const version = await this.transport.request<PromptVersionDetail>({
      method: 'GET',
      path: `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(commit)}`,
    });
    return buildPrompt(promptId, name, version.template ?? '', {
      version: version.version,
      commit: version.commit,
      status: version.status,
      variables: version.variables ?? [],
    });
  }

  private async tryGetById(id: string): Promise<PromptRead | undefined> {
    try {
      return await this.transport.request<PromptRead>({
        method: 'GET',
        path: `/prompts/${encodeURIComponent(id)}`,
        // A miss here is expected whenever the caller passed a name, so there
        // is no point spending the retry budget on it.
        maxAttempts: 0,
      });
    } catch (thrown) {
      if (thrown instanceof NotFoundError) return undefined;
      throw thrown;
    }
  }

  private async findByName(name: string): Promise<PromptRead | undefined> {
    const page = await this.transport.request<Page<PromptRead>>({
      method: 'GET',
      path: '/prompts',
      query: { q: name, page_size: 50 },
    });
    const items = page?.items ?? [];
    const lowered = name.trim().toLowerCase();
    const exact = items.find((item) => item.name?.trim().toLowerCase() === lowered);
    const chosen = exact ?? items[0];
    if (!chosen) return undefined;
    // The list projection carries the template, but re-read the record when it
    // does not so a caller never gets a prompt with an empty body.
    if (typeof chosen.template === 'string' && chosen.template.length > 0) return chosen;
    return (await this.tryGetById(chosen.id)) ?? chosen;
  }

  /** List prompts, for tooling that wants to enumerate rather than resolve. */
  async list(params: { q?: string; status?: PromptStatus; agent?: string; page?: number; pageSize?: number } = {}): Promise<Page<PromptRead>> {
    return this.transport.request<Page<PromptRead>>({
      method: 'GET',
      path: '/prompts',
      query: {
        q: params.q,
        status: params.status,
        agent: params.agent,
        page: params.page,
        page_size: params.pageSize,
      },
    });
  }

  /** Forget everything cached. Useful after a deploy that changed prompts. */
  clearCache(): void {
    this.cache.clear();
  }

  /** How many entries are currently cached. */
  get cacheSize(): number {
    return this.cache.size;
  }
}
