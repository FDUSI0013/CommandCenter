/* Fulcrum Ops — app shell: navigation, routing, session, boot */
(function(){
  'use strict';

  const NAV = [
    { label:'PLATFORM', items:[
      ['live-runs','Live Runs','activity'],
      ['replay','Replay Studio','replay'],
      ['metrics','Metrics','chart'],
    ]},
    { label:'AGENT GOVERNANCE', items:[
      ['connections','Hosting & Deployment','plug'],
      ['agents','Agent Registry','bot'],
      ['connectors','Connector & MCP Governance','link'],
      ['policies','Policy Center','shield'],
      ['approvals','Approvals & Audit','stamp'],
    ]},
    { label:'CONFIGURATION', items:[
      ['configurations','Configuration Center','settings'],
      ['prompts','Prompt Manager','pen'],
      ['knowledge','RAG & Knowledge Governance','book'],
      ['secrets','Secrets & Credentials','key'],
    ]},
    { label:'OPERATIONS', items:[
      ['quota','Quota, Cost & Capacity','gauge'],
      ['memory','Memory & State Management','brain'],
      ['deployments','Environments & Releases','rocket'],
    ]},
    { label:'QUALITY', items:[
      ['evaluations','Evaluations','beaker'],
      ['guardrails','Guardrails','shieldCheck'],
      ['testing','Testing & Regression','box'],
      ['feedback','Feedback & Quality Loop','chat'],
    ]},
    { label:'SYSTEM', items:[
      ['alerts','Alerts','bell'],
      ['exports','Exports','download'],
      ['licensing','Licensing & Entitlements','ticket'],
    ]},
  ];

  const APP = window.APP = {
    route:null, param:null, currentTitle:'', replayRun:null, metricsAgent:null, _cleanup:null,
    go(route){ location.hash = '#/'+route; },
    sourceRoute(source){
      // Legacy labels stay: alert rows raised before the screens were renamed still carry them.
      const map = { 'Connection Center':'connections','Hosting & Deployment':'connections','Quota, Cost & Capacity':'quota','Policy Center':'policies',
        'Secrets & Credentials':'secrets','Testing & Regression':'testing','RAG & Knowledge Governance':'knowledge',
        'Deployment & Environment':'deployments','Environments & Releases':'deployments','Live Runs':'live-runs','Agent Registry':'agents','Approvals & Audit':'approvals',
        'Guardrails':'guardrails','Metrics':'metrics','Evaluations':'evaluations','Configuration Center':'configurations',
        'Prompt Studio':'prompts','Prompt Manager':'prompts','Connector & MCP Governance':'connectors',
        'Memory & State Management':'memory','Feedback & Quality Loop':'feedback','Exports':'exports',
        'Licensing & Entitlements':'licensing' };
      return map[source] || 'alerts';
    },
    /** Re-read the two counts the sidebar shows. Cheap, and safe to call often. */
    renderBell(){ Store.refreshBadges(); },
  };

  function navCounts(){
    return { alerts: Store.badges.alerts, approvals: Store.badges.approvals };
  }

  function renderNav(){
    const nav = document.getElementById('nav');
    const counts = navCounts();
    nav.innerHTML = NAV.map(g=>`<div class="nav-group">
      <div class="nav-label">${g.label}</div>
      ${g.items.map(it=>`<div class="nav-item ${APP.route===it[0]||(it[0]==='agents'&&APP.route==='agent')?'active':''}" data-route="${it[0]}" title="${it[1]}">
        <span class="nav-ico">${ICONS[it[2]]||''}</span><span style="overflow:hidden;text-overflow:ellipsis">${it[1]}</span>
        ${it[0]==='alerts'?`<span class="nav-count" data-navcount="alerts">${counts.alerts||''}</span>`:''}
        ${it[0]==='approvals'?`<span class="nav-count" data-navcount="approvals">${counts.approvals||''}</span>`:''}
      </div>`).join('')}
    </div>`).join('')
    // The operator manual is a page of its own, not a hash route; it opens
    // in a new tab so reading it never loses the screen you were on.
    + `<div class="nav-group"><div class="nav-label">HELP</div>
      <a class="nav-item" href="/docs/" target="_blank" rel="noopener" title="Operator Manual"
         style="text-decoration:none;color:inherit">
        <span class="nav-ico">${ICONS.book||''}</span><span style="overflow:hidden;text-overflow:ellipsis">Documentation</span>
      </a></div>`;
    nav.querySelectorAll('.nav-item[data-route]').forEach(el=>{
      el.addEventListener('click', ()=>APP.go(el.dataset.route));
    });
  }

  /** Paint the badge numbers without rebuilding the whole navigation. */
  function paintBadges(){
    const counts = navCounts();
    Object.keys(counts).forEach(key=>{
      const el = document.querySelector(`[data-navcount="${key}"]`);
      if(el) el.textContent = counts[key] || '';
    });
  }

  function navigate(){
    const raw = location.hash.replace(/^#\/?/, '') || 'live-runs';
    // "#/replay?run=<id>" — the query rides on APP.query so outside links
    // (the chat frontend, a pasted URL) can land on a specific entity.
    const [hash, search] = raw.split('?');
    APP.query = {};
    if(search) for(const pair of search.split('&')){
      const i = pair.indexOf('=');
      if(i > 0) APP.query[decodeURIComponent(pair.slice(0,i))] = decodeURIComponent(pair.slice(i+1));
    }
    const [route, param] = hash.split('/');
    const screen = SCREENS[route] || SCREENS['live-runs'];
    // cleanup previous screen (timers, open streams)
    if(APP._cleanup){ try{ APP._cleanup(); }catch(e){} APP._cleanup = null; }
    C.closeMenu();
    APP.route = SCREENS[route] ? route : 'live-runs';
    APP.param = param || null;
    APP.currentTitle = screen.title;
    document.title = screen.title + ' — Fulcrum Ops';
    renderNav();
    const main = document.getElementById('main');
    main.innerHTML = '';
    screen.cleanup = null;
    screen.render(main, APP.param);
    if(screen.cleanup) APP._cleanup = screen.cleanup;
    main.scrollTop = 0;
  }

  // global cross-link handler
  document.addEventListener('click', (e)=>{
    const link = e.target.closest('[data-nav]');
    if(link){ e.preventDefault(); APP.go(link.dataset.nav); }
  });

  // ---------------------------------------------------------------- session

  function renderUserCard(){
    const s = Store.session;
    const card = document.getElementById('userCard');
    if(!s.user){ card.style.visibility = 'hidden'; return; }
    card.style.visibility = '';
    card.querySelector('.avatar').textContent = s.user.initials || U.initials(s.user.full_name || s.user.email);
    card.querySelector('.user-name').textContent = s.user.full_name || s.user.email;
    card.querySelector('.user-role').textContent = s.user.job_title || roleLabel(s.role);
  }

  function roleLabel(role){
    if(!role) return '';
    return role.charAt(0).toUpperCase() + role.slice(1);
  }

  function userMenu(anchor){
    const s = Store.session;
    const items = [{ head: s.user ? s.user.email : '' }];

    if((s.workspaces || []).length > 1){
      items.push({ label:'Switch Workspace', icon:'grid', onClick:()=>switchWorkspaceModal() });
    }
    items.push(
      { label:'Profile & Preferences', icon:'user', onClick:()=>APP.go('settings/profile') },
      { label:'API Access Tokens', icon:'key', onClick:()=>APP.go('settings/keys') },
      { sep:true },
      { label:'Sign Out', icon:'logout', danger:true, onClick:()=>C.confirmModal({
          title:'Sign Out', confirmLabel:'Sign Out',
          msg:'Sign out of Fulcrum Ops? Your session will be closed on this device.',
          onConfirm: signOut }) },
    );
    C.openMenu(anchor, items);
  }

  function switchWorkspaceModal(){
    const s = Store.session;
    C.openModal({
      title:'Switch Workspace', icon:'grid',
      body:`<div class="ws-list">${(s.workspaces||[]).map(w=>`
        <button class="ws-option ${w.slug===((s.workspace||{}).slug)?'current':''}" data-ws="${U.esc(w.slug)}">
          <div><div class="ws-name">${U.esc(w.name)}</div><div class="ws-slug">${U.esc(w.slug)}</div></div>
          ${w.slug===((s.workspace||{}).slug)?'<span class="badge bg-green">Current</span>':''}
        </button>`).join('')}</div>`,
      onOpen(modal, close){
        modal.querySelectorAll('[data-ws]').forEach(btn=>{
          btn.addEventListener('click', async ()=>{
            const slug = btn.dataset.ws;
            if(slug === (s.workspace||{}).slug){ close(); return; }
            try {
              await API.auth.switchWorkspace(slug);
              close();
              await bootSession();
              navigate();
              C.toast('success','Workspace switched', `You are now working in ${slug}.`);
            } catch (err) {
              C.toast('error','Could not switch', (err && err.message) || 'The workspace is unavailable.');
            }
          });
        });
      },
    });
  }

  async function signOut(){
    try { await API.auth.logout(); } catch (_) { /* the session is going away regardless */ }
    Store.session.clear();
    gate('You have been signed out.');
  }

  /** Show the sign-in gate and, once through it, start the app. */
  function gate(reason){
    document.getElementById('app').style.display = 'none';
    AUTH.show({
      reason,
      onSuccess: async () => {
        document.getElementById('app').style.display = '';
        await bootSession();
        start();
      },
    });
  }

  async function bootSession(){
    await Store.session.refresh();
    renderUserCard();
    await Store.refreshBadges();
  }

  let started = false;
  function start(){
    if(!started){
      started = true;
      window.addEventListener('hashchange', navigate);
      Store.on('badges', paintBadges);
      // Any mutation anywhere can change the two sidebar counts.
      Store.on('mutation', ()=>Store.refreshBadges());
    }
    if(!location.hash) location.hash = '#/live-runs';
    navigate();
  }

  async function boot(){
    document.getElementById('brandIcon').innerHTML = ICONS.bolt.replace('currentColor','#fff');
    document.getElementById('userCaret').innerHTML = ICONS.chevDown;
    document.getElementById('userCard').addEventListener('click', (e)=>userMenu(e.currentTarget));

    // A 401 from anywhere at any time means the session ended; re-gate rather
    // than letting screens render half-loaded against a dead session.
    API.onUnauthorized(()=>{
      if(!Store.session.isAuthenticated) return;
      Store.session.clear();
      gate('Your session expired. Sign in to continue.');
    });

    try {
      await bootSession();
      start();
    } catch (err) {
      if(err && err.status === 401) gate();
      else {
        document.getElementById('main').innerHTML =
          `<div class="screen-error">${ICONS.alert}
             <div class="se-title">Cannot reach the control plane</div>
             <div>${U.esc((err && err.message) || 'The API did not respond.')}</div>
             <button class="btn" id="retryBoot" style="margin-top:12px">Try again</button>
           </div>`;
        const rb = document.getElementById('retryBoot');
        if(rb) rb.addEventListener('click', ()=>location.reload());
      }
    }
  }

  let booted = false;
  function bootOnce(){ if(booted) return; booted = true; boot(); }
  document.addEventListener('DOMContentLoaded', bootOnce);
  if(document.readyState !== 'loading') bootOnce();
})();
