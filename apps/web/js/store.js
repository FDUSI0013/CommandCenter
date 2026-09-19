/* Fulcrum Ops — client state.
 *
 * The console used to read a module-scope object of seeded rows. Every list now
 * comes from the control plane, and each table owns its own rows (dataTable in
 * server mode), so the only state shared between screens is what lives here.
 * Nothing here fabricates a value.
 *
 *   Store.session   — the signed-in user, workspace and role
 *   Store.badges    — the two counts the sidebar shows
 *   Store.mutate    — wraps a mutating call so other screens hear about it
 *   Store.on/emit   — a tiny event bus so a mutation on one screen refreshes
 *                     the sidebar badges on another
 *
 * There was once a Store.collection() list cache and a matching `invalidates`
 * option on mutate(). No screen ever adopted the cache, so `invalidates` named
 * collections that did not exist and silently did nothing; both are gone rather
 * than left promising a refresh that never happened. A screen that must react
 * to someone else's mutation subscribes to the event that mutation emits.
 */
(function () {
  'use strict';

  const listeners = Object.create(null);

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

  /** Wrap a mutating call so the screens that care, and the sidebar badges, hear about it. */
  async function mutate(fn, { event, payload } = {}) {
    const result = await fn();
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
      // The counts belong to the session that read them; the next person to
      // sign in on this tab must not be shown the last one's numbers.
      badges.alerts = 0;
      badges.approvals = 0;
      badgesReadAt = 0;
      emit('session', session);
    },
  };

  /**
   * Counts the sidebar shows. Re-read after any mutation made in this tab, and
   * by the app shell on a timer, on navigation and when the tab regains focus —
   * alerts and approval requests are raised by agents and monitors, not by the
   * person watching, so waiting for a local mutation would never show them.
   *
   * `maxAge` lets those passive callers share one read: a call is skipped when
   * the counts were requested more recently than that. A caller that has just
   * changed something passes nothing and always gets a fresh read.
   */
  const badges = { alerts: 0, approvals: 0 };
  let badgesReadAt = 0;
  async function refreshBadges({ maxAge } = {}) {
    if (maxAge && Date.now() - badgesReadAt < maxAge) return;
    badgesReadAt = Date.now();
    try {
      const [alerts, approvals] = await Promise.all([
        API.alerts.summary(),
        API.approvals.summary(),
      ]);
      // A read that was in flight across a sign-out belongs to nobody.
      if (!session.isAuthenticated) return;
      badges.alerts = alerts.open || 0;
      badges.approvals = approvals.pending || 0;
      emit('badges', badges);
    } catch (_) {
      // Badge counts are decoration; a failure must not break navigation.
    }
  }

  window.Store = { mutate, on, off, emit, session, badges, refreshBadges };
})();
