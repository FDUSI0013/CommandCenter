/* Fulcrum Ops — sign-in.
 *
 * The console is a single page behind one gate: until the control plane
 * recognises the caller, nothing else is rendered and no data is fetched. The
 * gate paints over the whole app rather than routing to a separate page, so a
 * session that expires mid-session can re-authenticate in place and land the
 * user back on the screen they were already reading.
 */
(function () {
  'use strict';

  const ROOT_ID = 'auth-root';

  function root() {
    let el = document.getElementById(ROOT_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = ROOT_ID;
      document.body.appendChild(el);
    }
    return el;
  }

  /**
   * Paint the sign-in screen.
   *
   * @param {{reason?: string, onSuccess: function}} cfg
   *   reason    — why the gate appeared (an expired session says so)
   *   onSuccess — called once, with the session, after a successful sign-in
   */
  function show(cfg) {
    const el = root();
    el.innerHTML = `
      <div class="auth-shell">
        <form class="auth-card" autocomplete="on">
          <div class="auth-brand">
            <div class="auth-mark">${ICONS.bolt.replace('currentColor', '#fff')}</div>
            <div>
              <div class="auth-name">AI Command Center</div>
              <div class="auth-tag">AI Agent Control Plane</div>
            </div>
          </div>

          ${cfg && cfg.reason ? `<div class="auth-note">${U.esc(cfg.reason)}</div>` : ''}

          <label class="auth-field">
            <span>Work email</span>
            <input type="email" name="email" autocomplete="username" required
                   placeholder="you@company.com" autofocus>
          </label>

          <label class="auth-field">
            <span>Password</span>
            <input type="password" name="password" autocomplete="current-password" required
                   placeholder="••••••••••••">
          </label>

          <div class="auth-error" data-error hidden></div>

          <button type="submit" class="btn primary auth-submit">Sign in</button>

          <div class="auth-foot">
            Agents connect with an API key, not this password —
            issue one under Workspace settings once you are in.
          </div>
        </form>
      </div>`;

    const form = el.querySelector('form');
    const errorEl = el.querySelector('[data-error]');
    const submit = el.querySelector('.auth-submit');

    function fail(message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
      submit.disabled = false;
      submit.textContent = 'Sign in';
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      errorEl.hidden = true;
      submit.disabled = true;
      submit.textContent = 'Signing in…';

      const email = form.email.value.trim();
      const password = form.password.value;
      if (!email || !password) { fail('Enter your email and password.'); return; }

      try {
        const session = await API.auth.login(email, password);
        hide();
        cfg.onSuccess(session);
      } catch (err) {
        // The server deliberately returns one message for every kind of bad
        // credential, so it is shown verbatim rather than reinterpreted here.
        fail((err && err.message) || 'Sign-in failed. Try again.');
      }
    });
  }

  function hide() {
    const el = document.getElementById(ROOT_ID);
    if (el) el.remove();
  }

  window.AUTH = { show, hide };
})();
