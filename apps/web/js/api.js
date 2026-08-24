/* Fulcrum Ops — API client.
 *
 * Single place the console talks to the control plane. Everything else in the
 * app calls API.<domain>.<verb>(); nothing else constructs a URL or a header.
 *
 * Conventions this mirrors from the server:
 *   - list endpoints take {page, page_size, q, sort, ...filters} and return
 *     {items, total, page, page_size, pages}
 *   - errors return {error:{code,message,details,request_id}} with a real status
 *   - the session lives in an httpOnly cookie; an API key may be supplied for
 *     local tooling via API.setApiKey()
 */
(function () {
  'use strict';

  const BASE = (window.FULCRUM_API_BASE || '/api/v1').replace(/\/$/, '');
  const TIMEOUT_MS = 30000;

  let apiKey = null;
  let workspace = null;
  let onUnauthorized = null;

  class ApiError extends Error {
    constructor(status, body, requestId) {
      const info = (body && body.error) || {};
      super(info.message || `Request failed (${status})`);
      this.name = 'ApiError';
      this.status = status;
      this.code = info.code || 'http_error';
      this.details = info.details || null;
      this.requestId = info.request_id || requestId || null;
    }
    get isAuth() { return this.status === 401; }
    get isForbidden() { return this.status === 403; }
    get isNotFound() { return this.status === 404; }
    get isConflict() { return this.status === 409; }
    get isValidation() { return this.status === 422; }
    /** Field-level messages, for inline form errors. */
    get fieldErrors() {
      const fields = (this.details && this.details.fields) || [];
      const out = {};
      fields.forEach(f => { out[f.field] = f.message; });
      return out;
    }
  }

  function qs(params) {
    if (!params) return '';
    const sp = new URLSearchParams();
    Object.keys(params).forEach(k => {
      const v = params[k];
      if (v === undefined || v === null || v === '') return;
      if (Array.isArray(v)) v.forEach(item => sp.append(k, item));
      else sp.append(k, v);
    });
    const s = sp.toString();
    return s ? `?${s}` : '';
  }

  async function request(method, path, { body, params, signal, raw } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    if (signal) signal.addEventListener('abort', () => controller.abort());

    const headers = { 'Accept': 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (apiKey) headers['X-Fulcrum-Api-Key'] = apiKey;
    if (workspace) headers['X-Fulcrum-Workspace'] = workspace;

    let res;
    try {
      res = await fetch(`${BASE}${path}${qs(params)}`, {
        method,
        headers,
        credentials: 'include',
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') {
        throw new ApiError(0, { error: { code: 'timeout', message: 'The request timed out.' } });
      }
      throw new ApiError(0, { error: { code: 'network', message: 'Cannot reach the control plane.' } });
    }
    clearTimeout(timer);

    if (res.status === 401 && onUnauthorized) onUnauthorized();

    if (raw) {
      if (!res.ok) throw new ApiError(res.status, await safeJson(res), res.headers.get('X-Request-Id'));
      return res;
    }
    if (res.status === 204) return null;

    const payload = await safeJson(res);
    if (!res.ok) throw new ApiError(res.status, payload, res.headers.get('X-Request-Id'));
    return payload;
  }

  async function safeJson(res) {
    try { return await res.json(); } catch (_) { return null; }
  }

  const get = (p, params, opts) => request('GET', p, { params, ...(opts || {}) });
  const post = (p, body, params) => request('POST', p, { body, params });
  const put = (p, body) => request('PUT', p, { body });
  const patch = (p, body) => request('PATCH', p, { body });
  const del = (p, body) => request('DELETE', p, { body });

  /** Build a CRUD surface for a collection; screens add their verbs on top. */
  function collection(base) {
    return {
      list: (params) => get(base, params),
      get: (id) => get(`${base}/${encodeURIComponent(id)}`),
      create: (body) => post(base, body),
      update: (id, body) => patch(`${base}/${encodeURIComponent(id)}`, body),
      remove: (id) => del(`${base}/${encodeURIComponent(id)}`),
      action: (id, verb, body) => post(`${base}/${encodeURIComponent(id)}/${verb}`, body || {}),
    };
  }

  /**
   * Download an export as a real file. The server streams CSV/JSON with a
   * Content-Disposition; we honour it rather than re-deriving the name.
   */
  async function download(path, params) {
    const res = await request('GET', path, { params, raw: true });
    const blob = await res.blob();
    const disp = res.headers.get('Content-Disposition') || '';
    const match = /filename="?([^";]+)"?/.exec(disp);
    const name = match ? match[1] : 'export.csv';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return name;
  }

  /**
   * Server-sent events with automatic reconnect and backoff. Used by Live Runs
   * and by any screen that wants push updates. Returns a handle with .close().
   */
  function stream(path, { onMessage, onOpen, onError, params, events } = {}) {
    let es = null, closed = false, attempt = 0, timer = null;

    function parse(evt, handler) {
      if (!evt.data) return;
      try { handler(JSON.parse(evt.data)); }
      catch (_) { /* a malformed frame must not kill the stream */ }
    }

    function connect() {
      if (closed) return;
      es = new EventSource(`${BASE}${path}${qs(params)}`, { withCredentials: true });
      es.onopen = () => { attempt = 0; if (onOpen) onOpen(); };
      // Named server events (the run stream sends `open` and `run`) do not
      // reach onmessage, which only receives unnamed frames.
      Object.keys(events || {}).forEach(name => {
        es.addEventListener(name, evt => parse(evt, events[name]));
      });
      es.onmessage = (evt) => { if (onMessage) parse(evt, onMessage); };
      es.onerror = () => {
        if (closed) return;
        es.close();
        if (onError) onError();
        // 1s, 2s, 4s … capped at 30s so a long outage does not hammer the API.
        const delay = Math.min(30000, 1000 * Math.pow(2, attempt++));
        timer = setTimeout(connect, delay);
      };
    }
    connect();

    return {
      close() {
        closed = true;
        if (timer) clearTimeout(timer);
        if (es) es.close();
      },
    };
  }

  window.API = {
    ApiError,
    setApiKey(key) { apiKey = key || null; },
    setWorkspace(slug) { workspace = slug || null; },
    onUnauthorized(fn) { onUnauthorized = fn; },
    request, get, post, put, patch, del, download, stream, collection, qs,

    // ---- platform -------------------------------------------------------
    auth: {
      login: (email, password) => post('/auth/login', { email, password }),
      logout: () => post('/auth/logout', {}),
      me: () => get('/auth/me'),
      switchWorkspace: (slug) => post('/auth/workspace', { workspace: slug }),
      apiKeys: Object.assign(collection('/workspaces/api-keys'), {
        summary: () => get('/workspaces/api-keys/summary'),
        revoke: (id, body) => post(`/workspaces/api-keys/${encodeURIComponent(id)}/revoke`, body || {}),
        usage: (id, params) => get(`/workspaces/api-keys/${encodeURIComponent(id)}/usage`, params),
        export: (params) => download('/workspaces/api-keys/export', params),
      }),
      users: Object.assign(collection('/workspaces/users'), {
        summary: () => get('/workspaces/users/summary'),
        me: () => get('/workspaces/users/me'),
        /** Every member as a bare {id, full_name, initials} array — operator-readable. */
        directory: () => get('/workspaces/users/directory'),
        setRole: (id, body) => post(`/workspaces/users/${encodeURIComponent(id)}/role`, body || {}),
        export: (params) => download('/workspaces/users/export', params),
      }),
    },
    runs: {
      list: (params) => get('/runs', params),
      history: (params) => get('/runs/history', params),
      get: (id) => get(`/runs/${encodeURIComponent(id)}`),
      trace: (id) => get(`/runs/${encodeURIComponent(id)}/trace`),
      replay: (id) => get(`/runs/${encodeURIComponent(id)}/replay`),
      response: (id) => get(`/runs/${encodeURIComponent(id)}/response`),
      summary: (params) => get('/runs/summary', params),
      flag: (id, body) => post(`/runs/${encodeURIComponent(id)}/flag`, body || {}),
      stream: (opts) => stream('/runs/stream', opts),
      export: (params) => download('/runs/export', params),
    },
    metrics: {
      summary: (params) => get('/metrics/summary', params),
      series: (params) => get('/metrics/series', params),
      models: (params) => get('/metrics/models', params),
      platforms: (params) => get('/metrics/platforms', params),
      export: (params) => download('/metrics/export', params),
    },

    // ---- governance -----------------------------------------------------
    connections: Object.assign(collection('/connections'), {
      test: (id) => post(`/connections/${encodeURIComponent(id)}/test`, {}),
      sync: (id) => post(`/connections/${encodeURIComponent(id)}/sync`, {}),
      testAll: () => post('/connections/test-all', {}),
      syncAll: () => post('/connections/sync-all', {}),
      activity: (params) => get('/connections/activity', params),
      traffic: (id, params) => get(`/connections/${encodeURIComponent(id)}/traffic`, params),
      summary: () => get('/connections/summary'),
    }),
    agents: Object.assign(collection('/agents'), {
      summary: () => get('/agents/summary'),
      runs: (id, params) => get('/runs', Object.assign({ agent_id: id }, params || {})),
      versions: (id) => get(`/agents/${encodeURIComponent(id)}/versions`),
      /** Commit a new prompt version from the Agent Detail configuration tab. */
      createVersion: (id, body) => post(`/agents/${encodeURIComponent(id)}/versions`, body),
      /** Members who may own an agent — the New/Edit Agent owner picker. */
      owners: (params) => get('/workspaces/users', params),
      versionDiff: (id, params) => get(`/agents/${encodeURIComponent(id)}/versions/diff`, params),
      /** The configuration manifest itself — Copy JSON and Export Configuration. */
      configuration: (id) => get(`/agents/${encodeURIComponent(id)}/export`),
      activate: (id) => post(`/agents/${encodeURIComponent(id)}/activate`, {}),
      deactivate: (id) => post(`/agents/${encodeURIComponent(id)}/deactivate`, {}),
      exportConfig: (id) => download(`/agents/${encodeURIComponent(id)}/export`),
      run: (id, body) => post(`/agents/${encodeURIComponent(id)}/run`, body || {}),
      clone: (id, body) => post(`/agents/${encodeURIComponent(id)}/clone`, body || {}),
      export: (params) => download('/agents/export', params),
    }),
    connectors: Object.assign(collection('/connectors'), {
      summary: () => get('/connectors/summary'),
      block: (id, body) => post(`/connectors/${encodeURIComponent(id)}/block`, body || {}),
      unblock: (id) => post(`/connectors/${encodeURIComponent(id)}/unblock`, {}),
      test: (id) => post(`/connectors/${encodeURIComponent(id)}/test`, {}),
      grant: (id, body) => post(`/connectors/${encodeURIComponent(id)}/grants`, body),
      revokeGrant: (id, agentId) => del(`/connectors/${encodeURIComponent(id)}/grants/${encodeURIComponent(agentId)}`),
      export: (params) => download('/connectors/export', params),
    }),
    policies: Object.assign(collection('/policies'), {
      summary: () => get('/policies/summary'),
      activate: (id) => post(`/policies/${encodeURIComponent(id)}/activate`, {}),
      deactivate: (id, body) => post(`/policies/${encodeURIComponent(id)}/deactivate`, body || {}),
      clone: (id) => post(`/policies/${encodeURIComponent(id)}/clone`, {}),
      violations: (params) => get('/policies/violations', params),
      import: (body) => post('/policies/import', body),
      export: (params) => download('/policies/export', params),
    }),
    approvals: Object.assign(collection('/approvals'), {
      summary: () => get('/approvals/summary'),
      approve: (id, body) => post(`/approvals/${encodeURIComponent(id)}/approve`, body || {}),
      reject: (id, body) => post(`/approvals/${encodeURIComponent(id)}/reject`, body || {}),
      escalate: (id, body) => post(`/approvals/${encodeURIComponent(id)}/escalate`, body || {}),
      comment: (id, body) => post(`/approvals/${encodeURIComponent(id)}/comments`, body),
      comments: (id) => get(`/approvals/${encodeURIComponent(id)}/comments`),
      rules: collection('/approvals/rules'),
      /** Members a rule may route to — the New Approval Rule approver picker. */
      approvers: (params) => get('/workspaces/users', params),
      export: (params) => download('/approvals/export', params),
    }),
    audit: {
      list: (params) => get('/audit', params),
      verify: () => get('/audit/verify'),
      export: (params) => download('/audit/export', params),
    },

    // ---- configuration ---------------------------------------------------
    configurations: Object.assign(collection('/configurations'), {
      summary: () => get('/configurations/summary'),
      versions: (id) => get(`/configurations/${encodeURIComponent(id)}/versions`),
      newVersion: (id, body) => post(`/configurations/${encodeURIComponent(id)}/versions`, body),
      rollback: (id, body) => post(`/configurations/${encodeURIComponent(id)}/rollback`, body || {}),
      deprecate: (id, body) => post(`/configurations/${encodeURIComponent(id)}/deprecate`, body || {}),
      validate: (id) => post(`/configurations/${encodeURIComponent(id)}/validate`, {}),
      export: (params) => download('/configurations/export', params),
      /** Bring a deprecated or archived configuration back into service. */
      restore: (id) => post(`/configurations/${encodeURIComponent(id)}/restore`, {}),
      /** Copy a configuration and its live body into a fresh Draft. */
      clone: (id, body) => post(`/configurations/${encodeURIComponent(id)}/clone`, body || {}),
      /** One revision with its body — the version viewer and the diff picker. */
      version: (id, version) => get(`/configurations/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`),
      /** Field-level differences between two revisions. */
      versionDiff: (id, params) => get(`/configurations/${encodeURIComponent(id)}/versions/diff`, params),
      /** Impact & Usage plus Linked Configurations for the inspector. */
      usage: (id, params) => get(`/configurations/${encodeURIComponent(id)}/usage`, params),
      /** The declared field contract per configuration type. */
      schema: (params) => get('/configurations/schema', params),
      /** The filtered set and their live bodies, ready to re-import. */
      bundle: (params) => get('/configurations/bundle', params),
      /** Load a bundle back in — the Import dialog. */
      importBundle: (body) => post('/configurations/import', body),
      /** Members who may own a configuration — the New/Edit owner picker. */
      owners: (params) => get('/workspaces/users', params),
    }),
    prompts: Object.assign(collection('/prompts'), {
      execute: (id, body) => post(`/prompts/${encodeURIComponent(id)}/execute`, body || {}),
      summary: () => get('/prompts/summary'),
      versions: (id) => get(`/prompts/${encodeURIComponent(id)}/versions`),
      createVersion: (id, body) => post(`/prompts/${encodeURIComponent(id)}/versions`, body),
      restore: (id, version) => post(`/prompts/${encodeURIComponent(id)}/restore/${encodeURIComponent(version)}`, {}),
      diff: (id, params) => get(`/prompts/${encodeURIComponent(id)}/diff`, params),
      submitReview: (id, body) => post(`/prompts/${encodeURIComponent(id)}/submit-review`, body || {}),
      approve: (id, body) => post(`/prompts/${encodeURIComponent(id)}/approve`, body || {}),
      block: (id, body) => post(`/prompts/${encodeURIComponent(id)}/block`, body || {}),
      test: (id, body) => post(`/prompts/${encodeURIComponent(id)}/test`, body || {}),
      export: (params) => download('/prompts/export', params),
      /** One commit with its template — the version viewer and the diff picker. */
      version: (id, commit) => get(`/prompts/${encodeURIComponent(id)}/versions/${encodeURIComponent(commit)}`),
      /** Agents a prompt may be attached to — the New Prompt agent picker. */
      agents: (params) => get('/agents', params),
    }),
    knowledge: Object.assign(collection('/knowledge'), {
      summary: () => get('/knowledge/summary'),
      sync: (id) => post(`/knowledge/${encodeURIComponent(id)}/sync`, {}),
      documents: (id, params) => get(`/knowledge/${encodeURIComponent(id)}/documents`, params),
      export: (params) => download('/knowledge/export', params),
      /** Real progress for the sync bar — polled, never simulated. */
      syncStatus: (id) => get(`/knowledge/${encodeURIComponent(id)}/sync-status`),
      /** The dimensions behind the inspector's grounding gauge. */
      grounding: (id, params) => get(`/knowledge/${encodeURIComponent(id)}/grounding`, params),
      /** Members who may own a source — the Add Source owner picker. */
      owners: (params) => get('/workspaces/users', params),
    }),
    secrets: Object.assign(collection('/secrets'), {
      summary: () => get('/secrets/summary'),
      reveal: (id, body) => post(`/secrets/${encodeURIComponent(id)}/reveal`, body || {}),
      rotate: (id, body) => post(`/secrets/${encodeURIComponent(id)}/rotate`, body || {}),
      disable: (id, body) => post(`/secrets/${encodeURIComponent(id)}/disable`, body || {}),
      enable: (id) => post(`/secrets/${encodeURIComponent(id)}/enable`, {}),
      accessLog: (id, params) => get(`/secrets/${encodeURIComponent(id)}/access-log`, params),
      export: (params) => download('/secrets/export', params),
      /** Members who may own a credential — the Add / Edit Access owner picker. */
      owners: (params) => get('/workspaces/users', params),
    }),

    // ---- operations ------------------------------------------------------
    quota: {
      summary: (params) => get('/quota/summary', params),
      costBreakdown: (params) => get('/quota/cost-breakdown', params),
      costByService: (params) => get('/quota/cost-by-service', params),
      topDrivers: (params) => get('/quota/top-drivers', params),
      teamAllocation: (params) => get('/quota/team-allocation', params),
      insights: (params) => get('/quota/insights', params),
      usageSeries: (params) => get('/quota/usage-series', params),
      forecast: (params) => get('/quota/forecast', params),
      budgets: collection('/quota/budgets'),
      quotas: collection('/quota'),
      capacity: (params) => get('/quota/capacity', params),
      requestIncrease: (id, body) => post(`/quota/${encodeURIComponent(id)}/request-increase`, body || {}),
      export: (params) => download('/quota/export', params),
      /** Utilisation history for one provisioned pool. */
      capacitySeries: (params) => get('/quota/capacity-series', params),
      /** The alerts this screen raised and the changes it recorded. */
      events: (params) => get('/quota/events', params),
      /** Re-measure spend for every live budget and re-evaluate its thresholds. */
      refreshBudgets: (params) => post('/quota/budgets/refresh', {}, params),
      /** Teams a budget or quota may be scoped to — the scope picker. */
      teams: (params) => get('/quota/team-allocation', params),
    },
    memory: Object.assign(collection('/memory'), {
      summary: () => get('/memory/summary'),
      records: (id, params) => get(`/memory/${encodeURIComponent(id)}/records`, params),
      purge: (id, body) => post(`/memory/${encodeURIComponent(id)}/purge`, body || {}),
      backup: (id) => post(`/memory/${encodeURIComponent(id)}/backup`, {}),
      restore: (id, body) => post(`/memory/${encodeURIComponent(id)}/restore`, body || {}),
      retention: (id, body) => put(`/memory/${encodeURIComponent(id)}/retention`, body),
      export: (params) => download('/memory/export', params),
      /** The Sessions tab. */
      sessions: (params) => get('/memory/sessions', params),
      /** The Conversation State tab. */
      conversations: (params) => get('/memory/conversations', params),
      /** The Agent State tab. */
      agentState: (params) => get('/memory/agent-state', params),
      /** The Retention Policies tab — a bare array, not a page. */
      retentionPolicies: () => get('/memory/retention-policies'),
      /** The workspace-wide backup ledger. */
      backups: (params) => get('/memory/backups', params),
      /** One store's backup history — the Restore picker. */
      storeBackups: (id, params) => get(`/memory/${encodeURIComponent(id)}/backups`, params),
      /** Members who may own a store — the Create Store owner picker. */
      owners: (params) => get('/workspaces/users', params),
    }),
    environments: Object.assign(collection('/environments'), {
      restart: (id) => post(`/environments/${encodeURIComponent(id)}/restart`, {}),
      /** Restart with the reason the operator typed, which the audit row keeps. */
      restartWith: (id, body) => post(`/environments/${encodeURIComponent(id)}/restart`, body || {}),
    }),
    deployments: Object.assign(collection('/deployments'), {
      summary: () => get('/deployments/summary'),
      stages: (id) => get(`/deployments/${encodeURIComponent(id)}/stages`),
      stream: (id, opts) => stream(`/deployments/${encodeURIComponent(id)}/stream`, opts),
      promote: (id, body) => post(`/deployments/${encodeURIComponent(id)}/promote`, body || {}),
      halt: (id, body) => post(`/deployments/${encodeURIComponent(id)}/halt`, body || {}),
      approve: (id, body) => post(`/deployments/${encodeURIComponent(id)}/approve`, body || {}),
      rollback: (id, body) => post(`/deployments/${encodeURIComponent(id)}/rollback`, body || {}),
      export: (params) => download('/deployments/export', params),
      /** Environments a release may target — the Create / Promote picker. */
      environments: (params) => get('/environments', params),
      /** Agents a release may carry — the Create Deployment agent picker. */
      agents: (params) => get('/agents', params),
      /** The approval requests deployments open, for the Approvals tab. */
      approvals: (params) => get('/approvals', Object.assign({ action: 'Deploy' }, params || {})),
    }),

    // ---- quality ---------------------------------------------------------
    evaluations: Object.assign(collection('/evaluations'), {
      summary: () => get('/evaluations/summary'),
      run: (body) => post('/evaluations', body),
      rerun: (id) => post(`/evaluations/${encodeURIComponent(id)}/rerun`, {}),
      compare: (params) => get('/evaluations/compare', params),
      progress: (id) => get(`/evaluations/${encodeURIComponent(id)}/progress`),
      trend: (params) => get('/evaluations/trend', params),
      // Paged, so the Testing screen's Datasets tab can drive it server-side.
      datasets: (params) => get('/evaluations/datasets', params),
      // The detail view pages its per-case breakdown, which collection().get cannot express.
      detail: (id, params) => get(`/evaluations/${encodeURIComponent(id)}`, params),
      export: (params) => download('/evaluations/export', params),
    }),
    guardrails: Object.assign(collection('/guardrails'), {
      summary: () => get('/guardrails/summary'),
      test: (id, body) => post(`/guardrails/${encodeURIComponent(id)}/test`, body),
      tune: (id, body) => patch(`/guardrails/${encodeURIComponent(id)}/threshold`, body),
      enable: (id) => post(`/guardrails/${encodeURIComponent(id)}/enable`, {}),
      disable: (id) => post(`/guardrails/${encodeURIComponent(id)}/disable`, {}),
      events: (params) => get('/guardrails/events', params),
      eventsExport: (params) => download('/guardrails/events/export', params),
      export: (params) => download('/guardrails/export', params),
    }),
    testing: Object.assign(collection('/testing/suites'), {
      summary: () => get('/testing/summary'),
      run: (id, body) => post(`/testing/suites/${encodeURIComponent(id)}/run`, body || {}),
      runs: (id, params) => get(`/testing/suites/${encodeURIComponent(id)}/runs`, params),
      promoteBaseline: (id, runId) => post(`/testing/suites/${encodeURIComponent(id)}/promote-baseline`, { run_id: runId }),
      progress: (id, runId) => get(`/testing/suites/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/progress`),
      schedules: collection('/testing/schedules'),
      compare: (params) => get('/testing/compare', params),
      // The tabs beside Test Suites: every one is its own server-side view.
      baselines: (params) => get('/testing/baselines', params),
      environments: (params) => get('/testing/environments', params),
      allRuns: (params) => get('/testing/runs', params),
      runDetail: (runId) => get(`/testing/runs/${encodeURIComponent(runId)}`),
      runsExport: (params) => download('/testing/runs/export', params),
      export: (params) => download('/testing/export', params),
    }),
    feedback: Object.assign(collection('/feedback'), {
      summary: (params) => get('/feedback/summary', params),
      analyze: (body) => post('/feedback/analyze', body || {}),
      issues: collection('/feedback/issues'),
      backlog: collection('/feedback/backlog'),
      improvements: collection('/feedback/improvements'),
      createIssue: (body) => post('/feedback/issues', body),
      // The three breakdowns the Overview and Quality Insights tabs chart.
      funnel: (params) => get('/feedback/funnel', params),
      insights: (params) => get('/feedback/insights', params),
      themes: (params) => get('/feedback/themes', params),
      // Promotion path: issue -> backlog item -> fix task with a measured metric.
      issueToBacklog: (issueId, body) => post(`/feedback/issues/${encodeURIComponent(issueId)}/backlog`, body || {}),
      fixTask: (itemId, body) => post(`/feedback/backlog/${encodeURIComponent(itemId)}/fix-task`, body || {}),
      slaRules: () => get('/feedback/sla-rules'),
      saveSlaRules: (body) => put('/feedback/sla-rules', body),
      settings: () => get('/feedback/settings'),
      saveSettings: (body) => put('/feedback/settings', body),
      export: (params) => download('/feedback/export', params),
    }),

    // ---- system ----------------------------------------------------------
    alerts: Object.assign(collection('/alerts'), {
      summary: () => get('/alerts/summary'),
      acknowledge: (id) => post(`/alerts/${encodeURIComponent(id)}/acknowledge`, {}),
      acknowledgeAll: () => post('/alerts/acknowledge-all', {}),
      resolve: (id, body) => post(`/alerts/${encodeURIComponent(id)}/resolve`, body || {}),
      assign: (id, body) => post(`/alerts/${encodeURIComponent(id)}/assign`, body),
      mute: (id, body) => post(`/alerts/${encodeURIComponent(id)}/mute`, body || {}),
      rules: collection('/alerts/rules'),
      /** Members an alert may be routed to — the Assign dialog's picker. */
      assignees: () => get('/workspaces/users/directory'),
      export: (params) => download('/alerts/export', params),
    }),
    exports: Object.assign(collection('/exports'), {
      summary: () => get('/exports/summary'),
      download: (id) => download(`/exports/${encodeURIComponent(id)}/download`),
      schedules: collection('/exports/schedules'),
      /** A job is async: request it, poll status, then download when ready. */
      status: (id) => get(`/exports/${encodeURIComponent(id)}/status`),
      rerun: (id) => post(`/exports/${encodeURIComponent(id)}/rerun`, {}),
      retention: (id) => get(`/exports/${encodeURIComponent(id)}/retention`),
      datasets: () => get('/exports/datasets'),
      formats: () => get('/exports/formats'),
      runSchedule: (id) => post(`/exports/schedules/${encodeURIComponent(id)}/run`, {}),
      /** The job history itself as CSV — served synchronously, not as a job. */
      export: (params) => download('/exports/export', params),
    }),
    licensing: {
      summary: () => get('/licensing/summary'),
      plans: collection('/licensing/plans'),
      tenants: collection('/licensing/tenants'),
      entitlements: (licenseId) => get(`/licensing/tenants/${encodeURIComponent(licenseId)}/entitlements`),
      seats: (licenseId, params) => get(`/licensing/tenants/${encodeURIComponent(licenseId)}/seats`, params),
      assignSeat: (licenseId, body) => post(`/licensing/tenants/${encodeURIComponent(licenseId)}/seats`, body),
      purchaseSeats: (licenseId, body) => post(`/licensing/tenants/${encodeURIComponent(licenseId)}/seats/purchase`, body),
      suspend: (licenseId, body) => post(`/licensing/tenants/${encodeURIComponent(licenseId)}/suspend`, body || {}),
      reactivate: (licenseId) => post(`/licensing/tenants/${encodeURIComponent(licenseId)}/reactivate`, {}),
      invoice: (invoiceId) => download(`/licensing/invoices/${encodeURIComponent(invoiceId)}/pdf`),
      invoices: (params) => get('/licensing/invoices', params),
      entitlementCheck: (params) => get('/licensing/entitlement-check', params),
      /** Members who can hold a seat — the Reassign dialog's picker. */
      seatCandidates: (params) => get('/workspaces/users', params),
      export: (params) => download('/licensing/export', params),
    },
  };
})();
