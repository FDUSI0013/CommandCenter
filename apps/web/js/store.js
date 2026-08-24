/* Fulcrum Ops — client state.
 *
 * The console used to read a module-scope object of seeded rows. It now reads
 * this store, which is the same shape but hydrated from the control plane and
 * invalidated on every mutation. Screens ask for what they need and re-render
 * when it arrives; nothing here fabricates a value.
 *
 * Three things live here:
 *   Store.collection(name)  — a cached, refreshable list resource
 *   Store.session           — the signed-in user, workspace and role
 *   Store.on/emit           — a tiny event bus so a mutation on one screen
 *                             refreshes the sidebar badges on another
 */
(function () {
  'use strict';

  const listeners = Object.create(null);
  const cache = Object.create(null);

  function on(event, fn) {
    (listeners[event] || (listeners[event] = [])).push(fn);
    return () => off(event, fn);
  }
  function off(event, fn) {
    const arr = listeners[event];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
  }
  function emit(event, payload) {
    (listeners[event] || []).forEach(fn => {
      try { fn(payload); } catch (err) { console.error('listener for', event, err); }
    });
    (listeners['*'] || []).forEach(fn => { try { fn(event, payload); } catch (_) {} });
  }

  /**
   * A cached list resource.
   *
   *   const agents = Store.collection('agents', p => API.agents.list(p));
   *   await agents.load({status:'Active'});   // fetches, caches, emits
   *   agents.rows                             // current rows
   *   agents.invalidate()                     // next load() refetches
   */
  function collection(name, fetcher, options) {
    const opts = options || {};
    if (cache[name]) return cache[name];

    const state = {
      name,
      rows: [],
      total: 0,
      page: 1,
      pages: 1,
      pageSize: opts.pageSize || 25,
      params: {},
      loaded: false,
      loading: false,
      error: null,
      fetchedAt: 0,
    };

    let inflight = null;

    const res = {
      get rows() { return state.rows; },
      get total() { return state.total; },
      get pages() { return state.pages; },
      get loading() { return state.loading; },
      get error() { return state.error; },
      get loaded() { return state.loaded; },
      get params() { return state.params; },

      /** Fetch (or return the in-flight promise for) the current parameters. */
      async load(params, { force } = {}) {
        const next = Object.assign({ page: state.page, page_size: state.pageSize }, state.params, params || {});
        const sameParams = JSON.stringify(next) === JSON.stringify(state.params);
        if (!force && state.loaded && sameParams && inflight === null) return state.rows;
        if (inflight && sameParams) return inflight;

        state.params = next;
        state.loading = true;
        state.error = null;
        emit(`${name}:loading`, res);

        inflight = (async () => {
          try {
            const page = await fetcher(next);
            // Endpoints that return a bare array (summaries, sub-resources)
            // are treated as a single page.
            if (Array.isArray(page)) {
              state.rows = page;
              state.total = page.length;
              state.pages = 1;
            } else {
              state.rows = page.items || [];
              state.total = page.total || 0;
              state.pages = page.pages || 1;
              state.page = page.page || 1;
            }
            state.loaded = true;
            state.fetchedAt = Date.now();
            emit(`${name}:loaded`, res);
            return state.rows;
          } catch (err) {
            state.error = err;
            emit(`${name}:error`, err);
            throw err;
          } finally {
            state.loading = false;
            inflight = null;
          }
        })();

        return inflight;
      },

      /** Re-fetch with the parameters already in use. */
      refresh() { return res.load(null, { force: true }); },

      invalidate() { state.loaded = false; },

      setPage(page) { state.page = page; return res.load({ page }, { force: true }); },
      setPageSize(size) { state.pageSize = size; state.page = 1; return res.load({ page: 1, page_size: size }, { force: true }); },

      /** Replace one row in place after a mutation, without a round trip. */
      patchRow(id, changes) {
        const i = state.rows.findIndex(r => r.id === id);
        if (i >= 0) {
          state.rows[i] = Object.assign({}, state.rows[i], changes);
          emit(`${name}:changed`, state.rows[i]);
        }
      },

      removeRow(id) {
        state.rows = state.rows.filter(r => r.id !== id);
        state.total = Math.max(0, state.total - 1);
        emit(`${name}:changed`, null);
      },

      find(id) { return state.rows.find(r => r.id === id) || null; },
    };

    cache[name] = res;
    return res;
  }

  /** Wrap a mutating call so the affected collections refresh and screens hear about it. */
  async function mutate(fn, { invalidates, event, payload } = {}) {
    const result = await fn();
    (invalidates || []).forEach(name => {
      const c = cache[name];
      if (c) { c.invalidate(); c.refresh().catch(() => {}); }
    });
    if (event) emit(event, payload !== undefined ? payload : result);
    emit('mutation', { event, result });
    return result;
  }

  const session = {
    user: null,
    workspace: null,
    role: null,
    workspaces: [],
    entitlements: {},
    preferences: {},
    get isAuthenticated() { return Boolean(session.user); },
    can(role) {
      const order = ['viewer', 'member', 'approver', 'operator', 'admin', 'owner'];
      return order.indexOf(session.role) >= order.indexOf(role);
    },
    async refresh() {
      const me = await API.auth.me();
      session.user = me.user;
      session.workspace = me.workspace;
      session.role = me.role;
      session.workspaces = me.workspaces || [];
      session.entitlements = me.entitlements || {};
      session.preferences = me.preferences || {};
      emit('session', session);
      return session;
    },
    clear() {
      session.user = null;
      session.workspace = null;
      session.role = null;
      session.workspaces = [];
      session.preferences = {};
      Object.keys(cache).forEach(k => delete cache[k]);
      emit('session', session);
    },
  };

  /** Counts the sidebar shows; refreshed after any mutation that could change them. */
  const badges = { alerts: 0, approvals: 0 };
  async function refreshBadges() {
    try {
      const [alerts, approvals] = await Promise.all([
        API.alerts.summary(),
        API.approvals.summary(),
      ]);
      badges.alerts = alerts.open || 0;
      badges.approvals = approvals.pending || 0;
      emit('badges', badges);
    } catch (_) {
      // Badge counts are decoration; a failure must not break navigation.
    }
  }

  window.Store = { collection, mutate, on, off, emit, session, badges, refreshBadges, cache };
})();
